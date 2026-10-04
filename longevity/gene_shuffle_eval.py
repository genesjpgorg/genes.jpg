"""Reproduce saved gene-model metrics and evaluate DNA-token-order controls."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import torch

from longevity.shuffle_eval import species_metrics
from longevity.train import LongevityRegressor, collate, log, make_batches


def shuffle_batch(inputs, mask, keys, seed=0):
    """Permute only non-special, attended tokens; keep all CLS/SEP/PAD positions fixed."""
    result = inputs.clone()
    for row, key in enumerate(keys):
        positions = np.flatnonzero(
            (mask[row].numpy() != 0) & ~np.isin(inputs[row].numpy(), [1, 2, 3])
        )
        row_seed = int.from_bytes(hashlib.sha256(f"{seed}:{key}".encode()).digest()[:8], "little")
        permutation = np.random.default_rng(row_seed).permutation(positions)
        result[row, positions] = inputs[row, permutation]
    return result


@torch.no_grad()
def predict(model, frame, cfg, device, shuffled, seed):
    model.eval()
    args = SimpleNamespace(**cfg)
    values = np.zeros(len(frame), dtype=np.float32)
    arrays = [np.asarray(a, dtype=np.int32) for a in frame.input_ids]
    keys = [f"{r.assembly_accession}:{r.entrez_id}:{r.source_row}" for r in frame.itertuples()]
    batches = make_batches(
        frame.n_tokens.to_numpy(), args.eval_tokens_per_batch, args.max_len, False, random.Random(0)
    )
    for i, batch in enumerate(batches):
        packed = collate([arrays[j] for j in batch], args.max_len, False, random.Random(0))
        inputs, mask = packed[:2]
        if shuffled:
            inputs = shuffle_batch(inputs, mask, [keys[j] for j in batch], seed)
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=device.type == "cuda"):
            pred = model(inputs.to(device), mask.to(device))
        values[batch] = pred.float().cpu().numpy()
        if (i + 1) % 200 == 0:
            log(f"{'shuffled' if shuffled else 'intact'}: batch {i + 1}/{len(batches)}")
    out = frame[
        [
            "ncbi_taxid",
            "scientific_name",
            "class",
            "assembly_accession",
            "entrez_id",
            "hgnc_symbol",
            "source_row",
            "log10_longevity",
        ]
    ].copy()
    out["pred_log10"] = values * cfg["sd"] + cfg["mu"]
    return out


def file_hash(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def report(out, summary):
    lines = [
        "# Gene-genomic longevity model: token-shuffle control",
        "",
        ("Run: `longevity-agingatlas-all-genomic-10ep`. The recorded input is "
        "`pairs_genomic.parquet` (gene-genomic sequences, not CDS)."),
        "",
        ("**Checkpoint: `best.pt`, step 76,000**, selected by validation during training. "
        "The original train log identifies this checkpoint as the source of final held-out predictions. "
        "`model.pt` is the final training checkpoint and is not used in this comparison."),
        "",
        ("Full validation uses `predictions_val_full.parquet` (335 species); "
        "`predictions_val.parquet` is the smaller monitoring subset. Test uses "
        "`predictions_test.parquet` (294 species)."),
        "",
        "| Split | Evaluation | Species | MAE log10 years | MAE years | Pearson log10 | Pearson years | Spearman |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for split, modes in summary["results"].items():
        for mode, metrics in modes.items():
            cells = [split, mode, str(metrics["n_species"])]
            cells += [
                "undefined" if metrics[k] is None else f"{metrics[k]:.6f}"
                for k in ("mae_log10", "mae_years", "pearson_log10", "pearson_years", "spearman")
            ]
            lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        ("Predictions are averaged in log10 space per species, then exponentiated for "
        "year-scale metrics. Species receive equal weight; missing class annotations do not drop rows."),
        "",
        ("The intact rerun uses the recorded deterministic evaluation crop: first 1,022 DNA "
        "tokens for long inputs. Shuffling happens **after cropping and padding**, so the token "
        "multiset, length, attention mask and every CLS/SEP/PAD position stay fixed. Seed 0 "
        "permutations are deterministic per assembly, gene and source row, independent of batching. "
        "No retraining or label permutation occurs."),
        "",
        ("The shuffled input distribution differs from training. This tests sensitivity to token "
        "order; it does not establish biological causality. See `summary.json` for exact metrics, "
        "saved-versus-rerun differences and SHA256 provenance. Per-species CSVs accompany the report; "
        "per-input predictions remain on shared storage."),
        "",
        ("Historical software equivalence remains unverified. The rerun uses the cluster environment "
        "recorded in `environment.txt`; agreement with saved predictions is measured explicitly."),
    ]
    (out / "README.md").write_text("\n".join(lines) + "\n")


def run_eval(run, data, out, seed=0):
    if out.exists():
        raise FileExistsError(out)
    cfg = json.loads((run / "config.json").read_text())
    if cfg["long_inputs"] != "crop" or cfg["max_len"] != 1024:
        raise ValueError("Unsupported evaluation crop policy")
    out.mkdir(parents=True)
    summary = {
        "run_name": run.name,
        "checkpoint": "best.pt",
        "shuffle_seed": seed,
        "results": {},
        "reproduction": {},
        "source_files_sha256": {},
    }
    saved = {}
    for split, filename in [
        ("val", "predictions_val_full.parquet"),
        ("test", "predictions_test.parquet"),
    ]:
        saved[split] = pd.read_parquet(run / filename)
        if set(saved[split].ncbi_taxid) != set(cfg["split"][split]):
            raise ValueError(f"Saved {split} species differ from configuration")
        metrics, species = species_metrics(saved[split])
        species.to_csv(out / f"{split}_saved_species.csv", index=False)
        summary["results"][split] = {"saved": metrics}
        summary["source_files_sha256"][filename] = file_hash(run / filename)
        log(f"{split}/saved: {json.dumps(metrics)}")
    (out / "saved_metrics.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    checkpoint = run / "best.pt"
    state = torch.load(checkpoint, map_location="cpu", weights_only=False)
    summary["checkpoint_step"] = state.get("step")
    if state.get("step") != 76000:
        raise ValueError("Unexpected best checkpoint step")
    for key in ("model", "mu", "sd", "split", "head_hidden", "head_dropout"):
        if state["config"][key] != cfg[key]:
            raise ValueError(f"Checkpoint/config mismatch: {key}")
    summary["source_files_sha256"].update(
        {
            "best.pt": file_hash(checkpoint),
            "config.json": file_hash(run / "config.json"),
            "pairs_genomic.parquet": file_hash(data),
        }
    )
    torch.manual_seed(cfg["seed"])
    torch.backends.cuda.matmul.allow_tf32 = True
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = LongevityRegressor(cfg["attn"], cfg["head_hidden"], cfg["head_dropout"])
    model.load_state_dict(state["model"], strict=True)
    del state
    model.to(device)
    frame = pd.read_parquet(data)
    frame["source_row"] = np.arange(len(frame))
    for split in ("val", "test"):
        part = frame[frame.ncbi_taxid.isin(cfg["split"][split])].reset_index(drop=True)
        original = saved[split].reset_index(drop=True)
        # Preserve exact example ordering: do not infer a join from potentially duplicate species/gene IDs.
        if len(part) != len(original):
            raise ValueError(f"{split}: saved/source input count differs")
        for key in ("ncbi_taxid", "entrez_id", "scientific_name"):
            if part[key].tolist() != original[key].tolist():
                raise ValueError(f"{split}: saved/source input identity/order differs: {key}")
        if not np.allclose(part.log10_longevity, original.log10_longevity, atol=1e-10, rtol=0):
            raise ValueError("Saved/source labels differ")
        for mode in ("intact", "shuffled"):
            log(f"Evaluating {split}/{mode}: {len(part)} inputs")
            predictions = predict(model, part, cfg, device, mode == "shuffled", seed)
            metrics, species = species_metrics(predictions)
            summary["results"][split][mode] = metrics
            if mode == "intact":
                diff = (predictions.pred_log10 - original.pred_log10).abs()
                summary["reproduction"][split] = {
                    "pair_mean_abs_log10": float(diff.mean()),
                    "pair_max_abs_log10": float(diff.max()),
                    "species_mae_log10_difference": metrics["mae_log10"]
                    - summary["results"][split]["saved"]["mae_log10"],
                }
                if diff.mean() > 0.002:
                    raise ValueError(
                        f"{split}: intact reproduction discrepancy exceeds 0.002 mean log10 years"
                    )
            predictions.to_parquet(out / f"{split}_{mode}_predictions.parquet", index=False)
            species.to_csv(out / f"{split}_{mode}_species.csv", index=False)
            log(f"{split}/{mode}: {json.dumps(metrics)}")
            (out / "progress.json").write_text(
                json.dumps(summary, indent=2, allow_nan=False) + "\n"
            )
    (out / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    (out / "run_config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    report(out, summary)
    (out / "complete").write_text("0\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    run_eval(args.run, args.data, args.out)


if __name__ == "__main__":
    main()
