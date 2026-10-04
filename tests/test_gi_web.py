"""Local API integration tests use synthetic DNA and a deterministic GI stand-in."""

import hashlib
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pysam")
pytest.importorskip("scipy")
import pysam
from fastapi.testclient import TestClient

from longevity.gi_api import payload, request_hash
from longevity.gi_sequence_lifespan import CONFIG
from longevity.gi_web import JobManager, create_app

ROOT = Path(__file__).resolve().parents[1]


class FakeGI:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir()
        self.calls = 0

    def predict(self, row):
        key = request_hash(payload(CONFIG, row["sequence"]))
        path = self.directory / f"{key}.json"
        cached = path.exists()
        if not cached:
            self.calls += 1
            path.write_text(
                json.dumps(
                    {
                        "meta": {
                            "model": CONFIG["model"],
                            "sequence_length": 81920,
                            "task_specific_counts": {
                                "tss_index": 40960,
                                "scored_window": [36864, 45056],
                            },
                        },
                        "data": {
                            "input": {
                                "description": CONFIG["description"],
                                "sequence_name": CONFIG["sequence_name"],
                            },
                            "prediction": {
                                "expression_log_tpm": 1 + row["sequence"].count("C") * 0.1
                            },
                        },
                    }
                )
            )
        return {"status": "cached" if cached else "ok", "response_path": str(path)}


@pytest.fixture
def service(tmp_path):
    reference = tmp_path / "reference.fa"
    reference.write_text(">1\n" + "A" * 100000 + "\n")
    pysam.faidx(str(reference))
    fake = FakeGI(tmp_path / "fake-responses")
    manager = JobManager(tmp_path / "state", reference, tmp_path / "no-key", client=fake)
    gene = next(
        g
        for g, w in manager.predictor.weights["fdr_genes_ridge"].items()
        if w["gene_name"] == "ABHD4"
    )
    manager.predictor.windows = {
        gene: {
            "human_gene_id": gene,
            "gene_name": "ABHD4",
            "contig": "1",
            "tss0": 45000,
            "strand": "+",
            "window_start0": 4040,
            "window_end0": 85960,
            "sequence_sha256": hashlib.sha256(("A" * 81920).encode()).hexdigest(),
        }
    }
    with TestClient(create_app(manager), base_url="http://localhost") as client:
        yield client, manager, fake


def upload_job(client, gt="1/1"):
    content = f"##fileformat=VCFv4.3\n##reference=GRCh38\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tTEST\n1\t45006\t.\tA\tC\t.\tPASS\t.\tGT\t{gt}\n"
    uploaded = client.post("/api/uploads", files={"file": ("sample.vcf", content)})
    assert uploaded.status_code == 200, uploaded.text
    job = client.post(
        "/api/jobs",
        json={
            "upload_id": uploaded.json()["id"],
            "sample": "TEST",
            "model": "fdr_genes_ridge",
            "phase_draws": 4,
            "assembly": "GRCh38",
        },
    )
    assert job.status_code == 202, job.text
    return job.json()["id"]


def completed(client, job_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}").json()
        if response["status"] in {"complete", "failed", "cancelled"}:
            assert response["status"] == "complete", response
            return response["result"]
        time.sleep(0.02)
    raise AssertionError("Job did not finish")


