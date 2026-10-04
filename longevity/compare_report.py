"""Publishable report for a completed matched random-chunk versus CDS experiment."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np


def read_run(path):
    path = Path(path)
    config = json.loads((path / "config.json").read_text())
    records = [
        json.loads(line) for line in (path / "metrics.jsonl").read_text().splitlines() if line
    ]
    completed = [r for r in records if r["kind"] == "done"]
    if len(completed) != 1 or completed[0]["step"] != config.get(
        "stop_steps", config["total_steps"]
    ):
        raise ValueError(f"Run is not complete: {path}")
    return config, records


def make_report(run, reference, out, job=None):
    from longevity.comparison import TRAINING_KEYS, read_spec

    run, reference, out = Path(run), Path(reference), Path(out)
    cfg, records = read_run(run)
    ref_cfg, ref_records = read_run(reference)
    spec = read_spec(run / "comparison.json")
    if cfg["comparison_sha256"] != spec["sha256"] or cfg["split"] != ref_cfg["split"]:
        raise ValueError("Comparison fingerprint or split mismatch")
    for key in [*TRAINING_KEYS, "max_len", "epochs", "mu", "sd", "model"]:
        if cfg[key] != ref_cfg[key]:
            raise ValueError(f"Unmatched reference setting: {key}")
    out.mkdir(parents=True, exist_ok=True)
    series = {"CDS": ref_records, "Random chunks": records}
    final, best = {}, {}
    for name, recs in series.items():
        final[name] = {
            split: [r for r in recs if r["kind"] == split][-1] for split in ("val", "test")
        }
        best[name] = min(
            (r for r in recs if r["kind"] == "val"), key=lambda r: r["species_mae_log10"]
        )
        for split in ("val", "test"):
            if {r["ncbi_taxid"] for r in final[name][split]["species"]} != set(cfg["split"][split]):
                raise ValueError(f"Missing evaluation species: {name}/{split}")
    summary = {
        "comparison_sha256": spec["sha256"],
        "budget": spec["budget"],
        "cohort": {
            "assemblies": len(spec["cohort"]),
            "species_per_split": {k: len(v) for k, v in cfg["split"].items()},
        },
        "final": final,
        "best_validation": {
            k: {
                field: v[field]
                for field in ("step", "epoch", "species_mae_log10", "species_spearman")
            }
            for k, v in best.items()
        },
        "training": {
            "CDS": {
                "steps": ref_cfg["total_steps"],
                "minutes": next(r["train_minutes"] for r in ref_records if r["kind"] == "done"),
            },
            "Random chunks": {
                "steps": cfg["total_steps"],
                "minutes": next(r["train_minutes"] for r in records if r["kind"] == "done"),
            },
        },
        "runtime": cfg["runtime"],
        "historical_software_match": "unverified",
        "limitations": spec["limitations"],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    shutil.copyfile(run / "config.json", out / "run_config.json")
    shutil.copyfile(run / "comparison.json", out / "comparison.json")
    shutil.copyfile(run / "metrics.jsonl", out / "metrics.jsonl")
    for split in ("val", "test"):
        import pandas as pd

        predictions = pd.read_parquet(run / f"predictions_{split}.parquet")
        species = predictions.groupby(["ncbi_taxid", "scientific_name"], as_index=False).agg(
            true_log10=("log10_longevity", "first"),
            pred_log10=("pred_log10", "mean"),
            n_chunks=("chunk_id", "size"),
        )
        species.to_csv(out / f"species_predictions_{split}.csv", index=False)

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"CDS": "#2a78d6", "Random chunks": "#eb6834"}
    fig, axes = plt.subplots(1, 3, figsize=(14, 4), dpi=150)
    for name, recs in series.items():
        train = [r for r in recs if r["kind"] == "train"]
        val = [r for r in recs if r["kind"] == "val"]
        axes[0].plot(
            [r["epoch"] for r in train],
            [r["loss"] for r in train],
            color=colors[name],
            label=name,
            alpha=0.75,
        )
        for axis, field in zip(axes[1:], ["species_mae_log10", "species_spearman"]):
            axis.plot(
                [r["epoch"] for r in val],
                [r[field] for r in val],
                color=colors[name],
                label=name,
                marker=".",
            )
    for axis, title, ylabel in zip(
        axes,
        ["Training loss", "Validation MAE", "Validation Spearman"],
        ["MSE (z units)", "MAE (log10 years)", "Spearman rho"],
    ):
        axis.set(title=title, xlabel="Epoch", ylabel=ylabel)
        axis.grid(alpha=0.2)
        axis.legend()
    fig.tight_layout()
    fig.savefig(out / "learning_curves.png")
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(10, 4), dpi=150)
    for axis, split in zip(axes, ("val", "test")):
        all_values = []
        for name in series:
            rows = final[name][split]["species"]
            true = np.array([10 ** r["true_log10"] for r in rows])
            pred = np.array([10 ** r["pred_log10"] for r in rows])
            axis.scatter(true, pred, label=name, color=colors[name], alpha=0.8)
            all_values.extend([*true, *pred])
        lo, hi = min(all_values) * 0.8, max(all_values) * 1.2
        axis.plot([lo, hi], [lo, hi], color="gray", linestyle="--")
        axis.set(
            xscale="log",
            yscale="log",
            xlim=(lo, hi),
            ylim=(lo, hi),
            title=f"{split}: final model",
            xlabel="True longevity (years)",
            ylabel="Predicted longevity (years)",
        )
        axis.legend()
        axis.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(out / "predictions.png")
    plt.close(fig)

    table = ["| Split / metric | CDS | Random chunks |", "| --- | ---: | ---: |"]
    for split in ("val", "test"):
        for field, label in [
            ("species_mae_log10", "MAE (log10 years)"),
            ("species_spearman", "Spearman"),
            ("species_pearson", "Pearson"),
            ("baseline_species_mae_log10", "Train-mean baseline MAE"),
        ]:
            table.append(
                f"| {split} {label} | {final['CDS'][split][field]:.4f} | "
                f"{final['Random chunks'][split][field]:.4f} |"
            )
    cds_mae = final["CDS"]["test"]["species_mae_log10"]
    chunk_mae = final["Random chunks"]["test"]["species_mae_log10"]
    direction = "lower" if chunk_mae < cds_mae else "higher"
    pct = abs(chunk_mae / cds_mae - 1) * 100
    job = Path(job) if job else None
    if job:
        for filename in (
            "environment.txt",
            "prepare_environment.txt",
            "hardware.json",
            "source_commit.txt",
            "preparation_code_sha256.json",
            "snapshot.json",
            "input_audit.json",
            "run_provenance.json",
        ):
            if (job / filename).exists():
                shutil.copyfile(job / filename, out / filename)
    minutes = summary["training"]["Random chunks"]["minutes"]
    text = f"""# Random genome chunks versus CDS longevity prediction

