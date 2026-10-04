"""Standalone evaluation: load a saved model checkpoint, eval on chunk data (or CDS pairs),
report species-level metrics with mean/median/trimmed aggregation.

Usage: python -m longevity.xeval --ckpt runs/e4/model_best.pt --data chunks_dir \
         --split-file experiments/split_v3.json --split test --max-rows 1000
"""

from __future__ import annotations

import argparse
import json
import math
import time
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from longevity.train import collate, evaluate, log, make_batches
from longevity.xtrain import LongevityRegressorX


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", type=Path, required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--split-file", type=Path, required=True)
    ap.add_argument("--split", choices=["train", "val", "test"], default="test")
    ap.add_argument("--max-rows", type=int, default=0, help="cap rows per species (0=all)")
    ap.add_argument("--eval-tokens-per-batch", type=int, default=131072)
    ap.add_argument("--agg", default="mean", help="mean|median|trim10|mean,median,trim10")
    ap.add_argument("--attn", default="kernels-community/flash-attn2")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    device = torch.device("cuda")
    torch.backends.cuda.matmul.allow_tf32 = True
    state = torch.load(args.ckpt, map_location="cpu", weights_only=False)
    cfg = state.get("config", {})
    model = LongevityRegressorX(cfg.get("attn", args.attn), cfg.get("head_hidden", 256),
                                cfg.get("head_dropout", 0.1), cfg.get("pool", "cls"),
                                aux="aux_head.weight" in state["model"])
    model.load_state_dict(state["model"], strict=False)
    model.to(device).eval()
    mu, sd = cfg["mu"], cfg["sd"]
    log(f"ckpt {args.ckpt} pool={cfg.get('pool')} mu={mu:.3f} sd={sd:.3f}")

    with ExitStack() as stack:
        is_chunks = args.data.is_dir() or args.data.suffix in {".h5", ".hdf5"}
        if is_chunks:
            from longevity.chunks import load_chunks
            df, chunk_rows = load_chunks(args.data, stack)
        else:
            df = pd.read_parquet(args.data)
            df["_row"] = np.arange(len(df))
        split_map = json.loads(Path(args.split_file).read_text())
        part_of = {int(t): p for p, tax in split_map.items() for t in tax}
        df["_part"] = df.ncbi_taxid.map(part_of)
        df = df[df._part == args.split].reset_index(drop=True)
        log(f"{len(df)} rows, {df.ncbi_taxid.nunique()} species ({args.split})")
        if args.max_rows:
            sel = (df.groupby("ncbi_taxid", group_keys=False)
                     .apply(lambda g: g.head(args.max_rows)).index.to_numpy())
            sub = df.loc[sorted(sel)].reset_index(drop=True)
        else:
            sub = df
        if is_chunks:
            ids = chunk_rows.subset(sub._row.to_numpy())
        else:
            ids = [np.asarray(x, dtype=np.int32) for x in sub.input_ids]
        has_aux = model.aux_head is not None
        preds = np.zeros(len(sub), dtype=np.float32)
        auxs = np.zeros(len(sub), dtype=np.float32) if has_aux else None
        rng = __import__("random").Random(0)
        t0 = time.time()
        for b in make_batches(sub.n_tokens.to_numpy(), args.eval_tokens_per_batch, 1024, False, rng):
            inp, mask = collate([ids[i] for i in b], 1024, False, rng, encoded=is_chunks)
            with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
                out = model(inp.to(device), mask.to(device), want_aux=has_aux)
            if has_aux:
                out, aout = out
                auxs[b] = aout.detach().float().cpu().numpy()
            preds[b] = out.detach().float().cpu().numpy()
        sub["pred_log10"] = preds * sd + mu
        if has_aux:
            sub["pred_log10_aux"] = auxs * cfg.get("aux_sd", 1.0) + cfg.get("aux_mu", 0.0)
        log(f"forward done in {time.time()-t0:.0f}s")
        if args.out:
            sub.to_parquet(args.out)
        res = {}
        for agg in args.agg.split(","):
            if agg == "mean":
                f = "mean"
            elif agg == "median":
                f = "median"
            elif agg == "trim10":
                f = lambda x: x.sort_values().iloc[len(x) // 10 : -len(x) // 10 or None].mean()
            sp = sub.groupby("ncbi_taxid").agg(t=("log10_longevity", "first"),
                                             p=("pred_log10", f)).reset_index()
            mae = float((sp.p - sp.t).abs().mean())
            rho = float(sp.t.rank().corr(sp.p.rank()))
            pr = float(sp.t.corr(sp.p))
            res[agg] = {"mae": round(mae, 4), "spearman": round(rho, 4), "pearson": round(pr, 4)}
            print(f"{agg:8} {args.split}: MAE={mae:.4f} rho={rho:.4f} pearson={pr:.4f} "
                  f"(n={len(sp)} species)")
        base = (mu - sub.groupby("ncbi_taxid").log10_longevity.first()).abs().mean()
        print(f"baseline (train-mean) MAE={base:.4f}")


if __name__ == "__main__":
    main()
