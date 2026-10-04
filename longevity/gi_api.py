"""Fixed-context GI client, bounded throughput benchmark, resumable prediction runner.

Benchmarking is explicit and never starts the full dataset. The run subcommand must
be called separately. Credentials are read at runtime and never saved in results.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import requests

from longevity.gi_prepare import digest, download, load_config, log, write_json

BASE = "https://api.genomicintelligence.ai"


def api_key(env_file=".env"):
    import os

    key = os.environ.get("GI_API_KEY")
    if not key and Path(env_file).exists():
        for line in Path(env_file).read_text().splitlines():
            k, sep, value = line.partition("=")
            if sep and k.strip() == "GI_API_KEY":
                key = value.strip().strip("\"'")
    if not key:
        raise ValueError("GI_API_KEY is missing")
    return key


def payload(c, sequence):
    if len(sequence) != 2 * c["flank_bp"] or set(sequence) - set("ACGTN"):
        raise ValueError("Invalid prepared sequence")
    return {
        "model": c["model"],
        "sequence": sequence,
        "tss_index": c["flank_bp"],
        "sequence_name": c["sequence_name"],
        "options": {"description": c["description"]},
    }


def request_hash(body):
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_response(c, data):
    meta = data["meta"]
    model = meta["model"]
    if isinstance(model, dict):
        model = model.get("id", model.get("name"))
    if model != c["model"]:
        raise ValueError(f"Returned model mismatch: {model}")
    if data["data"]["input"]["description"] != c["description"]:
        raise ValueError("Returned description mismatch")
    if data["data"]["input"]["sequence_name"] != c["sequence_name"]:
        raise ValueError("Returned sequence_name mismatch")
    if meta["sequence_length"] != 2 * c["flank_bp"]:
        raise ValueError("Returned sequence length mismatch")
    counts = meta["task_specific_counts"]
    if counts["tss_index"] != c["flank_bp"]:
        raise ValueError("Returned TSS mismatch")
    start, end = counts["scored_window"]
    if not (0 <= start < c["flank_bp"] < end <= c["flank_bp"] * 2):
        raise ValueError("Invalid scored window")
    value = float(data["data"]["prediction"]["expression_log_tpm"])
    if not math.isfinite(value):
        raise ValueError("Non-finite prediction")
    return value


class Client:
    def __init__(self, config, cache, key, max_rps=8.0):
        self.config, self.cache, self.key = config, Path(cache), key
        self.cache.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.max_rps = max_rps
        self.local = threading.local()

    def throttle(self):
        with self.lock:
            now = time.monotonic()
            wait = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + 1 / self.max_rps
        if wait:
            time.sleep(wait)

    def predict(self, row, use_cache=True):
        body = payload(self.config, row["sequence"])
        key = request_hash(body)
        path = self.cache / f"{key}.json"
        base = {
            "request_hash": key,
            "request_id": row.get("request_id", row["ensembl_species"] + ":" + row["gene_id"]),
            "species": row["ensembl_species"],
            "gene_id": row["gene_id"],
            "sequence_sha256": row["sequence_sha256"],
        }
        if use_cache and path.exists():
            d = json.loads(path.read_text())
            validate_response(self.config, d)
            return dict(base, status="cached", attempts=[], response_path=str(path), wall_seconds=0)
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers["Authorization"] = "Bearer " + self.key
        attempts, overall = [], time.monotonic()
        for attempt in range(5):
            self.throttle()
            started = time.monotonic()
            try:
                response = self.local.session.post(
                    BASE + "/v1/tasks/expression/predict", json=body, timeout=(30, 180)
                )
                elapsed = time.monotonic() - started
                headers = {
                    k.lower(): v
                    for k, v in response.headers.items()
                    if k.lower().startswith("ratelimit") or k.lower() == "retry-after"
                }
                attempts.append(
                    {"http_status": response.status_code, "seconds": elapsed, "headers": headers}
                )
                if "ratelimit-limit" in headers:
                    # GI advertises 10-second bucket capacity, not requests per minute.
                    rpm = float(headers["ratelimit-limit"]) * 6
                    with self.lock:
                        self.max_rps = min(self.max_rps, 0.8 * rpm / 60)
                if response.status_code == 200:
                    data = response.json()
                    try:
                        value = validate_response(self.config, data)
                    except (KeyError, ValueError, TypeError) as e:
                        write_json(self.cache / f"{key}.invalid.json", data)
                        return dict(
                            base,
                            status="invalid_response",
                            error=str(e),
                            attempts=attempts,
                            wall_seconds=time.monotonic() - overall,
                        )
                    write_json(path, data)
                    return dict(
                        base,
                        status="ok",
                        expression_log_tpm=value,
                        attempts=attempts,
                        response_path=str(path),
                        cold_start=data["meta"].get("cold_start"),
                        server_timing=data["meta"],
                        wall_seconds=time.monotonic() - overall,
                    )
                try:
                    error = response.json().get("error", {})
                except ValueError:
                    error = {"code": "non_json_response"}
                if response.status_code not in (429, 500, 502, 503, 504):
                    return dict(
                        base,
                        status="failed",
                        error=error,
                        attempts=attempts,
                        wall_seconds=time.monotonic() - overall,
                    )
                delay = min(30, max(float(headers.get("retry-after", 0)), 2**attempt))
            except requests.RequestException as e:
                attempts.append(
                    {
                        "http_status": None,
                        "seconds": time.monotonic() - started,
                        "error_type": type(e).__name__,
                    }
                )
                delay = 2**attempt
            if attempt < 4:
                time.sleep(delay)
        return dict(
            base, status="failed", attempts=attempts, wall_seconds=time.monotonic() - overall
        )


def sample_windows(out, n, seed=20261004, analysis=None):
    """Diverse distinct genes/species, no local cache hits in throughput measurements."""
    paths = sorted((out / "windows").glob("*.parquet"))
    paths = [
        p
        for p in paths
        if (out / "qc" / f"{p.stem}.json").exists()
        and json.loads((out / "qc" / f"{p.stem}.json").read_text())["valid_windows"] >= 1000
    ]
    eligible = None
    if analysis:
        manifest = pd.read_parquet(out / "analyses" / analysis / "prediction_manifest.parquet")
        eligible = {sp: set(g.gene_id) for sp, g in manifest.groupby("ensembl_species")}
        paths = [p for p in paths if p.stem in eligible]
    if not paths:
        raise ValueError("No completed prepared species are available")
    rng = np.random.default_rng(seed)
    per_species = math.ceil(n / len(paths))
    rows = []
    for path in paths:
        f = pq.ParquetFile(path)
        meta = pd.read_parquet(out / "window_metadata" / path.name)
        if eligible:
            meta = meta[meta.gene_id.isin(eligible[path.stem])]
        indices = rng.choice(meta.index.to_numpy(), size=min(per_species, len(meta)), replace=False)
        # Each file uses row groups of <=64 rows; read only selected groups.
        offsets = np.cumsum(
            [0] + [f.metadata.row_group(i).num_rows for i in range(f.num_row_groups)]
        )
        for group in sorted(set(np.searchsorted(offsets[1:], indices, side="right"))):
            data = f.read_row_group(int(group)).to_pylist()
            rows.extend(
                data[int(i - offsets[group])]
                for i in indices
                if offsets[group] <= i < offsets[group + 1]
            )
    rng.shuffle(rows)
    seen, distinct = set(), []
    for row in rows:
        if row["sequence_sha256"] not in seen:
            distinct.append(row)
            seen.add(row["sequence_sha256"])
    if len(distinct) < n:
        raise ValueError(f"Only {len(distinct)} unique sequences available, need {n}")
    return distinct[:n]


def measure(client, rows, workers, log_path):
    records = []
    t0 = time.monotonic()
    with log_path.open("a") as handle, ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(client.predict, r, False) for r in rows]
        for f in as_completed(futures):
            r = f.result()
            records.append(r)
            handle.write(json.dumps(r) + "\n")
            handle.flush()
    wall = time.monotonic() - t0
    ok = [r for r in records if r["status"] == "ok"]
    latencies = [a["seconds"] for r in ok for a in r["attempts"] if a["http_status"] == 200]
    result = {
        "workers": workers,
        "requests": len(rows),
        "successful": len(ok),
        "failed": len(rows) - len(ok),
        "wall_seconds": wall,
        "successful_per_second": len(ok) / wall,
        "client_rate_ceiling_rps": client.max_rps,
        "http_429": sum(a["http_status"] == 429 for r in records for a in r["attempts"]),
        "http_5xx": sum(
            a["http_status"] is not None and a["http_status"] >= 500
            for r in records
            for a in r["attempts"]
        ),
        "cold_starts": sum(bool(r.get("cold_start")) for r in ok),
        "request_latency_p50_seconds": float(np.percentile(latencies, 50)) if latencies else None,
        "request_latency_p95_seconds": float(np.percentile(latencies, 95)) if latencies else None,
    }
    log(json.dumps(result))
    return result


def benchmark(c, out, args):
    bench = out / "benchmarks" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bench.mkdir(parents=True)
    models = download(BASE + "/v1/tasks/expression/models", bench / "models.json")
    download(BASE + "/v1/openapi.json", bench / "openapi.json")
    model = next(m for m in json.loads(models.read_text())["models"] if m["id"] == c["model"])
    assert model["bio_spec"]["recommended_flank_bp"] == c["flank_bp"]
    total = 1 + args.per_stage * len(args.concurrency) + args.sustained
    rows = sample_windows(out, total, seed=args.seed, analysis=args.analysis)
    pd.DataFrame([{k: v for k, v in r.items() if k != "sequence"} for r in rows]).to_parquet(
        bench / "sample_manifest.parquet", index=False
    )
    client = Client(c, out / "gi_responses", api_key())
    log_path = bench / "requests.jsonl"
    warmup = measure(client, rows[:1], 1, log_path)
    if warmup["successful"] != 1:
        raise RuntimeError(f"Initial prediction failed; inspect {log_path}")
    stages, offset = [], 1
    for workers in args.concurrency:
        result = measure(client, rows[offset : offset + args.per_stage], workers, log_path)
        stages.append(result)
        offset += args.per_stage
        write_json(bench / "progress.json", {"warmup": warmup, "stages": stages})
        if result["http_429"] or result["failed"]:
            break
    healthy = [s for s in stages if not s["failed"] and not s["http_429"]]
    if not healthy:
        raise RuntimeError("No healthy benchmark stage; do not start full prediction")
    best = max(healthy, key=lambda s: s["successful_per_second"])
    sustained = measure(client, rows[offset : offset + args.sustained], best["workers"], log_path)
    result = {
        "model": c["model"],
        "description": c["description"],
        "sequence_name": c["sequence_name"],
        "flank_bp": c["flank_bp"],
        "local_cache_bypassed": True,
        "warmup": warmup,
        "stages": stages,
        "sustained": sustained,
        "recommended_workers": best["workers"],
        "sample_species": sorted({r["ensembl_species"] for r in rows[: offset + args.sustained]}),
        "requests_log": str(log_path),
    }
    summary_path = (
        out / "analyses" / args.analysis / "summary.json" if args.analysis else out / "summary.json"
    )
    if summary_path.exists() and sustained["successful_per_second"]:
        summary = json.loads(summary_path.read_text())
        result["full_run_hours_at_measured_rate"] = (
            summary["prediction_requests"] / sustained["successful_per_second"] / 3600
        )
    write_json(bench / "summary.json", result)
    write_json(out / "benchmark_latest.json", dict(result, benchmark_directory=str(bench)))


def run(c, out, args):
    # A second runner would duplicate paid requests and race on cache/log files.
    with (out / "prediction.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError("Another prediction runner holds prediction.lock") from e
        run_locked(c, out, args)


def run_locked(c, out, args):
    if args.analysis is None:
        raise ValueError("Select --analysis core or --analysis broad explicitly")
    summary = json.loads((out / "analyses" / args.analysis / "summary.json").read_text())
    if summary["status"] != "prepared_not_predicted":
        raise ValueError("Analysis manifest is not ready; rerun preparation/finalization")
    if summary["config_sha256"] != digest(out / "config.json"):
        raise ValueError("Configuration differs from finalized manifest")
    manifest_path = out / "analyses" / args.analysis / "prediction_manifest.parquet"
    if summary["manifest_sha256"] != digest(manifest_path):
        raise ValueError("Prediction manifest checksum mismatch")
    manifest = pd.read_parquet(out / "analyses" / args.analysis / "prediction_manifest.parquet")
    if getattr(args, "order", "species") == "gene":
        return run_by_gene(c, out, args, manifest, summary)
    client = Client(c, out / "gi_responses", api_key(), max_rps=args.max_rps)
    remaining = args.limit
    with (
        (out / "prediction_requests.jsonl").open("a") as handle,
        ThreadPoolExecutor(max_workers=args.workers) as ex,
    ):
        pending = set()

        def drain(all_remaining=False):
            nonlocal pending
            if not pending:
                return
            done, pending = (
                wait(pending, return_when=FIRST_COMPLETED)
                if not all_remaining
                else (pending, set())
            )
            for future in done:
                result = future.result()
                handle.write(json.dumps(result) + "\n")
                handle.flush()
                if result["status"] not in ("ok", "cached"):
                    raise RuntimeError(
                        f"Prediction failed: {result['request_id']}. Resume after inspecting the log."
                    )

        for species, group in manifest.groupby("ensembl_species"):
            ids = set(group.gene_id)
            expected_hash = dict(zip(group.gene_id, group.sequence_sha256, strict=True))
            parquet = pq.ParquetFile(out / "windows" / f"{species}.parquet")
            for batch in parquet.iter_batches(batch_size=64):
                rows = [r for r in batch.to_pylist() if r["gene_id"] in ids]
                if remaining is not None:
                    rows = rows[:remaining]
                for row in rows:
                    if (
                        hashlib.sha256(row["sequence"].encode()).hexdigest()
                        != expected_hash[row["gene_id"]]
                    ):
                        raise ValueError("Sequence differs from finalized manifest")
                    pending.add(ex.submit(client.predict, row))
                    if len(pending) >= args.workers * 4:
                        drain()
                if remaining is not None:
                    remaining -= len(rows)
                    if remaining <= 0:
                        drain(all_remaining=True)
                        return
        drain(all_remaining=True)


def stage_gene_windows(out, view, manifest, summary):
    """Reorder sequences once on disk, avoiding repeated reads of species shards."""
    import duckdb

    target = view / "gene_windows.parquet"
    marker = view / "gene_windows.json"
    expected = {"manifest_sha256": summary["manifest_sha256"], "rows": len(manifest)}
    if target.exists() and marker.exists() and json.loads(marker.read_text()) == expected:
        return target
    temporary = target.with_suffix(".parquet.tmp")
    paths = [
        str(out / "windows" / f"{sp}.parquet") for sp in sorted(manifest.ensembl_species.unique())
    ]
    log(f"Staging {len(manifest):,} windows in gene order; no API calls during staging")
    with duckdb.connect() as db:
        db.execute("SET threads=4")
        db.execute("SET memory_limit='32GB'")
        db.register("manifest", manifest)
        db.read_parquet(paths).create_view("windows")
        relation = db.sql("""
            SELECT m.human_gene_id, m.request_id, w.sequence_sha256, w.sequence
            FROM manifest m JOIN windows w
              ON m.ensembl_species=w.ensembl_species AND m.gene_id=w.gene_id
            ORDER BY m.human_gene_id, m.request_id
        """)
        relation.write_parquet(str(temporary), compression="zstd", row_group_size=64)
    staged = pd.read_parquet(temporary, columns=["request_id", "sequence_sha256"])
    expected_rows = manifest[["request_id", "sequence_sha256"]].sort_values("request_id")
    if (
        not staged.sort_values("request_id")
        .reset_index(drop=True)
        .equals(expected_rows.reset_index(drop=True))
    ):
        raise ValueError("Gene staging differs from finalized manifest")
    temporary.replace(target)
    write_json(marker, expected)
    log("Gene-ordered sequence staging complete")
    return target


def iter_gene_windows(path):
    current, rows = None, []
    for batch in pq.ParquetFile(path).iter_batches(batch_size=64):
        for row in batch.to_pylist():
            gene = row["human_gene_id"]
            if current is not None and gene != current:
                yield current, rows
                rows = []
            current = gene
            rows.append(row)
    if rows:
        yield current, rows


def run_by_gene(c, out, args, manifest, summary):
    """Finish and publish all observed species of each gene before starting the next."""
    view = out / "analyses" / args.analysis
    genes_dir = view / "genes"
    genes_dir.mkdir(exist_ok=True)
    total_genes = int(manifest.human_gene_id.nunique())
    progress = {
        "status": "staging",
        "analysis": args.analysis,
        "order": "human_gene_id",
        "manifest_sha256": summary["manifest_sha256"],
        "total_genes": total_genes,
        "total_predictions": len(manifest),
        "completed_genes": 0,
        "completed_predictions": 0,
        "new_predictions": 0,
        "cached_predictions": 0,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    started = time.monotonic()

    def save_progress():
        elapsed = time.monotonic() - started
        progress["elapsed_seconds"] = elapsed
        progress["updated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        done = progress["completed_predictions"]
        progress["estimated_remaining_hours"] = (
            (len(manifest) - done)
            * progress.get("inference_elapsed_seconds", elapsed)
            / done
            / 3600
            if done
            else None
        )
        write_json(view / "run_progress.json", progress)

    save_progress()
    try:
        staged = stage_gene_windows(out, view, manifest, summary)
        inference_started = time.monotonic()
        metadata = manifest.set_index("request_id", drop=False)
        client = Client(c, out / "gi_responses", api_key(), max_rps=args.max_rps)
        progress["status"] = "running"
        save_progress()
        with (
            (out / "prediction_requests.jsonl").open("a") as handle,
            ThreadPoolExecutor(max_workers=args.workers) as executor,
        ):
            for gene, sequences in iter_gene_windows(staged):
                # A request limit stops at a gene boundary, never publishing an incomplete gene.
                if (
                    args.limit is not None
                    and progress["completed_predictions"] + len(sequences) > args.limit
                ):
                    progress["status"] = "limited"
                    save_progress()
                    return
                rows = []
                for sequence in sequences:
                    row = metadata.loc[sequence["request_id"]].to_dict()
                    if (
                        row["human_gene_id"] != gene
                        or sequence["sequence_sha256"] != row["sequence_sha256"]
                        or hashlib.sha256(sequence["sequence"].encode()).hexdigest()
                        != row["sequence_sha256"]
                    ):
                        raise ValueError("Sequence differs from finalized manifest")
                    rows.append(dict(row, sequence=sequence["sequence"]))
                records = []
                futures = [executor.submit(client.predict, row) for row in rows]
                for future in as_completed(futures):
                    result = future.result()
                    handle.write(json.dumps(result) + "\n")
                    handle.flush()
                    if result["status"] not in ("ok", "cached"):
                        raise RuntimeError(
                            f"Prediction failed: {result['request_id']}; inspect request log"
                        )
                    response = json.loads(Path(result["response_path"]).read_text())
                    value = validate_response(c, response)
                    window = response["meta"]["task_specific_counts"]["scored_window"]
                    records.append(
                        {
                            "request_id": result["request_id"],
                            "expression_log_tpm": value,
                            "request_hash": result["request_hash"],
                            "scored_window_start": window[0],
                            "scored_window_end": window[1],
                        }
                    )
                    progress[
                        "cached_predictions" if result["status"] == "cached" else "new_predictions"
                    ] += 1
                result_table = metadata.loc[[r["request_id"] for r in rows]].reset_index(drop=True)
                result_table = result_table.merge(
                    pd.DataFrame(records), on="request_id", validate="one_to_one"
                )
                if result_table.expression_log_tpm.isna().any():
                    raise ValueError(f"Incomplete gene result: {gene}")
                target = genes_dir / f"{gene}.parquet"
                temporary = target.with_suffix(".parquet.tmp")
                result_table.to_parquet(temporary, index=False)
                temporary.replace(target)
                progress["completed_genes"] += 1
                progress["completed_predictions"] += len(rows)
                progress["inference_elapsed_seconds"] = time.monotonic() - inference_started
                progress["last_completed_gene"] = gene
                save_progress()
                log(
                    f"Gene {progress['completed_genes']}/{total_genes} {gene}: {len(rows)} species; "
                    f"{progress['completed_predictions']:,}/{len(manifest):,} predictions; "
                    f"ETA {progress['estimated_remaining_hours']:.2f} h"
                )
        if (
            progress["completed_predictions"] != len(manifest)
            or progress["completed_genes"] != total_genes
        ):
            raise ValueError("Gene-ordered run did not cover the complete manifest")
        progress["status"] = "completed"
        save_progress()
    except BaseException as exc:
        progress["status"] = "failed"
        progress["error"] = f"{type(exc).__name__}: {exc}"
        save_progress()
        raise


def export(c, out, args):
    if not args.analysis:
        raise ValueError("Select --analysis core or --analysis broad")
    view = out / "analyses" / args.analysis
    manifest = pd.read_parquet(view / "prediction_manifest.parquet")
    wanted = dict(zip(manifest.request_id, manifest.sequence_sha256, strict=True))
    records = {}
    logs = sorted((out / "benchmarks").glob("*/requests.jsonl"))
    if (out / "prediction_requests.jsonl").exists():
        logs.append(out / "prediction_requests.jsonl")
    for path in logs:
        # A snapshot during an active run may end partway through the last write.
        snapshot = path.read_text()
        for line in snapshot[: snapshot.rfind("\n") + 1].splitlines():
            row = json.loads(line)
            if row["status"] not in ("ok", "cached"):
                continue
            if wanted.get(row["request_id"]) != row["sequence_sha256"]:
                continue
            response = json.loads(Path(row["response_path"]).read_text())
            value = validate_response(c, response)
            window = response["meta"]["task_specific_counts"]["scored_window"]
            records[row["request_id"]] = {
                "request_id": row["request_id"],
                "expression_log_tpm": value,
                "request_hash": row["request_hash"],
                "scored_window_start": window[0],
                "scored_window_end": window[1],
            }
    observations = pd.DataFrame(
        list(records.values()),
        columns=[
            "request_id",
            "expression_log_tpm",
            "request_hash",
            "scored_window_start",
            "scored_window_end",
        ],
    )
    result = manifest.merge(observations, on="request_id", how="left", validate="one_to_one")
    missing = int(result.expression_log_tpm.isna().sum())
    if missing and not args.allow_partial:
        raise ValueError(
            f"{missing:,} predictions missing; use --allow-partial only for inspection"
        )
    name = "expression.partial.parquet" if missing else "expression.parquet"
    result.to_parquet(view / name, index=False)
    write_json(
        view / "prediction_coverage.json",
        {
            "rows": len(result),
            "predicted": len(result) - missing,
            "missing": missing,
            "complete": not missing,
            "output": str(view / name),
        },
    )
    log(f"Exported {len(result) - missing:,}/{len(result):,} predictions to {view / name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["benchmark", "run", "export"])
    parser.add_argument("--config", default="configs/gi-hepatocytes.json")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--per-stage", type=int, default=32)
    parser.add_argument("--sustained", type=int, default=128)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-rps", type=float, default=8.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--order", choices=["species", "gene"], default="species")
    parser.add_argument("--analysis", choices=["core", "broad"])
    parser.add_argument("--seed", type=int, default=20261004)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    c, out = load_config(args.config)
    if args.stage == "benchmark":
        benchmark(c, out, args)
    elif args.stage == "run":
        run(c, out, args)
    else:
        export(c, out, args)


if __name__ == "__main__":
    main()
