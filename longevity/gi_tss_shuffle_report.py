"""Compare native and TSS-shuffled GI expression on identical species per gene."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from longevity.gi_prepare import digest, write_json


def bh(pvalues):
    p = np.asarray(pvalues, dtype=float)
    order = np.argsort(p)
    q = np.minimum.accumulate((p[order] * len(p) / np.arange(1, len(p) + 1))[::-1])[::-1]
    result = np.empty_like(q)
    result[order] = np.minimum(q, 1)
    return result


def permutation_p(y, life, seed, count=999999):
    y, life = stats.rankdata(y), stats.rankdata(life)
    y, life = y - y.mean(), life - life.mean()
    if np.linalg.norm(y) == 0:
        return 0.0, 1.0
    y, life = y / np.linalg.norm(y), life / np.linalg.norm(life)
    rho = float(y @ life)
    rng = np.random.default_rng(seed)
    extreme = 0
    for offset in range(0, count, 5000):
        permutations = rng.permuted(
            np.broadcast_to(life, (min(5000, count - offset), len(life))), axis=1
        )
        extreme += int((np.abs(permutations @ y) >= abs(rho) - 1e-12).sum())
    return rho, (extreme + 1) / (count + 1)


def row_rho(x, y):
    x, y = stats.rankdata(x, axis=1), stats.rankdata(y, axis=1)
    x, y = x - x.mean(axis=1, keepdims=True), y - y.mean(axis=1, keepdims=True)
    norm = np.sqrt((x * x).sum(axis=1) * (y * y).sum(axis=1))
    return np.divide((x * y).sum(axis=1), norm, out=np.full(len(x), np.nan), where=norm > 0)


def summarize_gene(frame, candidate, replicates, permutations=999999):
    native = frame[frame.condition == "native"].set_index("ensembl_species").sort_index()
    shuffled = (
        frame[frame.condition == "shuffled"]
        .pivot(index="ensembl_species", columns="replicate", values="expression_log_tpm")
        .sort_index()
    )
    if (
        list(shuffled.index) != list(native.index)
        or set(shuffled.columns) != set(range(replicates))
        or shuffled.isna().any().any()
    ):
        raise ValueError("Native/shuffled species or replicates differ")
    if len(native) != candidate.n_species:
        raise ValueError("Gene species count changed from candidate selection")
    life = native.max_longevity_yrs.to_numpy()
    y = native.expression_log_tpm.to_numpy()
    mean = shuffled.mean(axis=1).to_numpy()
    original = native.original_expression_log_tpm.to_numpy()
    native_rho, native_p = permutation_p(y, life, 3101, permutations)
    mean_rho, mean_p = permutation_p(mean, life, 3102, permutations)
    replicates_table = []
    for replicate in range(replicates):
        rho = stats.spearmanr(shuffled[replicate], life).statistic
        replicates_table.append(
            {
                "human_gene_id": candidate.human_gene_id,
                "gene_name": candidate.gene_name,
                "replicate": replicate,
                "rho": float(rho) if np.isfinite(rho) else 0.0,
            }
        )
    rs = np.array([r["rho"] for r in replicates_table])
    direction = float(np.sign(candidate.spearman_rho))
    # Paired species bootstrap: the two expression values and lifespan travel together.
    rng = np.random.default_rng(8192)
    indices = rng.integers(0, len(life), size=(20000, len(life)))
    delta = direction * (row_rho(y[indices], life[indices]) - row_rho(mean[indices], life[indices]))
    lower, upper = np.nanquantile(delta, [0.025, 0.975])
    table = {
        "human_gene_id": candidate.human_gene_id,
        "gene_name": candidate.gene_name,
        "n_species": len(native),
        "shuffle_replicates": replicates,
        "original_rho": float(candidate.spearman_rho),
        "native_rho": native_rho,
        "native_permutation_p": native_p,
        "shuffled_mean_rho": mean_rho,
        "shuffled_mean_permutation_p": mean_p,
        "shuffle_rho_median": float(np.median(rs)),
        "shuffle_rho_min": float(rs.min()),
        "shuffle_rho_max": float(rs.max()),
        "replicates_same_direction_as_original": int((np.sign(rs) == direction).sum()),
        "signed_rho_attenuation": direction * (native_rho - mean_rho),
        "attenuation_bootstrap_ci_low": float(lower),
        "attenuation_bootstrap_ci_high": float(upper),
        "native_retest_max_abs_difference": float(np.max(np.abs(y - original))),
        "mean_log_expression_change": float(np.mean(mean - y)),
        "native_shuffle_mean_rho": float(stats.spearmanr(y, mean).statistic),
    }
    species = native.reset_index()[
        [
            "human_gene_id",
            "gene_id",
            "ensembl_species",
            "max_longevity_yrs",
            "adult_weight_g",
            "anage_order",
            "original_expression_log_tpm",
        ]
    ].copy()
    species["gene_name"] = candidate.gene_name
    species["native_expression_log_tpm"] = y
    species["shuffled_mean_expression_log_tpm"] = mean
    species["shuffled_min_expression_log_tpm"] = shuffled.min(axis=1).to_numpy()
    species["shuffled_max_expression_log_tpm"] = shuffled.max(axis=1).to_numpy()
    species["shuffled_sd_expression_log_tpm"] = shuffled.std(axis=1).to_numpy()
    return table, replicates_table, species


def figures(directory, summary, species, replicate_rhos):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(2, 3, figsize=(16, 10), layout="constrained")
    for ax in axes.flat:
        ax.set_visible(False)
    for ax, row in zip(axes.flat, summary.itertuples(), strict=False):
        ax.set_visible(True)
        d = species[species.human_gene_id == row.human_gene_id]
        xx = d.max_longevity_yrs.to_numpy()
        y = d.native_expression_log_tpm.to_numpy()
        z = d.shuffled_mean_expression_log_tpm.to_numpy()
        ax.scatter(xx, y, color="#0072B2", s=33, alpha=0.85, label="Native, freshly predicted")
        ax.errorbar(
            xx,
            z,
            yerr=np.maximum(
                np.vstack(
                    [z - d.shuffled_min_expression_log_tpm, d.shuffled_max_expression_log_tpm - z]
                ),
                0,
            ),
            fmt="o",
            color="#D55E00",
            markersize=3,
            alpha=0.6,
            elinewidth=0.6,
            label="Shuffled mean and min–max",
        )
        grid = np.geomspace(xx.min(), xx.max(), 100)
        for values, color in [(y, "#0072B2"), (z, "#D55E00")]:
            fit = stats.linregress(np.log10(xx), values)
            ax.plot(grid, fit.intercept + fit.slope * np.log10(grid), color=color, lw=1.2)
        ax.set(
            xscale="log",
            xlabel="Recorded maximum lifespan (years)",
            ylabel="GI-predicted log(TPM + 1)",
            title=f"{row.gene_name} · {row.n_species} species\nNative rho {row.native_rho:+.3f}; shuffled mean rho {row.shuffled_mean_rho:+.3f}",
        )
        ax.legend(fontsize=7)
    fig.suptitle(
        "Effect of shuffling 8-bp blocks within ±4,096 bp of the TSS\nIdentical GI context; ten shuffles per species; no ancestry or body-mass adjustment",
        fontsize=14,
    )
    for ext in ["png", "svg", "pdf"]:
        fig.savefig(directory / f"native_vs_shuffled.{ext}", dpi=180)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(10, 5.5), layout="constrained")
    for i, row in enumerate(summary.itertuples()):
        rs = replicate_rhos[replicate_rhos.human_gene_id == row.human_gene_id].rho
        ax.scatter(
            i + np.linspace(-0.13, 0.13, len(rs)),
            rs,
            color="#D55E00",
            s=25,
            alpha=0.7,
            label="Individual shuffle replicates" if i == 0 else None,
        )
        ax.scatter(
            i, row.native_rho, color="#0072B2", marker="D", s=65, label="Native" if i == 0 else None
        )
        ax.scatter(
            i,
            row.shuffled_mean_rho,
            edgecolor="#222222",
            facecolor="none",
            s=95,
            label="Mean shuffled expression" if i == 0 else None,
        )
    ax.axhline(0, color="#aaaaaa", lw=0.8)
    ax.set(
        xticks=np.arange(len(summary)),
        xticklabels=summary.gene_name,
        ylabel="Spearman correlation with lifespan",
        ylim=(-1, 1),
        title="Correlation before and after TSS block shuffling",
    )
    ax.legend(fontsize=9)
    for ext in ["png", "svg", "pdf"]:
        fig.savefig(directory / f"correlations.{ext}", dpi=180)
    plt.close(fig)
    for svg in directory.glob("*.svg"):
        svg.write_text("\n".join(line.rstrip() for line in svg.read_text().splitlines()) + "\n")


def run(args):
    out = Path(args.output).resolve()
    protocol = json.loads((out / "protocol.json").read_text())
    candidates = pd.read_csv(out / "candidates.csv")
    complete = [
        (row, out / "genes" / f"{row.human_gene_id}.parquet")
        for row in candidates.itertuples()
        if (out / "genes" / f"{row.human_gene_id}.parquet").exists()
    ]
    if not complete or (len(complete) != len(candidates) and not args.partial):
        raise ValueError("Experiment incomplete; use --partial for completed genes only")
    directory = out / ("analysis" if len(complete) == len(candidates) else "partial_analysis")
    directory.mkdir(exist_ok=True)
    rows, replicates, observations, raw = [], [], [], []
    for candidate, path in complete:
        frame = pd.read_parquet(path)
        row, rs, obs = summarize_gene(frame, candidate, protocol["replicates"], args.permutations)
        rows.append(row)
        replicates.extend(rs)
        observations.append(obs)
        raw.append(frame)
    summary = pd.DataFrame(rows)
    # Correct across all six selected candidates, also in partial reports.
    for assay in ["native", "shuffled_mean"]:
        p = np.r_[summary[assay + "_permutation_p"], np.ones(len(candidates) - len(summary))]
        summary[assay + "_q_bh_six"] = bh(p)[: len(summary)]
    species = pd.concat(observations, ignore_index=True)
    replicates = pd.DataFrame(replicates)
    summary.to_csv(directory / "gene_summary.csv", index=False)
    species.to_csv(directory / "species_comparison.csv", index=False)
    replicates.to_csv(directory / "replicate_correlations.csv", index=False)
    # Compact public table contains all numeric predictions, no DNA or API credentials.
    pd.concat(raw, ignore_index=True)[
        [
            "human_gene_id",
            "ensembl_species",
            "condition",
            "replicate",
            "sequence_sha256",
            "request_hash",
            "expression_log_tpm",
            "scored_window_start",
            "scored_window_end",
        ]
    ].to_csv(directory / "predictions.csv", index=False)
    figures(directory, summary, species, replicates)
    lines = [
        "# TSS block shuffle validation",
        "",
        f"Completed genes: **{len(complete)}/{len(candidates)}**. Original DNA and shuffled DNA were predicted with GI `{protocol['model']}` under the identical description `{protocol['description']}`. There is one fresh native control and {protocol['replicates']} independent shuffles per species–gene pair.",
        "",
        f"Non-overlapping **8-bp blocks** were permuted separately in the {protocol['shuffle_flank_bp']:,}-bp upstream and downstream TSS flanks. The remainder of each {protocol['input_bp']:,}-bp input, its TSS index, sequence name, and context stayed fixed. Blocks containing N stayed in place. This preserves nucleotide composition and the block multiset within each flank; overlapping 8-mer counts at new junctions are not exactly preserved.",
        "",
        "Species are the observations. Each replicate yields a correlation across the same species; the main shuffled correlation uses the mean of ten log-expression predictions per species. No ancestry or body-mass adjustment is applied. Fresh native controls check whether the API reproduces the original baseline.",
        "",
        "| Gene | Native rho | Shuffled mean rho | Shuffled replicate range | Shuffled-mean BH q | Native minus shuffled, signed by original direction |",
        "|---|---:|---:|---|---:|---:|",
    ]
    for r in summary.itertuples():
        lines.append(
            f"| {r.gene_name} | {r.native_rho:+.3f} | {r.shuffled_mean_rho:+.3f} | {r.shuffle_rho_min:+.3f} to {r.shuffle_rho_max:+.3f} | {r.shuffled_mean_q_bh_six:.4g} | {r.signed_rho_attenuation:+.3f} |"
        )
    lines += [
        "",
        "![Native and shuffled expression versus lifespan](native_vs_shuffled.png)",
        "",
        "![Replicate correlations](correlations.png)",
        "",
        f"Permutation tests use {args.permutations:,} unrestricted lifespan-rank permutations, with the +1 correction. BH q values cover the six already-selected candidates, separately for native and shuffled-mean tests. They describe these selected genes rather than a new genome-wide discovery screen. Positive signed attenuation means a weaker association in the original direction. Its exploratory 95% interval uses 20,000 paired species bootstrap samples, conditional on the estimated shuffle means; selection and phylogeny are not modeled.",
        "",
        "A drop in expression alone does not establish loss of longevity association: correlations and species ordering are reported separately. Persistence after this local shuffle can reflect preserved sequence composition, short motifs, unperturbed distal sequence or model behavior. A loss supports dependence on the perturbed sequence organization but does not establish a causal effect on organismal lifespan. Synthetic shuffled promoters can also lie outside the model's training distribution.",
        "",
        "- [Per-gene statistics](gene_summary.csv), [species comparisons](species_comparison.csv), [each replicate's correlation](replicate_correlations.csv).",
        "- [All numeric predictions and hashes](predictions.csv), [frozen experimental protocol](protocol.json), [analysis provenance](analysis_provenance.json).",
        "",
    ]
    (directory / "README.md").write_text("\n".join(lines))
    shutil.copy2(out / "protocol.json", directory / "protocol.json")
    write_json(
        directory / "analysis_provenance.json",
        {
            "code_sha256": digest(__file__),
            "protocol_sha256": digest(out / "protocol.json"),
            "input_genes": [
                {"gene": row.gene_name, "sha256": digest(path)} for row, path in complete
            ],
            "permutations": args.permutations,
            "bootstrap_replicates": 20000,
            "complete": len(complete) == len(candidates),
        },
    )
    if args.publish_dir:
        if len(complete) != len(candidates):
            raise ValueError("Publish only a complete experiment report")
        target = Path(args.publish_dir)
        target.mkdir(parents=True, exist_ok=True)
        for path in directory.iterdir():
            if path.is_file():
                shutil.copy2(path, target / path.name)
    print(summary.to_string(index=False), flush=True)
    print(directory / "README.md", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--publish-dir")
    parser.add_argument("--partial", action="store_true")
    parser.add_argument("--permutations", type=int, default=999999)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