The matched random-chunk experiment completed **18 epochs / {cfg["total_steps"]:,} updates**
in {minutes:.1f} training minutes. Final test species MAE was **{chunk_mae:.4f} log10 years**,
{pct:.1f}% {direction} than the CDS reference ({cds_mae:.4f}). This is a single split/seed
comparison; it does not establish statistical significance or isolate compute effects.

## Final held-out results

{chr(10).join(table)}

Metrics use the final checkpoint for both models. Predictions are averaged over input
examples within each species, and metrics weight species equally. The test set contains
18 species; validation contains 16. Best validation scores are reported in
[summary.json](summary.json), without selecting a checkpoint using test results.

![Learning curves](learning_curves.png)

![Final species predictions](predictions.png)

## Controlled protocol

- Same 98 versioned assemblies, labels and training-species normalization as `longevity-anage100`.
- Exact saved 64/16/18 train/validation/test species assignments.
- 1,000 stored chunks per genome: 98,000 examples, each CLS + 1,022 DNA BPE tokens + SEP.
- Same ModernGENA ModernBERT encoder, CLS regression head, optimizer, learning rates,
  token budgets, attention backend, seed and 18 epochs. Same warmup fraction and cosine schedule.
- Genomes passed checksum verification. Historical AnAge taxonomy was frozen explicitly:
  newer NCBI metadata differs in two class and seven family annotations.
- Random chunks were sampled once with seed 0, then loaded unchanged from H5 for every epoch.

The chunk run uses 36,000 updates versus 5,040 for CDS. Different example counts,
sequence lengths and training-example species weights are inherent input differences.
This is **epoch matched, not compute matched**. The wall-clock cutoff was disabled to
complete all 18 epochs. No claim of improvement due solely to genomic location is warranted.

## Execution and reproducibility

Training ran on one NVIDIA RTX PRO 6000 Blackwell Server Edition (96 GB VRAM).
The actual environment is recorded in [environment.txt](environment.txt) and
[run_config.json](run_config.json). The verified training stack uses Torch 2.12.0,
Transformers 5.18.0 and `kernels` 0.17.2; the repository's general `model` extra alone
does not reproduce it. Transformers 5.18 matches the version stated in the CDS report.
**Historical full-software/pretrained-revision equivalence remains unverified**, as agreed.

The frozen [comparison specification](comparison.json), [input audit](input_audit.json),
[snapshot provenance](snapshot.json), [training metrics](metrics.jsonl), and per-species
[val](species_predictions_val.csv) / [test](species_predictions_test.csv) predictions
are included. Full H5 inputs and model weights remain on the shared disk under
`genes.jpg/datasets/longevity-anage100-chunks/` and
`genes.jpg/runs/longevity-anage100-chunks/model.pt`; large artifacts are not committed.

See [setup and reproduction commands](../longevity-chunks.md).
"""
    (out / "README.md").write_text(text)
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--reference", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--job", type=Path)
    args = ap.parse_args()
    make_report(args.run, args.reference, args.out, args.job)


if __name__ == "__main__":
    main()
