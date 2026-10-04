import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from longevity import gi_api
from longevity.gi_api import payload, request_hash, validate_response
from longevity.gi_prepare import (
    attributes,
    normalize_homologies,
    oriented_window,
    select_cohort,
    window_bounds,
)


def config():
    return json.loads(open("configs/gi-hepatocytes.json").read())


def test_tss_base_is_at_exact_flank_on_both_strands():
    sequence = "AACCGTACGTTAGCCATGGC"
    complement = str.maketrans("ACGT", "TGCA")
    for strand in ["+", "-"]:
        for tss in [5, 8, 13]:
            w = oriented_window(sequence, tss, strand, 4)
            assert len(w) == 8
            expected = sequence[tss] if strand == "+" else sequence[tss].translate(complement)
            assert w[4] == expected
    assert window_bounds(8, "-", 4) == (5, 13)
    assert oriented_window(sequence, 1, "+", 4) is None
    assert oriented_window(sequence, len(sequence) - 2, "-", 4) is None


def test_context_and_other_inputs_do_not_depend_on_species():
    c = config()
    a = payload(c, "A" * 81920)
    b = payload(c, "C" * 81920)
    assert {k: v for k, v in a.items() if k != "sequence"} == {
        k: v for k, v in b.items() if k != "sequence"
    }
    assert a["options"] == {
        "description": "Homo sapiens primary hepatocytes, untreated, bulk RNA-seq."
    }
    assert a["tss_index"] == 40960
    assert a["model"] == "g0-expression-8192"
    assert request_hash(a) != request_hash(b)
    b["sequence"] = a["sequence"]
    assert request_hash(a) == request_hash(b)
    b["options"]["description"] = "changed"
    assert request_hash(a) != request_hash(b)


def test_reject_model_tss_and_nonfinite_response_mismatches():
    c = config()
    response = {
        "meta": {
            "model": c["model"],
            "sequence_length": 81920,
            "task_specific_counts": {"tss_index": 40960, "scored_window": [20000, 70000]},
        },
        "data": {
            "prediction": {"expression_log_tpm": 2.5},
            "input": {"description": c["description"], "sequence_name": c["sequence_name"]},
        },
    }
    assert validate_response(c, response) == 2.5
    response["meta"]["model"] = "g0-expression"
    with pytest.raises(ValueError, match="model mismatch"):
        validate_response(c, response)
    response["meta"]["model"] = c["model"]
    response["meta"]["task_specific_counts"]["tss_index"] = 40959
    with pytest.raises(ValueError, match="TSS mismatch"):
        validate_response(c, response)
    response["meta"]["task_specific_counts"]["tss_index"] = 40960
    response["data"]["prediction"]["expression_log_tpm"] = float("nan")
    with pytest.raises(ValueError, match="Non-finite"):
        validate_response(c, response)


def test_repeated_gtf_tags_preserved():
    a = attributes('gene_id "G1"; tag "Ensembl_canonical"; tag "mRNA_start_NF";')
    assert a["tag"] == ["Ensembl_canonical", "mRNA_start_NF"]


def test_orthology_combines_both_exports_and_directions():
    # Ensembl's human export omits some entire partner-species pair sets.
    # The reverse export must also be consumed, irrespective of row direction.
    human = pd.DataFrame(
        [
            {
                "species": "homo_sapiens",
                "gene_stable_id": "H1",
                "homology_species": "rat",
                "homology_gene_stable_id": "R1",
                "homology_type": "ortholog_one2one",
                "is_high_confidence": 1,
            }
        ]
    )
    mouse = pd.DataFrame(
        [
            {
                "species": "mouse",
                "gene_stable_id": "M1",
                "homology_species": "homo_sapiens",
                "homology_gene_stable_id": "H1",
                "homology_type": "ortholog_one2one",
                "is_high_confidence": 1,
            },
            {
                "species": "mouse",
                "gene_stable_id": "M2",
                "homology_species": "homo_sapiens",
                "homology_gene_stable_id": "H2",
                "homology_type": "ortholog_one2many",
                "is_high_confidence": 1,
            },
            {
                "species": "mouse",
                "gene_stable_id": "M3",
                "homology_species": "homo_sapiens",
                "homology_gene_stable_id": "H3",
                "homology_type": "ortholog_one2one",
                "is_high_confidence": 0,
            },
        ]
    )
    combined = pd.concat([normalize_homologies(x, {"rat", "mouse"}) for x in [human, mouse]])
    assert set(
        map(tuple, combined[["human_gene_id", "gene_id", "ensembl_species"]].to_numpy())
    ) == {("H1", "R1", "rat"), ("H1", "M1", "mouse")}


