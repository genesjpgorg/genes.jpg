"""Budget matching and shard reuse must retain cohort, labels and input identities."""

import copy
import json

import h5py
import numpy as np
import pandas as pd
import pytest
from test_longevity_chunks import Tokenizer, metadata
from test_longevity_comparison import reference as reference_fixture

from longevity import chunks
from longevity.chunk_budget import make_budget
from longevity.comparison import create_spec, validate_chunk_counts
from longevity.matched_chunks import prepare_one, take_prefix

reference = reference_fixture


def test_per_genome_token_budget_uses_cropped_lengths(reference, tmp_path):
    run, pairs = reference
    spec = create_spec(run, pairs, "epochs")
    original = tmp_path / "original.json"
    original.write_text(json.dumps(spec))
    df = pd.read_parquet(pairs)
    df["n_tokens"] = [100, 400, 10000, 1022, 700, 900, 500, 700, 100]
    df.to_parquet(pairs)
    result = make_budget(original, pairs, "tokens")
    assert result["chunks_per_assembly"] == {"GCF_1": 1, "GCF_2": 2, "GCF_3": 2, "GCF_4": 1}
    assert result["split"] == spec["split"]
    assert result["settings"] == spec["settings"]
    assert result["mu"] == spec["mu"] and result["sd"] == spec["sd"]
    assert result["parent_comparison_sha256"] == spec["sha256"]
    assert result["sha256"] != spec["sha256"]
    assert result["input_budget"]["reference_dna_tokens"] == 5344
    rows = pd.DataFrame(
        [
            {"assembly_accession": acc}
            for acc, n in result["chunks_per_assembly"].items()
            for _ in range(n)
        ]
    )
    validate_chunk_counts(rows, result)
    with pytest.raises(ValueError, match="counts"):
        validate_chunk_counts(rows.iloc[1:], result)
    assert set(make_budget(original, pairs, "examples")["chunks_per_assembly"].values()) == {2}


def test_reuse_and_fresh_preparation_preserve_identical_prefix(tmp_path, monkeypatch):
    fasta = tmp_path / "g.fna"
    fasta.write_text(">a\n" + "ACGT" * 500)
    meta = metadata(1)
    record = dict(meta, path=fasta)
    spec = {
        "cohort": [meta],
        "settings": {"seed": 0},
        "parent_comparison_sha256": "parent",
        "chunks_per_assembly": {"GCF_1": 7},
        "sampling_pool_size": 10,
        "sha256": "new",
    }
    monkeypatch.setattr(chunks, "_tokenizer", Tokenizer(), raising=False)
    fresh = tmp_path / "fresh"
    prepare_one(record, spec, fresh, None, False)
    pool = tmp_path / "pool"
    original = copy.deepcopy(spec)
    original["chunks_per_assembly"]["GCF_1"] = 10
    original["sha256"] = "parent"
    prepare_one(record, original, pool, None, False)
    reused = tmp_path / "reused"
    prepare_one(record, spec, reused, pool, False)
    with h5py.File(fresh / "GCF_1.h5") as a, h5py.File(reused / "GCF_1.h5") as b:
        for key in a:
            np.testing.assert_array_equal(a[key][:], b[key][:])
        assert b["input_ids"].shape == (7, 1024)
        assert json.loads(b.attrs["metadata"])["comparison_sha256"] == "new"
    prepare_one(record, spec, reused, pool, True)
    with pytest.raises(FileExistsError):
        prepare_one(record, spec, reused, pool, False)
    with h5py.File(reused / "GCF_1.h5", "r+") as f:
        f["labels"][0] = 999
    with pytest.raises(ValueError, match="label"):
        prepare_one(record, spec, reused, pool, True)
    with pytest.raises(ValueError, match="exceeds"):
        take_prefix(pool / "GCF_1.h5", tmp_path / "oversized.h5", meta, 11)
    assert not (tmp_path / "oversized.h5").exists()


def test_training_and_audit_enforce_variable_budgets(reference, tmp_path, monkeypatch):
    import torch

    from longevity.audit_chunks import audit
    from longevity.comparison import digest
    from longevity.train import main

    class Model(torch.nn.Module):
        def __init__(self, *args):
            super().__init__()
            self.backbone = torch.nn.Embedding(16, 2)
            self.head = torch.nn.Linear(2, 1)

        def forward(self, input_ids, attention_mask):
            return self.head(self.backbone(input_ids[:, 1])).squeeze(-1)

    monkeypatch.setattr("longevity.train.LongevityRegressor", Model)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    spec = create_spec(*reference, "epochs")
    spec.pop("sha256")
    spec["chunks_per_assembly"] = {
        r["assembly_accession"]: i + 5 for i, r in enumerate(spec["cohort"])
    }
    spec["sha256"] = digest(spec)
    frozen = tmp_path / "variable.json"
    frozen.write_text(json.dumps(spec))
    fasta = tmp_path / "genome.fna"
    fasta.write_text(">a\n" + "ACGT" * 400)
    data = tmp_path / "chunks"
    for row in spec["cohort"]:
        acc = row["assembly_accession"]
        chunks.write_genome(
            data / f"{acc}.h5",
            fasta,
            dict(row, comparison_sha256=spec["sha256"]),
            Tokenizer(),
            count=spec["chunks_per_assembly"][acc],
        )
    report = audit(data, frozen, tmp_path / "audit.json")
    assert report["n_inputs"] == 26
    main(["--data", str(data), "--out", str(tmp_path / "run"), "--comparison", str(frozen)])
    cfg = json.loads((tmp_path / "run/config.json").read_text())
    assert cfg["split"] == spec["split"] and cfg["total_steps"] == 18
    records = [
        json.loads(line) for line in (tmp_path / "run/metrics.jsonl").read_text().splitlines()
    ]
    assert records[-1]["kind"] == "done"
    assert [r for r in records if r["kind"] == "test"][-1]["n_pairs"] == 8
