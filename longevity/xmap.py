"""Two-stage longevity: fit species-level longevity ~ F(features) on TRAIN species only,
apply to val/test. Features = model's per-species mean longevity pred + aux (mass) pred
(+ optional genome-meta fields).

Requires xeval parquet outputs with pred_log10 (and pred_log10_aux for mass models).

Usage: python -m longevity.xmap --train-preds e4_train.parquet --val-preds e4_val.parquet \
        --test-preds e4_test.parquet [--genome-meta]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def species_agg(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("ncbi_taxid")
    out = g.agg(sci=("scientific_name", "first"), true_l=("log10_longevity", "first"),
                lon_pred=("pred_log10", "mean"), lon_pred_sd=("pred_log10", "std"))
    if "pred_log10_aux" in df:
        out["mass_pred"] = g.pred_log10_aux.mean()
    return out.reset_index()


def report(name, true, pred):
    mae = np.abs(true - pred).mean()
    rho = pd.Series(true).rank().corr(pd.Series(pred).rank())
    pr = pd.Series(true).corr(pd.Series(pred))
    print(f"  {name:34} MAE={mae:.4f} rho={rho:.3f} pearson={pr:.3f}")
    return mae


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-preds", type=Path, required=True)
    ap.add_argument("--val-preds", type=Path, required=True)
    ap.add_argument("--test-preds", type=Path, required=True)
    ap.add_argument("--genome-meta", action="store_true",
                    help="add genome_size/gc_percent/scaffold_count features from dataset")
    args = ap.parse_args()

    tr = species_agg(pd.read_parquet(args.train_preds))
    va = species_agg(pd.read_parquet(args.val_preds))
    te = species_agg(pd.read_parquet(args.test_preds))
    for d in (tr, va, te):
        d["lon_pred_sd"] = d.lon_pred_sd.fillna(0)

    if args.genome_meta:
        g = pd.read_parquet("/mnt/filesystem-w7/genes.jpg/datasets/anage-longevity/genomes.parquet")
        g["log_gs"] = np.log10(g.genome_size.clip(lower=1))
        for d in (tr, va, te):
            extra = d.merge(g[["ncbi_taxid", "log_gs", "gc_percent", "scaffold_count"]],
                            on="ncbi_taxid", how="left")
            for c in ("log_gs", "gc_percent", "scaffold_count"):
                d[c] = extra[c].fillna(extra[c].median())

    base_feats = [c for c in ("lon_pred", "lon_pred_sd", "mass_pred", "log_gs",
                              "gc_percent", "scaffold_count") if c in tr.columns]
    Xtr, ytr = tr[base_feats].to_numpy(), tr.true_l.to_numpy()
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.linear_model import RidgeCV, LinearRegression

    def eval_all(name, fn):
        for split_name, d in (("train", tr), ("val", va), ("test", te)):
            report(f"{name} {split_name}", d.true_l.to_numpy(), fn(d[base_feats].to_numpy()))

    print("single-model species means:")
    for split_name, d in (("val", va), ("test", te)):
        report(f"  raw lon_pred {split_name}", d.true_l.to_numpy(), d.lon_pred.to_numpy())
    if "mass_pred" in tr:
        for split_name, d in (("train", tr), ("val", va), ("test", te)):
            report(f"  aux mass_pred vs true_l {split_name}",
                   d.true_l.to_numpy(), d.mass_pred.to_numpy())
        # pure allometric map: longevity ~ a*mass_pred + b (fit on train)
        a, b = np.polyfit(tr.mass_pred, ytr, 1)
        for split_name, d in (("val", va), ("test", te)):
            report(f"  allo a*mass+b {split_name}", d.true_l.to_numpy(),
                   a * d.mass_pred + b)
        print(f"  allo fit: longevity ~= {a:.3f}*mass_pred {b:+.3f}")
    eval_all("ridge", RidgeCV(alphas=np.logspace(-3, 3, 31)).fit(Xtr, ytr).predict)
    eval_all("gbm", GradientBoostingRegressor(
        n_estimators=300, learning_rate=0.06, max_depth=3,
        subsample=0.9, random_state=0).fit(Xtr, ytr).predict)


if __name__ == "__main__":
    main()
