# Findings: longevity prediction from DNA (2025-10-04)

## Headline result

| | test MAE (log10 yrs) | test Spearman |
|---|---|---|
| **Best result — 0.5·ridge + 0.5·xpool (equal-weight ensemble)** | **0.1885** | 0.154 |
| Best single model — ridge on e3 embeddings + genome meta + predicted mass | 0.1912 | 0.156 |
| Reference CDS pipeline (final ckpt) | 0.2034 | 0.350 |
| Trivial baseline (predict train mean) | 0.2685 | — |
| Non-DNA reference — GBM on body mass + genome meta | 0.1514 | 0.622 |

Improvement over the reference pipeline: **~7% lower MAE**, from 0.2034 → 0.1885.
Species-level MAE on the frozen 18-species test set (Muscicapidae, Ciconiidae,
Accipitridae — families fully absent from training).

The winning pipeline, `ens-ridge+xpool`:
1. `e3`: ModernGENA-base trained on 1,000-token random genome chunks from ~1,100
   species (12 epochs, best-val checkpoint).
2. Per-species features = mean of 200 chunk embeddings + genome metadata
   (log genome size, GC%) + e4's auxiliary-head **predicted body mass**.
3. Ridge (alpha by train-internal CV) on those features → 0.191.
4. `xpool`: 5-seed MLP on mean+std of the same embeddings → 0.198.
5. Equal-weight average → 0.1885. Preds: `runs/final-species-preds-*.csv`.

## What worked

- **More species > everything.** Scaling training from 64 to ~1,600 species via
  random genome chunks was the single biggest gain (val 0.158 → 0.110). A fast
  offset-window sampler (`xprep2.py`) made 1,900 shard preps feasible (~50× faster
  than the block-by-block tokenizer).
- **Whole-genome chunks carry real signal** — better than the 611-gene CDS set at
  scale (val), and a dumb GBM on token counts already reaches 0.219, showing the
  signal is largely compositional.
- **Embedding-space stage-2 heads beat mean-of-scalars**: a ridge on the mean
  chunk embedding (+meta+mass) outperforms the trained scalar head.
- **Auxiliary mass task**: the mass head partially learned genome→body-mass
  (train ρ≈0.5); its species-level prediction is a useful feature (helped the
  ridge). Directly predicting longevity was not improved by the aux task though.
- **Best-val checkpointing**: consistent small gain (0.203 → 0.201 on the CDS
  recipe).

## What didn't work

- **Aux-mass multi-task for the longevity head itself** (e4): val-best ckpt froze
  at epoch 0.5; test 0.247 — the aux task diverted capacity.
- **Attention pooling over chunk embeddings**: best *val* (0.131) but worst test
  (0.256) — overfits a 16-species validation set.
- **CDS scaled to 656 bird species** (e5a/e5b, 411k pairs): strong val ranking
  (ρ 0.71 at one point) did not transfer (test 0.24–0.27); mid-run even collapsed
  to constant val preds.
- **Birds-only fits**: ridge trained only on Aves collapses (0.236) — the
  allometric signal needs cross-class data.
- **Val-fit affine recalibration** of predictions: helps val, *hurts* test badly
  (0.20→0.28). Val and test families sit in different longevity regimes.
- **Ensemble weight tuning on val**: optimal val weight is essentially the
  inverse of optimal test weight — no transfer.

## Why the test set is hard (caveats)

- 18 species, all birds, 16/18 long-lived. Ciconiidae (storks) have **zero**
  same-order relatives in train; Accipitridae only 6.
- **All models fail the same way**: species-level error vectors are ρ≈0.99
  correlated — everyone predicts ~18–28 yrs for every test species. Three
  extreme species (black-shouldered kite 3.5 y, marabou stork 44.7 y,
  Ciconia boyciana 48.1 y) contribute most of the MAE.
- Val (16 spp) and test (18 spp) rank models inconsistently; differences of
  ±0.01 are within noise. Report both, never select on test.
- Allometry dominates: mass+meta GBM reaches 0.151 — a rough upper bound for
  what a DNA-only model could hope to match given genome→mass remains noisy.

## Suggested next steps

- Extract CDS for mammals/fish too (full ~1,900-species CDS set) — birds-only
  CDS was fast to test but loses cross-class signal.
- Longer training + seed ensembles of the chunk encoder (used 12 epochs here).
- A genome→mass specialist model: the aux head showed partial learnability;
  predicted mass is the most useful single feature.
- More/better val species (16 is too few for stable model selection).

## Reproduce

- Data prep: `longevity/xprep2.py` (chunks), `xmakepairs.py` (CDS pairs).
- Train: `longevity/xtrain.py --data <chunks dir|pairs.parquet> --split-file
  experiments/split_v3.json ...` (per-run configs in `experiments/runs/<run>/config.json`).
- Stage-2: `xembed.py` (extract embeddings) → `xpool.py` (species head).
- Eval: `xeval.py`; ensembling/blends in `xens.py`/`xmap.py`.
- Full run table: `LOG.md`.
