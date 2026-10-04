# Using the saved regressors, starting from genomic sequence

Run commands from the repository root on branch `gi-hepatocyte-tss-shuffle`. Install dependencies:

```bash
uv sync --extra analysis
```

The workflow is **annotated genomic sequence → gene-sense TSS windows → GI expression → saved lifespan regression**. A genome alone does not identify the correct genes and TSSs: provide assembly-matched annotations and human-anchored orthologue IDs. This model expects the same feature definitions and GI context as training.

## 1. Reference-human genome and TSS map

The committed [reference_human_tss.csv](reference_human_tss.csv) contains 3,034 usable gene windows from **GRCh38, Ensembl release 116**. It includes the transcript, contig, zero-based TSS, strand, window coordinates and expected sequence SHA-256. Use the exact primary-assembly FASTA below; `chr1` and `1` are different contig identifiers, so do not rename contigs without also updating the map.

```bash
mkdir -p data/gi-reference
curl --fail --location --continue-at - \
  https://ftp.ensembl.org/pub/release-116/fasta/homo_sapiens/dna/Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz \
  --output data/gi-reference/Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz
```

The compressed file's SHA-256 is `d8c3af0094a7bba6125763bad779ec18a81483c739c6ed122094bdf86c187b92`. Source URLs and checksums for both the FASTA and GTF are in [reference_human_provenance.json](reference_human_provenance.json). The GTF is not required when using the committed TSS map.

Extract windows for both fitted regressors:

```bash
uv run python -m longevity.gi_sequence_lifespan prepare \
  --genome data/gi-reference/Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz \
  --tss-map docs/gi-longevity-final/reference_human_tss.csv \
  --model both \
  --output data/gi-reference/prepared
```

This writes `windows.fasta.gz`, `sequence_qc.csv` and `sequence_provenance.json`. Each FASTA record's first header token is a human Ensembl gene ID. The sequence is 81,920 bp in gene orientation, with the TSS at index 40,960. Minus-strand windows are reverse-complemented with the correct one-base interval offset. Reference hashes must match; insufficient flanks, excessive N or absent contigs are recorded in QC. See [methodology](METHODOLOGY.md) for all rules. Preparation of the full reference took a few minutes on the original machine and requires enough memory for one chromosome.

To run only the 57-feature model, use `--model fdr_genes_ridge` in both preparation and prediction. The reference has 56 valid windows for it; the remaining feature is imputed.

## 2. Run GI expression inference and predict lifespan

