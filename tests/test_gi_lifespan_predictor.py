import json

import numpy as np
import pandas as pd
from scipy import stats

from longevity.gi_lifespan_predictor import (
    HUMAN,
    MODELS,
    final_models,
    predict_exported,
    prepare_features,
    ridge_path,
    spearman_columns,
)


def test_pairwise_spearman_matches_scipy_with_ties_and_missingness():
    rng = np.random.default_rng(24)
    x = rng.integers(0, 8, size=(30, 8)).astype(float)
    x[rng.random(x.shape) < 0.2] = np.nan
    x[:, 0] = 2
    y = rng.integers(2, 12, size=30).astype(float)
    rho, p, n = spearman_columns(x, y)
    assert rho[0] == 0 and p[0] == 1
    for j in range(1, x.shape[1]):
        keep = np.isfinite(x[:, j])
        reference = stats.spearmanr(x[keep, j], y[keep])
        np.testing.assert_allclose(
            [rho[j], p[j]], [reference.statistic, reference.pvalue], atol=1e-12
        )
        assert n[j] == keep.sum()


def test_ridge_matches_primal_solution_and_training_only_transforms():
    rng = np.random.default_rng(25)
    x = rng.normal(size=(25, 5))
    x[::3, 2] = np.nan
    y = rng.normal(size=25)
    test = rng.normal(size=(3, 5))
    test[0, 2] = np.nan
    features = prepare_features(x, y, "all_genes_ridge")
    alpha = 10.0
    actual = ridge_path(features, y, test, np.array([alpha]))[:, 0]
    z = features.x
    beta = np.linalg.solve(z.T @ z + alpha * np.eye(z.shape[1]), z.T @ (y - y.mean()))
    np.testing.assert_allclose(actual, y.mean() + features.transform(test) @ beta)
    np.testing.assert_allclose(features.median, np.nanmedian(x, axis=0))
    before = features.mean.copy()
    features.transform(test * 10000)
    np.testing.assert_array_equal(features.mean, before)


def test_human_features_cannot_change_selection_tuning_or_fitted_weights(tmp_path):
    rng = np.random.default_rng(26)
    species = [f"species_{i:02}" for i in range(25)]
    genes = [f"GENE{i}" for i in range(5)]
    x = rng.normal(size=(26, 5))
    matrix = pd.DataFrame(x, index=species + [HUMAN], columns=genes)
    training = pd.DataFrame({"max_longevity_yrs": np.exp(2 + x[:25, 0] / 2)}, index=species)
    annotation = pd.DataFrame({"gene_name": genes}, index=genes)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    a = final_models(matrix, training, first, annotation)
    matrix.loc[HUMAN] = 30
    b = final_models(matrix, training, second, annotation)
    assert HUMAN not in a["training_species"]
    assert not a["human_outcome_used"]
    for mode in MODELS:
        for key in ("alpha", "selected_genes", "intercept_log_years", "inner_fold_feature_counts"):
            assert a[mode][key] == b[mode][key]
    pd.testing.assert_frame_equal(
        pd.read_csv(first / "coefficients.csv"), pd.read_csv(second / "coefficients.csv")
    )
    for mode in MODELS:
        exported = predict_exported(second, matrix.loc[[HUMAN]], mode)
        np.testing.assert_allclose(
            exported.predicted_log_years.iloc[0], b[mode]["predicted_log_years"]
        )
    assert (
        json.loads((first / "human_prediction_frozen.json").read_text())["human_outcome_used"]
        is False
    )
