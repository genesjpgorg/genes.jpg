# Artifact data dictionary

All paths below are relative to this directory. CSV files have headers; `.csv.gz` files are gzip-compressed CSV. Numeric JSON/CSV values retain more precision than the human-readable reports. The [artifact manifest](artifacts.json) lists exact row counts, columns, sizes and SHA-256 values. Missing expression is encoded as an empty CSV value and must not be replaced with zero.

## Keys, units and biological labels

| Field | Meaning |
|---|---|
| `human_gene_id` | Stable human Ensembl gene ID; the primary gene key across species and models |
| `gene_name` | Display symbol; may be unavailable or species-specific. Do not use it as a join key |
| `gene_id`, `transcript_id` | Native species' annotated gene/transcript IDs |
| `ensembl_species` | Species key, usually lowercase with underscores; one row is one species, not one animal |
| `anage_order` | Original AnAge order label, used for display/diagnostics, not a predictor |
| `max_longevity_yrs`, `observed_years` | Recorded maximum species lifespan in years |
| `expression_log_tpm` | GI output in log(TPM+1); do not log-transform it again or exponentiate it before applying saved weights |
| `adult_weight_g` | Adult body mass in grams, retained as metadata in some legacy tables; not used in the requested raw correlations or regressors |
| `sequence_sha256`, `request_hash` | DNA digest and digest of the complete GI request, respectively; audit keys, not biological features |
| `contig`, `tss0`, `strand` | Assembly contig, zero-based genomic TSS position and gene orientation (+/−) |
| `window_start0`, `window_end0` | Zero-based half-open genomic interval, before any reverse complementation |
| `ensembl_assembly` | Genome assembly identifier; the reference-human table uses GRCh38 |

## Original Spearman screen

`spearman/all_genes.csv` has 2,001 rows; `spearman/hits.csv` is its exact 30-row BH-significant subset. `spearman/observations.csv.gz` has 63,906 gene–species observations. Join using `human_gene_id` and `ensembl_species`.

| Column | Meaning |
|---|---|
| `n_species` | Species with an observed expression prediction for this gene |
| `status` | Original test completion status; all archived tests completed successfully |
| `spearman_rho` | Rank correlation, in [−1,1]; positive means higher predicted expression in longer-lived species |
| `spearman_p` | Primary two-sided Monte Carlo permutation p value, with +1 correction |
| `spearman_permutations`, `spearman_extreme_count` | Number of null draws and draws as or more extreme in absolute rho |
| `spearman_mc_p_low`, `spearman_mc_p_high` | Marginal 95% binomial interval for the permutation tail probability; numerical, not biological, uncertainty |
| `spearman_q_bh_planned` | BH-adjusted permutation p over 3,036 planned genes; defines the 30-hit list at q<0.05 |
| `spearman_q_by_planned` | BY-adjusted permutation p over the same family |
| `spearman_asymptotic_p`, `spearman_asymptotic_q_bh_planned` | Alternative asymptotic significance; not the primary 30-hit test |
| `significant_bh_0_05`, `significant_by_0_05` | Explicit Boolean flags for the corresponding q<0.05 thresholds |

`monte_carlo_sensitivity.csv` applies marginal simulation-interval endpoints to assess threshold sensitivity. Its scenarios are not simultaneous confidence bounds on the total number of discoveries. `provenance.json` preserves original input/code checksums and adds export checksums; the original code digest describes the historical multi-assay implementation, while the new reproduction script verifies the raw Spearman result alone.

## Shuffled data

`shuffle_comparison.csv` has one row for each of the **six perturbed genes**. It joins the existing [shuffle summary](../gi-longevity-candidates/shuffle/gene_summary.csv) to the original screen without changing either calculation.

