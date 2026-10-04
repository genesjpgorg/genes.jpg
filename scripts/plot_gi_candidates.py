"""Reproduce the six GI candidate scatterplots from the committed compact tables."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats


def main():
    root = Path(__file__).resolve().parents[1]
    out = root / "docs/gi-longevity-candidates"
    points = pd.read_csv(out / "species_expression.csv")
    candidates = pd.read_csv(out / "candidates.csv")
    orders = sorted(points.anage_order.unique())
    colors = dict(zip(orders, plt.get_cmap("tab20")(np.linspace(0, 1, len(orders))), strict=True))
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(2, 3, figsize=(16, 10), layout="constrained")
    for ax, candidate in zip(axes.flat, candidates.itertuples(), strict=True):
        data = points[points.human_gene_id == candidate.human_gene_id]
        rho = stats.spearmanr(data.max_longevity_yrs, data.expression_log_tpm).statistic
        np.testing.assert_allclose(rho, candidate.spearman_rho, atol=1e-12)
        for order, group in data.groupby("anage_order"):
            ax.scatter(
                group.max_longevity_yrs,
                group.expression_log_tpm,
                color=colors[order],
                s=38,
                alpha=0.9,
                edgecolor="white",
                linewidth=0.4,
            )
        fit = stats.linregress(np.log10(data.max_longevity_yrs), data.expression_log_tpm)
        xx = np.geomspace(data.max_longevity_yrs.min(), data.max_longevity_yrs.max(), 100)
        ax.plot(xx, fit.intercept + fit.slope * np.log10(xx), "--", color="#444444", lw=1.2)
        labels = {
            "homo_sapiens": "Human",
            "mus_musculus": "Mouse",
            "myotis_lucifugus": "Little brown bat",
            "heterocephalus_glaber_female": "Naked mole-rat",
        }
        for species, label in labels.items():
            matched = data[data.ensembl_species == species]
            if len(matched):
                r = matched.iloc[0]
                ax.annotate(
                    label,
                    (r.max_longevity_yrs, r.expression_log_tpm),
                    xytext=(-4 if r.max_longevity_yrs > 80 else 4, 5),
                    textcoords="offset points",
                    ha="right" if r.max_longevity_yrs > 80 else "left",
                    fontsize=7,
                    color="#333333",
                )
        ax.set(
            xscale="log",
            xlabel="Recorded maximum lifespan (years; log scale)",
            ylabel="GI-predicted expression, log(TPM + 1)",
            title=f"{candidate.gene_name}  |  {len(data)} species\nSpearman rho = {rho:+.3f}  |  BH q = {candidate.spearman_q_bh_planned:.4f}",
        )
        ax.grid(alpha=0.15)
    handles = [
        plt.Line2D([], [], marker="o", linestyle="", color=colors[o], label=o) for o in orders
    ]
    fig.legend(handles=handles, loc="outside lower center", ncol=7, fontsize=8)
    fig.suptitle(
        "Maximum lifespan and predicted hepatocyte expression\nSix leading Spearman candidates; no ancestry or body-mass adjustment",
        fontsize=15,
    )
    for extension in ["png", "svg", "pdf"]:
        fig.savefig(out / f"lifespan_expression.{extension}", dpi=200)
    plt.close(fig)


if __name__ == "__main__":
    main()
