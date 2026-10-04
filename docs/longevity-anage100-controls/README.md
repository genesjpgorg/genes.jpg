# longevity-anage100-chunks: intact and shuffled-token evaluation

The same final checkpoint is evaluated on the exact same validation/test examples.
Only the order of DNA tokens within each input is changed; CLS/SEP, labels, splits
and the token multiset stay fixed. This is an evaluation-time perturbation, not
a separately trained model. Per-example permutations are fixed and reproducible
with shuffle seed 0.

| Split | Input | Species | MAE log10 years | MAE years | Pearson log10 | Pearson years | Spearman |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| val | intact | 16 | 0.1993 | 7.4362 | 0.2335 | 0.3257 | 0.3941 |
| val | shuffled | 16 | 0.1688 | 5.9658 | 0.2487 | 0.3664 | 0.3735 |
| test | intact | 18 | 0.1878 | 10.3016 | 0.2992 | 0.2922 | 0.3416 |
| test | shuffled | 18 | 0.2045 | 11.2619 | 0.3544 | 0.3146 | 0.2611 |

Predictions are first averaged in log10 space within species (the training
protocol), then exponentiated for raw-year metrics. Every species has equal
weight in these metrics, including species with missing class annotations.
Spearman is identical under the monotonic log/year transformation; undefined
correlations (constant predictions or fewer than two species) are reported explicitly.

A drop under shuffling measures sensitivity to token order. The shuffled examples
are out of the training distribution, so this does not by itself establish biological
causality or substitute for a model trained on shuffled inputs.

See [summary.json](summary.json) for checkpoint/input provenance and the per-species
CSV files for observed and predicted longevity. Training configuration and the frozen
cohort/split specification accompany this report.
