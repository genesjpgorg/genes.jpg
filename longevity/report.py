"""Figures and summary numbers for a longevity.train run (used by docs/longevity-pilot).

Writes into --out: loss.png, val_mae.png, val_spearman.png, pred_vs_true.png and
summary.json (data description, taxonomy baselines, final metrics).

Usage:
  python -m longevity.report --run /mnt/.../runs/longevity-anage100 --out docs/longevity-pilot
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

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
BLUE, ORANGE = "#2a78d6", "#eb6834"


def style(ax, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    if xlabel == "epoch":
        ax.xaxis.set_major_locator(matplotlib.ticker.MultipleLocator(2))
    ax.set_title(title, loc="left", color=INK, fontsize=11)
    ax.set_xlabel(xlabel, color=INK2)
    ax.set_ylabel(ylabel, color=INK2)
    ax.grid(True, color=GRID, linewidth=1)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2)


def fig() -> tuple[plt.Figure, plt.Axes]:
    f, ax = plt.subplots(figsize=(7, 3.6), dpi=150)
    f.patch.set_facecolor(SURFACE)
    return f, ax


def ref_line(ax, y: float, label: str) -> None:
    ax.axhline(y, color=INK2, linewidth=1)
    ax.annotate(label, (1, y), xycoords=("axes fraction", "data"), xytext=(-4, 4),
                textcoords="offset points", ha="right", color=INK2, fontsize=8)


def taxonomy_baselines(df: pd.DataFrame, split: dict, mu: float) -> dict:
    """Species MAE (log10 yrs) of predicting the train mean / train mean of same order or family."""
    sp = df.groupby("ncbi_taxid").agg(y=("log10_longevity", "first"), order=("order", "first"),
                                      family=("family", "first"))
    tr = sp.loc[split["train"]]
    out = {}
    for name in ("val", "test"):
        s = sp.loc[split[name]]
        res = {"train_mean": float((s.y - mu).abs().mean())}
        for rank in ("order", "family"):
            m = tr.groupby(rank).y.mean()
            pred = s[rank].map(m).fillna(mu)
            res[f"same_{rank}_mean"] = float((s.y - pred).abs().mean())
            res[f"n_with_{rank}_in_train"] = int(s[rank].isin(m.index).sum())
        out[name] = res
    return out


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
    test = [r for r in recs if r["kind"] == "test"][-1]
    spe = cfg["steps_per_epoch"]

    # 1. training loss
    f, ax = fig()
    ep = train.step / spe
    ax.plot(ep, train.loss, color=BLUE, linewidth=1, alpha=0.35)
    smooth = train.loss.rolling(5, center=True, min_periods=1).mean()
    ax.plot(ep, smooth, color=BLUE, linewidth=2, label="train loss (5-point mean)")
    ref_line(ax, 1.0, "predict train mean = 1.0")
    style(ax, "Training loss (MSE on z-scored log10 longevity)", "epoch", "MSE (z units)")
    ax.legend(frameon=False, loc="lower left", labelcolor=INK2)
    f.tight_layout()
    f.savefig(a.out / "loss.png", facecolor=SURFACE)

    # 2. val MAE + final test MAE
    base_val = val.baseline_species_mae_log10.iloc[-1]
    f, ax = fig()
    ax.plot(val.epoch, val.species_mae_log10, color=BLUE, linewidth=2, marker="o", markersize=4,
            markeredgecolor=SURFACE, markeredgewidth=1.5, label="val (16 species)")
    ref_line(ax, base_val, f"val baseline (train mean) {base_val:.3f}")
    ax.plot([val.epoch.iloc[-1]], [test["species_mae_log10"]], marker="D", markersize=7,
            color=ORANGE, markeredgecolor=SURFACE, markeredgewidth=1.5, linestyle="none",
            label=f"test, final model {test['species_mae_log10']:.3f} "
                  f"(baseline {test['baseline_species_mae_log10']:.3f})")
    style(ax, "Species-level MAE, log10 years (lower is better)", "epoch", "MAE (log10 yrs)")
    ax.legend(frameon=False, loc="upper right", labelcolor=INK2, fontsize=8)
    f.tight_layout()
    f.savefig(a.out / "val_mae.png", facecolor=SURFACE)

    # 3. val Spearman + final test
    f, ax = fig()
    ax.plot(val.epoch, val.species_spearman, color=BLUE, linewidth=2, marker="o", markersize=4,
            markeredgecolor=SURFACE, markeredgewidth=1.5, label="val (16 species)")
    ref_line(ax, 0.0, "no correlation")
    ax.plot([val.epoch.iloc[-1]], [test["species_spearman"]], marker="D", markersize=7,
            color=ORANGE, markeredgecolor=SURFACE, markeredgewidth=1.5, linestyle="none",
            label=f"test, final model {test['species_spearman']:.2f}")
    style(ax, "Species-level Spearman ρ (higher is better)", "epoch", "Spearman ρ")
    ax.legend(frameon=False, loc="lower right", labelcolor=INK2, fontsize=8)
    f.tight_layout()
    f.savefig(a.out / "val_spearman.png", facecolor=SURFACE)

    # 4. predicted vs true, final model
    f, ax = plt.subplots(figsize=(5.2, 5.2), dpi=150)
    f.patch.set_facecolor(SURFACE)
    last_val = val.iloc[-1]["species"]
    for name, rows, color, mk in (("val", last_val, BLUE, "o"), ("test", test["species"], ORANGE, "D")):
        t = np.array([10 ** r["true_log10"] for r in rows])
        p = np.array([10 ** r["pred_log10"] for r in rows])
        ax.scatter(t, p, s=40, color=color, marker=mk, edgecolors=SURFACE, linewidths=1.5,
                   label=f"{name} ({len(rows)} species)", zorder=3)
    lim = (2.5, 80)
    ax.plot(lim, lim, color=INK2, linewidth=1)
    ax.annotate("perfect prediction", (45, 45), xytext=(4, -10), textcoords="offset points",
                ha="left", color=INK2, fontsize=8)
    ax.axhline(10 ** cfg["mu"], color=INK2, linewidth=1, linestyle=(0, (1, 2)))
    ax.annotate(f"train mean {10 ** cfg['mu']:.1f} yrs", (2.7, 10 ** cfg["mu"]), xytext=(0, 4),
                textcoords="offset points", color=INK2, fontsize=8)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(lim)
    ax.set_ylim(lim)
    ticks = [3, 5, 10, 20, 50]
    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_locator(matplotlib.ticker.FixedLocator(ticks))
        axis.set_major_formatter(matplotlib.ticker.FixedFormatter([str(t) for t in ticks]))
        axis.set_minor_locator(matplotlib.ticker.NullLocator())
    style(ax, "Final model: predicted vs true max longevity", "true (years)", "predicted (years)")
    ax.legend(frameon=False, loc="upper left", labelcolor=INK2)
    f.tight_layout()
    f.savefig(a.out / "pred_vs_true.png", facecolor=SURFACE)

    # summary numbers
    df = pd.read_parquet(cfg["data"], columns=["ncbi_taxid", "scientific_name", "class", "order",
                                                "family", "entrez_id", "identity", "n_tokens",
                                                "cds_len", "max_longevity_yrs",
                                                "log10_longevity"])
    used = set(sum(cfg["split"].values(), []))
    d = df[df.ncbi_taxid.isin(used)]
    sp = d.groupby("ncbi_taxid").agg(cls=("class", "first"), order=("order", "first"),
                                     family=("family", "first"), genes=("entrez_id", "size"),
                                     yrs=("max_longevity_yrs", "first"))
    split_tab = {}
    for k, ids in cfg["split"].items():
        s = sp.loc[ids]
        split_tab[k] = dict(species=len(s), pairs=int(d.ncbi_taxid.isin(ids).sum()),
                            families=int(s.family.nunique()),
                            top_families=s.family.value_counts().head(3).to_dict(),
                            median_yrs=float(s.yrs.median()),
                            min_yrs=float(s.yrs.min()), max_yrs=float(s.yrs.max()))
    best = val.loc[val.species_mae_log10.idxmin()]
    summary = dict(
        data=dict(pairs=len(d), species=len(sp), families=int(sp.family.nunique()),
                  orders=int(sp.order.nunique()), classes=sp.cls.value_counts().to_dict(),
                  genes_per_species_median=float(sp.genes.median()),
                  cds_bp_median=float(d.cds_len.median()), tokens_median=float(d.n_tokens.median()),
                  identity_median=float(d.identity.median()),
                  longevity_yrs=dict(min=float(sp.yrs.min()), median=float(sp.yrs.median()),
                                     max=float(sp.yrs.max()))),
        split=split_tab,
        baselines=taxonomy_baselines(d, cfg["split"], cfg["mu"]),
        final=dict(val={k: last for k, last in val.iloc[-1].items()
                        if k.startswith(("species_", "pair_", "baseline")) and k != "species"},
                   test={k: v for k, v in test.items()
                         if k.startswith(("species_", "pair_", "baseline")) and k != "species"}),
        best_val=dict(epoch=float(best.epoch), species_mae_log10=float(best.species_mae_log10),
                      species_spearman=float(best.species_spearman)),
        train=dict(first_loss=float(train.loss.iloc[0]), last_loss=float(train.loss.iloc[-1]),
                   tok_per_s_median=float(train.tok_per_s.median()),
                   minutes=float(train.elapsed_min.iloc[-1])),
        test_species=test["species"],
    )
    (a.out / "summary.json").write_text(json.dumps(summary, indent=2, default=float))
    print(json.dumps({k: v for k, v in summary.items() if k != "test_species"}, indent=1,
                     default=float))


if __name__ == "__main__":
    main()