def test_selection_is_deterministic_and_balances_orders():
    d = pd.DataFrame(
        [
            {
                "ensembl_species": f"{order}_{i}",
                "anage_order": order,
                "adult_weight_g": 10 + i * 10,
                "max_longevity_yrs": 2 + i,
            }
            for order in ["Rodentia", "Carnivora", "Primates"]
            for i in range(6)
        ]
    )
    a = select_cohort(d, 9, 4)
    b = select_cohort(d.sample(frac=1, random_state=1), 9, 4)
    assert set(a.ensembl_species) == set(b.ensembl_species)
    assert a.anage_order.value_counts().to_dict() == dict.fromkeys(
        ["Carnivora", "Primates", "Rodentia"], 3
    )
    assert not a.ensembl_species.duplicated().any()


def test_runner_enforces_limit_and_manifest_sequence_integrity(tmp_path, monkeypatch):
    from longevity.gi_prepare import digest, write_json

    c = config()
    write_json(tmp_path / "config.json", c)
    view = tmp_path / "analyses" / "broad"
    view.mkdir(parents=True)
    (tmp_path / "windows").mkdir()
    seq = "A" * 81920
    rows = [
        {
            "ensembl_species": "mouse",
            "gene_id": f"G{i}",
            "sequence": seq,
            "sequence_sha256": hashlib.sha256(seq.encode()).hexdigest(),
        }
        for i in range(4)
    ]
    pd.DataFrame(rows).to_parquet(tmp_path / "windows/mouse.parquet", index=False)
    pd.DataFrame(rows).drop(columns="sequence").to_parquet(
        view / "prediction_manifest.parquet", index=False
    )
    write_json(
        view / "summary.json",
        {
            "status": "prepared_not_predicted",
            "config_sha256": digest(tmp_path / "config.json"),
            "manifest_sha256": digest(view / "prediction_manifest.parquet"),
        },
    )
    submitted = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def predict(self, row):
            submitted.append(row["gene_id"])
            return {"status": "ok", "request_id": "mouse:" + row["gene_id"]}

    monkeypatch.setattr(gi_api, "Client", FakeClient)
    monkeypatch.setattr(gi_api, "api_key", lambda: "unused_test_key")
    args = SimpleNamespace(analysis="broad", workers=2, max_rps=8, limit=2)
    gi_api.run(c, tmp_path, args)
    assert sorted(submitted) == ["G0", "G1"]
    assert len((tmp_path / "prediction_requests.jsonl").read_text().splitlines()) == 2
    rows[0]["sequence"] = "C" * 81920
    pd.DataFrame(rows).to_parquet(tmp_path / "windows/mouse.parquet", index=False)
    submitted.clear()
    with pytest.raises(ValueError, match="Sequence differs"):
        gi_api.run(c, tmp_path, args)
    assert not submitted


