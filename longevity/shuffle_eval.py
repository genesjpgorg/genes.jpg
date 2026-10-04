"""Evaluate fixed checkpoints with intact or within-chunk-shuffled DNA tokens."""

from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch

from longevity.chunks import load_chunks
from longevity.comparison import read_spec, validate_chunk_counts, validate_cohort
from longevity.train import LongevityRegressor, evaluate, log


class ShuffledRows:
    """Reproducible per-example permutations independent of batching and access order."""

    def __init__(self, rows, frame, seed):
        self.rows = rows
        self.keys = list(zip(frame.assembly_accession, frame.chunk_id))
        self.seed = seed

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        row = np.array(self.rows[index], copy=True)
        acc, chunk = self.keys[index]
        key = f"{self.seed}:{acc}:{chunk}".encode()
        seed = int.from_bytes(hashlib.sha256(key).digest()[:8], "little")
        rng = np.random.default_rng(seed)
        row[1:-1] = rng.permutation(row[1:-1])
        return row


def species_metrics(predictions):
    """Average log predictions per species, then report both log10 and raw-year scales."""
    species = predictions.groupby("ncbi_taxid", as_index=False, dropna=False).agg(
        scientific_name=("scientific_name", "first"),
        observed_log10=("log10_longevity", "first"),
        predicted_log10=("pred_log10", "mean"),
        n_inputs=("pred_log10", "size"),
    )
    if not np.isfinite(species[["observed_log10", "predicted_log10"]].to_numpy()).all():
        raise ValueError("Nonfinite species predictions")
    species["observed_years"] = 10.0**species.observed_log10
    species["predicted_years"] = 10.0**species.predicted_log10
    if not np.isfinite(species[["observed_years", "predicted_years"]].to_numpy()).all():
        raise ValueError("Nonfinite raw-year predictions")
    result = {"n_species": len(species), "n_inputs": len(predictions)}
    for scale in ("log10", "years"):
        observed, predicted = species[f"observed_{scale}"], species[f"predicted_{scale}"]
        result[f"mae_{scale}"] = float((observed - predicted).abs().mean())
        corr = observed.corr(predicted) if len(species) > 1 else float("nan")
        result[f"pearson_{scale}"] = float(corr) if np.isfinite(corr) else None
    corr = (
        species.observed_log10.rank().corr(species.predicted_log10.rank())
        if len(species) > 1
        else float("nan")
    )
    result["spearman"] = float(corr) if np.isfinite(corr) else None
    return result, species


def write_report(out, summary):
    def fmt(value):
        return "undefined" if value is None else f"{value:.4f}"

    lines = [
        f"# {summary['run_name']}: intact and shuffled-token evaluation",
        "",
        "The same final checkpoint is evaluated on the exact same validation/test examples.",
        "Only the order of DNA tokens within each input is changed; CLS/SEP, labels, splits",
        "and the token multiset stay fixed. This is an evaluation-time perturbation, not",
        "a separately trained model. Per-example permutations are fixed and reproducible",
        f"with shuffle seed {summary['shuffle_seed']}.",
        "",
        "| Split | Input | Species | MAE log10 years | MAE years | Pearson log10 | Pearson years | Spearman |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for split, modes in summary["results"].items():
        for mode, metrics in modes.items():
            cells = [split, mode, str(metrics["n_species"])] + [
                fmt(metrics[k])
                for k in ("mae_log10", "mae_years", "pearson_log10", "pearson_years", "spearman")
            ]
            lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "Predictions are first averaged in log10 space within species (the training",
        "protocol), then exponentiated for raw-year metrics. Every species has equal",
        "weight in these metrics, including species with missing class annotations.",
        "Spearman is identical under the monotonic log/year transformation; undefined",
        "correlations (constant predictions or fewer than two species) are reported explicitly.",
        "",
        "A drop under shuffling measures sensitivity to token order. The shuffled examples",
        "are out of the training distribution, so this does not by itself establish biological",
        "causality or substitute for a model trained on shuffled inputs.",
        "",
        "See [summary.json](summary.json) for checkpoint/input provenance and the per-species",
        "CSV files for observed and predicted longevity. Training configuration and the frozen",
        "cohort/split specification accompany this report.",
    ]
    (out / "README.md").write_text("\n".join(lines) + "\n")


def run_eval(run, data, out, seed=0):
    run, data, out = Path(run), Path(data), Path(out)
    if out.exists():
        raise FileExistsError(out)
    cfg = json.loads((run / "config.json").read_text())
    spec = read_spec(run / "comparison.json")
    if cfg["comparison_sha256"] != spec["sha256"]:
        raise ValueError("Run and comparison fingerprints differ")
    checkpoint = run / "model.pt"
    with checkpoint.open("rb") as handle:
        checkpoint_sha = hashlib.file_digest(handle, "sha256").hexdigest()
    torch.manual_seed(cfg["seed"])
    torch.backends.cuda.matmul.allow_tf32 = True
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = LongevityRegressor(cfg["attn"], cfg["head_hidden"], cfg["head_dropout"])
    saved = torch.load(checkpoint, map_location="cpu", weights_only=False)
    if saved["config"]["comparison_sha256"] != spec["sha256"]:
        raise ValueError("Checkpoint belongs to another comparison")
    model.load_state_dict(saved["model"], strict=True)
    del saved
    model.to(device)
    args = SimpleNamespace(**cfg)
    summary = {
        "run_name": run.name,
        "checkpoint": "model.pt",
        "checkpoint_sha256": checkpoint_sha,
        "comparison_sha256": spec["sha256"],
        "shuffle_seed": seed,
        "shuffle": "fixed per assembly accession and chunk ID; DNA tokens only",
        "results": {},
    }
    with ExitStack() as stack:
        df, rows = load_chunks(data, stack)
        validate_cohort(df, spec)
        if not df.comparison_sha256.eq(spec["sha256"]).all():
            raise ValueError("H5 comparison fingerprints differ")
        validate_chunk_counts(df, spec)
        out.mkdir(parents=True)
        for split in ("val", "test"):
            frame = df[df.ncbi_taxid.isin(cfg["split"][split])].reset_index(drop=True)
            if set(frame.ncbi_taxid) != set(spec["split"][split]) or frame.empty:
                raise ValueError(f"Missing {split} species")
            ids = rows.subset(frame._row.to_numpy())
            summary["results"][split] = {}
            for mode in ("intact", "shuffled"):
                log(f"Evaluating {run.name}: {split}/{mode}, {len(frame)} inputs")
                inputs = ids if mode == "intact" else ShuffledRows(ids, frame, seed)
                _, predictions = evaluate(model, frame, inputs, cfg["mu"], cfg["sd"], args, device)
                metrics, species = species_metrics(predictions)
                if set(species.ncbi_taxid) != set(spec["split"][split]):
                    raise ValueError("Evaluation dropped species")
                species.to_csv(out / f"{split}_{mode}_species.csv", index=False)
                predictions.to_parquet(out / f"{split}_{mode}_predictions.parquet", index=False)
                summary["results"][split][mode] = metrics
                log(f"{split}/{mode}: {json.dumps(metrics)}")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    (out / "run_config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    (out / "comparison.json").write_text(json.dumps(spec, indent=2) + "\n")
    write_report(out, summary)
    (out / "complete").write_text("0\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    run_eval(args.run, args.data, args.out, args.seed)


if __name__ == "__main__":
    main()
