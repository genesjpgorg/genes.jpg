"""Classical baseline: species-level features from chunk shards -> ridge/GBM -> longevity.

Features per chunk (from BPE token ids, no detokenization needed):
  - token unigram frequencies are NOT interpretable, so instead we compute simple
    distributional stats: token-id histogram over 16 coarse bins, plus mean token id
    entropy proxy. Cheaper: use sklearn's Histogram-based GBM over
    [GC-like proxy via token bigram stats] -> instead just do per-species mean/std of
    per-chunk predicted values from a trained model is circular.

This script instead builds token-count features: for each chunk, count of each of the
32k vocab tokens is too wide; use 256 hashed bins -> per-species mean histogram (256-d)
+ per-species std. Fit GradientBoosting/Ridge on train species, report val/test MAE.

Usage: python -m longevity.xgb --data chunks_dir --split-file split_v3.json --max-rows 300
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import h5py
import numpy as np
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--split-file", type=Path, required=True)
    ap.add_argument("--max-rows", type=int, default=300)
    ap.add_argument("--bins", type=int, default=256)
    args = ap.parse_args()

    split = json.loads(Path(args.split_file).read_text())
    part_of = {int(t): p for p, tax in split.items() for t in tax}
    rows = []
    paths = sorted(args.data.glob("*.h5"))
    for pi, p in enumerate(paths):
        with h5py.File(p, "r") as f:
            meta = json.loads(f.attrs["metadata"])
            part = part_of.get(int(meta["ncbi_taxid"]))
            if part is None:
                continue
            ids = f["input_ids"]
            n = min(args.max_rows, ids.shape[0])
            X = np.asarray(ids[:n, 1:-1], dtype=np.int32)
            # hashed token histogram per chunk then species mean/std
            h = np.zeros((n, args.bins), np.float32)
            np.add.at(h, (np.repeat(np.arange(n), X.shape[1]), (X.ravel() % args.bins)), 1)
            h /= X.shape[1]
            rows.append(dict(part=part, taxid=int(meta["ncbi_taxid"]),
                             sci=meta["scientific_name"], fam=meta["family"],
                             y=float(meta["max_longevity_yrs"]),
                             mu=h.mean(0), sd=h.std(0)))
            if pi % 200 == 0:
                print(f"{pi}/{len(paths)}", flush=True)
    df = pd.DataFrame(rows)
    feats = np.hstack([np.stack(df.mu), np.stack(df.sd)])
    y = np.log10(df.y.to_numpy())
    print(df.part.value_counts())

    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import RidgeCV

    tr, va, te = (df.part.to_numpy() == s for s in ("train", "val", "test"))
    for name, mdl in [
        ("ridge", RidgeCV(alphas=np.logspace(-2, 4, 25))),
        ("hgb", HistGradientBoostingRegressor(max_iter=600, learning_rate=0.06,
                                            max_depth=6, l2_regularization=1.0,
                                            early_stopping=False, random_state=0)),
    ]:
        mdl.fit(feats[tr], y[tr])
        for split_name, m in (("val", va), ("test", te)):
            if m.sum() == 0:
                continue
            pred = mdl.predict(feats[m])
            mae = float(np.abs(pred - y[m]).mean())
            sp = float(pd.Series(pred).rank().corr(pd.Series(y[m]).rank()))
            print(f"{name} {split_name}: MAE={mae:.4f} spearman={sp:.3f} (n={m.sum()})")


if __name__ == "__main__":
    main()
