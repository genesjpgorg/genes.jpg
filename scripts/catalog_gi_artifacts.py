"""Write an auditable inventory of GI/lifespan artifacts for downstream agents."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FINAL = ROOT / "docs/gi-longevity-final"
DESCRIPTIONS = {
    "README.md": "Human-readable findings, scope, navigation and interpretation for this analysis.",
    "METHODOLOGY.md": "Complete sequence, association, perturbation, regression and validation methodology.",
    "USAGE.md": "Runnable genome-to-GI-to-lifespan instructions, reference-human values and exact parameter formula.",
    "DATA_DICTIONARY.md": "Field definitions, units, primary keys and distinctions among analysis families.",
    "all_genes.csv": "All 2,001 historical unadjusted Spearman tests, with permutation and asymptotic significance.",
    "hits.csv": "The exact original 30 BH-significant permutation Spearman hits, retaining full statistics.",
    "observations.csv.gz": "63,906 original gene/species observations sufficient to reproduce the 30-hit screen.",
    "monte_carlo_sensitivity.csv": "Sensitivity of marginal discoveries to permutation-tail simulation uncertainty.",
    "shuffle_comparison.csv": "Six-gene join of original-screen statistics and fresh-native/shuffled association statistics.",
    "reference_human_tss.csv": "3,034 GRCh38/Ensembl116 TSS coordinates, orientations and expected DNA hashes.",
    "reference_human_expression.csv": "One-row reference-human expression vector, with the two unavailable genes left missing.",
    "reference_human_provenance.json": "Reference FASTA/GTF source URLs and digests, missing genes and expected human predictions.",
    "reference_human_check.json": "Verification of independent sequence extraction, cached GI responses and both regressor outputs.",
    "coefficients.csv": "Reusable fitted weights and training medians, means, scales; filter by model before inference.",
    "human_prediction_frozen.json": "Saved model intercepts, penalties, feature lists, human predictions and training species.",
    "expression_matrix.csv.gz": "Full 48-species by 3,036-gene expression matrix; missing entries remain empty.",
    "training_species.csv": "47 nonhuman species and their lifespan labels; excludes human from all fitting.",
    "gene_annotations.csv": "Human Ensembl gene IDs and display names for all 3,036 regression features.",
    "nonhuman_gene_screen.csv": "Training-only 3,036-gene asymptotic Spearman screen defining the 57-feature final model.",
    "final_model_tuning.csv": "Inner-validation scores for fixed ridge penalties, and the chosen alpha for each model.",
    "cross_validation_predictions.csv": "141 outer held-out predictions: 47 nonhumans times two regressors and baseline.",
    "cross_validation_metrics.csv": "Nonhuman held-out performance summaries; errors in years and natural-log years.",
    "cv_selected_genes.csv": "Selected genes in each outer training fold; use for feature-stability assessment.",
    "human_validation.csv": "Held-out human predictions compared with recorded maximum lifespan of 122.5 years.",
    "coverage_diagnostics.csv": "Descriptive prediction-error comparison between lower and higher expression coverage.",
    "gene_summary.csv": "Six candidate genes' native and shuffled-mean rho/p/q, replicate ranges and paired attenuation intervals.",
    "species_comparison.csv": "187 native/shuffled gene-species pairs, with mean, range and SD of shuffled expression.",
    "predictions.csv": "All 2,057 native/shuffled numeric predictions, sequence/request hashes and scoring bounds; no DNA.",
    "replicate_correlations.csv": "60 descriptive Spearman rho values: six genes times ten shuffle replicate indices.",
    "inference_qc.json": "API counts, native-control agreement, sequence QC and scoring-window diagnostics.",
    "protocol.json": "Frozen design and hyperparameters for the containing experiment.",
    "provenance.json": "Source identity, snapshot and checksums for the containing experiment/export.",
    "source_provenance.json": "Full-run provenance and input-table digests for regression.",
    "preparation_provenance.json": "Sequence-preparation code and input/output hashes for the shuffle experiment.",
    "analysis_provenance.json": "Shuffle-analysis code, input hashes and resampling parameters.",
    "candidates.csv": "The six selected original candidates and their historical screen statistics.",
    "species_expression.csv": "The original 187 observations plotted for the six candidates.",
    "shuffle-design.md": "Pre-perturbation design, input geometry, controls and disconnected execution instructions.",
}
FIGURES = {
    "lifespan_expression": "Original six-candidate lifespan versus expression scatterplots; q family is 3,036.",
    "native_vs_shuffled": "Native and shuffled-mean expression scatterplots; error bars are ten-shuffle min-max.",
    "correlations": "Native rho, shuffled-mean rho and individual replicate rho for the six candidates.",
    "heldout_predictions": "Observed versus predicted maximum lifespan; nested nonhuman holdouts and separate human holdout.",
}
CODE = {
    "longevity/gi_sequence_lifespan.py": "Prepare gene-sense TSS FASTA; run fixed-context GI inference; apply saved lifespan models.",
    "longevity/gi_lifespan_predictor.py": "Training-only feature screening, nested CV, ridge fitting, export and numeric inference.",
    "longevity/gi_api.py": "Validated fixed-context GI client, request hashing, response caching and inference runner.",
    "longevity/gi_prepare.py": "Mammal/orthologue discovery, canonical transcript and oriented-window preparation.",
    "longevity/gi_tss_shuffle.py": "Reproducible local block shuffles and native/shuffled API prediction.",
    "longevity/gi_tss_shuffle_report.py": "Paired species analysis, permutation tests, bootstrap and perturbation figures.",
    "scripts/reproduce_gi_spearman.py": "Verify all original Spearman rho/p/q and extreme counts from committed observations.",
    "scripts/export_gi_final_artifacts.py": "Export original 30-hit results and human reference manifests from the archived dataset.",
    "scripts/catalog_gi_artifacts.py": "Generate this inventory, including artifact checksums and table schemas.",
    "scripts/predict_gi_lifespan.py": "Apply saved weights to a numeric expression CSV without retraining.",
    "scripts/plot_gi_candidates.py": "Regenerate original six-candidate scatterplots from committed CSVs.",
    "scripts/run_gi_tss_shuffle.sh": "Run/resume shuffled inference and publish its final report.",
    "configs/gi-hepatocytes.json": "Original fixed model/context and preparation configuration.",
    "pyproject.toml": "Python project and analysis dependencies; use uv sync --extra analysis.",
    "uv.lock": "Locked Python dependency resolutions.",
}


def main():
    files = set()
    for directory in [
        FINAL,
        ROOT / "docs/gi-lifespan-predictor",
        ROOT / "docs/gi-longevity-candidates",
    ]:
        files.update(p for p in directory.rglob("*") if p.is_file() and p.name != "artifacts.json")
    files.update(ROOT / name for name in CODE)
    files.update(ROOT.glob("tests/test_gi*py"))
    entries = []
    for path in sorted(files):
        relative = path.relative_to(ROOT).as_posix()
        description = CODE.get(relative, DESCRIPTIONS.get(path.name))
        if path.stem in FIGURES:
            description = FIGURES[path.stem]
        if relative.startswith("tests/"):
            description = (
                "Focused automated verification for " + path.stem.removeprefix("test_") + "."
            )
        if description is None:
            raise ValueError(f"Missing artifact description: {relative}")
        entry = {
            "path": relative,
            "description": description,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        if path.name.endswith((".csv", ".csv.gz")):
            frame = pd.read_csv(path)
            entry["kind"] = "table"
            entry["rows"] = len(frame)
            entry["column_count"] = len(frame.columns)
            if len(frame.columns) > 100:
                entry["index_column"] = {"name": frame.columns[0], "dtype": "string"}
                entry["feature_columns"] = {
                    "count": len(frame.columns) - 1,
                    "names_from": "docs/gi-lifespan-predictor/gene_annotations.csv:human_gene_id",
                    "name_pattern": "ENSG followed by 11 digits",
                    "dtype": "floating point or missing",
                    "unit": "GI-predicted log(TPM+1)",
                }
            else:
                entry["columns"] = [{"name": c, "dtype": str(frame[c].dtype)} for c in frame]
            entry["field_definitions"] = "docs/gi-longevity-final/DATA_DICTIONARY.md"
        elif path.suffix in [".png", ".svg", ".pdf"]:
            entry["kind"] = "figure"
            entry["presentation_guidance"] = (
                "docs/gi-longevity-final/README.md#figures-for-presentations"
            )
        else:
            entry["kind"] = {
                ".md": "documentation",
                ".json": "metadata",
                ".py": "code",
                ".sh": "code",
            }.get(path.suffix, "configuration")
        entries.append(entry)
    summary = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "paths_relative_to": "repository root",
        "entry_point": "docs/gi-longevity-final/README.md",
        "manifest_self_checksum": "omitted to avoid circular hashing",
        "study_scopes": {
            "original_screen": {
                "genes_tested": 2001,
                "observations": 63906,
                "planned_bh_family": 3036,
                "bh_hits": 30,
                "human_included": True,
                "ancestry_or_mass_adjustment": False,
            },
            "shuffle": {
                "candidate_genes": 6,
                "species_gene_pairs": 187,
                "shuffles_per_pair": 10,
                "total_api_predictions": 2057,
                "bh_family": 6,
                "shuffled_mean_bh_hits": 0,
            },
            "regression": {
                "genes_available": 3036,
                "training_species": 47,
                "human_held_out_from_all_training": True,
                "selected_model_genes": 57,
                "all_gene_model_genes": 3036,
                "target": "natural log of recorded maximum lifespan in years",
            },
        },
        "parameter_contract": {
            "weights": "docs/gi-lifespan-predictor/coefficients.csv",
            "intercepts_and_features": "docs/gi-lifespan-predictor/human_prediction_frozen.json",
            "formula_and_commands": "docs/gi-longevity-final/USAGE.md",
        },
        "artifacts": entries,
    }
    (FINAL / "artifacts.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"Cataloged {len(entries)} artifacts")


if __name__ == "__main__":
    main()
