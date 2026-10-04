"""The comparison must fail closed when data or protocol drift."""

import argparse
import copy
import json
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("h5py")
torch = pytest.importorskip("torch")

from test_longevity_chunks import Tokenizer, metadata

from longevity.chunks import load_chunks, write_genome
from longevity.comparison import apply_settings, create_spec, digest, read_spec, validate_cohort
from longevity.train import main


@pytest.fixture
def reference(tmp_path):
    base = read_spec(Path(__file__).parents[1] / "configs/longevity-anage100-comparison.json")
    cfg = dict(base["settings"])
    cfg.update(
        model=base["model"],
        input_kind="gene_cds",
        min_genes=2,
        epochs=18,
        split={"train": [1, 2], "val": [3], "test": [4]},
        total_steps=36,
        warmup_steps=1,
        steps_per_epoch=2,
        attn="eager",
        tokens_per_batch=32768,
        eval_tokens_per_batch=131072,
    )
    train_y = np.log10([10.0, 20.0])
    cfg.update(mu=train_y.mean(), sd=train_y.std(ddof=1))
    run = tmp_path / "reference"
    run.mkdir()
    (run / "config.json").write_text(json.dumps(cfg))
    (run / "metrics.jsonl").write_text(json.dumps({"kind": "done", "step": 36}) + "\n")
    rows = [metadata(i) for i in range(1, 5) for _ in range(2)] + [metadata(5)]
    df = pd.DataFrame(rows)
    df["log10_longevity"] = np.log10(df.max_longevity_yrs)
    pairs = tmp_path / "pairs.parquet"
    df.to_parquet(pairs)
    return run, pairs


def test_freezes_effective_cds_cohort_and_settings(reference, tmp_path):
    spec = create_spec(*reference, "epochs")
    assert len(spec["cohort"]) == 4  # fifth species fails the CDS min-genes threshold
    assert spec["settings"]["epochs"] == 18
    assert spec["settings"]["time_budget_min"] == 0
    args = argparse.Namespace(**spec["settings"])
    args.epochs = 2
    with pytest.raises(ValueError, match="epochs conflicts"):
        apply_settings(args, spec, {"epochs"})
    apply_settings(args, spec, set())
    assert args.epochs == 18
    df = pd.DataFrame(spec["cohort"])
    validate_cohort(df, spec)
    with pytest.raises(ValueError, match="genome mismatch"):
        validate_cohort(df.iloc[1:], spec)
    df.loc[0, "family"] = "Wrong family"
    with pytest.raises(ValueError, match="family"):
        validate_cohort(df, spec)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    assert read_spec(path) == spec
    spec["settings"]["epochs"] = 19
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="checksum"):
        read_spec(path)


def test_reference_label_and_split_drift_rejected(reference):
    run, pairs = reference
    df = pd.read_parquet(pairs)
    df.loc[df.ncbi_taxid == 1, "max_longevity_yrs"] = 50
    df.loc[df.ncbi_taxid == 1, "log10_longevity"] = np.log10(50)
    df.to_parquet(pairs)
    with pytest.raises(ValueError, match="normalization"):
        create_spec(run, pairs, "epochs")
    cfg = json.loads((run / "config.json").read_text())
    cfg["split"]["test"].append(1)
    (run / "config.json").write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="Overlapping"):
        create_spec(run, pairs, "epochs")


