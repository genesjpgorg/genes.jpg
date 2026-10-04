"""Freeze a per-genome random-chunk budget from reference input counts or DNA tokens."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from longevity.comparison import chunk_counts, digest, read_spec, validate_cohort


def make_budget(comparison, pairs, mode):
    spec = read_spec(comparison)
    df = pd.read_parquet(pairs)
    wanted = {r["assembly_accession"] for r in spec["cohort"]}
    df = df[df.assembly_accession.isin(wanted)]
    validate_cohort(df, spec)
    if not np.isfinite(df.n_tokens).all() or (df.n_tokens < 1).any():
        raise ValueError("Invalid reference token lengths")
    dna = df.n_tokens.clip(upper=1022).groupby(df.assembly_accession).sum()
    counts = (
        df.groupby("assembly_accession").size()
        if mode == "examples"
        else (dna / 1022 + 0.5).astype(int).clip(lower=1)
    )
    parent = spec.pop("sha256")
    spec["parent_comparison_sha256"] = parent
    spec["chunks_per_assembly"] = {acc: int(n) for acc, n in counts.items()}
    spec["sampling_pool_size"] = 1000
    if counts.max() > 1000:
        raise ValueError("This reduced-budget experiment requires counts at most 1000")
    with Path(pairs).open("rb") as handle:
        source_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    spec["input_budget"] = {
        "mode": mode,
        "source_pairs": str(Path(pairs).resolve()),
        "source_pairs_sha256": source_hash,
        "reference_examples": len(df),
        "reference_dna_tokens": int(dna.sum()),
        "random_chunks": int(counts.sum()),
        "random_dna_tokens": int(counts.sum() * 1022),
        "rounding": "nearest whole chunk per genome, half up, minimum one"
        if mode == "tokens"
        else "exact example count per genome",
        "reference_tokens": "sum(min(n_tokens, 1022)) per genome per epoch; excludes CLS/SEP",
        "sampling": "first N rows of the original seeded 1000-draw pool, preserving reusable samples",
    }
    spec["limitations"][1] = (
        "Fixed 1022-DNA-token chunks differ from variable gene lengths. "
        "See input_budget for the matching rule and rounding; epochs and splits are unchanged."
    )
    chunk_counts(spec)
    spec["sha256"] = digest(spec)
    return spec


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--comparison", type=Path, required=True)
    ap.add_argument("--pairs", type=Path, required=True)
    ap.add_argument("--mode", choices=["tokens", "examples"], required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    spec = make_budget(args.comparison, args.pairs, args.mode)
    args.out.write_text(json.dumps(spec, indent=2, allow_nan=False) + "\n")
    print(json.dumps(spec["input_budget"], indent=2))


if __name__ == "__main__":
    main()