| Field or family | Meaning |
|---|---|
| `original_screen_rho`, `original_screen_permutation_p`, `original_screen_q_bh_3036` | Historical unadjusted screen; q covers 3,036 genes |
| `original_rho`, `native_rho` | Original/fresh-native rho; these agree because fresh predictions exactly reproduce the baseline |
| `native_permutation_p`, `native_q_bh_six` | New simulation of native p and its correction across the six candidates; not the original screen q |
| `shuffled_mean_rho`, `shuffled_mean_permutation_p`, `shuffled_mean_q_bh_six` | Primary shuffled association based on each species' mean log-expression over ten shuffles; q covers six genes |
| `shuffled_significant_bh_0_05` | q<0.05 for the primary shuffled-mean association; false for all six |
| `shuffle_replicates` | Ten independent block permutations per gene–species pair |
| `shuffle_rho_median`, `shuffle_rho_min`, `shuffle_rho_max` | Summary of ten descriptive correlations, one across species for each replicate index |
| `replicates_same_direction_as_original` | Count, out of ten, with rho of the original sign |
| `signed_rho_attenuation` | sign(original rho) × (native rho − shuffled-mean rho); positive means weaker in the original direction |
| `attenuation_bootstrap_ci_low/high` | Exploratory 95% paired-species bootstrap interval for signed attenuation |
| `native_retest_max_abs_difference` | Largest absolute difference in native expression from the original predictions; zero for every gene |
| `mean_log_expression_change` | Mean over species of shuffled-mean minus native log(TPM+1) |
| `native_shuffle_mean_rho` | Correlation of native expression with shuffled-mean expression across species, rather than with lifespan |

The shuffle directory's `predictions.csv` contains all 2,057 numeric predictions. `condition` is `native` or `shuffled`; `replicate=-1` denotes native and 0–9 denote shuffles. `scored_window_start/end` are API-reported input-relative bounds, not genomic coordinates. `species_comparison.csv` holds one row per gene/species, including native, shuffle mean, min, max and sample SD. `replicate_correlations.csv` holds 60 descriptive rho values. Min–max error bars visualize the ten perturbations, not uncertainty in lifespan or a confidence interval.

## Regression inputs, parameters and validation

In `../gi-lifespan-predictor/`, `expression_matrix.csv.gz` has 48 species rows and 3,036 gene columns; the first column is the species index. `training_species.csv` contains only the 47 nonhumans and their targets. `gene_annotations.csv` maps feature IDs to symbols.

| File / fields | Meaning |
|---|---|
| `coefficients.csv` | One row per model/feature: 57 selected-model rows plus 3,036 all-gene rows |
| `model` | `fdr_genes_ridge`, `all_genes_ridge`, or `baseline` in validation tables |
| `coefficient_standardized` | Weight multiplying standardized expression |
| `coefficient_original_scale` | Standardized weight divided by training scale; requires an appropriately shifted intercept |
| `training_median`, `training_mean`, `training_scale` | Saved imputation value and post-imputation centering/scaling values |
| `training_spearman_rho/p`, `training_q_bh`, `training_n_observed` | Final nonhuman association screen and observed counts for each feature |
| `human_prediction_frozen.json` | Model-specific `intercept_log_years`, alpha, selected feature IDs, training species and frozen human predictions; `human_outcome_used=false` |
| `nonhuman_gene_screen.csv` | All 3,036 training-only association tests and eligibility/selection flags; final selected set has 57 genes |
| `final_model_tuning.csv` | Inner-fold MSE for each fixed alpha; `selected` identifies the chosen penalty |
| `cv_selected_genes.csv` | Selected features for each outer held-out species, before its prediction; feature stability diagnostics |
| `cross_validation_predictions.csv` | 141 rows: three models × 47 nonhuman test species; each prediction comes from its own nested training run |
| `n_features`, `features_observed` | Number fitted in that outer model and number available in its test species |
| `expression_genes_observed` | Overall expression coverage for the test species, independent of feature selection |
| `predicted_log_years`, `predicted_years` | Natural-log lifespan and exponentiated lifespan, respectively |
| `cross_validation_metrics.csv` | MAE/median absolute error in years, RMSE and R² on natural-log lifespan, and Spearman correlation across test species |
| `human_validation.csv` | Human observed/predicted years and signed error = prediction−observation |
| `coverage_diagnostics.csv` | Errors below/above median coverage; descriptive, not a second training procedure |

`reference_human_expression.csv` is a one-row slice of the saved full matrix, including two empty features. `reference_human_check.json` records the independent FASTA-to-cached-GI-to-regression check. `reference_human_provenance.json` identifies the assembly, annotation release, source URLs, digests, missing features and expected values. Genomic sequence never serves as the regression feature directly; it is first converted into the GI expression vector.

## Files for agents and presentations

PNG, SVG and PDF variants depict the same data; their suggested captions are in the [guide](README.md#figures-for-presentations). Source scripts regenerate figures from numeric tables. Markdown files state scope and limitations; JSON provenance files establish identity of sources/code rather than statistical evidence. `artifacts.json` inventories these roles, includes schemas and checksums, and intentionally excludes its own checksum to avoid a circular digest. Do not treat file byte order, row numbers or gene symbols as stable analysis keys.
