"""Figures and summary numbers for a full-dataset longevity.train run (docs/longevity-full).

Unlike report.py (pilot), validation here is step-based on a fixed per-species gene sample,
the final model is best.pt evaluated on all genes (kinds val_full / test), and the summary adds
taxonomy baselines and a per-class breakdown.

Writes into --out: loss.png, val_mae.png, val_spearman.png, pred_vs_true.png, summary.json.

Usage:
  python -m longevity.report_full --run /mnt/.../runs/longevity-agingatlas-all-genomic-10ep \
      --out docs/longevity-full
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from longevity.report import BLUE, GRID, INK, INK2, ORANGE, SURFACE, fig, ref_line, style  # noqa: E402

AQUA, OTHER = "#1baf7a", "#a3a29e"
CLASS_COLORS = {"Mammalia": ("mammals", BLUE), "Aves": ("birds", ORANGE),
                "Actinopteri": ("ray-finned fish", AQUA)}


def spearman(a: pd.Series, b: pd.Series) -> float:
    return float(np.corrcoef(a.rank(), b.rank())[0, 1]) if len(a) > 2 and a.std() > 0 and b.std() > 0 else float("nan")


def species_table(run: Path, data: str) -> tuple[dict, pd.DataFrame]:
    """Per-species predictions for val_full/test joined with taxonomy and taxonomy baselines."""
    d = pd.read_parquet(data, columns=["ncbi_taxid", "scientific_name", "class", "order", "family",
                                       "split", "log10_longevity", "max_longevity_yrs"])
    sp = d.groupby("ncbi_taxid").agg(name=("scientific_name", "first"), cls=("class", "first"),
                                     order=("order", "first"), family=("family", "first"),
                                     split=("split", "first"), y=("log10_longevity", "first"),
                                     yrs=("max_longevity_yrs", "first"))
    for c in ("cls", "order", "family"):
        sp[c] = sp[c].fillna("NA")
    tr = sp[sp.split == "train"]
    mu = tr.y.mean()
    cmean, omean = tr.groupby("cls").y.mean(), tr.groupby("order").y.mean()
    out, frames = {}, []
    for split, f in (("val", "predictions_val_full.parquet"), ("test", "predictions_test.parquet")):
        p = pd.read_parquet(run / f)
        s = (p.groupby("ncbi_taxid").agg(pred=("pred_log10", "mean"), n_genes=("entrez_id", "size"))
              .join(sp))
        s["base_train_mean"] = mu
        s["base_class_mean"] = s.cls.map(cmean).fillna(mu)
        s["base_order_mean"] = s.order.map(omean).fillna(s.base_class_mean)
        res = dict(n_species=len(s), n_orders_in_train=int(s.order.isin(omean.index).sum()))
        for col, key in (("pred", "model"), ("base_train_mean", "train_mean"),
                         ("base_class_mean", "class_mean"), ("base_order_mean", "order_mean")):
            res[key] = dict(mae=float((s[col] - s.y).abs().mean()), spearman=spearman(s[col], s.y),
                            pearson=float(np.corrcoef(s[col], s.y)[0, 1]) if s[col].std() > 0 else float("nan"))
        classes = {}
        for c, g in s.groupby("cls"):
            classes[c] = dict(n=len(g), model_mae=float((g.pred - g.y).abs().mean()),
                              class_mean_mae=float((g.base_class_mean - g.y).abs().mean()),
                              order_mean_mae=float((g.base_order_mean - g.y).abs().mean()),
                              spearman_within=spearman(g.pred, g.y),
                              order_mean_spearman_within=spearman(g.base_order_mean, g.y),
                              true_sd=float(g.y.std()), pred_sd=float(g.pred.std()))
        res["by_class"] = classes
        out[split] = res
        frames.append(s.assign(split=split))
    return out, pd.concat(frames)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((a.run / "config.json").read_text())
    recs = [json.loads(x) for x in (a.run / "metrics.jsonl").read_text().splitlines() if x]
    train = pd.DataFrame([r for r in recs if r["kind"] == "train"])
    val = pd.DataFrame([r for r in recs if r["kind"] == "val"])
    vfull = [r for r in recs if r["kind"] == "val_full"][-1]
    test = [r for r in recs if r["kind"] == "test"][-1]
    spe = cfg["steps_per_epoch"]
    best_ep = vfull["epoch"]
    tab, species = species_table(a.run, cfg["data"])
    n_sub = cfg.get("eval_genes_per_species")

    # 1. training loss
    f, ax = fig()
    ep = train.step / spe
    ax.plot(ep, train.loss, color=BLUE, linewidth=1, alpha=0.3)
    ax.plot(ep, train.loss.rolling(9, center=True, min_periods=1).mean(), color=BLUE, linewidth=2,
            label="train loss (9-point mean)")
    ref_line(ax, 1.0, "predict train mean = 1.0")
    ax.axvline(best_ep, color=INK2, linewidth=1, linestyle=(0, (1, 2)))
    ax.annotate(f"best val checkpoint (epoch {best_ep:.1f})", (best_ep, 0.8), xytext=(4, 0),
                textcoords="offset points", color=INK2, fontsize=8)
    style(ax, "Training loss (MSE on z-scored log10 longevity)", "epoch", "MSE (z units)")
    ax.legend(frameon=False, loc="lower left", labelcolor=INK2)
    f.tight_layout()
    f.savefig(a.out / "loss.png", facecolor=SURFACE)

    # 2. val MAE (gene sample, every 2k steps) + final full val/test with best.pt
    f, ax = fig()
    ax.plot(val.epoch, val.species_mae_log10, color=BLUE, linewidth=2, marker="o", markersize=3,
            markeredgecolor=SURFACE, markeredgewidth=1,
            label=f"val, {n_sub} genes/species ({int(val.n_species.iloc[-1])} species)")
    ref_line(ax, tab["val"]["train_mean"]["mae"], f"predict train mean {tab['val']['train_mean']['mae']:.3f}")
    ref_line(ax, tab["val"]["order_mean"]["mae"], f"mean of same order {tab['val']['order_mean']['mae']:.3f}")
    ax.plot([best_ep], [vfull["species_mae_log10"]], marker="o", markersize=8, color=BLUE,
            markeredgecolor=INK, markeredgewidth=1, linestyle="none",
            label=f"val, all genes, best.pt {vfull['species_mae_log10']:.3f}")
    ax.plot([best_ep], [test["species_mae_log10"]], marker="D", markersize=7, color=ORANGE,
            markeredgecolor=SURFACE, markeredgewidth=1.5, linestyle="none",
            label=f"test, all genes, best.pt {test['species_mae_log10']:.3f}")
    style(ax, "Species-level MAE, log10 years (lower is better)", "epoch", "MAE (log10 yrs)")
    ax.legend(frameon=False, loc="upper right", bbox_to_anchor=(1, 0.6), labelcolor=INK2, fontsize=8)
    f.tight_layout()
    f.savefig(a.out / "val_mae.png", facecolor=SURFACE)

    # 3. val Spearman
    f, ax = fig()
    ax.plot(val.epoch, val.species_spearman, color=BLUE, linewidth=2, marker="o", markersize=3,
            markeredgecolor=SURFACE, markeredgewidth=1, label=f"val, {n_sub} genes/species")
    ref_line(ax, tab["val"]["order_mean"]["spearman"],
             f"mean of same order {tab['val']['order_mean']['spearman']:.2f}")
    ax.plot([best_ep], [vfull["species_spearman"]], marker="o", markersize=8, color=BLUE,
            markeredgecolor=INK, markeredgewidth=1, linestyle="none",
            label=f"val, all genes {vfull['species_spearman']:.2f}")
    ax.plot([best_ep], [test["species_spearman"]], marker="D", markersize=7, color=ORANGE,
            markeredgecolor=SURFACE, markeredgewidth=1.5, linestyle="none",
            label=f"test, all genes {test['species_spearman']:.2f}")
    style(ax, "Species-level Spearman ρ (higher is better)", "epoch", "Spearman ρ")
    ax.legend(frameon=False, loc="lower right", labelcolor=INK2, fontsize=8)
    f.tight_layout()
    f.savefig(a.out / "val_spearman.png", facecolor=SURFACE)

    # 4. test: predicted vs true by class
    t = species[species.split == "test"]
    f, ax = plt.subplots(figsize=(5.6, 5.6), dpi=150)
    f.patch.set_facecolor(SURFACE)
    other = t[~t.cls.isin(CLASS_COLORS)]
    ax.scatter(10 ** other.y, 10 ** other.pred, s=22, color=OTHER, edgecolors=SURFACE, linewidths=1,
               label=f"other classes ({len(other)})", zorder=2)
    for c, (label, color) in CLASS_COLORS.items():
        g = t[t.cls == c]
        ax.scatter(10 ** g.y, 10 ** g.pred, s=26, color=color, edgecolors=SURFACE, linewidths=1,
                   label=f"{label} ({len(g)}, ρ {tab['test']['by_class'][c]['spearman_within']:.2f})",
                   zorder=3)
    lim = (0.8, 400)
    ax.plot(lim, lim, color=INK2, linewidth=1)
    ax.annotate("perfect prediction", (150, 150), xytext=(4, -12), textcoords="offset points",
                color=INK2, fontsize=8)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(lim)
    ax.set_ylim(lim)
    ticks = [1, 3, 10, 30, 100, 300]
    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_locator(matplotlib.ticker.FixedLocator(ticks))
        axis.set_major_formatter(matplotlib.ticker.FixedFormatter([str(x) for x in ticks]))
        axis.set_minor_locator(matplotlib.ticker.NullLocator())
    style(ax, f"Test species (n={len(t)}): predicted vs true max longevity", "true (years)",
          "predicted (years)")
    ax.legend(frameon=False, loc="upper left", labelcolor=INK2, fontsize=8)
    f.tight_layout()
    f.savefig(a.out / "pred_vs_true.png", facecolor=SURFACE)

    best = val.loc[val.species_mae_log10.idxmin()]
    summary = dict(
        run=str(a.run), data=cfg["data"], steps_per_epoch=spe, total_steps=cfg["total_steps"],
        best_step=int(vfull["step"]), best_epoch=best_ep,
        best_val_sample=dict(step=int(best.step), species_mae_log10=float(best.species_mae_log10),
                             species_spearman=float(best.species_spearman)),
        final=dict(val_full={k: v for k, v in vfull.items() if k != "species"},
                   test={k: v for k, v in test.items() if k != "species"}),
        species_level=tab,
        train=dict(first_loss=float(train.loss.iloc[0]), last_loss=float(train.loss.iloc[-1]),
                   loss_at_best=float(train.loc[(train.step - vfull["step"]).abs().idxmin(), "loss"]),
                   tok_per_s_median=float(train.tok_per_s.median()),
                   hours=float(train.elapsed_min.iloc[-1] / 60)),
    )
    (a.out / "summary.json").write_text(json.dumps(summary, indent=1, default=float))
    species.reset_index().to_csv(a.out / "species_predictions.csv", index=False, float_format="%.4f")
    print(json.dumps({k: v for k, v in summary.items() if k != "species_level"}, indent=1, default=float))


if __name__ == "__main__":
    main()