def test_live_pipeline_semantics_with_fake_gi_and_cache(service):
    client, manager, fake = service
    config = client.get("/api/config").json()
    assert config["reference_ready"] and config["api_key_ready"]
    assert all("reference_years" not in model for model in config["models"])
    assert client.get("/").status_code == 200
    job_id = upload_job(client)
    result = completed(client, job_id)
    assert result["genes_affected"] == 1
    assert result["requests_total"] == result["api_requests"] == fake.calls == 2
    assert result["percent_change"] != 0
    assert result["genes"][0]["expression_delta"] == pytest.approx(0.1)
    assert (
        client.get(f"/api/jobs/{job_id}/result.json").json()["percent_change"]
        == result["percent_change"]
    )
    assert "ABHD4" in client.get(f"/api/jobs/{job_id}/genes.csv").text
    repeat = completed(client, upload_job(client))
    assert repeat["cache_hits"] == 2 and repeat["api_requests"] == 0 and fake.calls == 2
    assert repeat["percent_change"] == result["percent_change"]
    assert (manager.state / "jobs" / job_id / "sequence_plan.json").is_file()
    # Pre-update saved jobs must also omit absolute estimates in status and downloads.
    old_result = dict(
        result,
        result_schema_version=1,
        reference_predicted_years=30,
        modified_predicted_years=31,
        frozen_reference_predicted_years=30,
    )
    manager.update(job_id, result=old_result)
    for exported in (
        client.get(f"/api/jobs/{job_id}").json()["result"],
        client.get(f"/api/jobs/{job_id}/result.json").json(),
        client.post(f"/api/jobs/{job_id}/cancel").json()["result"],
    ):
        assert not any(key.endswith("_years") for key in exported)
        assert exported["percent_change"] == result["percent_change"]
        assert exported["result_schema_version"] == 2


def test_no_variant_calls_are_exactly_reference_and_no_gi(service):
    client, _, fake = service
    result = completed(client, upload_job(client, "0/0"))
    assert fake.calls == 0
    assert result["percent_change"] == 0
    assert not any(key.endswith("_years") for key in result)
    assert result["phase_percent_range"] == [0, 0]


def test_local_origin_bad_input_and_unknown_ids(service):
    client, _, _ = service
    assert client.get("/api/config", headers={"Host": "attacker.invalid"}).status_code == 400
    assert (
        client.post(
            "/api/jobs", headers={"Origin": "https://attacker.invalid"}, json={}
        ).status_code
        == 403
    )
    assert client.get("/api/jobs/not-an-id").status_code == 404
    bad = client.post("/api/uploads", files={"file": ("bad.vcf", "not a vcf")})
    assert bad.status_code == 422
    assert "Missing VCF" in bad.json()["detail"]
    assert "SYNTHETIC_DEMO" in client.get("/api/example.vcf").text


def test_cancellation_does_not_return_partial_prediction(service):
    import threading

    client, _, fake = service
    entered, release = threading.Event(), threading.Event()
    original = fake.predict

    def paused(row):
        entered.set()
        assert release.wait(timeout=5)
        return original(row)

    fake.predict = paused
    job_id = upload_job(client)
    assert entered.wait(timeout=5)
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "cancelled":
            break
        time.sleep(0.02)
    assert job["status"] == "cancelled"
    assert "result" not in job
    assert client.get(f"/api/jobs/{job_id}/result.json").status_code == 409


def test_interrupted_job_restarts_from_cached_inference(service, monkeypatch):
    import threading

    client, manager, fake = service
    entered, release = threading.Event(), threading.Event()
    original = fake.predict

    def paused(row):
        entered.set()
        assert release.wait(timeout=5)
        return original(row)

    fake.predict = paused
    job_id = upload_job(client)
    assert entered.wait(timeout=5)
    closing = threading.Thread(target=manager.close)
    closing.start()
    assert manager.stopping.wait(timeout=5)
    release.set()
    closing.join(timeout=5)
    assert not closing.is_alive()
    assert manager.snapshot(job_id)["status"] == "queued"
    calls_before = fake.calls
    fake.predict = original
    # Restore the same synthetic reference mapping before the resumed worker starts.
    monkeypatch.setattr("longevity.gi_web.FrozenPredictor", lambda root: manager.predictor)
    restarted = JobManager(manager.state, manager.reference, "unused", client=fake)
    with TestClient(create_app(restarted), base_url="http://localhost") as new_client:
        result = completed(new_client, job_id)
        assert result["result_schema_version"] == 2
        assert len(result["sequence_plan"]) == 1
        assert fake.calls == calls_before
        assert result["cache_hits"] == 2