@pytest.mark.parametrize("budget", ["epochs", "updates"])
def test_training_enforces_comparison(reference, tmp_path, monkeypatch, budget):
    # A cheap regressor exercises complete epoch/scheduler/H5 wiring; the chunk
    # tests separately exercise the real tiny ModernBERT forward/backward pass.
    class Regressor(torch.nn.Module):
        def __init__(self, *args):
            super().__init__()
            self.backbone = torch.nn.Embedding(16, 2)
            self.head = torch.nn.Linear(2, 1)

        def forward(self, input_ids, attention_mask):
            return self.head(self.backbone(input_ids[:, 1])).squeeze(-1)

    monkeypatch.setattr("longevity.train.LongevityRegressor", Regressor)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    torch.set_num_threads(2)
    if budget == "epochs":
        cfg_path = reference[0] / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg.update(eval_every_steps=100, eval_genes_per_species=50,
                   final_eval_genes_per_species=0, save_best=True)
        cfg_path.write_text(json.dumps(cfg))
    spec = create_spec(*reference, budget)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    fasta = tmp_path / "genome.fna"
    fasta.write_text(">a\n" + "ACGT" * 400)
    shards = tmp_path / "shards"
    for row in spec["cohort"]:
        meta = dict(row, comparison_sha256=spec["sha256"])
        write_genome(shards / f"{row['assembly_accession']}.h5", fasta, meta, Tokenizer())
    with ExitStack() as stack:
        df, _ = load_chunks(shards, stack)
        validate_cohort(df, spec)
    argv = ["--data", str(shards), "--out", str(tmp_path / "run"), "--comparison", str(spec_path)]
    with pytest.raises(ValueError, match="lr-encoder conflicts"):
        main([*argv, "--lr-encoder", "0.01"])
    main(argv)
    cfg = json.loads((tmp_path / "run/config.json").read_text())
    assert {k: cfg["split"][k] for k in ("train", "val", "test")} == spec["split"]
    assert set(cfg["split"].get("val_sub", [])) <= set(cfg["split"]["val"])
    assert (cfg["mu"], cfg["sd"]) == (spec["mu"], spec["sd"])
    assert cfg["comparison_sha256"] == spec["sha256"]
    assert cfg["epochs"] == 18 and cfg["max_len"] == 1024
    expected_steps = cfg["steps_per_epoch"] * 18 if budget == "epochs" else 36
    assert cfg["total_steps"] == expected_steps
    metrics = [
        json.loads(line) for line in (tmp_path / "run/metrics.jsonl").read_text().splitlines()
    ]
    assert metrics[-1]["step"] == expected_steps
    assert cfg["warmup_steps"] == (max(1, int(0.05 * expected_steps)) if budget == "epochs" else 1)
    validations = [r for r in metrics if r["kind"] == "val"]
    if budget == "epochs":
        # Periodic evals run on the capped val_sub sample (kind "val"); the final
        # full-set eval is recorded separately as "val_full".
        assert len(validations) == (expected_steps - 1) // 100
        assert all(r["n_pairs"] == 50 for r in validations)
        finals = [r for r in metrics if r["kind"] == "val_full"]
        assert len(finals) == 1 and finals[0]["n_pairs"] == 1000
        saved = torch.load(tmp_path / "run/best.pt", weights_only=False)
        assert saved["val_species_mae_log10"] == min(r["species_mae_log10"] for r in validations)
    else:
        assert len(validations) == 18
    # Correct shape and labels alone cannot pass off shards from a different protocol.
    bad_spec = copy.deepcopy(spec)
    bad_spec.pop("sha256")
    bad_spec["settings"]["seed"] += 1
    bad_spec["sha256"] = digest(bad_spec)
    spec_path.write_text(json.dumps(bad_spec))
    argv[3] = str(tmp_path / "other_run")
    with pytest.raises(SystemExit):
        main(argv)


def test_prepare_selects_only_reference_assemblies(reference, tmp_path, monkeypatch):
    from transformers import AutoTokenizer

    from longevity.chunks import main as prepare

    spec = create_spec(*reference, "epochs")
    spec_path = tmp_path / "comparison.json"
    spec_path.write_text(json.dumps(spec))
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "genome.fna").write_text(">a\n" + "ACGT" * 400)
    genomes = pd.DataFrame(
        [
            {
                "assembly_accession": f"GCF_{i}",
                "ncbi_taxid": i + 100,
                "species_taxid": i,
                "sequence_file": "genome.fna",
            }
            for i in range(1, 6)
        ]
    )
    genomes.to_parquet(dataset / "genomes.parquet")
    species = pd.DataFrame([metadata(i) for i in range(1, 6)])
    species.drop(columns=["assembly_accession", "max_longevity_yrs"]).to_parquet(
        dataset / "species.parquet"
    )
    species[["ncbi_taxid", "max_longevity_yrs"]].assign(is_primary=True).to_parquet(
        dataset / "longevity.parquet"
    )
    monkeypatch.setattr(AutoTokenizer, "from_pretrained", lambda *a, **kw: Tokenizer())
    monkeypatch.setattr(
        "sys.argv",
        [
            "chunks",
            "--dataset",
            str(dataset),
            "--out",
            str(tmp_path / "prepared"),
            "--comparison",
            str(spec_path),
        ],
    )
    prepare()
    with ExitStack() as stack:
        df, rows = load_chunks(tmp_path / "prepared", stack)
        validate_cohort(df, spec)
        assert len(rows) == 4000
        assert df.comparison_sha256.eq(spec["sha256"]).all()
    genomes.iloc[1:].to_parquet(dataset / "genomes.parquet")
    with pytest.raises(ValueError, match="genome mismatch"):
        prepare()
