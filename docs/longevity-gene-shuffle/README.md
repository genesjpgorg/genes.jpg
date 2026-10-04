# Gene-genomic longevity model: token-shuffle control

Run: `longevity-agingatlas-all-genomic-10ep`. The recorded input is `pairs_genomic.parquet` (gene-genomic sequences, not CDS).

**Checkpoint: `best.pt`, step 76,000**, selected by validation during training. The original train log identifies this checkpoint as the source of final held-out predictions. `model.pt` is the final training checkpoint and is not used in this comparison.

Full validation uses `predictions_val_full.parquet` (335 species); `predictions_val.parquet` is the smaller monitoring subset. Test uses `predictions_test.parquet` (294 species).

| Split | Evaluation | Species | MAE log10 years | MAE years | Pearson log10 | Pearson years | Spearman |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| val | saved | 335 | 0.176242 | 8.442433 | 0.718888 | 0.435842 | 0.699408 |
| val | intact | 335 | 0.176242 | 8.442434 | 0.718888 | 0.435842 | 0.699408 |
| val | shuffled | 335 | 0.212750 | 9.323434 | 0.572517 | 0.322741 | 0.548365 |
| test | saved | 294 | 0.180746 | 9.368050 | 0.672252 | 0.536916 | 0.662103 |
| test | intact | 294 | 0.180746 | 9.368047 | 0.672252 | 0.536917 | 0.662103 |
| test | shuffled | 294 | 0.224832 | 11.328097 | 0.458315 | 0.285960 | 0.414943 |

Predictions are averaged in log10 space per species, then exponentiated for year-scale metrics. Species receive equal weight; missing class annotations do not drop rows.

The intact rerun uses the recorded deterministic evaluation crop: first 1,022 DNA tokens for long inputs. Shuffling happens **after cropping and padding**, so the token multiset, length, attention mask and every CLS/SEP/PAD position stay fixed. Seed 0 permutations are deterministic per assembly, gene and source row, independent of batching. No retraining or label permutation occurs.

The shuffled input distribution differs from training. This tests sensitivity to token order; it does not establish biological causality. See `summary.json` for exact metrics, saved-versus-rerun differences and SHA256 provenance. Per-species CSVs accompany the report; per-input predictions remain on shared storage.

Historical software equivalence remains unverified. The rerun uses the cluster environment recorded in `environment.txt`; agreement with saved predictions is measured explicitly.
