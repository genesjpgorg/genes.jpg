"""Experiment trainer: same model/protocol as longevity.train plus

- --split-file: frozen train/val/test taxid lists; unlisted species are dropped
- --chunks-per-epoch: resample N rows per training species each epoch (chunk or CDS mode)
- --eval-chunks: cap rows per species in evals during training; final eval uses all rows
- best-checkpoint tracking by a val metric; test is evaluated on the best checkpoint
- --loss mse|huber
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch import nn

from longevity.train import (CLS, SEP, PAD, MODEL, collate,
                             evaluate, log, make_batches)


class LongevityRegressorX(nn.Module):
    """ModernGENA encoder + MLP head; pool = 'cls' ([CLS] state) or 'mean' (masked mean).

    Optional aux_head shares the pooled state for a second regression task
    (e.g. log10 adult body mass); only used during training.
    """

    def __init__(self, attn: str, hidden: int = 256, dropout: float = 0.1, pool: str = "cls",
                 aux: bool = False):
        super().__init__()
        from transformers import AutoModel

        kw = {"attention_dropout": 0.0} if "flash" in attn else {}
        self.backbone = AutoModel.from_pretrained(MODEL, trust_remote_code=True,
                                                  attn_implementation=attn, **kw)
        d = self.backbone.config.hidden_size
        self.head = nn.Sequential(nn.Linear(d, hidden), nn.GELU(), nn.Dropout(dropout),
                                  nn.Linear(hidden, 1))
        self.aux_head = nn.Linear(d, 1) if aux else None
        self.pool = pool

    def _pooled(self, input_ids, attention_mask):
        h = self.backbone(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        if self.pool == "mean":
            m = attention_mask.unsqueeze(-1).to(h.dtype)
            h = (h * m).sum(1) / m.sum(1).clamp(min=1)
        else:
            h = h[:, 0]
        return h

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor,
                want_aux: bool = False):
        h = self._pooled(input_ids, attention_mask)
        out = self.head(h).squeeze(-1)
        if want_aux:
            return out, self.aux_head(h).squeeze(-1)
        return out


def subsample_per_species(df: pd.DataFrame, n: int, rng: np.random.Generator) -> np.ndarray:
    """Sorted positions: up to n rows per species."""
    order = rng.permutation(len(df))
    tax = df.ncbi_taxid.to_numpy()[order]
    _, start = np.unique(tax, return_index=True)
    sel = np.concatenate([order[s : s + n] for s in start])
    return np.sort(sel)


def make_species_batches(df: pd.DataFrame, ep_idx: np.ndarray, unit_size: int,
                         rows_per_batch: int, rng: random.Random) -> list[list[list[int]]]:
    """Batches of whole species units: batch = [unit_rows...] each ~unit_size rows of one species.

    The mean of a unit's predictions is the species estimate the metric uses.
    """
    tax = df.ncbi_taxid.to_numpy()[ep_idx]
    order = np.argsort(tax, kind="stable")
    sorted_tax = tax[order]
    units, start = [], 0
    while start < len(order):
        sp = sorted_tax[start]
        end = start
        while end < len(order) and sorted_tax[end] == sp:
            end += 1
        rows = list(order[start:end])  # positions within ep_idx
        rng.shuffle(rows)
        for i in range(0, len(rows), unit_size):
            units.append(rows[i : i + unit_size])
        start = end
    rng.shuffle(units)
    per_batch = max(1, rows_per_batch // unit_size)
    return [units[i : i + per_batch] for i in range(0, len(units), per_batch)]


def main(argv=None) -> None:
    with ExitStack() as stack:
        _main(argv, stack)


def _main(argv, stack) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, required=True, help="pairs.parquet, or H5 shard/directory")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--split-file", type=Path, required=True,
                    help='JSON {"train": [taxid..], "val": [..], "test": [..]}; others dropped')
    ap.add_argument("--min-genes", type=int, default=0)
    ap.add_argument("--epochs", type=float, default=18)
    ap.add_argument("--max-steps", type=int, default=0)
    ap.add_argument("--time-budget-min", type=float, default=0)
    ap.add_argument("--max-len", type=int, default=None)
    ap.add_argument("--tokens-per-batch", type=int, default=32768)
    ap.add_argument("--eval-tokens-per-batch", type=int, default=131072)
    ap.add_argument("--lr-encoder", type=float, default=3e-5)
    ap.add_argument("--lr-head", type=float, default=1e-3)
    ap.add_argument("--weight-decay", type=float, default=0.01)
    ap.add_argument("--warmup", type=float, default=0.05)
    ap.add_argument("--min-lr-ratio", type=float, default=0.1)
    ap.add_argument("--head-hidden", type=int, default=256)
    ap.add_argument("--head-dropout", type=float, default=0.1)
    ap.add_argument("--grad-clip", type=float, default=1.0)
    ap.add_argument("--attn", default="kernels-community/flash-attn2")
    ap.add_argument("--loss", choices=["mse", "huber"], default="mse")
    ap.add_argument("--huber-delta", type=float, default=1.0)
    ap.add_argument("--compile", action="store_true")
    ap.add_argument("--evals-per-epoch", type=int, default=1)
    ap.add_argument("--log-every", type=int, default=50)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--chunks-per-epoch", type=int, default=0,
                    help="resample this many rows per train species each epoch; 0 = all")
    ap.add_argument("--eval-chunks", type=int, default=0,
                    help="cap rows per species for during-training evals; 0 = all")
    ap.add_argument("--best-metric", default="species_mae_log10",
                    help="val metric for best-checkpoint selection (minimise)")
    ap.add_argument("--pool", choices=["cls", "mean"], default="cls")
    ap.add_argument("--species-mean-weight", type=float, default=0.0,
                    help="if >0, batch by species units and add MSE on per-species mean prediction")
    ap.add_argument("--unit-size", type=int, default=8, help="chunks per species unit in a batch")
    ap.add_argument("--chunk-mse-weight", type=float, default=0.25,
                    help="per-chunk MSE weight when species-mean loss is active")
    ap.add_argument("--cpe-class", type=str, default="",
                    help='JSON {"Aves":96}: per-class chunks/species/epoch (default: --chunks-per-epoch)')
    ap.add_argument("--aux-table", type=Path, default=None,
                    help="parquet with ncbi_taxid + aux column; auxiliary regression task")
    ap.add_argument("--aux-col", default="adult_weight_g")
    ap.add_argument("--aux-weight", type=float, default=0.3)
    ap.add_argument("--freeze-encoder", action="store_true")
    ap.add_argument("--init", type=Path, help="initialise weights from a previous run's model.pt")
    args = ap.parse_args(argv)

    is_chunks = args.data.is_dir() or args.data.suffix in {".h5", ".hdf5"}
    if is_chunks and args.max_len not in (None, 1024):
        ap.error("H5 mode requires --max-len 1024")
    args.max_len = args.max_len or 1024
    args.out.mkdir(parents=True, exist_ok=True)
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    rng = random.Random(args.seed)
    prng = np.random.default_rng(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    torch.backends.cuda.matmul.allow_tf32 = True

    # ---- data and split
    if is_chunks:
        from longevity.chunks import load_chunks

        df, chunk_rows = load_chunks(args.data, stack)
        df["_pos"] = np.arange(len(df))
    else:
        df = pd.read_parquet(args.data)
        df["_row"] = np.arange(len(df))
        df["_pos"] = np.arange(len(df))
    split_map = json.loads(Path(args.split_file).read_text())
    part_of = {}
    for part in ("train", "val", "test"):
        for t in split_map.get(part, []):
            if int(t) in part_of:
                ap.error(f"taxid {t} in two splits")
            part_of[int(t)] = part
    df["_part"] = df.ncbi_taxid.map(part_of)
    dropped = int(df._part.isna().sum())
    df = df[df._part.notna()].reset_index(drop=True)
    if args.min_genes:
        n = df.groupby("ncbi_taxid").size()
        keep = set(n[n >= args.min_genes].index)
        df = df[df.ncbi_taxid.isin(keep)].reset_index(drop=True)
    parts = {k: df[df._part == k].reset_index(drop=True) for k in ("train", "val", "test")}
    if is_chunks:
        ids = {k: chunk_rows.subset(v._row.to_numpy()) for k, v in parts.items()}
    else:
        ids = {k: [np.asarray(x, dtype=np.int32) for x in v.input_ids] for k, v in parts.items()}
    tr = parts["train"]
    if tr.empty:
        ap.error("no training examples remain after filtering/splitting")
    sp_y = tr.groupby("ncbi_taxid").log10_longevity.first()
    mu = float(sp_y.mean())
    sd = float(sp_y.std()) if len(sp_y) > 1 and sp_y.std() > 0 else 1.0

    aux_mu = aux_sd = None
    if args.aux_table:
        aux = pd.read_parquet(args.aux_table)[["ncbi_taxid", "aux_val"]].dropna()
        df = df.merge(aux, on="ncbi_taxid", how="left")
        parts = {k: df[df._part == k].reset_index(drop=True) for k in ("train", "val", "test")}
        tr = parts["train"]
        sp_a = tr.groupby("ncbi_taxid").aux_val.first().dropna()
        aux_mu, aux_sd = float(sp_a.mean()), float(sp_a.std() or 1.0)
        n_cov = int(tr.aux_val.notna().sum())
        log(f"aux task '{args.aux_col}': {n_cov} train rows covered, mu={aux_mu:.3f} sd={aux_sd:.3f}")
    for k, v in parts.items():
        log(f"{k}: {v.ncbi_taxid.nunique()} species, {len(v)} rows")
    log(f"dropped {dropped} rows (unlisted species); target log10 longevity: mu={mu:.3f} sd={sd:.3f}")

    # eval-time row caps (fixed subset for comparability across evals)
    eval_pos = {}
    for k in ("val", "test"):
        n_sp_rows = args.eval_chunks
        if n_sp_rows and len(parts[k]):
            eval_pos[k] = subsample_per_species(parts[k], n_sp_rows, np.random.default_rng(123))
        else:
            eval_pos[k] = np.arange(len(parts[k]))

    # ---- model and optimiser
    model = LongevityRegressorX(args.attn, args.head_hidden, args.head_dropout, args.pool,
                                aux=bool(args.aux_table)).to(device)
    if args.init:
        state = torch.load(args.init, map_location="cpu", weights_only=False)
        sdict = state["model"] if "model" in state else state
        missing, unexpected = model.load_state_dict(sdict, strict=False)
        log(f"initialised weights from {args.init} (missing={missing}, unexpected={unexpected})")
    if args.freeze_encoder:
        for p in model.backbone.parameters():
            p.requires_grad = False
    n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    decay, no_decay = [], []
    for n, p in model.backbone.named_parameters():
        if not p.requires_grad:
            continue
        (decay if p.ndim >= 2 and "embeddings" not in n else no_decay).append(p)
    groups = [
        {"params": decay, "lr": args.lr_encoder, "weight_decay": args.weight_decay},
        {"params": no_decay, "lr": args.lr_encoder, "weight_decay": 0.0},
        {"params": list(model.head.parameters())
                  + (list(model.aux_head.parameters()) if model.aux_head is not None else []),
         "lr": args.lr_head, "weight_decay": args.weight_decay},
    ]
    groups = [g for g in groups if g["params"]]
    opt = torch.optim.AdamW(groups, betas=(0.9, 0.98), eps=1e-6, fused=device.type == "cuda")
    base_lrs = [g["lr"] for g in groups]
    fwd = torch.compile(model) if args.compile else model

    tr_lengths = tr.n_tokens.to_numpy()
    tr_arr = ids["train"]

    cpe_class = json.loads(args.cpe_class) if args.cpe_class else {}

    def _ssp_positions(cand: np.ndarray, n: int, g: np.random.Generator) -> np.ndarray:
        """Positions (within tr): up to n rows per species, chosen from cand."""
        if n <= 0:
            return cand
        order = g.permutation(len(cand))
        tax = tr.ncbi_taxid.to_numpy()[cand][order]
        _, start = np.unique(tax, return_index=True)
        return np.concatenate([cand[order[s : s + n]] for s in start])

    def epoch_index(ep: int) -> np.ndarray:
        if not (args.chunks_per_epoch or cpe_class):
            return np.arange(len(tr))
        rng_ep = np.random.default_rng(args.seed * 100003 + ep)
        if cpe_class:
            cls_arr = tr["class"].to_numpy() if "class" in tr else np.full(len(tr), "")
            idx = np.arange(len(tr))
            parts = [_ssp_positions(idx[cls_arr == cl], n, rng_ep)
                     for cl, n in cpe_class.items()]
            parts.append(_ssp_positions(
                idx[~np.isin(cls_arr, list(cpe_class))], args.chunks_per_epoch, rng_ep))
            return np.sort(np.concatenate(parts))
        return subsample_per_species(tr, args.chunks_per_epoch, rng_ep)

    species_mode = args.species_mean_weight > 0
    if species_mode and not is_chunks:
        ap.error("--species-mean-weight is supported for fixed-length genome chunks only")

    def epoch_batches(ep: int):
        ep_idx = epoch_index(ep)
        if species_mode:
            rows_per_batch = args.tokens_per_batch // args.max_len
            for ub in make_species_batches(tr, ep_idx, args.unit_size, rows_per_batch, rng):
                yield [i for u in ub for i in u], [len(u) for u in ub], ep_idx
        else:
            for b in make_batches(tr_lengths[ep_idx], args.tokens_per_batch,
                                  args.max_len, True, rng):
                yield b, None, ep_idx

    steps_per_epoch = len(list(epoch_batches(0)))
    total_steps = max(1, math.ceil(steps_per_epoch * args.epochs))
    if args.max_steps:
        total_steps = min(total_steps, args.max_steps)
    warmup = max(1, int(args.warmup * total_steps))
    eval_every = max(1, int(steps_per_epoch / args.evals_per_epoch))

    def lr_scale(step: int) -> float:
        if step < warmup:
            return (step + 1) / warmup
        p = (step - warmup) / max(1, total_steps - warmup)
        return args.min_lr_ratio + (1 - args.min_lr_ratio) * 0.5 * (1 + math.cos(math.pi * min(p, 1)))

    import transformers

    config = dict({k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
                  data=str(args.data), out=str(args.out), model=MODEL,
                  runtime={"python": sys.version, "torch": torch.__version__,
                           "transformers": transformers.__version__, "cuda": torch.version.cuda,
                           "device": str(device)},
                  input_kind="genome_chunks" if is_chunks else "gene_cds",
                  n_params=n_params, mu=mu, sd=sd, aux_mu=aux_mu, aux_sd=aux_sd,
                  steps_per_epoch=steps_per_epoch,
                  total_steps=total_steps, warmup_steps=warmup, dropped_rows=dropped,
                  split={k: sorted(v.ncbi_taxid.unique().tolist()) for k, v in parts.items()})
    (args.out / "config.json").write_text(json.dumps(config, indent=2))
    log(f"model {n_params / 1e6:.1f}M params (trainable), attn={args.attn}, "
        f"{steps_per_epoch} steps/epoch, {total_steps} total, warmup {warmup}")

    metrics_f = stack.enter_context((args.out / "metrics.jsonl").open("a"))

    def write(rec: dict) -> None:
        metrics_f.write(json.dumps(rec) + "\n")
        metrics_f.flush()

    best = {"metric": float("inf"), "step": -1}

    def run_eval(step: int, epoch: float, split: str, full: bool = False) -> dict | None:
        if parts[split].empty:
            return None
        t0 = time.time()
        pos = np.arange(len(parts[split])) if full else eval_pos[split]
        sub = parts[split].iloc[pos]
        sub_ids = ids[split].subset(pos) if is_chunks else [ids[split][i] for i in pos]
        m, preds = evaluate(model, sub.reset_index(drop=True), sub_ids, mu, sd, args, device)
        preds.to_parquet(args.out / f"predictions_{split}.parquet")
        species = m.pop("species")
        write(dict(kind=split, step=step, epoch=round(epoch, 3), eval_rows=int(len(pos)),
                   seconds=round(time.time() - t0, 1), **m, species=species))
        log(f"{split} step {step}: " + " ".join(
            f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}" for k, v in m.items())
            + f" ({time.time() - t0:.0f}s)")
        return m

    def maybe_save_best(m: dict, step: int, epoch: float) -> None:
        v = m[args.best_metric]
        if v < best["metric"]:
            best.update(metric=v, step=step, epoch=round(epoch, 3))
            torch.save({"model": model.state_dict(), "config": config,
                        "best": best}, args.out / "model_best.pt")
            log(f"  new best {args.best_metric}={v:.4f} -> model_best.pt")

    # ---- train
    model.train()
    step, t_start, done = 0, time.time(), False
    win_loss, win_n, win_tok, win_t = 0.0, 0, 0, time.time()
    epoch = 0
    while not done:
        for b, unit_lens, ep_idx in epoch_batches(epoch):
            for g, base in zip(opt.param_groups, base_lrs):
                g["lr"] = base * lr_scale(step)
            rows = [tr_arr[ep_idx[i]] for i in b]
            inp, mask = collate(rows, args.max_len, True, rng, encoded=is_chunks)
            inp, mask = inp.to(device, non_blocking=True), mask.to(device, non_blocking=True)
            y = torch.tensor(
                ((tr.log10_longevity.to_numpy()[ep_idx[b]] - mu) / sd),
                dtype=torch.float32).to(device, non_blocking=True)
            with torch.autocast("cuda", dtype=torch.bfloat16, enabled=device.type == "cuda"):
                if aux_mu is not None:
                    pred, aux_pred = fwd(inp, mask, want_aux=True)
                else:
                    pred = fwd(inp, mask)
            if aux_mu is not None:
                av = torch.tensor(tr.aux_val.to_numpy()[ep_idx[b]], dtype=torch.float32)
                av = av.to(device, non_blocking=True)
                amask = torch.isfinite(av)
                az = (av - aux_mu) / aux_sd
                aux_loss = (nn.functional.mse_loss(aux_pred.float()[amask], az[amask])
                            if amask.any() else pred.sum() * 0)
            if unit_lens is not None:
                pred_f = pred.float()
                means, ys = [], []
                s = 0
                for L in unit_lens:
                    means.append(pred_f[s : s + L].mean())
                    ys.append(y[s])
                    s += L
                loss = (args.chunk_mse_weight * nn.functional.mse_loss(pred_f, y)
                        + args.species_mean_weight * nn.functional.mse_loss(
                            torch.stack(means), torch.stack(ys)))
            elif args.loss == "huber":
                loss = nn.functional.huber_loss(pred.float(), y, delta=args.huber_delta)
            else:
                loss = nn.functional.mse_loss(pred.float(), y)
            if aux_mu is not None:
                loss = loss + args.aux_weight * aux_loss
            if not torch.isfinite(loss).item():
                raise FloatingPointError(f"Nonfinite loss at step {step + 1}; aborting run")
            opt.zero_grad(set_to_none=True)
            loss.backward()
            gnorm = torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
            opt.step()
            step += 1
            win_loss += loss.item() * len(b)
            win_n += len(b)
            win_tok += int(mask.sum())
            if step % args.log_every == 0 or step == total_steps:
                dt = time.time() - win_t
                rec = {"kind": "train", "step": step, "epoch": round(step / steps_per_epoch, 3),
                       "loss": win_loss / win_n, "grad_norm": float(gnorm),
                       "lr_encoder": opt.param_groups[0]["lr"], "seq_per_s": win_n / dt,
                       "tok_per_s": win_tok / dt, "elapsed_min": (time.time() - t_start) / 60,
                       "peak_gb": torch.cuda.max_memory_allocated() / 2**30 if device.type == "cuda" else 0}
                write(rec)
                log(f"step {step}/{total_steps} ep {rec['epoch']:.2f} loss {rec['loss']:.4f} "
                    f"gn {rec['grad_norm']:.2f} lr {rec['lr_encoder']:.2e} {rec['seq_per_s']:.0f} seq/s "
                    f"{rec['tok_per_s'] / 1e3:.0f}k tok/s {rec['peak_gb']:.1f} GB "
                    f"{rec['elapsed_min']:.1f} min")
                win_loss, win_n, win_tok, win_t = 0.0, 0, 0, time.time()
            over_time = args.time_budget_min and time.time() - t_start > args.time_budget_min * 60
            if step >= total_steps or over_time:
                if over_time:
                    log(f"time budget of {args.time_budget_min} min reached at step {step}")
                done = True
                break
            if step % eval_every == 0:
                m = run_eval(step, step / steps_per_epoch, "val")
                if m:
                    maybe_save_best(m, step, step / steps_per_epoch)
        epoch += 1

    train_min = (time.time() - t_start) / 60
    log(f"training finished: {step} steps in {train_min:.1f} min; "
        f"best val {args.best_metric}={best['metric']:.4f} at step {best['step']}")
    torch.save({"model": model.state_dict(), "config": config}, args.out / "model_final.pt")
    # evaluate the BEST checkpoint on full val and test rows
    if best["step"] >= 0:
        state = torch.load(args.out / "model_best.pt", map_location=device, weights_only=False)
        model.load_state_dict(state["model"])
        log(f"loaded best checkpoint (step {best['step']}) for final val/test eval")
    m = run_eval(step, step / steps_per_epoch, "val", full=True)
    if m:
        write({"kind": "best_summary", "split": "val", "best_step": best["step"], **m})
    m = run_eval(step, step / steps_per_epoch, "test", full=True)
    if m:
        write({"kind": "best_summary", "split": "test", "best_step": best["step"], **m})
    write({"kind": "done", "step": step, "train_minutes": train_min,
           "best_val_metric": best["metric"], "best_step": best["step"]})


if __name__ == "__main__":
    main()
