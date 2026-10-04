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
    """ModernGENA encoder + 2-layer MLP on the [CLS] hidden state -> one scalar.

    A gene longer than the encoder window arrives as several windows; their [CLS] states are
    averaged into one gene vector (gene_idx maps each window row to its gene) before the head.
    """

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

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor,
                gene_idx: torch.Tensor | None = None, n_genes: int | None = None) -> torch.Tensor:
        cls = self.backbone(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state[:, 0]
        if gene_idx is not None:
            pooled = cls.new_zeros(n_genes, cls.shape[-1]).index_add_(0, gene_idx, cls)
            counts = torch.bincount(gene_idx, minlength=n_genes).clamp(min=1).unsqueeze(-1)
            cls = pooled / counts.to(cls.dtype)
        return self.head(cls).squeeze(-1)


# ---------------------------------------------------------------- data


def n_windows(lengths: np.ndarray, max_len: int, chunk: bool) -> np.ndarray:
    """Encoder windows per gene: all of it in --long-inputs chunk mode, else one (cropped)."""
    return np.maximum(1, -(-lengths // (max_len - 2))) if chunk else np.ones_like(lengths)


def window_len(lengths: np.ndarray, max_len: int, chunk: bool) -> np.ndarray:
    """Padded length of a gene's longest window (windows are equal-sized splits)."""
    k = n_windows(lengths, max_len, chunk)
    return np.minimum(-(-lengths // k) + 2, max_len)


def make_batches(lengths: np.ndarray, tokens_per_batch: int, max_len: int, shuffle: bool,
                 rng: random.Random, chunk: bool = False) -> list[list[int]]:
    """Length-bucketed batches of genes whose padded size (windows x longest window) stays
    under tokens_per_batch."""
    idx = list(range(len(lengths)))
    if shuffle:
        rng.shuffle(idx)
    lens = window_len(lengths, max_len, chunk)
    wins = n_windows(lengths, max_len, chunk)
    chunk = 8192  # sort within large chunks: little padding, still random across epochs
    batches = []
    for c in range(0, len(idx), chunk):
        part = sorted(idx[c : c + chunk], key=lambda i: lens[i])
        cur, longest, rows = [], 0, 0
        for i in part:
            longest_new = max(longest, lens[i])
            if cur and longest_new * (rows + wins[i]) > tokens_per_batch:
                batches.append(cur)
                cur, longest_new, rows = [], lens[i], 0
            cur.append(i)
            longest, rows = longest_new, rows + wins[i]
        if cur:
            batches.append(cur)
    if shuffle:
        rng.shuffle(batches)
    return batches


def collate(ids: list[np.ndarray], max_len: int, train: bool, rng: random.Random,
            chunk: bool = False, encoded: bool = False):
    """Token rows for a batch plus the gene index of each row.

    encoded: rows are premade H5 samples (1022 DNA tokens + CLS/SEP), stacked as is.
    chunk: a gene longer than max_len-2 tokens is split into k equal windows covering all of it.
    Otherwise it is cropped to one window (random in training, first at evaluation).
    """
    if encoded:
        inp = torch.from_numpy(np.stack(ids).astype(np.int64))
        if inp.shape[1] != max_len or not (inp[:, 0] == CLS).all() or not (inp[:, -1] == SEP).all():
            raise ValueError("H5 input must contain 1022 DNA tokens + CLS/SEP")
        return inp, torch.ones_like(inp), torch.arange(len(ids))
    body = max_len - 2
    rows, gene_idx = [], []
    for g, x in enumerate(ids):
        if len(x) > body and chunk:
            k = -(-len(x) // body)
            step = -(-len(x) // k)
            parts = [x[s : s + step] for s in range(0, len(x), step)]
        elif len(x) > body:
            s = rng.randrange(len(x) - body + 1) if train else 0
            parts = [x[s : s + body]]
        else:
            parts = [x]
        for part in parts:
            rows.append([CLS, *part.tolist(), SEP])
            gene_idx.append(g)
    L = max(map(len, rows))
    inp = torch.full((len(rows), L), PAD, dtype=torch.long)
    mask = torch.zeros((len(rows), L), dtype=torch.long)
    for r, row in enumerate(rows):
        inp[r, : len(row)] = torch.tensor(row)
        mask[r, : len(row)] = 1
    return inp, mask, torch.tensor(gene_idx, dtype=torch.long)


def genes_per_species(df: pd.DataFrame, n: int, seed: int) -> pd.DataFrame:
    """A fixed random sample of at most n genes (or chunks) per species."""
    order = [c for c in ("ncbi_taxid", "entrez_id", "assembly_accession", "chunk_id") if c in df]
    return (df.sample(frac=1, random_state=seed).groupby("ncbi_taxid").head(n)
              .sort_values(order).reset_index(drop=True))


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
    chunk = getattr(args, "long_inputs", "crop") == "chunk"
    for b in make_batches(lengths, args.eval_tokens_per_batch, args.max_len, False, rng, chunk):
        inp, mask, gidx = collate([ids[i] for i in b], args.max_len, False, rng, chunk,
                                  encoded="chunk_id" in df)
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=device.type == "cuda"):
            win = (gidx.to(device, non_blocking=True), len(b)) if chunk else ()  # windows -> gene
            out = model(inp.to(device, non_blocking=True), mask.to(device, non_blocking=True), *win)
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
                    help="encoder window in tokens (1024); H5 chunks require 1024 including CLS/SEP")
    ap.add_argument("--long-inputs", choices=["chunk", "crop"], default="crop",
                    help="genes longer than the window: split into windows and average their "
                         "[CLS] (chunk, whole gene used) or crop to one window (crop)")
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
    ap.add_argument("--eval-every-steps", type=int, default=0, help="overrides --evals-per-epoch")
    ap.add_argument("--eval-genes-per-species", type=int, default=0,
                    help="periodic val on a fixed sample of N genes per species (0 = all genes)")
    ap.add_argument("--final-eval-genes-per-species", type=int, default=0,
                    help="cap genes per species in the final val/test evaluation (0 = all)")
    ap.add_argument("--save-best", action="store_true",
                    help="keep best.pt by periodic val species MAE and run final val/test with it")
    ap.add_argument("--log-every", type=int, default=20)
    ap.add_argument("--no-save", action="store_true", help="skip writing model weights")
    ap.add_argument("--seed", type=int, default=0)
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
    for col in ("class", "order", "family"):  # NCBI has no class for turtles/crocodilians etc.
        if col in df:
            df[col] = df[col].fillna("NA")
    if spec:
        from longevity.comparison import validate_chunk_counts, validate_cohort

        validate_cohort(df, spec)
        try:
            validate_chunk_counts(df, spec)
        except ValueError as e:
            ap.error(str(e))
        if "comparison_sha256" not in df or not df.comparison_sha256.eq(spec["sha256"]).all():
            ap.error("H5 shards were not prepared with this comparison specification")
    df = df[~df.ncbi_taxid.isin(args.exclude_species)].reset_index(drop=True)
    if args.min_genes:
        n = df.groupby("ncbi_taxid").entrez_id.transform("size")
        log(f"dropping {df[n < args.min_genes].ncbi_taxid.nunique()} species with < {args.min_genes} genes")
        df = df[n >= args.min_genes].reset_index(drop=True)
    if not args.val_species and not args.test_species and "split" in df and not args.val_frac:
        # splits precomputed by longevity.prepare
        sp = df.groupby("ncbi_taxid").split.first()
        args.val_species = sorted(map(int, sp.index[sp == "val"]))
        args.test_species = sorted(map(int, sp.index[sp == "test"]))
        log(f"using the dataset's split column: {len(args.val_species)} val / "
            f"{len(args.test_species)} test species")
    elif not args.val_species and not args.test_species and args.val_frac + args.test_frac > 0:
        args.val_species, args.test_species = group_split(df, args.split_by, args.val_frac,
                                                          args.test_frac, args.seed)
    held = set(args.val_species) | set(args.test_species)
    parts = {"train": df[~df.ncbi_taxid.isin(held)], "val": df[df.ncbi_taxid.isin(args.val_species)],
             "test": df[df.ncbi_taxid.isin(args.test_species)]}
    parts = {k: v.reset_index(drop=True) for k, v in parts.items()}
    if args.final_eval_genes_per_species:
        for k in ("val", "test"):  # cap genes per species for the final evaluations too
            if not parts[k].empty:
                parts[k] = genes_per_species(parts[k], args.final_eval_genes_per_species, args.seed)
    if args.eval_genes_per_species and not parts["val"].empty:
        # fixed per-species gene sample for frequent, cheap validation; final evals use everything
        parts["val_sub"] = genes_per_species(parts["val"], args.eval_genes_per_species, args.seed)
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
        lens = v.n_tokens.to_numpy()
        wins = n_windows(lens, args.max_len, args.long_inputs == "chunk")
        used = lens if args.long_inputs == "chunk" else np.minimum(lens, args.max_len - 2)
        log(f"{k}: {v.ncbi_taxid.nunique()} species, {len(v)} pairs, {int(wins.sum())} windows, "
            f"{int(used.sum() + 2 * wins.sum()):,} tokens")
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
    chunk = args.long_inputs == "chunk"
    steps_per_epoch = len(make_batches(lengths, args.tokens_per_batch, args.max_len, True,
                                       random.Random(0), chunk))
    total_steps = max(1, math.ceil(steps_per_epoch * args.epochs))
    if args.max_steps:
        total_steps = min(total_steps, args.max_steps)
    warmup = max(1, int(args.warmup * total_steps))
    eval_every = args.eval_every_steps or max(1, steps_per_epoch // args.evals_per_epoch)
    periodic = "val_sub" if "val_sub" in parts else "val"
    best = {"mae": float("inf"), "step": None}

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
        splits = {k: config["split"].get(k, []) for k in ("train", "val", "test")}
        if splits != spec["split"] or not set(config["split"].get("val_sub", [])) <= set(splits["val"]):
            ap.error("effective species splits differ from CDS reference")
        (args.out / "comparison.json").write_text(json.dumps(spec, indent=2))
    (args.out / "config.json").write_text(json.dumps(config, indent=2))
    log(f"model {n_params / 1e6:.1f}M params, attn={args.attn}, {steps_per_epoch} steps/epoch, "
        f"{total_steps} total, warmup {warmup}")

    metrics_f = stack.enter_context((args.out / "metrics.jsonl").open("a"))

    def write(rec: dict) -> None:
        metrics_f.write(json.dumps(rec) + "\n")
        metrics_f.flush()

    def run_eval(step: int, epoch: float, split: str = "val", kind: str | None = None):
        if parts[split].empty:
            return None
        t0 = time.time()
        m, preds = evaluate(model, parts[split], ids[split], mu, sd, args, device)
        preds.to_parquet(args.out / f"predictions_{kind or split}.parquet")
        species = m.pop("species")
        write(dict(kind=kind or split, step=step, epoch=round(epoch, 3),
                   seconds=round(time.time() - t0, 1), **m, species=species))
        log(f"{split} step {step}: " + " ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
                                              for k, v in m.items()) + f" ({time.time() - t0:.0f}s)")
        count_name, unit = ("n_chunks", "chunks") if is_chunks else ("n_genes", "genes")
        if len(species) <= 30:
            for s in species:
                log(f"    {s['scientific_name']:<28} true {10 ** s['true_log10']:7.2f} yrs  "
                    f"pred {10 ** s['pred_log10']:7.2f} yrs  ({s[count_name]} {unit})")
        return m

    # ---- train
    model.train()
    step, t_start, done = 0, time.time(), False
    win_loss, win_n, win_tok, win_t = 0.0, 0, 0, time.time()
    epoch = 0
    while not done:
        for b in make_batches(lengths, args.tokens_per_batch, args.max_len, True, rng, chunk):
            for g, base in zip(opt.param_groups, base_lrs):
                g["lr"] = base * lr_scale(step)
            inp, mask, gidx = collate([ids["train"][i] for i in b], args.max_len, True, rng, chunk,
                                      encoded=is_chunks)
            inp, mask = inp.to(device, non_blocking=True), mask.to(device, non_blocking=True)
            win = (gidx.to(device, non_blocking=True), len(b)) if chunk else ()
            y = y_train[b].to(device, non_blocking=True)
            with torch.autocast("cuda", dtype=torch.bfloat16, enabled=device.type == "cuda"):
                pred = fwd(inp, mask, *win)
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
                m = run_eval(step, step / steps_per_epoch, periodic, "val")
                if args.save_best and m and m["species_mae_log10"] < best["mae"]:
                    best.update(mae=m["species_mae_log10"], step=step)
                    torch.save({"model": model.state_dict(), "config": json.loads(json.dumps(config, default=str)), "step": step,
                                "val_species_mae_log10": best["mae"]}, args.out / "best.pt")
                    log(f"new best val species MAE {best['mae']:.4f} at step {step} -> best.pt")
        epoch += 1

    train_min = (time.time() - t_start) / 60
    log(f"training finished: {step} steps in {train_min:.1f} min")
    if not args.no_save:
        torch.save({"model": model.state_dict(), "config": json.loads(json.dumps(config, default=str))}, args.out / "model.pt")
        log(f"saved {args.out / 'model.pt'}")
    if args.save_best and best["step"] is not None:
        model.load_state_dict(torch.load(args.out / "best.pt", map_location=device)["model"])
        log(f"final evaluation uses best.pt (step {best['step']}, val MAE {best['mae']:.4f})")
    final_step = best["step"] if args.save_best and best["step"] is not None else step
    # "val_full" when periodic val used a gene sample or picked best.pt; otherwise the usual "val"
    final_kind = "val_full" if (args.save_best or "val_sub" in parts) else "val"
    run_eval(final_step, final_step / steps_per_epoch, "val", final_kind)
    run_eval(final_step, final_step / steps_per_epoch, "test")
    write(dict(kind="done", step=step, eval_step=final_step, train_minutes=train_min))


if __name__ == "__main__":
    main()
