"""Apply the frozen GI expression-to-lifespan model to an expression CSV."""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from longevity.gi_lifespan_predictor import MODELS, predict_exported


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", default="docs/gi-lifespan-predictor")
    parser.add_argument("--model", choices=MODELS, default="fdr_genes_ridge")
    parser.add_argument(
        "--expression-csv",
        required=True,
        help="Rows are species; first column is species name; remaining columns are human Ensembl gene IDs with GI expression_log_tpm values",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    expression = pd.read_csv(args.expression_csv, index_col=0)
    predict_exported(args.model_dir, expression, args.model).to_csv(args.output)


if __name__ == "__main__":
    main()
