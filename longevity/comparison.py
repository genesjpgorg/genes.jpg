"""Freeze a CDS run's cohort and training protocol for a controlled input comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

SCHEMA = "longevity-comparison-v1"
COHORT_COLUMNS = [
    "assembly_accession",
    "ncbi_taxid",
    "scientific_name",
    "class",
    "order",
    "family",
    "max_longevity_yrs",
    "log10_longevity",
]
TRAINING_KEYS = [
    "tokens_per_batch",
    "eval_tokens_per_batch",
    "lr_encoder",
    "lr_head",
    "weight_decay",
    "warmup",
    "min_lr_ratio",
    "head_hidden",
    "head_dropout",
    "grad_clip",
    "attn",
    "compile",
    "evals_per_epoch",
    "log_every",
    "seed",
]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def read_spec(path):
    spec = json.loads(Path(path).read_text())
    fingerprint = spec.pop("sha256")
    if spec.get("schema") != SCHEMA or digest(spec) != fingerprint:
        raise ValueError("Invalid comparison specification or checksum")
    spec["sha256"] = fingerprint
    return spec


def cohort(df):
    missing = set(COHORT_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"Missing cohort columns: {sorted(missing)}")
    out = df[COHORT_COLUMNS].drop_duplicates().sort_values("assembly_accession")
    if out.empty or out.assembly_accession.duplicated().any():
        raise ValueError("Empty cohort or conflicting genome metadata")
    if not np.isfinite(out.log10_longevity).all() or not np.allclose(
        out.log10_longevity, np.log10(out.max_longevity_yrs), rtol=0, atol=1e-7
    ):
        raise ValueError("Invalid longevity labels")
    if out.groupby("ncbi_taxid").log10_longevity.nunique().max() > 1:
        raise ValueError("Conflicting species labels")
    return json.loads(out.to_json(orient="records", double_precision=15))


def validate_cohort(df, spec):
    actual = pd.DataFrame(cohort(df)).set_index("assembly_accession")
    expected = pd.DataFrame(spec["cohort"]).set_index("assembly_accession")
    if set(actual.index) != set(expected.index):
        raise ValueError(
            f"Comparison genome mismatch: missing={sorted(set(expected.index) - set(actual.index))}; "
            f"extra={sorted(set(actual.index) - set(expected.index))}"
        )
    actual = actual.loc[expected.index]
    for col in expected:
        if col in {"max_longevity_yrs", "log10_longevity"}:
            equal = np.allclose(actual[col], expected[col], rtol=0, atol=1e-7)
        else:
            equal = actual[col].fillna("").equals(expected[col].fillna(""))
        if not equal:
            raise ValueError(f"Comparison cohort differs in {col}")


def create_spec(run, pairs, budget, allow_running_reference=False):
    run, pairs = Path(run), Path(pairs)
    cfg = json.loads((run / "config.json").read_text())
    if cfg.get("input_kind", "gene_cds") != "gene_cds":
        raise ValueError("Reference must be a CDS run")
    if set(cfg["split"]) - {"train", "val", "test", "val_sub"}:
        raise ValueError("Unknown reference split names")
    if not set(cfg["split"].get("val_sub", [])).issubset(cfg["split"].get("val", [])):
        raise ValueError("Reference val_sub is not a subset of validation")
    split = {k: cfg["split"][k] for k in ("train", "val", "test")}
    if not split["train"]:
        raise ValueError("Reference must contain train/val/test assignments")
    used = [i for ids in split.values() for i in ids]
    if len(used) != len(set(used)):
        raise ValueError("Overlapping reference splits")
    df = pd.read_parquet(pairs, columns=COHORT_COLUMNS)
    df = df[~df.ncbi_taxid.isin(cfg.get("exclude_species", []))]
    if cfg.get("min_genes", 0):
        df = df[df.groupby("ncbi_taxid").ncbi_taxid.transform("size") >= cfg["min_genes"]]
    if set(df.ncbi_taxid) != set(used):
        raise ValueError("CDS pairs after reference filters do not match reference split species")
    records = cohort(df)
    train_y = df[df.ncbi_taxid.isin(split["train"])].groupby("ncbi_taxid").log10_longevity.first()
    mu, sd = float(train_y.mean()), float(train_y.std())
    sd = sd if len(train_y) > 1 and sd > 0 else 1.0
    if not np.allclose([mu, sd], [cfg["mu"], cfg["sd"]], rtol=0, atol=1e-10):
        raise ValueError("CDS labels disagree with reference normalization")
    if cfg["max_len"] != 1024:
        raise ValueError("Reference max_len must be 1024 to match chunk inputs")
    metrics = [json.loads(s) for s in (run / "metrics.jsonl").read_text().splitlines() if s]
    done = [r for r in metrics if r["kind"] == "done"]
    running = not done and allow_running_reference and budget == "epochs"
    if not running and (len(done) != 1 or not 0 < done[0]["step"] <= cfg["total_steps"]):
        raise ValueError("Reference must have exactly one completed training run")
    if not running and budget == "epochs" and done[0]["step"] != cfg["total_steps"]:
        raise ValueError(
            "Epoch matching requires a CDS reference that completed its planned epochs"
        )
    settings = {k: cfg[k] for k in TRAINING_KEYS}
    settings.update(
        max_len=1024,
        epochs=cfg["epochs"],
        time_budget_min=0,
        max_steps=0,
        val_species=split["val"],
        test_species=split["test"],
        exclude_species=[],
        min_genes=0,
        val_frac=0.0,
        test_frac=0.0,
        split_by=cfg["split_by"],
    )
    for key in ("eval_every_steps", "eval_genes_per_species", "final_eval_genes_per_species", "save_best"):
        if key in cfg:
            settings[key] = cfg[key]
    if cfg.get("long_inputs", "crop") != "crop":
        raise ValueError("Only the reference crop input policy is supported")
    with pairs.open("rb") as handle:
        pairs_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    spec = {
        "schema": SCHEMA,
        "historical_software_match": "unverified",
        "reference_run": str(run.resolve()),
        "reference_pairs": str(pairs.resolve()),
        "reference_config_sha256": hashlib.sha256((run / "config.json").read_bytes()).hexdigest(),
        "reference_pairs_sha256": pairs_hash,
        "model": cfg["model"],
        "cohort": records,
        "split": split,
        "mu": cfg["mu"],
        "sd": cfg["sd"],
        "settings": settings,
        "budget": budget,
        "reference_max_len": cfg["max_len"],
        "reference_total_steps": cfg["total_steps"],
        "reference_completed_steps": done[0]["step"] if done else None,
        "reference_status_at_freeze": "running" if running else "complete",
        "reference_warmup_steps": cfg["warmup_steps"],
        "reference_steps_per_epoch": cfg["steps_per_epoch"],
        "limitations": [
            (
                "Historical model/tokenizer revisions, software environment and genome bytes "
                "were not pinned by the CDS run; assembly accessions are matched."
            ),
            (
                "1000 chunks per genome differs from variable CDS counts and lengths; "
                "example weighting and observed tokens differ."
            ),
        ],
    }
    spec["sha256"] = digest(spec)
    return spec


def apply_settings(args, spec, explicit):
    for key, value in spec["settings"].items():
        if key in explicit and getattr(args, key) != value:
            raise ValueError(f"--{key.replace('_', '-')} conflicts with comparison specification")
        setattr(args, key, value)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reference-run", type=Path, required=True)
    ap.add_argument("--pairs", type=Path, required=True, help="CDS pairs used by the reference run")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--budget", choices=["updates", "epochs"], required=True)
    ap.add_argument("--allow-running-reference", action="store_true")
    args = ap.parse_args()
    spec = create_spec(args.reference_run, args.pairs, args.budget, args.allow_running_reference)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(spec, indent=2, allow_nan=False) + "\n")
    print(
        f"Saved {len(spec['cohort'])} assemblies; splits "
        f"{ {k: len(v) for k, v in spec['split'].items()} }; sha256={spec['sha256']}"
    )


if __name__ == "__main__":
    main()
