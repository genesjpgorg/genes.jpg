# Gene-genomic longevity evaluation: saved metrics

These metrics reproduce the saved full held-out predictions from `best.pt` at step 76,000.
The source run uses `pairs_genomic.parquet`. Full validation is `predictions_val_full.parquet`, not the monitoring subset.

**Intact GPU reproduction and shuffled-input evaluation are running; those results will replace this interim report automatically.**

| Split | Species | MAE log10 | MAE years | Pearson log10 | Pearson years | Spearman |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| val | 335 | 0.176242 | 8.442433 | 0.718888 | 0.435842 | 0.699408 |
| test | 294 | 0.180746 | 9.368050 | 0.672252 | 0.536916 | 0.662103 |

Metrics use mean log10 prediction per species, then exponentiation for year-scale metrics. Every species has equal weight.

The shuffle control uses the same checkpoint and input examples. It preserves every CLS/SEP/PAD position and the attention mask, and permutes only DNA tokens after the deterministic evaluation crop.

Remote tmux: `longevity-gene-shuffle`. Shared logs: `runs/longevity-gene-shuffle-job/evaluation.log`. It shares a GPU lock with the pending random-chunk training job.
