"""Dataset snapshot, whole-input audit and completed-run reporting checks."""

import json

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("h5py")
pytest.importorskip("torch")

from test_longevity_chunks import Tokenizer, metadata
from test_longevity_comparison import reference as reference_fixture

from longevity.audit_chunks import audit
from longevity.chunks import write_genome
from longevity.compare_report import make_report
from longevity.comparison import create_spec
from longevity.snapshot import snapshot

reference = reference_fixture


def test_snapshot_preserves_reference_taxonomy_and_verifies_bytes(reference, tmp_path):
    import hashlib

    spec = create_spec(*reference, "epochs")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    dataset = tmp_path / "source"
    dataset.mkdir()
    fasta = dataset / "genome.fna"
    fasta.write_text(">a\n" + "ACGT" * 400)
    md5 = hashlib.md5(fasta.read_bytes()).hexdigest()
    records = [
        {
            "assembly_accession": f"GCF_{i}",
            "species_taxid": i,
            "sequence_file": fasta.name,
            "sequence_md5": md5,
        }
        for i in range(1, 6)
    ]
    pd.DataFrame(records).to_parquet(dataset / "genomes.parquet")
    sp = pd.DataFrame([metadata(i) for i in range(1, 6)])
    sp.loc[0, "family"] = "New taxonomy"
    sp.to_parquet(dataset / "species.parquet")
    sp[["ncbi_taxid", "max_longevity_yrs"]].assign(is_primary=True).to_parquet(
        dataset / "longevity.parquet"
    )
    target = tmp_path / "snapshot"
    snapshot(dataset, spec_path, target, 2)
    assert set(pd.read_parquet(target / "genomes.parquet").assembly_accession) == {
        "GCF_1",
        "GCF_2",
        "GCF_3",
        "GCF_4",
    }
    assert pd.read_parquet(target / "species.parquet").iloc[0].family == "Family 1"
    provenance = json.loads((target / "snapshot.json").read_text())
    assert len(provenance["verified_sequence_md5"]) == 4
    assert len(provenance["taxonomy_differences_from_current_dataset"]) == 1
    fasta.write_text("corrupt bytes")
    with pytest.raises(ValueError, match="checksum mismatch"):
        snapshot(dataset, spec_path, tmp_path / "bad-snapshot", 2)


def test_audit_checks_all_tokens(reference, tmp_path):
    import h5py

    spec = create_spec(*reference, "epochs")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    fasta = tmp_path / "genome.fna"
    fasta.write_text(">a\n" + "ACGT" * 400)
    directory = tmp_path / "shards"
    for row in spec["cohort"]:
        write_genome(
            directory / f"{row['assembly_accession']}.h5",
            fasta,
            dict(row, comparison_sha256=spec["sha256"]),
            Tokenizer(),
        )
    result = audit(directory, spec_path, tmp_path / "audit.json")
    assert result["n_inputs"] == 4000
    assert result["inputs_per_split"] == {"train": 2000, "val": 1000, "test": 1000}
    with h5py.File(directory / "GCF_1.h5", "r+") as handle:
        handle["input_ids"][500, 512] = 40000
    with pytest.raises(ValueError, match="outside the DNA vocabulary"):
        audit(directory, spec_path, tmp_path / "bad-audit.json")


def test_completed_report_and_incomplete_rejection(reference, tmp_path):
    pytest.importorskip("matplotlib")
    spec = create_spec(*reference, "epochs")
    ref, _ = reference
    cfg = json.loads((ref / "config.json").read_text())
    cfg.update(runtime={"torch": "test"}, comparison_sha256=spec["sha256"])
    target = tmp_path / "chunk-run"
    target.mkdir()
    for directory, input_kind in ((ref, "gene_cds"), (target, "genome_chunks")):
        cfg["input_kind"] = input_kind
        (directory / "config.json").write_text(json.dumps(cfg))
        records = [{"kind": "train", "epoch": 1, "step": 2, "loss": 1.0}]
        for split, taxid in [("val", 3), ("test", 4)]:
            label = float(np.log10(taxid * 10))
            species = [{"ncbi_taxid": taxid, "true_log10": label, "pred_log10": label - 0.1}]
            records.append(
                {
                    "kind": split,
                    "step": 36,
                    "epoch": 18,
                    "species_mae_log10": 0.1,
                    "species_spearman": 0.5,
                    "species_pearson": 0.5,
                    "baseline_species_mae_log10": 0.2,
                    "species": species,
                }
            )
            pd.DataFrame(
                [
                    {
                        "ncbi_taxid": taxid,
                        "scientific_name": f"Species {taxid}",
                        "log10_longevity": label,
                        "pred_log10": label - 0.1,
                        "chunk_id": 0,
                    }
                ]
            ).to_parquet(directory / f"predictions_{split}.parquet")
        records.append({"kind": "done", "step": 36, "train_minutes": 1.0})
        (directory / "metrics.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    (target / "comparison.json").write_text(json.dumps(spec))
    out = tmp_path / "report"
    result = make_report(target, ref, out)
    assert result["cohort"]["assemblies"] == 4
    for name in (
        "README.md",
        "summary.json",
        "learning_curves.png",
        "predictions.png",
        "species_predictions_test.csv",
    ):
        assert (out / name).stat().st_size > 0
    lines = (target / "metrics.jsonl").read_text().splitlines()
    (target / "metrics.jsonl").write_text("\n".join(lines[:-1]))
    with pytest.raises(ValueError, match="not complete"):
        make_report(target, ref, tmp_path / "incomplete-report")


def test_publisher_only_publishes_successful_complete_report(tmp_path, monkeypatch):
    from scripts import publish_chunk_report as publisher

    job, worktree = tmp_path / "job", tmp_path / "worktree"
    job.mkdir()
    worktree.mkdir()
    (job / "pipeline.exit").write_text("1\n")
    commands = []

    def fake_run(command, cwd):
        commands.append(command)
        if command[:3] == ["git", "branch", "--show-current"]:
            return "experiment"
        if command[:3] == ["gh", "pr", "list"]:
            return "[]"
        if command[:3] == ["gh", "pr", "create"]:
            return "https://github.com/example/repo/pull/1"
        return "abc123"

    monkeypatch.setattr(publisher, "run", fake_run)
    with pytest.raises(RuntimeError, match="pipeline failed"):
        publisher.publish(worktree, job, "docs/report")
    assert not commands
    (job / "pipeline.exit").write_text("0\n")
    with pytest.raises(RuntimeError, match="complete report"):
        publisher.publish(worktree, job, "docs/report")
    assert not commands
    report = job / "report"
    report.mkdir()
    (report / "summary.json").write_text("{}")
    (report / "README.md").write_text("Completed test report")
    publisher.publish(worktree, job, "docs/report")
    assert json.loads((job / "publisher-status.json").read_text())["state"] == "published"
    assert ["git", "push", "origin", "experiment"] in commands
