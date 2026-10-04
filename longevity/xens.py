"""Ensemble species predictions from multiple run predictions_{split}.parquet files.

For each run, per-pair pred_log10 -> species mean. Then a weighted average across runs
(weights optimised on val, applied to test). Reports species MAE + Spearman.

Usage: python -m longevity.xens --runs runA runB [--runs ...]
"""

from __future__ import annotations

import argparse
import itertools
from pathlib import Path

import numpy as np
import pandas as pd


def species_table(parquet: Path, split: str) -> pd.DataFrame:
    df = pd.read_parquet(parquet)
    sp = (df.groupby("ncbi_taxid")
            .agg(sci=("scientific_name", "first"), true=("log10_longevity", "first"),
                 pred=("pred_log10", "mean"))
            .reset_index())
    return sp


def metrics(true: np.ndarray, pred: np.ndarray) -> dict:
    return {"mae": float(np.abs(true - pred).mean()),
            "spearman": float(pd.Series(true).rank().corr(pd.Series(pred).rank())),
            "pearson": float(pd.Series(true).corr(pd.Series(pred)))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, nargs="+", required=True)
    args = ap.parse_args()

    tabs = {}
    for split in ("val", "test"):
        ts = []
        for r in args.runs:
            f = r / f"predictions_{split}.parquet"
            if f.exists():
                t = species_table(f, split)
                t.columns = ["ncbi_taxid", "sci", "true", f"pred_{r.name}"]
                ts.append(t)
        if not ts:
            continue
        m = ts[0]
        for t in ts[1:]:
            m = m.merge(t[["ncbi_taxid", t.columns[-1]]], on="ncbi_taxid", how="inner")
        tabs[split] = m

    pred_cols = [c for c in tabs["test"].columns if c.startswith("pred_")]
    names = [c[5:] for c in pred_cols]
    print(f"runs: {names}")
    for split, m in tabs.items():
        print(f"\n=== {split} ({len(m)} species) ===")
        for c in pred_cols:
            print(f"  {c}: {metrics(m['true'].to_numpy(), m[c].to_numpy())}")
        if len(pred_cols) > 1:
            P = np.stack([m[c].to_numpy() for c in pred_cols])
            y = m["true"].to_numpy()
            best = (None, 9e9)
            for ws in itertools.product(range(0, 11), repeat=len(pred_cols)):
                if sum(ws) == 0:
                    continue
                w = np.array(ws) / sum(ws)
                mae = np.abs(w @ P - y).mean()
                if mae < best[1]:
                    best = (w, mae)
            w, mae = best
            print(f"  best-weight {dict(zip(names, w.round(2)))}: {metrics(y, w @ P)}")
            if split == "val":
                globals()["_w"] = w
            elif "_w" in globals():
                w = globals()["_w"]
                print(f"  val-chosen-weight applied: {metrics(y, w @ P)}")


if __name__ == "__main__":
    main()
