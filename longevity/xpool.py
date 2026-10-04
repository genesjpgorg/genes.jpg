"""Stage-2 species head on per-chunk embeddings: attention pooling + MLP -> longevity.

Trains on train-species embeddings, reports val/test species MAE + Spearman.
Variants: --pool attn|mean|meanstd|attnmean
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch import nn


class AttnPool(nn.Module):
    def __init__(self, d, n_heads=4):
        super().__init__()
        self.q = nn.Parameter(torch.randn(n_heads, d) * 0.02)
        self.n_heads = n_heads

    def forward(self, h):  # h: (B, K, d) -> (B, n_heads*d)
        w = torch.softmax(torch.einsum("bkd,qd->bkq", h, self.q) / (h.shape[-1] ** 0.5), dim=1)
        pooled = torch.einsum("bkq,bkd->bqd", w, h)  # (B, n_heads, d)
        return pooled.reshape(h.shape[0], -1)


class SpeciesHead(nn.Module):
    def __init__(self, d, pool="attn", hidden=256, dropout=0.2, n_heads=4):
        super().__init__()
        self.pool = pool
        self.attn = AttnPool(d, n_heads) if "attn" in pool else None
        din = d * (2 if pool == "meanstd" else n_heads if pool == "attn" else n_heads + 1
                   if pool == "attnmean" else 1)
        self.mlp = nn.Sequential(nn.Linear(din, hidden), nn.GELU(), nn.Dropout(dropout),
                                 nn.Linear(hidden, 1))

    def forward(self, h, mask, extra=None):
        feats = []
        if self.attn is not None:
            w = torch.softmax(torch.einsum("bkd,qd->bkq", h, self.attn.q)
                              / (h.shape[-1] ** 0.5), dim=1)
            w = w * mask.unsqueeze(-1)
            w = w / w.sum(1, keepdim=True).clamp(min=1)
            pooled = torch.einsum("bkq,bkd->bqd", w, h).reshape(h.shape[0], -1)
            feats.append(pooled)
        if self.pool in ("mean", "meanstd", "attnmean"):
            m = mask.unsqueeze(-1)
            mu = (h * m).sum(1) / m.sum(1).clamp(min=1)
            feats.append(mu)
            if self.pool == "meanstd":
                sd = (((h - mu.unsqueeze(1)) * m) ** 2).sum(1) / m.sum(1).clamp(min=1)
                feats.append(sd.sqrt())
        x = torch.cat(feats, -1) if len(feats) > 1 else feats[0]
        if extra is not None:
            x = torch.cat([x, extra], -1)
        return self.mlp(x).squeeze(-1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emb-dir", type=Path, required=True)
    ap.add_argument("--pool", default="attn", choices=["attn", "mean", "meanstd", "attnmean"])
    ap.add_argument("--heads", type=int, default=4)
    ap.add_argument("--hidden", type=int, default=256)
    ap.add_argument("--dropout", type=float, default=0.2)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--wd", type=float, default=0.01)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch-species", type=int, default=64)
    ap.add_argument("--chunks-per-step", type=int, default=64,
                    help="subsample chunks per species per step (0=all)")
    ap.add_argument("--seed", type=str, default="0")
    ap.add_argument("--extra", type=Path, default=None,
                    help="csv/parquet with ncbi_taxid + scalar feature cols joined per species")
    args = ap.parse_args()

    seeds = [int(s) for s in args.seed.split(",")]
    torch.manual_seed(seeds[0])
    rng = np.random.default_rng(seeds[0])
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    data = {}
    for split in ("train", "val", "test"):
        e = args.emb_dir / f"{split}_emb.npy"
        m = args.emb_dir / f"{split}_meta.csv"
        if e.exists():
            meta = pd.read_csv(m)
            data[split] = (np.asarray(np.load(e), dtype=np.float32),
                           meta.log10_longevity.to_numpy(), meta)
            print(split, data[split][0].shape)

    extra_names = []
    if args.extra:
        ext = (pd.read_parquet(args.extra) if str(args.extra).endswith(".parquet")
               else pd.read_csv(args.extra))
        extra_names = [c for c in ext.columns if c not in ("ncbi_taxid",) and
                       ext[c].dtype.kind == "f"]
        for split in list(data):
            X, y, meta = data[split]
            j = meta.merge(ext, on="ncbi_taxid", how="left")
            ex = np.ascontiguousarray(j[extra_names].to_numpy(np.float32)).copy()
            med = np.nanmedian(ex, axis=0)
            inds = np.where(~np.isfinite(ex))
            ex[inds] = np.take(med, inds[1])
            data[split] = (X, y, meta, ex)
        print("extra features:", extra_names)

    Xtr, ytr, mtr = data["train"][:3]
    mu, sd = ytr.mean(), ytr.std()
    yz = {k: (v[1] - mu) / sd for k, v in data.items()}
    d_emb = Xtr.shape[2]
    all_val, all_test = [], []
    for sd_i in seeds:
        torch.manual_seed(sd_i)
        rng = np.random.default_rng(sd_i)
        model = SpeciesHead(d_emb, args.pool, args.hidden, args.dropout, args.heads).to(device)
        if extra_names:
            model.mlp[0] = nn.Linear(model.mlp[0].in_features + len(extra_names),
                                     model.mlp[0].out_features).to(device)
        opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.wd)
        n = len(Xtr)
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
        tr = torch.tensor(Xtr, device=device)
        tr_x = data["train"][3] if extra_names else None
        best = (1e9, None)

        def evaluate(split):
            X, _, meta = data[split][:3]
            ex = data[split][3] if extra_names else None
            preds = []
            with torch.no_grad():
                for i in range(0, len(X), 256):
                    h = torch.tensor(X[i : i + 256], device=device)
                    mask = (h.abs().sum(-1) > 0).float()
                    exb = (torch.tensor(ex[i : i + 256], device=device)
                           if ex is not None else None)
                    preds.append(model(h, mask, exb).cpu().numpy())
            p = np.concatenate(preds) * sd + mu
            t = meta.log10_longevity.to_numpy()
            return (float(np.abs(p - t).mean()),
                    float(pd.Series(p).rank().corr(pd.Series(t).rank())), p)

        for ep in range(args.epochs):
            model.train()
            perm = rng.permutation(n)
            tot = 0.0
            for i in range(0, n, args.batch_species):
                idx = perm[i : i + args.batch_species]
                h = tr[torch.tensor(idx, device=device)]
                if args.chunks_per_step and h.shape[1] > args.chunks_per_step:
                    sel = torch.randperm(h.shape[1], device=device)[: args.chunks_per_step]
                    h = h[:, sel]
                mask = (h.abs().sum(-1) > 0).float()
                y = torch.tensor(yz["train"][idx], dtype=torch.float32, device=device)
                exb = (torch.tensor(tr_x[idx], device=device)
                       if tr_x is not None else None)
                loss = nn.functional.mse_loss(model(h, mask, exb), y)
                opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                tot += loss.item() * len(idx)
            sched.step()
            if ep % 5 == 0 or ep == args.epochs - 1:
                model.eval()
                vm, vr, _ = evaluate("val")
                print(f"ep {ep:3d} train_loss {tot / n:.4f} val_mae {vm:.4f} rho {vr:.3f}",
                      flush=True)
                if vm < best[0]:
                    best = (vm, {k: v.clone() for k, v in model.state_dict().items()})
        if best[1] is not None:
            model.load_state_dict(best[1])
        model.eval()
        for split in ("train", "val", "test"):
            if split in data:
                mae, rho, p = evaluate(split)
                print(f"seed{sd_i} FINAL {split}: MAE={mae:.4f} rho={rho:.4f}")
                if split == "val":
                    all_val.append(p)
                if split == "test":
                    all_test.append(p)
    for nm, arr in (("val", all_val), ("test", all_test)):
        if arr:
            p = np.mean(arr, axis=0)
            t = data[nm][2].log10_longevity.to_numpy()
            print(f"ENSEMBLE {nm}: MAE={float(np.abs(p - t).mean()):.4f} "
                  f"rho={float(pd.Series(p).rank().corr(pd.Series(t).rank())):.4f}")
            meta = data[nm][2].copy()
            meta["pred_log10"] = p
            meta.to_parquet(args.emb_dir / f"xpool_{args.pool}_{nm}_preds.parquet")


if __name__ == "__main__":
    main()
