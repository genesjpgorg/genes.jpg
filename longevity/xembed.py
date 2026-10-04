"""Extract per-chunk CLS embeddings for species from a trained checkpoint.

Writes <out>/<split>_emb.npy (n_species, K, hidden) + <split>_meta.csv.
"""

from __future__ import annotations

import argparse
import json
import time
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from longevity.train import collate, log, make_batches
from longevity.xtrain import LongevityRegressorX


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", type=Path, required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--split-file", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--per-species", type=int, default=200)
    ap.add_argument("--eval-tokens-per-batch", type=int, default=131072)
    ap.add_argument("--attn", default="kernels-community/flash-attn2")
    ap.add_argument("--splits", default="train,val,test")
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
    hidden = model.backbone.config.hidden_size
    log(f"ckpt pool={cfg.get('pool')} hidden={hidden} mu={cfg['mu']:.3f} sd={cfg['sd']:.3f}")
    args.out.mkdir(parents=True, exist_ok=True)

    with ExitStack() as stack:
        is_chunks = args.data.is_dir() or args.data.suffix in {".h5", ".hdf5"}
        if is_chunks:
            from longevity.chunks import load_chunks
            df, chunk_rows = load_chunks(args.data, stack)
        else:
            raise SystemExit("embed extraction implemented for chunk shards only")
        split_map = json.loads(Path(args.split_file).read_text())
        part_of = {int(t): p for p, tax in split_map.items() for t in tax}
        df["_part"] = df.ncbi_taxid.map(part_of)

        rng = np.random.default_rng(0)
        for split in args.splits.split(","):
            sub_all = df[df._part == split]
            sp_ids = sorted(sub_all.ncbi_taxid.unique())
            K = args.per_species
            emb = np.zeros((len(sp_ids), K, hidden), np.float16)
            true_l = np.zeros(len(sp_ids))
            names, counts = [], []
            t0 = time.time()
            for si, tax in enumerate(sp_ids):
                g = sub_all[sub_all.ncbi_taxid == tax]
                take = min(K, len(g))
                pos = rng.choice(len(g), take, replace=False)
                rows = g.iloc[np.sort(pos)]
                ids = chunk_rows.subset(rows._row.to_numpy())
                outs = []
                lens = rows.n_tokens.to_numpy()
                for b in make_batches(lens, args.eval_tokens_per_batch, 1024, False,
                                      __import__("random").Random(0)):
                    inp, mask = collate([ids[i] for i in b], 1024, False,
                                        __import__("random").Random(0), encoded=True)
                    with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
                        h = model._pooled(inp.to(device), mask.to(device))
                    outs.append(h.float().cpu().numpy())
                h = np.concatenate(outs)[:K]
                emb[si, : len(h)] = h
                true_l[si] = rows.log10_longevity.iloc[0]
                names.append(rows.scientific_name.iloc[0])
                counts.append(len(h))
                if si % 200 == 0:
                    log(f"{split} [{si}/{len(sp_ids)}] {time.time()-t0:.0f}s")
            np.save(args.out / f"{split}_emb.npy", emb)
            pd.DataFrame({"ncbi_taxid": sp_ids, "scientific_name": names,
                          "log10_longevity": true_l, "n_emb": counts}).to_csv(
                args.out / f"{split}_meta.csv", index=False)
            log(f"{split}: wrote {emb.shape} in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
