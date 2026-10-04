"""Loopback web service for VCF -> GI expression -> frozen lifespan model."""

from __future__ import annotations

import asyncio
import csv
import fcntl
import hashlib
import io
import json
import logging
import os
import sqlite3
import threading
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import pysam
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from longevity.gi_api import Client, api_key, payload, request_hash, validate_response
from longevity.gi_sequence_lifespan import CONFIG
from longevity.gi_vcf import (
    MODELS,
    PHASE_SEED,
    FrozenPredictor,
    InputError,
    compare_predictions,
    gene_sequences,
    scan_variants,
    vcf_header,
)

LOG = logging.getLogger("gi_web")
ROOT = Path(__file__).resolve().parents[1]
MAX_UPLOAD = 512 * 1024**2
TERMINAL = {"complete", "failed", "cancelled"}


def reported_result(result):
    """Expose relative effects only, including for results saved by older servers."""
    absolute_fields = {
        "reference_predicted_years",
        "modified_predicted_years",
        "frozen_reference_predicted_years",
    }
    return {
        **{key: value for key, value in result.items() if key not in absolute_fields},
        "result_schema_version": 2,
    }


def now():
    return datetime.now(UTC).isoformat()


def save_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def identifier(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(404, "Unknown ID") from exc
    return value


class Cancelled(Exception):
    pass


class Interrupted(Exception):
    pass


class JobManager:
    def __init__(self, state, reference, env_file, root=ROOT, client=None):
        self.state, self.reference = Path(state), Path(reference)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        for directory in ("jobs", "uploads", "responses"):
            (self.state / directory).mkdir(exist_ok=True, mode=0o700)
        self.process_lock = (self.state / "server.lock").open("w")
        try:
            fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.process_lock.close()
            raise RuntimeError(
                "Another GI web server is using this state directory; use one worker."
            ) from exc
        self.predictor = FrozenPredictor(root)
        self.client = client
        if self.client is None:
            try:
                self.client = Client(
                    CONFIG, self.state / "responses", api_key(env_file), max_rps=1.5
                )
            except ValueError:
                pass
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vcf-job")
        self.cancel_events = {}
        self.jobs = {}
        for path in sorted((self.state / "jobs").glob("*/status.json")):
            job = json.loads(path.read_text())
            self.jobs[job["id"]] = job
            if job["status"] not in TERMINAL:
                self.update(
                    job["id"],
                    status="queued",
                    stage="queued",
                    message="Resuming after server restart; cached GI responses will be reused.",
                )
                self.enqueue(job["id"])

    def ready(self):
        return self.reference.is_file() and Path(str(self.reference) + ".fai").is_file()

    def update(self, job_id, **fields):
        with self.lock:
            self.jobs[job_id].update(fields, updated_at=now())
            save_json(self.state / "jobs" / job_id / "status.json", self.jobs[job_id])

    def snapshot(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise HTTPException(404, "Job not found")
            job = dict(self.jobs[job_id])
            if "result" in job:
                job["result"] = reported_result(job["result"])
            return job

    def enqueue(self, job_id):
        self.cancel_events[job_id] = threading.Event()
        self.executor.submit(self.run, job_id)

    def create(self, config):
        upload_id = identifier(config["upload_id"])
        upload = self.state / "uploads" / upload_id
        if not (upload / "metadata.json").is_file():
            raise HTTPException(404, "Upload not found")
        metadata = json.loads((upload / "metadata.json").read_text())
        if config["sample"] not in metadata["samples"]:
            raise HTTPException(422, "Select a sample present in this VCF.")
        if not self.ready():
            raise HTTPException(503, "Indexed GRCh38 reference is not ready.")
        if self.client is None:
            raise HTTPException(503, "GI_API_KEY is not configured on the server.")
        job_id = str(uuid.uuid4())
        directory = self.state / "jobs" / job_id
        directory.mkdir(mode=0o700)
        job = dict(
            config,
            id=job_id,
            filename=metadata["filename"],
            upload_sha256=metadata["sha256"],
            status="queued",
            stage="queued",
            created_at=now(),
            updated_at=now(),
            counts={},
            requests_completed=0,
            requests_total=0,
            api_requests=0,
            cache_hits=0,
        )
        with self.lock:
            self.jobs[job_id] = job
            save_json(directory / "status.json", job)
        self.enqueue(job_id)
        return job_id

    def cancel(self, job_id):
        job = self.snapshot(job_id)
        if job["status"] not in TERMINAL:
            self.cancel_events[job_id].set()
            self.update(
                job_id, message="Cancellation requested; in-flight GI calls may finish first."
            )
        return self.snapshot(job_id)

    def run(self, job_id):
        directory = self.state / "jobs" / job_id
        job = self.snapshot(job_id)
        event = self.cancel_events[job_id]

        def progress(**fields):
            if event.is_set():
                raise Cancelled
            if self.stopping.is_set():
                raise Interrupted
            self.update(job_id, **fields)

        try:
            progress(
                status="running",
                stage="scanning",
                message="Filtering genotype calls to the model's TSS windows.",
            )
            windows = self.predictor.model_windows(job["model"])
            upload_path = self.state / "uploads" / job["upload_id"] / "input.vcf"
            plan = []
            sequence_path = directory / "sequences.sqlite"
            # Regenerate deterministic sequence plan on restart, reuse API cache.
            if sequence_path.exists():
                sequence_path.unlink()
            with (
                pysam.FastaFile(str(self.reference)) as fasta,
                sqlite3.connect(sequence_path) as database,
            ):
                variants, counts = scan_variants(
                    upload_path, job["sample"], windows, fasta, progress
                )
                database.execute(
                    "CREATE TABLE sequences (hash TEXT PRIMARY KEY, sequence BLOB, gene TEXT)"
                )

                def register(sequence, gene):
                    key = request_hash(payload(CONFIG, sequence))
                    database.execute(
                        "INSERT OR IGNORE INTO sequences VALUES (?, ?, ?)",
                        (key, zlib.compress(sequence.encode()), gene),
                    )
                    return key

                candidates = [row for row in windows if row["human_gene_id"] in variants]
                for number, row in enumerate(candidates, 1):
                    progress(
                        stage="assembling",
                        genes_prepared=number - 1,
                        genes_to_prepare=len(candidates),
                        message="Constructing and checking phased TSS sequences.",
                    )
                    gene = row["human_gene_id"]
                    native, pairs = gene_sequences(fasta, row, variants[gene], job["phase_draws"])
                    if all(sequence == native for pair in pairs for sequence in pair):
                        continue
                    native_hash = register(native, gene)
                    hashes = [[register(sequence, gene) for sequence in pair] for pair in pairs]
                    plan.append(
                        {
                            "human_gene_id": gene,
                            "native_hash": native_hash,
                            "haplotype_hashes": hashes,
                            "variant_records": len(variants[gene]),
                        }
                    )
                    database.commit()
                total = database.execute("SELECT COUNT(*) FROM sequences").fetchone()[0]
                save_json(directory / "sequence_plan.json", plan)
                progress(
                    stage="inference",
                    genes_affected=len(plan),
                    genes_prepared=len(candidates),
                    requests_total=total,
                    requests_completed=0,
                    api_requests=0,
                    cache_hits=0,
                    message="Running matched reference and modified DNA through GI.",
                )
                expressions, completed, api_requests, cache_hits = {}, 0, 0, 0

                def infer(item):
                    if event.is_set():
                        raise Cancelled
                    if self.stopping.is_set():
                        raise Interrupted
                    key, blob, gene = item
                    sequence = zlib.decompress(blob).decode()
                    response = self.client.predict(
                        {
                            "sequence": sequence,
                            "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                            "ensembl_species": "homo_sapiens",
                            "gene_id": gene,
                        }
                    )
                    if response["status"] not in {"ok", "cached"}:
                        raise InputError(
                            "GI inference failed after retries. Check the server key/quota and retry; completed calls remain cached."
                        )
                    value = validate_response(
                        CONFIG, json.loads(Path(response["response_path"]).read_text())
                    )
                    return key, value, response["status"]

                cursor = database.execute(
                    "SELECT hash, sequence, gene FROM sequences ORDER BY rowid"
                )
                with ThreadPoolExecutor(max_workers=4, thread_name_prefix="gi-call") as pool:
                    while batch := cursor.fetchmany(4):
                        pending = [pool.submit(infer, item) for item in batch]
                        for future in as_completed(pending):
                            key, value, status = future.result()
                            expressions[key] = value
                            completed += 1
                            api_requests += status == "ok"
                            cache_hits += status == "cached"
                            progress(
                                requests_completed=completed,
                                api_requests=api_requests,
                                cache_hits=cache_hits,
                            )
            progress(
                stage="comparing",
                message="Applying frozen regression weights and calculating the reference-relative change.",
            )
            result = reported_result(
                compare_predictions(
                    self.predictor, job["model"], plan, expressions, job["phase_draws"]
                )
            )
            result.update(
                job_id=job_id,
                model=job["model"],
                sample=job["sample"],
                input_sha256=job["upload_sha256"],
                counts=counts,
                genes_affected=len(plan),
                requests_total=total,
                api_requests=api_requests,
                cache_hits=cache_hits,
                assembly="GRCh38",
                ensembl_release=116,
                gi_config=CONFIG,
                phase_draws=job["phase_draws"],
                phase_seed=PHASE_SEED,
                sequence_plan=plan,
                created_at=now(),
                interpretation="Experimental change in a comparative-species model, not a validated estimate of personal lifespan or a causal variant effect.",
                assumptions=[
                    "Missing, filtered and absent calls retain reference sequence; a variant-only VCF cannot establish callability.",
                    "Diploid expression is the mean of haplotype TPM, averaged over deterministic phase draws.",
                    "Native DNA is re-inferred for affected genes using the same GI service/cache as modified DNA; unaffected genes retain frozen reference expression.",
                    "The range across phase draws measures phase sensitivity, not a confidence interval.",
                    "Fixed annotated TSS, fixed human hepatocyte context; copy number, TSS switching and tissue changes are not modeled.",
                ],
                model_artifact_sha256={
                    str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in [
                        ROOT / "docs/gi-lifespan-predictor/coefficients.csv",
                        ROOT / "docs/gi-lifespan-predictor/human_prediction_frozen.json",
                        ROOT / "docs/gi-longevity-final/reference_human_tss.csv",
                        ROOT / "docs/gi-longevity-final/reference_human_expression.csv",
                    ]
                },
            )
            save_json(directory / "expressions.json", expressions)
            save_json(directory / "result.json", result)
            progress(
                status="complete", stage="complete", message="Analysis complete.", result=result
            )
        except Interrupted:
            self.update(
                job_id,
                status="queued",
                stage="queued",
                message="Server stopped; this job will resume on restart using cached GI calls.",
            )
        except Cancelled:
            self.update(
                job_id,
                status="cancelled",
                stage="cancelled",
                message="Analysis cancelled. Completed GI calls remain cached.",
            )
        except (InputError, UnicodeError, EOFError, OSError) as exc:
            # Input errors are intentionally actionable; do not expose arbitrary API bodies.
            message = (
                str(exc)
                if isinstance(exc, InputError)
                else "Could not read input/reference data. Check the VCF and server files."
            )
            self.update(job_id, status="failed", stage="failed", message=message)
            LOG.warning("Job %s failed: %s", job_id, type(exc).__name__)
        except Exception:
            LOG.exception("Unexpected failure in job %s", job_id)
            self.update(
                job_id,
                status="failed",
                stage="failed",
                message="Unexpected server error; inspect the local server log. Completed GI calls remain cached.",
            )

    def close(self):
        # Finish in-flight API calls, leave the job resumable without draining the queue.
        self.stopping.set()
        self.executor.shutdown(wait=True)
        self.process_lock.close()


class AnalysisRequest(BaseModel):
    upload_id: str
    sample: str
    model: Literal["fdr_genes_ridge", "all_genes_ridge"] = "fdr_genes_ridge"
    phase_draws: Literal[2, 4, 8] = 4
    assembly: Literal["GRCh38"]


def create_app(manager=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.manager = manager or JobManager(
            os.environ.get("GI_WEB_STATE", str(Path.home() / ".local/share/gi-lifespan-web")),
            os.environ.get(
                "GI_REFERENCE_FASTA", str(Path.home() / ".local/share/gi-lifespan-web/GRCh38.fa")
            ),
            os.environ.get("GI_ENV_FILE", str(ROOT / ".env")),
        )
        yield
        await asyncio.to_thread(app.state.manager.close)

    app = FastAPI(title="TSS Variant Explorer", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            if origin and origin != expected:
                return JSONResponse({"detail": "Use the local app origin."}, status_code=403)
            if request.url.path == "/api/uploads" and "content-length" not in request.headers:
                return JSONResponse(
                    {"detail": "Upload requires Content-Length. Use the browser or curl -F."},
                    status_code=411,
                )
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "Invalid content length."}, status_code=400)
            if length > MAX_UPLOAD + 1024**2:
                return JSONResponse({"detail": "Upload limit is 512 MB."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def service(request):
        return request.app.state.manager

    @app.get("/api/config")
    def configuration(request: Request):
        m = service(request)
        return {
            "reference_ready": m.ready(),
            "api_key_ready": m.client is not None,
            "assembly": "GRCh38",
            "gi_model": CONFIG["model"],
            "context": CONFIG["description"],
            "max_upload_mb": 512,
            "models": [
                {
                    "id": model,
                    "features": len(m.predictor.weights[model]),
                    "human_windows": len(m.predictor.model_windows(model)),
                }
                for model in MODELS
            ],
        }

    @app.post("/api/uploads")
    async def upload(request: Request, file: Annotated[UploadFile, File()]):
        m, upload_id = service(request), str(uuid.uuid4())
        directory = m.state / "uploads" / upload_id
        directory.mkdir(mode=0o700)
        target = directory / "input.vcf"
        digest, size = hashlib.sha256(), 0
        try:
            with target.open("wb") as stream:
                target.chmod(0o600)
                while chunk := await file.read(1024**2):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(413, "Upload limit is 512 MB.")
                    digest.update(chunk)
                    stream.write(chunk)
            header = await asyncio.to_thread(vcf_header, target)
            metadata = dict(
                header,
                id=upload_id,
                filename=Path(file.filename or "input.vcf").name[:200],
                bytes=size,
                sha256=digest.hexdigest(),
                created_at=now(),
            )
            save_json(directory / "metadata.json", metadata)
            return metadata
        except (InputError, UnicodeError, EOFError, OSError) as exc:
            target.unlink(missing_ok=True)
            directory.rmdir()
            raise HTTPException(
                422,
                str(exc) if isinstance(exc, InputError) else "Invalid or damaged VCF/gzip file.",
            ) from exc
        except HTTPException:
            target.unlink(missing_ok=True)
            directory.rmdir()
            raise
        finally:
            await file.close()

    @app.post("/api/jobs", status_code=202)
    def create_job(body: AnalysisRequest, request: Request):
        job_id = service(request).create(body.model_dump())
        return {"id": job_id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, request: Request):
        return service(request).snapshot(identifier(job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, request: Request):
        return service(request).cancel(identifier(job_id))

    @app.get("/api/jobs/{job_id}/result.json")
    def result_json(job_id: str, request: Request):
        job = service(request).snapshot(identifier(job_id))
        if job["status"] != "complete":
            raise HTTPException(409, "Result is not ready.")
        return JSONResponse(
            job["result"],
            headers={"Content-Disposition": f'attachment; filename="gi-lifespan-{job_id}.json"'},
        )

    @app.get("/api/jobs/{job_id}/genes.csv")
    def result_csv(job_id: str, request: Request):
        job = service(request).snapshot(identifier(job_id))
        if job["status"] != "complete":
            raise HTTPException(409, "Result is not ready.")
        rows = job["result"]["genes"]
        columns = (
            list(rows[0])
            if rows
            else [
                "human_gene_id",
                "gene_name",
                "reference_expression_log1p_tpm",
                "modified_expression_log1p_tpm",
                "log_lifespan_contribution",
            ]
        )
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            stream.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="gi-gene-effects-{job_id}.csv"'},
        )

    @app.get("/api/example.vcf")
    def example(request: Request):
        m = service(request)
        if not m.ready():
            raise HTTPException(503, "Reference is not ready.")
        row = next(
            r for r in m.predictor.model_windows("fdr_genes_ridge") if r["gene_name"] == "ABHD4"
        )
        pos = row["tss0"] + 5
        with pysam.FastaFile(str(m.reference)) as fasta:
            ref = fasta.fetch(row["contig"], pos, pos + 1).upper()
            length = fasta.get_reference_length(row["contig"])
        alt = next(x for x in "ACGT" if x != ref)
        content = f'##fileformat=VCFv4.3\n##reference=GRCh38\n##contig=<ID={row["contig"]},length={length}>\n##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSYNTHETIC_DEMO\n{row["contig"]}\t{pos + 1}\t.\t{ref}\t{alt}\t.\tPASS\t.\tGT\t1/1\n'
        return Response(
            content,
            media_type="text/plain",
            headers={"Content-Disposition": 'attachment; filename="synthetic-ABHD4-GRCh38.vcf"'},
        )

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")
    return app


app = create_app()
