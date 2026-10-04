"""Fine-tune ModernGENA to predict species maximum longevity from single-gene CDS.

Each example is one (species, gene) CDS from `longevity.prepare`, fed as [CLS] CDS [SEP]
(random window of --max-len tokens when longer, first window at eval). The [CLS] state goes
through a 2-layer MLP to a z-scored log10(max longevity); all weights are trained. A species'
prediction is the mean over its genes. Train/val/test are split by species.

H5 variant: --data chunks/ reads premade 1024-position samples lazily, with no resampling.

Usage:
  python -m longevity.train --data .../sample5/pairs.parquet --out runs/sample5 \
      --val-species 55149 --epochs 3
  python -m longevity.train ... --max-steps 20      # pipeline smoke test
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

from genesjpg.encoders import MODERNGENA

MODEL = MODERNGENA
CLS, SEP, PAD = 1, 2, 3


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- model


class LongevityRegressor(nn.Module):
    """ModernGENA encoder + 2-layer MLP on the [CLS] hidden state -> one scalar."""

    def __init__(self, attn: str = "kernels-community/flash-attn2", hidden: int = 256,
                 dropout: float = 0.1):
        super().__init__()
        from transformers import AutoModel

        kw = {"attention_dropout": 0.0} if "flash" in attn else {}  # flash kernel rejects p>0
        self.backbone = AutoModel.from_pretrained(MODEL, trust_remote_code=True,
                                                  attn_implementation=attn, **kw)
        d = self.backbone.config.hidden_size
        self.head = nn.Sequential(nn.Linear(d, hidden), nn.GELU(), nn.Dropout(dropout),
                                  nn.Linear(hidden, 1))

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        h = self.backbone(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        return self.head(h[:, 0]).squeeze(-1)


# ---------------------------------------------------------------- data


def make_batches(lengths: np.ndarray, tokens_per_batch: int, max_len: int, shuffle: bool,
                 rng: random.Random) -> list[list[int]]:
    """Length-bucketed batches whose padded size (rows x longest) stays under tokens_per_batch."""
    idx = list(range(len(lengths)))
    if shuffle:
        rng.shuffle(idx)
    lens = np.minimum(lengths + 2, max_len)
    chunk = 8192  # sort within large chunks: little padding, still random across epochs
    batches = []
    for c in range(0, len(idx), chunk):
        part = sorted(idx[c : c + chunk], key=lambda i: lens[i])
        cur, longest = [], 0
        for i in part:
            longest_new = max(longest, lens[i])
            if cur and longest_new * (len(cur) + 1) > tokens_per_batch:
                batches.append(cur)
                cur, longest_new = [], lens[i]
            cur.append(i)
            longest = longest_new
        if cur:
            batches.append(cur)
    if shuffle:
        rng.shuffle(batches)
    return batches


def collate(ids: list[np.ndarray], max_len: int, train: bool, rng: random.Random, encoded=False):
    if encoded:
        inp = torch.from_numpy(np.stack(ids).astype(np.int64))
        if inp.shape[1] != max_len or not (inp[:, 0] == CLS).all() or not (inp[:, -1] == SEP).all():
            raise ValueError("H5 input must contain 1022 DNA tokens + CLS/SEP")
        return inp, torch.ones_like(inp)
    body = max_len - 2
    rows = []
    for x in ids:
        if len(x) > body:
            s = rng.randrange(len(x) - body + 1) if train else 0
            x = x[s : s + body]
        rows.append([CLS, *x.tolist(), SEP])
    L = max(map(len, rows))
    inp = torch.full((len(rows), L), PAD, dtype=torch.long)
    mask = torch.zeros((len(rows), L), dtype=torch.long)
    for r, row in enumerate(rows):
        inp[r, : len(row)] = torch.tensor(row)
        mask[r, : len(row)] = 1
    return inp, mask


def group_split(df: pd.DataFrame, by: str, val_frac: float, test_frac: float,
                seed: int) -> tuple[list[int], list[int]]:
    """Assign whole taxonomic groups (e.g. families) to val/test so relatives don't leak.

    Groups are shuffled and taken until each split has its share of species.
    """
    sp = df.groupby("ncbi_taxid")[by].first().fillna("NA")
    groups = sp.groupby(sp).groups
    names = sorted(groups)
    random.Random(seed).shuffle(names)
    n = len(sp)
    val, test = [], []
    for g in names:
        members = list(groups[g])
        if len(test) < test_frac * n:
            test += members
        elif len(val) < val_frac * n:
            val += members
    return sorted(map(int, val)), sorted(map(int, test))


# ---------------------------------------------------------------- eval


@torch.no_grad()
def evaluate(model, df: pd.DataFrame, ids: list[np.ndarray], mu: float, sd: float, args,
             device) -> tuple[dict, pd.DataFrame]:
    model.eval()
    rng = random.Random(0)
    preds = np.zeros(len(df), dtype=np.float32)
    lengths = df.n_tokens.to_numpy()
    for b in make_batches(lengths, args.eval_tokens_per_batch, args.max_len, False, rng):
        inp, mask = collate([ids[i] for i in b], args.max_len, False, rng, encoded="chunk_id" in df)
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=device.type == "cuda"):
            out = model(inp.to(device, non_blocking=True), mask.to(device, non_blocking=True))
        preds[b] = out.float().cpu().numpy()
    model.train()
    is_chunks = "chunk_id" in df
    identity = ["assembly_accession", "chunk_id"] if is_chunks else ["entrez_id", "hgnc_symbol"]
    count_col = "chunk_id" if is_chunks else "entrez_id"
    count_name = "n_chunks" if is_chunks else "n_genes"
    df = df[["ncbi_taxid", "scientific_name", "class", *identity, "log10_longevity"]].copy()
    df["pred_log10"] = preds * sd + mu
    sp = (df.groupby(["ncbi_taxid", "scientific_name", "class"], dropna=False)
            .agg(true_log10=("log10_longevity", "first"), pred_log10=("pred_log10", "mean"),
                 pred_sd=("pred_log10", "std"), **{count_name: (count_col, "size")})
            .reset_index())
    m = {
        "pair_mse_z": float((((df.pred_log10 - df.log10_longevity) / sd) ** 2).mean()),
        "pair_mae_log10": float((df.pred_log10 - df.log10_longevity).abs().mean()),
        "species_mae_log10": float((sp.pred_log10 - sp.true_log10).abs().mean()),
        "baseline_species_mae_log10": float((mu - sp.true_log10).abs().mean()),  # predict train mean
        "n_species": len(sp), "n_pairs": len(df),
    }
    if len(sp) >= 3:
        m["species_spearman"] = float(sp.true_log10.rank().corr(sp.pred_log10.rank()))  # no scipy
        m["species_pearson"] = float(sp.true_log10.corr(sp.pred_log10))
    sp["class"] = sp["class"].fillna("unclassified")
    m["species"] = sp.round(4).to_dict("records")
    return m, df


# ---------------------------------------------------------------- main


def main(argv=None) -> None:
    with ExitStack() as stack:
        _main(argv, stack)


def _main(argv, stack) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, required=True, help="pairs.parquet, or H5 shard/directory from longevity.chunks")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--val-species", type=int, nargs="*", default=[], help="NCBI taxids")
    ap.add_argument("--test-species", type=int, nargs="*", default=[], help="NCBI taxids")
    ap.add_argument("--exclude-species", type=int, nargs="*", default=[], help="NCBI taxids")
    ap.add_argument("--val-frac", type=float, default=0.0,
                    help="auto split (when no --val/--test-species): fraction of species for val")
    ap.add_argument("--test-frac", type=float, default=0.0)
    ap.add_argument("--split-by", default="family", help="taxonomic column kept within one split")
    ap.add_argument("--min-genes", type=int, default=0, help="drop species with fewer genes")
    ap.add_argument("--epochs", type=float, default=3)
    ap.add_argument("--max-steps", type=int, default=0, help="stop after N steps (smoke test)")
    ap.add_argument("--time-budget-min", type=float, default=0, help="stop training after N minutes")
    ap.add_argument("--max-len", type=int, default=None,
                    help="gene input limit (1024); H5 chunks require 1024 including CLS/SEP")
    ap.add_argument("--tokens-per-batch", type=int, default=32768)
    ap.add_argument("--eval-tokens-per-batch", type=int, default=131072)
    ap.add_argument("--lr-encoder", type=float, default=3e-5)
    ap.add_argument("--lr-head", type=float, default=1e-3)
    ap.add_argument("--weight-decay", type=float, default=0.01)
    ap.add_argument("--warmup", type=float, default=0.05, help="fraction of steps")
    ap.add_argument("--min-lr-ratio", type=float, default=0.1)
    ap.add_argument("--head-hidden", type=int, default=256)
    ap.add_argument("--head-dropout", type=float, default=0.1)
    ap.add_argument("--grad-clip", type=float, default=1.0)
    ap.add_argument("--attn", default="kernels-community/flash-attn2")
    ap.add_argument("--compile", action="store_true")
    ap.add_argument("--evals-per-epoch", type=int, default=1)
    ap.add_argument("--log-every", type=int, default=20)
    ap.add_argument("--no-save", action="store_true", help="skip writing model weights")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--eval-every-steps", type=int, default=0)
    ap.add_argument("--eval-genes-per-species", type=int, default=0,
                    help="Fixed per-species example cap during validation; also applies to chunks")
    ap.add_argument("--final-eval-genes-per-species", type=int, default=0)
    ap.add_argument("--save-best", action="store_true")
    ap.add_argument("--comparison", type=Path, help="Enforce a frozen CDS cohort and protocol")
    args = ap.parse_args(argv)
    spec = None
    if args.comparison:
        from longevity.comparison import apply_settings, read_spec

        spec = read_spec(args.comparison)
        flags = {a.split("=")[0] for a in (sys.argv[1:] if argv is None else argv)}
        explicit = {action.dest for action in ap._actions if flags.intersection(action.option_strings)}
        apply_settings(args, spec, explicit)
        if spec["model"] != MODEL:
            ap.error("comparison specifies a different encoder")
    is_chunks = args.data.is_dir() or args.data.suffix in {".h5", ".hdf5"}
    if spec and not is_chunks:
        ap.error("--comparison currently applies to the H5 variant of the reference CDS run")
    if is_chunks and args.max_len not in (None, 1024):
        ap.error("H5 mode requires --max-len 1024: 1022 DNA BPE + CLS/SEP; no cropping")
    args.max_len = args.max_len or 1024
    if is_chunks and args.min_genes:
        ap.error("--min-genes applies only to gene pairs")
    if (args.epochs <= 0 or args.max_len < 3 or args.evals_per_epoch < 1
            or args.log_every < 1 or args.max_steps < 0):
        ap.error("epochs, evals-per-epoch, log-every must be positive; max-len >= 3; max-steps >= 0")
    if min(args.eval_every_steps, args.eval_genes_per_species, args.final_eval_genes_per_species) < 0:
        ap.error("evaluation cadence and example caps must be nonnegative")
    if min(args.tokens_per_batch, args.eval_tokens_per_batch) < args.max_len:
        ap.error("token budgets must fit at least one complete input")
    if min(args.val_frac, args.test_frac) < 0 or args.val_frac + args.test_frac >= 1:
        ap.error("split fractions must be nonnegative and sum to less than one")
    if set(args.val_species) & set(args.test_species):
        ap.error("validation and test species must be disjoint")

    if spec and (args.out / "config.json").exists():
        ap.error("comparison training requires a fresh output directory; existing runs are preserved")
    args.out.mkdir(parents=True, exist_ok=True)
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    rng = random.Random(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    torch.backends.cuda.matmul.allow_tf32 = True

    # ---- data and species split
    if is_chunks:
        from longevity.chunks import load_chunks

        df, chunk_rows = load_chunks(args.data, stack)
    else:
        df = pd.read_parquet(args.data)
    if spec:
        from longevity.comparison import validate_chunk_counts, validate_cohort

        validate_cohort(df, spec)
        validate_chunk_counts(df, spec)
        if "comparison_sha256" not in df or not df.comparison_sha256.eq(spec["sha256"]).all():
            ap.error("H5 shards were not prepared with this comparison specification")
    df = df[~df.ncbi_taxid.isin(args.exclude_species)].reset_index(drop=True)
    if args.min_genes:
        n = df.groupby("ncbi_taxid").entrez_id.transform("size")
        log(f"dropping {df[n < args.min_genes].ncbi_taxid.nunique()} species with < {args.min_genes} genes")
        df = df[n >= args.min_genes].reset_index(drop=True)
    if not args.val_species and not args.test_species and args.val_frac + args.test_frac > 0:
        args.val_species, args.test_species = group_split(df, args.split_by, args.val_frac,
                                                          args.test_frac, args.seed)
    held = set(args.val_species) | set(args.test_species)
    parts = {"train": df[~df.ncbi_taxid.isin(held)], "val": df[df.ncbi_taxid.isin(args.val_species)],
             "test": df[df.ncbi_taxid.isin(args.test_species)]}
    parts = {k: v.reset_index(drop=True) for k, v in parts.items()}
    ids = ({k: chunk_rows.subset(v._row.to_numpy()) for k, v in parts.items()} if is_chunks else
           {k: [np.asarray(x, dtype=np.int32) for x in v.input_ids] for k, v in parts.items()})
    tr = parts["train"]
    if tr.empty:
        ap.error("no training examples remain after filtering/splitting")
    # target scaling from species-level values so species with many genes don't dominate
    sp_y = tr.groupby("ncbi_taxid").log10_longevity.first()
    mu = float(sp_y.mean())
    sd = float(sp_y.std()) if len(sp_y) > 1 and sp_y.std() > 0 else 1.0
    if spec:
        if not np.allclose([mu, sd], [spec["mu"], spec["sd"]], rtol=0, atol=1e-7):
            ap.error("training target normalization differs from the CDS reference")
        mu, sd = spec["mu"], spec["sd"]
    y_train = torch.tensor(((tr.log10_longevity - mu) / sd).values, dtype=torch.float32)
    for k, v in parts.items():
        log(f"{k}: {v.ncbi_taxid.nunique()} species, {len(v)} pairs, "
            f"{int(v.n_tokens.clip(upper=args.max_len - 2).sum() + 2 * len(v)):,} tokens")
    log(f"target log10 longevity: mu={mu:.3f} sd={sd:.3f}")

    # ---- model and optimiser
    model = LongevityRegressor(args.attn, args.head_hidden, args.head_dropout).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    decay, no_decay = [], []
    for n, p in model.backbone.named_parameters():
        (decay if p.ndim >= 2 and "embeddings" not in n else no_decay).append(p)
    groups = [
        {"params": decay, "lr": args.lr_encoder, "weight_decay": args.weight_decay},
        {"params": no_decay, "lr": args.lr_encoder, "weight_decay": 0.0},
        {"params": list(model.head.parameters()), "lr": args.lr_head, "weight_decay": args.weight_decay},
    ]
    opt = torch.optim.AdamW(groups, betas=(0.9, 0.98), eps=1e-6, fused=device.type == "cuda")
    base_lrs = [g["lr"] for g in groups]
    fwd = torch.compile(model) if args.compile else model

    lengths = tr.n_tokens.to_numpy()
    steps_per_epoch = len(make_batches(lengths, args.tokens_per_batch, args.max_len, True,
                                       random.Random(0)))
    total_steps = max(1, math.ceil(steps_per_epoch * args.epochs))
    if args.max_steps:
        total_steps = min(total_steps, args.max_steps)
    warmup = max(1, int(args.warmup * total_steps))
    eval_every = args.eval_every_steps or max(1, steps_per_epoch // args.evals_per_epoch)

    stop_steps = total_steps
    if spec and spec["budget"] == "updates":
        total_steps = spec["reference_total_steps"]
        stop_steps = spec["reference_completed_steps"]
        warmup = spec["reference_warmup_steps"]
        eval_every = max(1, spec["reference_steps_per_epoch"] // args.evals_per_epoch)

    def lr_scale(step: int) -> float:
        if step < warmup:
            return (step + 1) / warmup
        p = (step - warmup) / max(1, total_steps - warmup)
        return args.min_lr_ratio + (1 - args.min_lr_ratio) * 0.5 * (1 + math.cos(math.pi * min(p, 1)))

    import transformers

    config = dict(vars(args), data=str(args.data), out=str(args.out), model=MODEL,
                  comparison=str(args.comparison) if args.comparison else None,
                  comparison_sha256=spec["sha256"] if spec else None,
                  stop_steps=stop_steps, eval_every_steps=eval_every,
                  runtime={"python": sys.version, "torch": torch.__version__,
                           "transformers": transformers.__version__, "cuda": torch.version.cuda,
                           "device": str(device)},
                  input_kind="genome_chunks" if is_chunks else "gene_cds",
                  n_params=n_params, mu=mu, sd=sd, steps_per_epoch=steps_per_epoch,
                  total_steps=total_steps, warmup_steps=warmup,
                  split={k: sorted(v.ncbi_taxid.unique().tolist()) for k, v in parts.items()})
    if spec:
        if config["split"] != spec["split"]:
            ap.error("effective species splits differ from CDS reference")
        (args.out / "comparison.json").write_text(json.dumps(spec, indent=2))
    (args.out / "config.json").write_text(json.dumps(config, indent=2))
    log(f"model {n_params / 1e6:.1f}M params, attn={args.attn}, {steps_per_epoch} steps/epoch, "
        f"{total_steps} total, warmup {warmup}")

    metrics_f = stack.enter_context((args.out / "metrics.jsonl").open("a"))

    def write(rec: dict) -> None:
        metrics_f.write(json.dumps(rec) + "\n")
        metrics_f.flush()

    best_val = float("inf")

    def run_eval(step: int, epoch: float, split: str = "val", final=False) -> None:
        nonlocal best_val
        if parts[split].empty:
            return
        t0 = time.time()
        cap = args.final_eval_genes_per_species if final else args.eval_genes_per_species
        frame, split_ids = parts[split], ids[split]
        if cap:
            positions = np.sort(frame.groupby("ncbi_taxid", group_keys=False).sample(
                frac=1, random_state=args.seed).groupby("ncbi_taxid", sort=False).head(cap).index)
            frame = frame.iloc[positions].reset_index(drop=True)
            split_ids = (split_ids.subset(positions) if is_chunks else [split_ids[i] for i in positions])
        m, preds = evaluate(model, frame, split_ids, mu, sd, args, device)
        if args.save_best and not final and split == "val" and m["species_mae_log10"] < best_val:
            best_val = m["species_mae_log10"]
            tmp = args.out / "best.pt.part"
            torch.save({"model": model.state_dict(), "config": config, "step": step,
                        "val_mae_log10": best_val}, tmp)
            tmp.replace(args.out / "best.pt")
        preds.to_parquet(args.out / f"predictions_{split}.parquet")
        species = m.pop("species")
        write(dict(kind=split, step=step, epoch=round(epoch, 3), seconds=round(time.time() - t0, 1),
                   **m, species=species))
        log(f"{split} step {step}: " + " ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
                                              for k, v in m.items()) + f" ({time.time() - t0:.0f}s)")
        count_name, unit = ("n_chunks", "chunks") if is_chunks else ("n_genes", "genes")
        for s in species:
            log(f"    {s['scientific_name']:<28} true {10 ** s['true_log10']:7.2f} yrs  "
                f"pred {10 ** s['pred_log10']:7.2f} yrs  ({s[count_name]} {unit})")

    # ---- train
    model.train()
    step, t_start, done = 0, time.time(), False
    win_loss, win_n, win_tok, win_t = 0.0, 0, 0, time.time()
    epoch = 0
    while not done:
        for b in make_batches(lengths, args.tokens_per_batch, args.max_len, True, rng):
            for g, base in zip(opt.param_groups, base_lrs):
                g["lr"] = base * lr_scale(step)
            inp, mask = collate([ids["train"][i] for i in b], args.max_len, True, rng, encoded=is_chunks)
            inp, mask = inp.to(device, non_blocking=True), mask.to(device, non_blocking=True)
            y = y_train[b].to(device, non_blocking=True)
            with torch.autocast("cuda", dtype=torch.bfloat16, enabled=device.type == "cuda"):
                pred = fwd(inp, mask)
            loss = nn.functional.mse_loss(pred.float(), y)
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
            if step % args.log_every == 0 or step == stop_steps:
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
            if step >= stop_steps or over_time:
                if over_time:
                    log(f"time budget of {args.time_budget_min} min reached at step {step}")
                done = True
                break
            if step % eval_every == 0:
                run_eval(step, step / steps_per_epoch)
        epoch += 1

    train_min = (time.time() - t_start) / 60
    log(f"training finished: {step} steps in {train_min:.1f} min")
    run_eval(step, step / steps_per_epoch, "val", final=True)
    run_eval(step, step / steps_per_epoch, "test", final=True)
    write({"kind": "done", "step": step, "train_minutes": train_min})
    if not args.no_save:
        torch.save({"model": model.state_dict(), "config": config}, args.out / "model.pt")
        log(f"saved {args.out / 'model.pt'}")


if __name__ == "__main__":
    main()