def test_gene_runner_publishes_complete_genes_and_resumes_failed_gene(tmp_path, monkeypatch):
    from longevity.gi_prepare import digest, write_json

    c = config()
    write_json(tmp_path / "config.json", c)
    view = tmp_path / "analyses/broad"
    view.mkdir(parents=True)
    (tmp_path / "windows").mkdir()
    rows = []
    for i, (gene, species) in enumerate(
        (g, s) for g in ["H1", "H2"] for s in ["mouse", "rat", "human"]
    ):
        sequence = format(i, "08b").translate(str.maketrans("01", "AC")) + "G" * (81920 - 8)
        rows.append(
            {
                "human_gene_id": gene,
                "gene_id": species + gene,
                "ensembl_species": species,
                "request_id": species + ":" + species + gene,
                "sequence": sequence,
                "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
            }
        )
    for species in ["mouse", "rat", "human"]:
        pd.DataFrame([r for r in reversed(rows) if r["ensembl_species"] == species]).to_parquet(
            tmp_path / "windows" / f"{species}.parquet", index=False
        )
    manifest = pd.DataFrame(rows).drop(columns="sequence")
    manifest.to_parquet(view / "prediction_manifest.parquet", index=False)
    write_json(
        view / "summary.json",
        {
            "status": "prepared_not_predicted",
            "config_sha256": digest(tmp_path / "config.json"),
            "manifest_sha256": digest(view / "prediction_manifest.parquet"),
        },
    )
    fail = True
    network = []

    class FakeClient:
        def __init__(self, config, cache, key, max_rps):
            self.cache = cache
            cache.mkdir(exist_ok=True)

        def predict(self, row):
            if row["human_gene_id"] == "H2":
                first = pd.read_parquet(view / "genes/H1.parquet")
                assert len(first) == 3 and first.expression_log_tpm.notna().all()
                if fail and row["ensembl_species"] == "mouse":
                    return {"status": "failed", "request_id": row["request_id"]}
            key = request_hash(payload(c, row["sequence"]))
            path = self.cache / f"{key}.json"
            status = "cached" if path.exists() else "ok"
            if status == "ok":
                network.append(row["request_id"])
                write_json(
                    path,
                    {
                        "meta": {
                            "model": c["model"],
                            "sequence_length": 81920,
                            "task_specific_counts": {
                                "tss_index": 40960,
                                "scored_window": [10000, 70000],
                            },
                        },
                        "data": {
                            "input": {
                                "description": c["description"],
                                "sequence_name": c["sequence_name"],
                            },
                            "prediction": {"expression_log_tpm": 2.5},
                        },
                    },
                )
            return {
                "status": status,
                "request_id": row["request_id"],
                "request_hash": key,
                "response_path": str(path),
            }

    monkeypatch.setattr(gi_api, "Client", FakeClient)
    monkeypatch.setattr(gi_api, "api_key", lambda: "unused_test_key")
    args = SimpleNamespace(analysis="broad", workers=2, max_rps=8, limit=None, order="gene")
    with pytest.raises(RuntimeError, match="Prediction failed"):
        gi_api.run(c, tmp_path, args)
    assert (view / "genes/H1.parquet").exists()
    assert not (view / "genes/H2.parquet").exists()
    assert json.loads((view / "run_progress.json").read_text())["status"] == "failed"
    fail = False
    gi_api.run(c, tmp_path, args)
    status = json.loads((view / "run_progress.json").read_text())
    assert status["status"] == "completed"
    assert status["completed_genes"] == 2 and status["completed_predictions"] == 6
    assert len(network) == len(set(network)) == 6  # Completed API calls are never repeated.
    assert pd.read_parquet(view / "genes/H2.parquet").request_id.tolist() == sorted(
        manifest[manifest.human_gene_id == "H2"].request_id
    )
    staged = pd.read_parquet(view / "gene_windows.parquet")
    staged.loc[0, "sequence"] = "T" * 81920
    staged.to_parquet(view / "gene_windows.parquet", index=False)
    with pytest.raises(ValueError, match="Sequence differs"):
        gi_api.run(c, tmp_path, args)
    assert len(network) == 6


def test_prediction_lock_prevents_duplicate_runner(tmp_path):
    import fcntl

    with (tmp_path / "prediction.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Another prediction runner"):
            gi_api.run(config(), tmp_path, SimpleNamespace())