Put `GI_API_KEY=gi_...` in a local `.env`, or set `GI_API_KEY` in the environment. Never commit the key. The existing client reads it at runtime and uses Bearer authentication. The expression endpoint is `POST https://api.genomicintelligence.ai/v1/tasks/expression/predict`; see the [official REST guide](https://docs.genomicintelligence.ai/rest-api).

```bash
uv run python -m longevity.gi_sequence_lifespan predict \
  --windows data/gi-reference/prepared/windows.fasta.gz \
  --sample-id homo_sapiens \
  --model both \
  --env-file .env \
  --workers 4 --max-rps 1.5 \
  --output data/gi-reference/predicted
```

The command always sends these values with each DNA sequence:

| Request field | Value |
|---|---|
| `model` | `g0-expression-8192` |
| `tss_index` | `40960` |
| `sequence_name` | `orthologue_tss_window` |
| `options.description` | `Homo sapiens primary hepatocytes, untreated, bulk RNA-seq.` |
| `sequence` | The current gene's 81,920 bases |

**Do not substitute the actual species name in the description.** `--sample-id` labels local output rows only. The same fixed context, including `Homo sapiens`, is used for every species. Do not exponentiate GI expression before regression: feed `expression_log_tpm` directly into the saved preprocessing.

With an empty cache, reference human requires **56 API calls** for the selected model or **3,034 calls** for both/all genes. At the configured 1.5 requests/second this implies roughly 40 seconds or 34 minutes of request spacing, plus preparation, API latency and any retries. These are throughput estimates, not a current service guarantee. The requested GI model must still be available; response metadata is validated and a model/context mismatch fails the run.

For disconnected execution, use a new tmux session from the repository root:

```bash
tmux new-session -d -s gi-reference-lifespan \
  'uv run python -m longevity.gi_sequence_lifespan predict --windows data/gi-reference/prepared/windows.fasta.gz --sample-id homo_sapiens --model both --env-file .env --output data/gi-reference/predicted > data/gi-reference/prediction.log 2>&1'
```

Repeat the same prediction command to resume: responses are cached by the exact sequence/model/context request hash. Successful cached responses are validated before use. `--cache PATH` selects a different response cache; `--cache-only` prohibits network calls and does not require a key. The latter was used for the committed end-to-end reference check against the original GI response cache.

Outputs:

| File | Contents |
|---|---|
| `responses/<request_hash>.json` | Validated GI response cache; remains outside Git |
| `gi_predictions.csv` | One gene per row: predicted log(TPM+1), sequence/request hashes, cached/new status |
| `expression.csv` | One species row, human Ensembl IDs as feature columns |
| `lifespan_predictions.csv` | Predicted natural-log lifespan, predicted maximum lifespan in years, observed/total feature counts, model name |
| `prediction_provenance.json` | Fixed GI context, input/model/code hashes, request counts and imputed gene IDs |

## 3. Expected human reference output

The original GI predictions and saved model parameters give:

| Model | Predicted maximum lifespan | Fitted genes observed |
|---|---:|---:|
| `fdr_genes_ridge` | **32.5275164162 years** | 56/57 |
| `all_genes_ridge` | **46.5555822887 years** | 3,034/3,036 |

Human's recorded AnAge maximum lifespan is **122.5 years**. The models substantially underpredict it. Human lifespan was excluded from all model selection and fitting. Genes `ENSG00000170464` and `ENSG00000266200` lack eligible human windows and receive saved training medians; only the first is in the 57-feature model. These are reference-assembly results, not personal genome or life-expectancy predictions. Future GI model changes could alter fresh predictions even when the public model ID stays the same; the archived expression matrix and cache-based check preserve the numerical result reported here.

For a fast check without downloading a genome or calling GI:

```bash
uv run python scripts/predict_gi_lifespan.py \
  --expression-csv docs/gi-longevity-final/reference_human_expression.csv \
  --model fdr_genes_ridge --output /tmp/human-selected.csv
uv run python scripts/predict_gi_lifespan.py \
  --expression-csv docs/gi-longevity-final/reference_human_expression.csv \
  --model all_genes_ridge --output /tmp/human-all.csv
```

The [reference check](reference_human_check.json) records independent sequence extraction, hash comparisons, validated cached GI inference and agreement with both saved human predictions.

## 4. A different mammalian genome

Provide a plain or gzip FASTA and a CSV with **one unambiguous row per fitted orthologue**:

```csv
human_gene_id,contig,tss0,strand
ENSG00000100439,1,100000,+
ENSG00000151790,2,200000,-
```

These coordinates are illustrative, not biological reference coordinates. Use actual assembly-matched transcript annotations and high-confidence one-to-one orthology to human. `human_gene_id` is the stable ID without a version suffix; `tss0` is the zero-based position of the first transcribed base, on the genomic plus coordinate system. For plus transcripts it is GTF start−1; for minus transcripts it is GTF end−1. Match the canonical complete protein-coding transcript policy from training. Do not reuse the GRCh38 map on another species/assembly, and do not put orthologue species IDs in the `human_gene_id` column.

For exact reference reproduction, retain the optional `sequence_sha256` column. For a genuinely new genome or a changed sequence, omit that reference-hash column and retain provenance for the new input. Run the same preparation and prediction commands with the new FASTA, map and local `--sample-id`. The full contigs must include real flanking DNA. Unknown genes in a window FASTA are ignored; duplicate fitted IDs, invalid DNA windows and invalid responses are rejected. Missing fitted genes are median-imputed, so always inspect feature coverage and QC before interpreting a result. The original training coverage ranged widely; the most incomplete species had larger errors.

Already-extracted windows can be supplied directly to the `predict` stage if they satisfy the exact orientation, length and TSS rules. A single arbitrary genomic fragment cannot be treated as a complete gene-expression feature vector.

## Regression parameters

Use [coefficients.csv](../gi-lifespan-predictor/coefficients.csv) filtered by `model` together with that model's entry in [human_prediction_frozen.json](../gi-lifespan-predictor/human_prediction_frozen.json). For gene j:

```text
x_j = expression_log_tpm, or training_median_j if missing
z_j = (x_j - training_mean_j) / training_scale_j
log_lifespan = intercept_log_years + sum(coefficient_standardized_j * z_j)
maximum_lifespan_years = exp(log_lifespan)
```

`training_scale` is the population standard deviation after training-only imputation. For coefficients on the original expression scale, use `coefficient_original_scale = coefficient_standardized / training_scale` and intercept `intercept_log_years − sum(coefficient_original_scale × training_mean)`. Do not combine original-scale coefficients with the centered intercept unchanged. The parameters contain no body-mass or ancestry terms.

The selected model has alpha=100 and 57 features; the all-gene model has alpha=0.01 and 3,036 features. Feature screening, preprocessing, tuning and fitting all excluded human lifespan. [Full training/CV reproduction instructions](../gi-lifespan-predictor/README.md#reproduce) and [model implementation](../../longevity/gi_lifespan_predictor.py) are included. The two inference commands apply saved parameters; they do not retrain models or select genes using the submitted species.
