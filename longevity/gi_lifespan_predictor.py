"""Expression-only lifespan regression with nested species CV and a held-out human.

Feature filtering, association screening, imputation, scaling, and penalty tuning
are repeated inside training folds. The human outcome is never supplied to a
training function and is evaluated only after frozen predictions are written.
No ancestry or body-mass covariates are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

HUMAN = "homo_sapiens"
SEED = 20261004
ALPHAS = np.logspace(-2, 5, 8)
MODELS = ("fdr_genes_ridge", "all_genes_ridge")
LABELS = {
    "fdr_genes_ridge": "FDR-selected genes + ridge",
    "all_genes_ridge": "All eligible genes + ridge",
    "baseline": "Training geometric-mean lifespan",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def bh(pvalues):
    p = np.asarray(pvalues, float)
    order = np.argsort(p)
    q = np.empty(len(p))
    q[order] = np.minimum(
        1, np.minimum.accumulate((p[order] * len(p) / np.arange(1, len(p) + 1))[::-1])[::-1]
    )
    return q


def spearman_columns(x, y):
    """Pairwise-complete Spearman tests, including ties and missing entries.

    The rank of y is recomputed for the observed species of each gene. Missing
    values are never median-imputed for this association screen. Asymptotic
    two-sided t p values match scipy.stats.spearmanr on each observed pair.
    """
    present = np.isfinite(x)
    n = present.sum(axis=0)
    rx = stats.rankdata(x, axis=0, nan_policy="omit")
    # Matrix multiplication counts observed outcomes below/equal to each value.
    ry = (y[:, None] > y[None, :]).astype(float) @ present
    ry += ((y[:, None] == y[None, :]).astype(float) @ present + 1) / 2
    mid = (n + 1) / 2
    rx = np.where(present, rx - mid, 0)
    ry = np.where(present, ry - mid, 0)
    den = np.sqrt((rx * rx).sum(axis=0) * (ry * ry).sum(axis=0))
    rho = np.divide((rx * ry).sum(axis=0), den, out=np.zeros(x.shape[1]), where=den > 0)
    rho = np.clip(rho, -1, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = rho * np.sqrt((n - 2) / ((1 + rho) * (1 - rho)))
    p = 2 * stats.t.sf(np.abs(t), np.maximum(n - 2, 1))
    p[(den == 0) | (n < 3)] = 1
    return rho, p, n


@dataclass
class Features:
    columns: np.ndarray
    median: np.ndarray
    mean: np.ndarray
    scale: np.ndarray
    x: np.ndarray
    rho: np.ndarray
    p: np.ndarray
    q: np.ndarray
    n_observed: np.ndarray
    eligible: np.ndarray

    def transform(self, x):
        values = x[:, self.columns]
        return (np.where(np.isfinite(values), values, self.median) - self.mean) / self.scale


def prepare_features(x, y, mode, min_fraction=0.6, fdr=0.05):
    """Fit preprocessing using only the supplied training rows/outcomes."""
    n = np.isfinite(x).sum(axis=0)
    eligible = n >= max(10, int(np.ceil(min_fraction * len(y))))
    rho = np.zeros(x.shape[1])
    p = np.ones(x.shape[1])
    if eligible.any():
        rho[eligible], p[eligible], _ = spearman_columns(x[:, eligible], y)
    # The prespecified 3,036-gene universe stays the multiple-testing family;
    # genes ineligible in a training fold receive p=1.
    q = bh(p)
    selected = eligible & ((q <= fdr) if mode == "fdr_genes_ridge" else True)
    columns = np.flatnonzero(selected)
    values = x[:, columns]
    median = np.nanmedian(values, axis=0)
    filled = np.where(np.isfinite(values), values, median)
    mean, scale = filled.mean(axis=0), filled.std(axis=0)
    variable = scale > 1e-12
    columns, median, mean, scale = (a[variable] for a in (columns, median, mean, scale))
    z = (filled[:, variable] - mean) / scale
    return Features(columns, median, mean, scale, z, rho, p, q, n, eligible)


def ridge_path(features, y, test_x, alphas=ALPHAS):
    """Predict all ridge penalties with one small n-species eigendecomposition."""
    center = float(np.mean(y))
    if not len(features.columns):
        return np.full((len(test_x), len(alphas)), center)
    z, test_z = features.x, features.transform(test_x)
    eig, u = np.linalg.eigh(z @ z.T)
    eig = np.maximum(eig, 0)
    right = (u.T @ (y - center))[:, None] / (eig[:, None] + alphas)
    return center + (test_z @ z.T @ u) @ right


def ridge_coefficients(features, y, alpha):
    if not len(features.columns):
        return np.zeros(0)
    z = features.x
    return z.T @ np.linalg.solve(z @ z.T + alpha * np.eye(len(z)), y - y.mean())


def inner_folds(n, seed=SEED):
    return np.array_split(np.random.default_rng(seed).permutation(n), 5)


def tune(x, y, mode):
    errors = np.zeros((len(y), len(ALPHAS)))
    selections = []
    for validation in inner_folds(len(y)):
        training = np.setdiff1d(np.arange(len(y)), validation)
        features = prepare_features(x[training], y[training], mode)
        prediction = ridge_path(features, y[training], x[validation])
        errors[validation] = (prediction - y[validation, None]) ** 2
        selections.append(len(features.columns))
    mse = errors.mean(axis=0)
    # Prefer stronger shrinkage if errors are numerically tied (e.g. zero genes).
    best = np.flatnonzero(np.isclose(mse, mse.min(), rtol=1e-10, atol=1e-12))[-1]
    return float(ALPHAS[best]), mse, selections


def fit_predict(x, y, test_x, mode):
    alpha, mse, selections = tune(x, y, mode)
    features = prepare_features(x, y, mode)
    prediction = ridge_path(features, y, test_x, np.array([alpha]))[:, 0]
    return prediction, features, alpha, mse, selections


def load_inputs(source, output):
    """Freeze the expression matrix and nonhuman targets; exclude human y from training."""
    source, output = Path(source), Path(output)
    progress = json.loads((source / "run_progress.json").read_text())
    if progress["status"] != "completed":
        raise ValueError("Full prediction run must be complete")
    columns = ["human_gene_id", "ensembl_species", "gene_name", "expression_log_tpm"]
    tables = [
        pd.read_parquet(p, columns=columns) for p in sorted((source / "genes").glob("*.parquet"))
    ]
    data = pd.concat(tables, ignore_index=True)
    if (
        len(data) != progress["total_predictions"]
        or data.duplicated(["ensembl_species", "human_gene_id"]).any()
    ):
        raise ValueError("Prediction table does not match the completed run")
    matrix = data.pivot(
        index="ensembl_species", columns="human_gene_id", values="expression_log_tpm"
    ).sort_index()
    if matrix.shape[1] != progress["total_genes"]:
        raise ValueError("Incorrect gene count")
    # The cohort target is copied for nonhumans only; human y is never passed to
    # any training/evaluation function. Reading one CSV cell later evaluates it.
    meta = (
        pd.read_csv(source / "analysis_cohort.csv").set_index("ensembl_species").loc[matrix.index]
    )
    training = meta.drop(index=HUMAN)[
        ["anage_name", "anage_common_name", "anage_order", "max_longevity_yrs"]
    ]
    annotations = (
        data[data.ensembl_species == HUMAN]
        .set_index("human_gene_id")[["gene_name"]]
        .reindex(matrix.columns)
    )
    annotations.index.name = "human_gene_id"
    annotations.gene_name = annotations.gene_name.fillna(annotations.index.to_series())
    matrix.to_csv(output / "expression_matrix.csv.gz", compression={"method": "gzip", "mtime": 0})
    training.to_csv(output / "training_species.csv")
    annotations.to_csv(output / "gene_annotations.csv")
    write_json(
        output / "source_provenance.json",
        {
            "source": str(source.resolve()),
            "run_progress": progress,
            "prediction_manifest_sha256": sha(source / "prediction_manifest.parquet"),
            "source_cohort_sha256": sha(source / "analysis_cohort.csv"),
            "expression_matrix_sha256": sha(output / "expression_matrix.csv.gz"),
            "prediction_rows": len(data),
            "species": len(matrix),
            "genes": matrix.shape[1],
            "model": "g0-expression-8192",
            "context": "Homo sapiens primary hepatocytes, untreated, bulk RNA-seq.",
        },
    )
    return matrix, training, annotations


def evaluate_cv(matrix, training):
    species = training.index.to_numpy()
    x = matrix.loc[species].to_numpy(float)
    y = np.log(training.max_longevity_yrs.to_numpy(float))
    rows, gene_counts = [], []
    for i, held_out in enumerate(species):
        idx = np.arange(len(y)) != i
        baseline = float(y[idx].mean())
        rows.append(
            {
                "ensembl_species": held_out,
                "model": "baseline",
                "observed_years": float(np.exp(y[i])),
                "predicted_log_years": baseline,
                "predicted_years": float(np.exp(baseline)),
                "n_features": 0,
                "alpha": np.nan,
                "expression_genes_observed": int(np.isfinite(x[i]).sum()),
                "features_observed": 0,
            }
        )
        for mode in MODELS:
            pred, features, alpha, _, _selections = fit_predict(x[idx], y[idx], x[[i]], mode)
            rows.append(
                {
                    "ensembl_species": held_out,
                    "model": mode,
                    "observed_years": float(np.exp(y[i])),
                    "predicted_log_years": float(pred[0]),
                    "predicted_years": float(np.exp(pred[0])),
                    "n_features": len(features.columns),
                    "alpha": alpha,
                    "expression_genes_observed": int(np.isfinite(x[i]).sum()),
                    "features_observed": int(np.isfinite(x[i, features.columns]).sum()),
                }
            )
            if mode == "fdr_genes_ridge":
                gene_counts.extend(
                    {"ensembl_species_held_out": held_out, "human_gene_id": matrix.columns[j]}
                    for j in features.columns
                )
        print(f"Nested CV {i + 1}/{len(species)}: {held_out}", flush=True)
    return pd.DataFrame(rows), pd.DataFrame(gene_counts)


def metrics(frame):
    rows = []
    for model, d in frame.groupby("model", sort=False):
        observed = d.observed_years.to_numpy()
        prediction = d.predicted_years.to_numpy()
        residual = d.predicted_log_years.to_numpy() - np.log(observed)
        rows.append(
            {
                "model": model,
                "species": len(d),
                "mae_years": float(np.mean(np.abs(prediction - observed))),
                "rmse_log_years": float(np.sqrt(np.mean(residual**2))),
                "median_absolute_error_years": float(np.median(np.abs(prediction - observed))),
                "spearman": (
                    float(stats.spearmanr(observed, prediction).statistic)
                    if model != "baseline"
                    else np.nan
                ),
                "r2_log": float(
                    1
                    - np.sum(residual**2)
                    / np.sum((np.log(observed) - np.log(observed).mean()) ** 2)
                ),
            }
        )
    return pd.DataFrame(rows)


def final_models(matrix, training, output, annotations):
    x = matrix.loc[training.index].to_numpy(float)
    test_x = matrix.loc[[HUMAN]].to_numpy(float)
    y = np.log(training.max_longevity_yrs.to_numpy(float))
    prediction = {
        "baseline": {
            "predicted_log_years": float(y.mean()),
            "predicted_years": float(np.exp(y.mean())),
        }
    }
    coefficients, screens, tuning = [], [], []
    for mode in MODELS:
        pred, features, alpha, mse, selections = fit_predict(x, y, test_x, mode)
        beta = ridge_coefficients(features, y, alpha)
        prediction[mode] = {
            "predicted_log_years": float(pred[0]),
            "predicted_years": float(np.exp(pred[0])),
            "alpha": alpha,
            "n_features": len(features.columns),
            "features_observed_in_human": int(np.isfinite(test_x[:, features.columns]).sum()),
            "inner_fold_feature_counts": selections,
            "intercept_log_years": float(y.mean()),
            "selected_genes": matrix.columns[features.columns].tolist(),
        }
        for j, b, median, mean, scale in zip(
            features.columns, beta, features.median, features.mean, features.scale
        ):
            coefficients.append(
                {
                    "model": mode,
                    "human_gene_id": matrix.columns[j],
                    "gene_name": annotations.loc[matrix.columns[j], "gene_name"],
                    "coefficient_standardized": float(b),
                    "coefficient_original_scale": float(b / scale),
                    "training_median": median,
                    "training_mean": mean,
                    "training_scale": scale,
                    "training_spearman_rho": features.rho[j],
                    "training_spearman_p": features.p[j],
                    "training_q_bh": features.q[j],
                    "training_n_observed": int(features.n_observed[j]),
                }
            )
        for alpha_value, error in zip(ALPHAS, mse):
            tuning.append(
                {
                    "model": mode,
                    "alpha": alpha_value,
                    "inner_mse_log_years": error,
                    "selected": bool(alpha_value == alpha),
                }
            )
        if mode == "fdr_genes_ridge":
            for j, gene in enumerate(matrix.columns):
                screens.append(
                    {
                        "human_gene_id": gene,
                        "gene_name": annotations.loc[gene, "gene_name"],
                        "n_observed": int(features.n_observed[j]),
                        "eligible": bool(features.eligible[j]),
                        "spearman_rho": features.rho[j],
                        "spearman_p": features.p[j],
                        "q_bh": features.q[j],
                        "selected": bool(j in features.columns),
                    }
                )
    prediction["frozen_at_utc"] = datetime.now(UTC).isoformat()
    prediction["training_species"] = training.index.tolist()
    prediction["human_outcome_used"] = False
    write_json(output / "human_prediction_frozen.json", prediction)
    pd.DataFrame(coefficients).to_csv(output / "coefficients.csv", index=False)
    pd.DataFrame(screens).sort_values("spearman_p").to_csv(
        output / "nonhuman_gene_screen.csv", index=False
    )
    pd.DataFrame(tuning).to_csv(output / "final_model_tuning.csv", index=False)
    return prediction


def save_figure(fig, path):
    for ext in ("png", "svg", "pdf"):
        target = path.with_suffix("." + ext)
        fig.savefig(target, dpi=170, bbox_inches="tight")
        if ext == "svg":
            target.write_text(
                "\n".join(line.rstrip() for line in target.read_text().splitlines()) + "\n"
            )
    plt.close(fig)


def predict_exported(model_dir, expression, model="fdr_genes_ridge"):
    """Apply exported coefficients to a species × Ensembl-gene numeric table."""
    if model not in MODELS:
        raise ValueError(f"Unknown model: {model}")
    model_dir = Path(model_dir)
    frozen = json.loads((model_dir / "human_prediction_frozen.json").read_text())[model]
    coefficients = pd.read_csv(model_dir / "coefficients.csv")
    weights = coefficients[coefficients.model == model].set_index("human_gene_id")
    if expression.index.has_duplicates or expression.columns.has_duplicates:
        raise ValueError("Expression table species and gene names must be unique")
    values = expression.reindex(columns=weights.index).to_numpy(float)
    if np.isinf(values).any():
        raise ValueError("Expression values must be finite or missing")
    observed = np.isfinite(values).sum(axis=1)
    if len(weights) and (observed == 0).any():
        raise ValueError("Every input species must have at least one fitted gene")
    filled = np.where(np.isfinite(values), values, weights.training_median.to_numpy())
    scaled = (filled - weights.training_mean.to_numpy()) / weights.training_scale.to_numpy()
    log_prediction = (
        frozen["intercept_log_years"] + scaled @ weights.coefficient_standardized.to_numpy()
    )
    return pd.DataFrame(
        {
            "predicted_log_years": log_prediction,
            "predicted_maximum_lifespan_years": np.exp(log_prediction),
            "fitted_genes_observed": observed,
            "fitted_genes_total": len(weights),
        },
        index=expression.index,
    )


def coverage_diagnostics(cv):
    rows = []
    for model, d in cv.groupby("model", sort=False):
        absolute_error = np.abs(d.predicted_years - d.observed_years)
        absolute_log_error = np.abs(d.predicted_log_years - np.log(d.observed_years))
        cutoff = float(d.expression_genes_observed.median())
        for label, keep in (
            ("below_median_coverage", d.expression_genes_observed < cutoff),
            ("at_or_above_median_coverage", d.expression_genes_observed >= cutoff),
        ):
            rows.append(
                {
                    "model": model,
                    "coverage_group": label,
                    "species": int(keep.sum()),
                    "coverage_median_cutoff_genes": cutoff,
                    "mae_years": float(absolute_error[keep].mean()),
                    "mean_absolute_log_error": float(absolute_log_error[keep].mean()),
                    "absolute_log_error_share": float(
                        absolute_log_error[keep].sum() / absolute_log_error.sum()
                    ),
                    "rho_coverage_vs_absolute_log_error": float(
                        stats.spearmanr(d.expression_genes_observed, absolute_log_error).statistic
                    ),
                }
            )
    return pd.DataFrame(rows)


def figures(cv, human, scores, output):
    plt.rcParams.update({"font.size": 11, "svg.hashsalt": "gi-lifespan-predictor"})
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.3))
    for ax, model in zip(axes, MODELS):
        d = cv[cv.model == model]
        h = human[human.model == model].iloc[0]
        m = scores[scores.model == model].iloc[0]
        ax.scatter(
            d.observed_years,
            d.predicted_years,
            c="#377eb8",
            alpha=0.78,
            s=35,
            label="Nonhuman: nested leave-one-species-out",
        )
        ax.scatter(
            [h.observed_years],
            [h.predicted_years],
            c="#e07a23",
            marker="*",
            s=190,
            label="Human: held out from all training",
            zorder=5,
        )
        ax.annotate(
            f"Human: {h.predicted_years:.1f} y",
            (h.observed_years, h.predicted_years),
            xytext=(-6, 10),
            textcoords="offset points",
            ha="right",
            fontsize=10,
        )
        ax.plot([2, 160], [2, 160], "--", color="0.5", lw=1)
        ax.set(
            xscale="log",
            yscale="log",
            xlim=(2, 160),
            ylim=(2, 160),
            xlabel="Observed maximum lifespan (years)",
            ylabel="Predicted maximum lifespan (years)",
            title=LABELS[model],
        )
        ax.text(
            0.04,
            0.96,
            f"47 nonhuman holdouts\nMAE = {m.mae_years:.1f} y\nSpearman ρ = {m.spearman:.2f}\nLog R² = {m.r2_log:.2f}",
            transform=ax.transAxes,
            va="top",
            fontsize=10,
        )
        ax.grid(alpha=0.15, which="both")
    axes[0].legend(loc="lower right", fontsize=8)
    fig.suptitle(
        "Expression-only lifespan prediction: human excluded from selection and tuning", fontsize=13
    )
    fig.tight_layout()
    save_figure(fig, output / "heldout_predictions")


def report(output, matrix, training, cv, human, scores, frozen):
    fdr = pd.read_csv(output / "nonhuman_gene_screen.csv")
    selected = fdr[fdr.selected]
    gene_names = ", ".join(selected.gene_name.astype(str)) or "none (intercept-only fallback)"
    rows = []
    for _, row in human.iterrows():
        m = scores[scores.model == row.model].iloc[0]
        rho_display = f"{m.spearman:.3f}" if row.model != "baseline" else "N/A"
        rows.append(
            f"| {LABELS[row.model]} | {m.mae_years:.2f} | {m.rmse_log_years:.3f} | {rho_display} | {row.predicted_years:.2f} | {row.error_years:+.2f} |"
        )
    human_observed = human.observed_years.iloc[0]
    coverage = pd.read_csv(output / "coverage_diagnostics.csv").set_index(
        ["model", "coverage_group"]
    )
    low = coverage.loc[("all_genes_ridge", "below_median_coverage")]
    high = coverage.loc[("all_genes_ridge", "at_or_above_median_coverage")]
    lines = [
        "# Predicting lifespan from GI hepatocyte expression",
        "",
        f"Both regression models improve prediction of nonhuman mammals over the constant baseline, but **substantially underpredict human maximum lifespan**: **{frozen['fdr_genes_ridge']['predicted_years']:.1f} years** from statistically selected genes and **{frozen['all_genes_ridge']['predicted_years']:.1f} years** from all genes, versus **{human_observed:g} years** recorded in AnAge. This human validation is unsuccessful: comparative predictive signal does not yield an accurate human estimate.",
        "",
        f"The human outcome was held out from feature selection, preprocessing, ridge-penalty tuning, and fitting. The target is **recorded maximum lifespan**, not life expectancy. Human AnAge maximum lifespan is **{human_observed:g} years**; it was compared with the frozen predictions only after fitting.",
        "",
        "| Model | Nonhuman CV MAE, years | CV RMSE, natural-log years | CV Spearman | Human prediction, years | Human error, years |",
        "|---|---:|---:|---:|---:|---:|",
        *rows,
        "",
        "![Observed versus held-out predicted maximum lifespan](heldout_predictions.png)",
        "",
        "[SVG](heldout_predictions.svg) · [PDF](heldout_predictions.pdf) · [All nonhuman holdout predictions](cross_validation_predictions.csv) · [Human predictions](human_validation.csv)",
        "",
        f"The FDR-selected final model uses **{len(selected)} genes**: {gene_names}. These are selected anew using only the 47 nonhuman mammals, rather than reusing candidates from the earlier screen that included humans. The final ridge penalties are {frozen['fdr_genes_ridge']['alpha']:g} (FDR model) and {frozen['all_genes_ridge']['alpha']:g} (all-genes model). The all-genes model retains {frozen['all_genes_ridge']['n_features']} genes after training-only availability and variance filtering. [Gene screen](nonhuman_gene_screen.csv) · [Regression coefficients and preprocessing](coefficients.csv).",
        "",
        "## Methods",
        "",
        f"- Inputs: the completed run of {matrix.shape[1]:,} orthologues across {len(matrix)} mammalian species. Each feature is GI `expression_log_tpm`; GI model `g0-expression-8192`, description `Homo sapiens primary hepatocytes, untreated, bulk RNA-seq.` for every species. No shuffled sequences enter this predictor.",
        "- Target: natural logarithm of AnAge maximum lifespan in years. Predictions are exponentiated without a log-normal bias correction; they estimate a lifespan on the log scale, not an arithmetic conditional mean. No ancestry, body mass, species identity, or missingness indicators are predictors.",
        "- Fixed held-out species: Homo sapiens. All 47 remaining mammals enter nested validation, including species with incomplete expression coverage. The available expression matrix and human sequences may be known, but human lifespan is never supplied to a model-selection or fitting function.",
        "- Training-only eligibility: an orthologue must have measured predictions in at least max(10, ceiling(0.6 × training-species count)) species. Zero-variance features are dropped after training-median imputation. Training means and standard deviations standardize each feature; validation and human features receive those same transformations.",
        "- Selected-gene model: pairwise-complete, two-sided Spearman association with training log lifespan, with tie-aware ranks and SciPy's asymptotic t p values. BH FDR ≤0.05 over the prespecified 3,036-gene family; ineligible genes get p=1. Screening uses observed values, not imputed observations. This approximate screen differs from the earlier permutation association screen. If no genes survive in a fold, predictions use the training mean log lifespan.",
        "- Regression: ridge minimizes sum of squared log-lifespan residuals + alpha × sum of squared standardized-feature coefficients, with an unpenalized intercept. Penalties 0.01, 0.1, 1, 10, 100, 1,000, 10,000 and 100,000 are compared using five shuffled inner folds (seed 20261004), minimizing pooled validation MSE. Numerically tied penalties prefer stronger shrinkage. Screening, imputation, and scaling are refitted in each inner fold.",
        "- Evaluation: outer leave-one-species-out CV across the 47 nonhuman mammals. Each outer training set runs its own five-fold inner tuning. Thus each reported prediction is excluded from every step producing it, including gene selection. The baseline is the geometric mean lifespan in each outer training set.",
        "- Baseline ranking is omitted: leave-one-out training-mean predictions vary inversely with the held-out target by construction, so their Spearman correlation is not a meaningful comparator. Compare the baseline by prediction errors.",
        "- Human prediction: rerun the same five-fold tuning and fitting on all 47 nonhumans. `human_prediction_frozen.json` is written before the evaluation routine reads the human target. No choice is made between the two model families based on the human result. Both are reported alongside the baseline.",
        "",
        "The exported prediction formula is `log(lifespan_years) = intercept_log_years + sum(coefficient_standardized[j] * (imputed_expression[j] - training_mean[j]) / training_scale[j])`. Missing expression uses `training_median[j]`. The `coefficient_original_scale` column is the standardized coefficient divided by its training scale; using it without centering requires intercept `intercept_log_years - sum(coefficient_original_scale[j] * training_mean[j])`. The supplied inference script applies the centered formula directly.",
        "",
        "## Interpretation and limits",
        "",
        "This is a small comparative prediction experiment, with 47 training species and thousands of possible features. The species-wise validation allows related species in training, as requested; ancestry is not corrected. Human prediction therefore tests a new species within a dataset containing other primates, not transfer to an unseen mammalian order. Human maximum lifespan also exceeds the largest nonhuman training target (110 years), making it an extrapolation test.",
        "",
        "Coverage varies sharply across species. Training-median imputation lets every species be tested but can pull sparsely covered species toward the intercept; the CV table reports both overall coverage and how many fitted features are observed. Coefficients describe conditional prediction after shrinkage and are not causal gene effects. Original six-gene associations are not assumed to transfer to this independently screened model. One held-out human outcome is an illustrative check; it cannot by itself establish general predictive validity.",
        "",
        f"For the all-gene model, the {int(low.species)} species below the median coverage of {int(low.coverage_median_cutoff_genes):,} observed genes contribute {100 * low.absolute_log_error_share:.1f}% of total absolute log error. Their MAE is {low.mae_years:.2f} years, versus {high.mae_years:.2f} years in the {int(high.species)} better-covered species; mean absolute log errors are {low.mean_absolute_log_error:.3f} versus {high.mean_absolute_log_error:.3f}. Sparse coverage contributes to poorer prediction, but is not the only difficulty: the well-covered blue whale is also markedly underpredicted. Human coverage is {frozen['all_genes_ridge']['features_observed_in_human']:,}/{frozen['all_genes_ridge']['n_features']:,} all-gene features and {frozen['fdr_genes_ridge']['features_observed_in_human']}/{frozen['fdr_genes_ridge']['n_features']} selected features, so missing human expression cannot explain the large underestimate on its own. [Coverage diagnostics](coverage_diagnostics.csv).",
        "",
        "## Reproduce",
        "",
        "The compressed expression matrix, nonhuman targets, gene annotations, fitted coefficients, fold predictions, and provenance are included. These contain expression and lifespan data only, without DNA or API credentials.",
        "",
        "```bash",
        "uv sync --extra analysis",
        "OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 uv run python -m longevity.gi_lifespan_predictor \\",
        "  --prepared docs/gi-lifespan-predictor --output /tmp/gi-lifespan-predictor-reproduced",
        "```",
        "",
        "The prepared-input command regenerates nested CV and frozen human predictions without needing the original dataset or reading the human target. Add `--human-lifespan-years 122.5` to additionally create human evaluation artifacts; the argument is used only after the fitting routine has frozen predictions. The original full-data invocation uses `--source data/datasets/gi-hepatocytes-ensembl116/analyses/broad` instead of `--prepared`.",
        "",
        "To apply the exported model to another numeric expression table (species as rows, first column species name, remaining columns human Ensembl gene IDs; missing genes are imputed using saved training medians):",
        "",
        "```bash",
        "uv run python scripts/predict_gi_lifespan.py --expression-csv expression.csv --output lifespan_predictions.csv",
        "# Add --model all_genes_ridge to use the model without association screening.",
        "```",
        "",
        "[Source provenance](source_provenance.json) · [Protocol](protocol.json) · [Frozen human predictions](human_prediction_frozen.json) · [Tuning results](final_model_tuning.csv) · [Feature selection in outer folds](cv_selected_genes.csv)",
        "",
    ]
    (output / "README.md").write_text("\n".join(lines))


def run(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if args.prepared:
        source = Path(args.prepared)
        matrix = pd.read_csv(source / "expression_matrix.csv.gz", index_col=0)
        training = pd.read_csv(source / "training_species.csv", index_col=0)
        annotations = pd.read_csv(source / "gene_annotations.csv", index_col=0)
        import shutil

        for name in (
            "expression_matrix.csv.gz",
            "training_species.csv",
            "gene_annotations.csv",
            "source_provenance.json",
        ):
            if (source / name).resolve() != (output / name).resolve():
                shutil.copy2(source / name, output / name)
    else:
        matrix, training, annotations = load_inputs(args.source, output)
    if HUMAN in training.index or not training.index.equals(matrix.drop(index=HUMAN).index):
        raise ValueError("Training table must contain exactly the nonhuman species")
    if not (training.max_longevity_yrs > 0).all() or np.isinf(matrix.to_numpy()).any():
        raise ValueError("Invalid lifespan or expression data")
    write_json(
        output / "protocol.json",
        {
            "held_out_species": HUMAN,
            "training_species": len(training),
            "gene_universe": matrix.shape[1],
            "models": list(MODELS),
            "target": "natural log of maximum lifespan in years",
            "outer_validation": "leave-one-nonhuman-species-out",
            "inner_validation": "5 shuffled folds",
            "seed": SEED,
            "ridge_alphas": ALPHAS.tolist(),
            "fdr": 0.05,
            "screen": "pairwise Spearman asymptotic t p, BH",
            "minimum_training_coverage_fraction": 0.6,
            "minimum_training_observations": 10,
            "preprocessing": "training median imputation; training mean and population standard deviation",
            "ancestry_adjustment": False,
            "body_mass_adjustment": False,
            "human_target_in_fitting": False,
            "code_sha256": sha(__file__),
        },
    )
    cv, genes = evaluate_cv(matrix, training)
    cv.to_csv(output / "cross_validation_predictions.csv", index=False)
    genes.to_csv(output / "cv_selected_genes.csv", index=False)
    scores = metrics(cv)
    scores.to_csv(output / "cross_validation_metrics.csv", index=False)
    coverage_diagnostics(cv).to_csv(output / "coverage_diagnostics.csv", index=False)
    frozen = final_models(matrix, training, output, annotations)
    # Deliberately separate fitting from reading the held-out lifespan.
    if args.source:
        cohort = pd.read_csv(Path(args.source) / "analysis_cohort.csv").set_index("ensembl_species")
        actual = float(cohort.loc[HUMAN, "max_longevity_yrs"])
    else:
        actual = args.human_lifespan_years
    if actual is not None:
        human = pd.DataFrame(
            [
                {
                    "model": mode,
                    "observed_years": actual,
                    "predicted_years": frozen[mode]["predicted_years"],
                    "error_years": frozen[mode]["predicted_years"] - actual,
                }
                for mode in (*MODELS, "baseline")
            ]
        )
        human.to_csv(output / "human_validation.csv", index=False)
        figures(cv, human, scores, output)
        report(output, matrix, training, cv, human, scores, frozen)
        print(human.to_string(index=False), flush=True)
    print(scores.to_string(index=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source", type=Path)
    group.add_argument("--prepared", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--human-lifespan-years", type=float)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
