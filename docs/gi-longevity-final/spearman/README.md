# Frozen unadjusted Spearman results: 30 significant genes

This is the original 2,001-gene snapshot (63,906 species–gene observations), frozen on 2026-10-04 at 10:48:42 UTC and analyzed at 11:33:06 UTC. Species are independent observations. Neither body mass nor ancestry is adjusted. These are associations between recorded maximum lifespan and GI-predicted hepatocyte expression.

Two-sided permutation p values use 999,999 unrestricted permutations per gene, tied ranks retained, gene-specific seeds derived from base seed 8192, and the +1 correction. BH correction covers the 3,036 planned genes, including the 1,035 not yet analyzed as p=1. The threshold is BH q < 0.05; all 30 rows below pass. BY is a separate, more conservative correction: five genes pass.

[All 2,001 results](all_genes.csv) · [Exact 30-hit CSV](hits.csv) · [Observations](observations.csv.gz) · [Provenance](provenance.json) · [Monte Carlo sensitivity](monte_carlo_sensitivity.csv)

| Gene | Human Ensembl ID | Species | Spearman rho | Permutation p | BH q (3,036 genes) | BY q |
|---|---|---:|---:|---:|---:|---:|
| ABHD4 | ENSG00000100439 | 30 | -0.774165 | 1e-06 | 0.001518 | 0.0130482 |
| TDO2 | ENSG00000151790 | 35 | +0.721331 | 1e-06 | 0.001518 | 0.0130482 |
| CCDC85A | ENSG00000055813 | 30 | +0.751224 | 3e-06 | 0.003036 | 0.0260965 |
| LGI1 | ENSG00000108231 | 32 | -0.747754 | 5e-06 | 0.003795 | 0.0326206 |
| ACSS1 | ENSG00000154930 | 30 | +0.728304 | 7e-06 | 0.0042504 | 0.0365351 |
| ATG14 | ENSG00000126775 | 30 | -0.710948 | 1.9e-05 | 0.009614 | 0.0826388 |
| SLC12A1 | ENSG00000074803 | 35 | -0.646445 | 4.1e-05 | 0.0177823 | 0.152851 |
| DSTYK | ENSG00000133059 | 34 | -0.640783 | 5.8e-05 | 0.0209147 | 0.179776 |
| CRHR2 | ENSG00000106113 | 31 | -0.672720 | 6.2e-05 | 0.0209147 | 0.179776 |
| WDFY1 | ENSG00000085449 | 33 | +0.647930 | 6.9e-05 | 0.0209484 | 0.180066 |
| RBM46 | ENSG00000151962 | 30 | -0.666889 | 7.9e-05 | 0.021804 | 0.18742 |
| HSPB8 | ENSG00000152137 | 33 | -0.628710 | 0.00012 | 0.0292757 | 0.251645 |
| BMP6 | ENSG00000153162 | 30 | -0.651830 | 0.000133 | 0.0292757 | 0.251645 |
| FGF7 | ENSG00000140285 | 36 | +0.604674 | 0.000135 | 0.0292757 | 0.251645 |
| RSU1 | ENSG00000148484 | 34 | -0.613579 | 0.000157 | 0.0301702 | 0.259334 |
| TGFBR1 | ENSG00000106799 | 31 | -0.634456 | 0.000159 | 0.0301702 | 0.259334 |
| SPC25 | ENSG00000152253 | 32 | -0.620772 | 0.000203 | 0.0345145 | 0.296676 |
| RGS1 | ENSG00000090104 | 32 | +0.617655 | 0.000205 | 0.0345145 | 0.296676 |
| HELLS | ENSG00000119969 | 30 | -0.636212 | 0.000216 | 0.0345145 | 0.296676 |
| SCUBE3 | ENSG00000146197 | 30 | -0.627309 | 0.000294 | 0.04209 | 0.361792 |
| RPE65 | ENSG00000116745 | 35 | -0.586031 | 0.000302 | 0.04209 | 0.361792 |
| CALB1 | ENSG00000104327 | 34 | -0.591854 | 0.000305 | 0.04209 | 0.361792 |
| NDRG1 | ENSG00000104419 | 31 | +0.615369 | 0.000322 | 0.042504 | 0.365351 |
| ITGA3 | ENSG00000005884 | 30 | -0.615932 | 0.000364 | 0.0450729 | 0.387432 |
| TRAF3IP3 | ENSG00000009790 | 36 | -0.569351 | 0.00038 | 0.0450729 | 0.387432 |
| ASCC1 | ENSG00000138303 | 32 | -0.597690 | 0.000386 | 0.0450729 | 0.387432 |
| EPHB1 | ENSG00000154928 | 30 | +0.616001 | 0.000401 | 0.0450902 | 0.387581 |
| EPYC | ENSG00000083782 | 40 | -0.534563 | 0.000425 | 0.0460821 | 0.396107 |
| TNFSF8 | ENSG00000106952 | 30 | -0.613022 | 0.000449 | 0.0463496 | 0.398406 |
| DLST | ENSG00000119689 | 31 | -0.599596 | 0.000458 | 0.0463496 | 0.398406 |

The CSV retains full numerical precision. The same snapshot yields 33 hits using asymptotic Spearman p values; those are not the requested 30-hit result. The later regression screen uses all 3,036 genes, excludes human from training, and selects 57 genes using asymptotic p values. These three counts refer to different analyses.

Monte Carlo uncertainty can affect marginal hits: replacing the simulated p values with their marginal interval endpoints gives 27–32 hits. This is a numerical sensitivity analysis, not a simultaneous confidence interval for the number of discoveries. The archived 30-hit list remains unchanged.

Reproduce all historical rho, permutation p, BH q and extreme counts from the committed observations (about 2–3 minutes with four workers on the original machine):

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 uv run python scripts/reproduce_gi_spearman.py --output /tmp/gi-spearman-reproduced.csv
```

For figures and interpretation, see the [artifact guide](../README.md) and [data dictionary](../DATA_DICTIONARY.md).
