"""Export the frozen 30-hit screen and reference-human sequence manifest."""

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def export(root, output):
    snapshot = root / "analyses/broad/associations/20261004T104842Z"
    original = snapshot / "independent_species/20261004T113306Z"
    directory = output / "spearman"
    directory.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(original / "gene_associations.csv", float_precision="round_trip")
    columns = ["human_gene_id", "gene_name", "n_species", "status"] + [
        c for c in source if c.startswith("spearman_")
    ]
    result = source[columns].copy()
    result["significant_bh_0_05"] = result.spearman_q_bh_planned < 0.05
    result["significant_by_0_05"] = result.spearman_q_by_planned < 0.05
    hits = result[result.significant_bh_0_05]
    assert len(result) == 2001 and len(hits) == 30
    result.to_csv(directory / "all_genes.csv", index=False)
    hits.to_csv(directory / "hits.csv", index=False)
    observations = pd.read_parquet(snapshot / "expression_snapshot.parquet")
    observations[
        [
            "human_gene_id",
            "gene_name",
            "ensembl_species",
            "anage_order",
            "max_longevity_yrs",
            "expression_log_tpm",
            "request_hash",
        ]
    ].to_csv(
        directory / "observations.csv.gz", index=False, compression={"method": "gzip", "mtime": 0}
    )
    provenance = json.loads((original / "provenance.json").read_text())
    provenance.update(
        export_source_results_sha256=sha(original / "gene_associations.csv"),
        exported_all_genes_sha256=sha(directory / "all_genes.csv"),
        exported_observations_sha256=sha(directory / "observations.csv.gz"),
        snapshot_genes=2001,
        observations=63906,
        significant_bh=30,
        export_script_sha256=sha(__file__),
    )
    (directory / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    pd.read_csv(original / "monte_carlo_sensitivity.csv").to_csv(
        directory / "monte_carlo_sensitivity.csv", index=False
    )
    model_dir = output.parent / "gi-lifespan-predictor"
    universe = set(pd.read_csv(model_dir / "gene_annotations.csv").human_gene_id)
    human = pd.read_parquet(root / "window_metadata/homo_sapiens.parquet")
    human = human[human.human_gene_id.isin(universe)].sort_values("human_gene_id")
    manifest = pd.read_parquet(root / "analyses/broad/prediction_manifest.parquet")
    manifest = manifest[manifest.ensembl_species == "homo_sapiens"]
    assert len(human) == 3034 and set(human.human_gene_id) == set(manifest.human_gene_id)
    human[
        [
            "human_gene_id",
            "gene_name",
            "transcript_id",
            "contig",
            "tss0",
            "strand",
            "ensembl_assembly",
            "window_start0",
            "window_end0",
            "sequence_sha256",
        ]
    ].to_csv(output / "reference_human_tss.csv", index=False)
    matrix = pd.read_csv(model_dir / "expression_matrix.csv.gz", index_col=0)
    matrix.loc[["homo_sapiens"]].to_csv(output / "reference_human_expression.csv")
    sources = [
        json.loads(p.read_text())
        for p in sorted((root / "sources/homo_sapiens").glob("*.source.json"))
    ]
    (output / "reference_human_provenance.json").write_text(
        json.dumps(
            {
                "assembly": "GRCh38",
                "ensembl_release": 116,
                "sources": sources,
                "manifest_sha256": sha(output / "reference_human_tss.csv"),
                "expression_sha256": sha(output / "reference_human_expression.csv"),
                "absent_genes_imputed": sorted(universe - set(human.human_gene_id)),
                "reference_predictions": {
                    "fdr_genes_ridge": 32.52751641619777,
                    "all_genes_ridge": 46.55558228872374,
                },
                "observed_human_maximum_lifespan_years": 122.5,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("docs/gi-longevity-final"))
    args = parser.parse_args()
    export(args.dataset_root, args.output)
