"""Order controls preserve inputs and evaluate every held-out species."""

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import torch
from test_longevity_comparison import reference as reference_fixture

from longevity.comparison import create_spec
from longevity.shuffle_eval import ShuffledRows, species_metrics
from longevity.train import evaluate

reference = reference_fixture


def test_shuffle_preserves_tokens_endpoints_and_is_order_independent():
    rows = np.array([[1, *range(10, 1032), 2]] * 3)
    original = rows.copy()
    frame = pd.DataFrame({"assembly_accession": ["a", "a", "b"], "chunk_id": [0, 1, 0]})
    shuffled = ShuffledRows(rows, frame, 42)
    for i in (2, 0, 1, 0):
        assert np.array_equal(np.sort(shuffled[i]), np.sort(rows[i]))
        assert shuffled[i][0] == 1 and shuffled[i][-1] == 2
        assert not np.array_equal(shuffled[i], rows[i])
    np.testing.assert_array_equal(rows, original)
    reordered = ShuffledRows(rows[[2, 0]], frame.iloc[[2, 0]], 42)
    np.testing.assert_array_equal(reordered[0], shuffled[2])
    np.testing.assert_array_equal(reordered[1], shuffled[0])
    assert not np.array_equal(shuffled[0], shuffled[1])


def test_metrics_aggregate_in_log_space_and_weight_species_equally():
    frame = pd.DataFrame(
        {
            "ncbi_taxid": [1, 1, 2],
            "scientific_name": ["a", "a", "b"],
            "class": [None, None, "x"],
            "log10_longevity": [1.0, 1.0, 2.0],
            "pred_log10": [0.0, 2.0, 1.0],
        }
    )
    metrics, species = species_metrics(frame)
    assert metrics["n_species"] == 2 and metrics["n_inputs"] == 3
    assert metrics["mae_log10"] == 0.5
    assert metrics["mae_years"] == pytest.approx(45)
    assert metrics["pearson_years"] is None and metrics["spearman"] is None
    assert species.predicted_years.tolist() == [10.0, 10.0]
    frame.loc[2, "pred_log10"] = 2.0
    metrics, _ = species_metrics(frame)
    assert metrics["mae_years"] == 0
    assert metrics["pearson_years"] == pytest.approx(1)
    frame.loc[2, "pred_log10"] = np.nan
    with pytest.raises(ValueError, match="Nonfinite"):
        species_metrics(frame)


def test_evaluate_retains_species_without_class():
    class Model(torch.nn.Module):
        def forward(self, input_ids, attention_mask):
            return input_ids[:, 1].float()

    frame = pd.DataFrame(
        {
            "ncbi_taxid": [1, 2],
            "scientific_name": ["a", "b"],
            "class": [None, "x"],
            "log10_longevity": [1.0, 2.0],
            "assembly_accession": ["a", "b"],
            "chunk_id": [0, 0],
            "n_tokens": [1, 1],
        }
    )
    metrics, predictions = evaluate(
        Model(),
        frame,
        [np.array([1, 1, 2]), np.array([1, 2, 2])],
        0.0,
        1.0,
        SimpleNamespace(max_len=3, eval_tokens_per_batch=6),
        torch.device("cpu"),
    )
    assert metrics["n_species"] == 2
    assert metrics["species_mae_log10"] == 0
    assert len(predictions) == 2


def test_running_reference_requires_explicit_epoch_authorization(reference):
    run, pairs = reference
    cfg = json.loads((run / "config.json").read_text())
    cfg["split"]["val_sub"] = cfg["split"]["val"]
    cfg.update(
        eval_every_steps=2000,
        eval_genes_per_species=50,
        final_eval_genes_per_species=0,
        save_best=True,
    )
    (run / "config.json").write_text(json.dumps(cfg))
    (run / "metrics.jsonl").write_text('{"kind":"train", "step":1}\n')
    with pytest.raises(ValueError, match="completed"):
        create_spec(run, pairs, "epochs")
    with pytest.raises(ValueError, match="completed"):
        create_spec(run, pairs, "updates", allow_running_reference=True)
    spec = create_spec(run, pairs, "epochs", allow_running_reference=True)
    assert spec["reference_completed_steps"] is None
    assert spec["reference_status_at_freeze"] == "running"
    assert set(spec["split"]) == {"train", "val", "test"}
    assert spec["settings"]["eval_genes_per_species"] == 50
    assert spec["settings"]["save_best"] is True
    cfg["split"]["val_sub"] = [1]
    (run / "config.json").write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="subset"):
        create_spec(run, pairs, "epochs", allow_running_reference=True)


def test_publisher_requires_completed_full_species_coverage(tmp_path):
    from scripts.publish_longevity_controls import copy_report

    source = tmp_path / "source"
    source.mkdir()
    (source / "complete").write_text("0\n")
    (source / "comparison.json").write_text(
        json.dumps({"sha256": "abc", "split": {"val": [1, 2], "test": [3]}})
    )
    summary = {
        "comparison_sha256": "abc",
        "results": {
            s: {m: {"n_species": n} for m in ("intact", "shuffled")}
            for s, n in (("val", 2), ("test", 1))
        },
    }
    (source / "summary.json").write_text(json.dumps(summary))
    (source / "README.md").write_text("Report")
    (source / "large.parquet").write_bytes(b"not for GitHub")
    copy_report(source, tmp_path / "report")
    assert (tmp_path / "report/README.md").exists()
    assert not (tmp_path / "report/large.parquet").exists()
    summary["results"]["val"]["shuffled"]["n_species"] = 1
    (source / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="coverage"):
        copy_report(source, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
