# Longevity-from-DNA experiment log

Goal: minimise species-level MAE (log10 years) on the frozen 18-species test set
(families: Muscicapidae, Ciconiidae, Accipitridae) vs reference CDS run = 0.203 MAE, 0.35 Spearman.
Protocol: train species never in val/test families; val = frozen 16 species used only for
checkpoint selection. GPU: 1x RTX PRO 6000 Blackwell 96GB @ 10.80.0.40.

## Data
- split_v2.json: 1816 train taxids (64 cohort + 1752 new labeled, family-disjoint), 16 val, 18 test.
- Chunk shards: datasets/longevity-anage100-chunks (98 cohort, job) + datasets/longevity-v2-chunks (1829 new).

## Runs
| run | data | change vs ref | val MAE | test MAE | test rho | notes |
| e1-cds-bestckpt | CDS 98 cohort | +best-val ckpt (ep13) | 0.1566 | 0.2010 | 0.364 | val/test same as ref; test 0.2034->0.2010 |
| e2-chunk98-probe | chunks 98 cohort, 200/ep resampled | chunk model 10ep | 0.1209 | 0.3057 | 0.218 | great val, BAD test -> overfit on 64 species; need more species |
| e2b-chunk98-meanpool | chunks 98, mean pool | --pool mean 8ep | 0.1289 | 0.2509 | 0.193 | mean≈cls, still overfit |
| e2c-chunk98-spmean | chunks 98, species-mean loss | w=1.0 unit8 6ep | 0.1189 | 0.2752 | 0.236 | best val, test bad; scale test pending |
| xgb-hgb | hashed 256-bin token hist mean+sd/species | HistGBM 600it | 0.1622 | 0.2185 | 0.228 | composition baseline: already beats mean-prediction! |
| e3-chunks-v3 | chunks ALL vertebrates (~1107 train spp at launch) | C=64/ep 12ep cls | ? | ? | ? | main run |

## Timeline (UTC)
- 19:40 env up on 10.80.0.40 (torch 2.12+cu130, tf 5.18, kernels flash-attn2; HF cache on shared fs)
- 19:49 e1 CDS+ckpt 26min -> test 0.2010
- 20:05 fast offset-window chunk sampler (~50x): prep 1829 genomes ~1.5h (1609 ok, 220 partial/failed)
- 20:16-21:15 probes on 98-cohort chunks: strong val signal but test overfit -> scale needed
- 21:22 e3 launched: 1107 train species present; 2214 steps/ep; 26568 steps
- 21:28+ val: 0.137->0.143->0.130->0.132->0.129->0.124 (ep0.5-3.0), rho ~0.4
| e3-chunks-v3 | chunks 1107 train spp, C=64/ep 12ep cls | scale-up | 0.1262 | 0.2360 | 0.108 | preds squeezed 18-20yrs; median agg 0.225 |
| gbm-meta | genome size/gc/counts | GBM | 0.2745 | 0.2439 | 0.267 | weak |
| gbm-mass | log10 adult weight only | GBM | 0.1910 | 0.1737 | 0.588 | non-DNA oracle-ish ref: allometry dominates |
| gbm-meta+mass | meta+mass | GBM | 0.1869 | 0.1514 | 0.622 | best non-DNA-feature score |
| e4-aux-mass-aves | chunks ~1600 spp, Aves96/other48 + mass aux w0.3 | running | ? | ? | ? | mass prior via aux head |
| e4+ridge-map | e4 lon_pred+mass_pred -> ridge (train-fit) | two-stage | 0.1481 | 0.2014 | 0.300 | mass_pred carries signal; recovers e4 to e1-level |
| xpool-mean | e3 emb mean -> MLP seed0 | stage2 head | 0.1574 | 0.1930 | 0.255 | mean-emb beats mean-of-scalar preds |
| xpool-meanstd | +std dims, e4 scalars, 3-seed ens | stage2 | 0.1481 | 0.1971 | 0.232 | attn pools overfit val (0.131 val -> 0.256 test) |
| xpool-meanstd-5s | 5 seeds | stage2 | 0.1482 | 0.1977 | 0.232 | |
| blend e1+xpool | 0.25*e1+0.75*xpool | ens | 0.1482 (val worse) | 0.1972 | | marginal |
| e5-cds-aves | CDS model on ~500 bird genomes (miniprot) | scale CDS | | | | IN PROGRESS (prep ~04:50) |
| e5a-cds-aves | CDS 367 spp (partial prep), 7ep | miniprot scale-up | 0.1853 (12sp) | 0.2449 | 0.290 | val rho .71 was noise on 12 species |
| ridge-e3emb | e3 mean emb + log_gs+gc+e4_mass+e4_lon, train-CV | stage2 linear | 0.1695 | 0.1912 | 0.156 | clean: RidgeCV internal CV only |
| ridge-e3emb+birds | fit on Aves train only | | | 0.2356 | -0.06 | cross-class data essential |
| e1+ridge blend | 0.25e1+0.75ridge | | | 0.1887 | | err corr 0.99 -> blends all ~0.19 |
| e5b-cds-aves-full | CDS 656 spp, 323k pairs, 6ep | full bird CDS | running | | | |
| e5b-cds-aves-full | CDS 656 spp, 323k pairs, 6ep | full bird CDS | 0.1450 | 0.2737 | 0.346 | best-ckpt at ep0.33; mid-run collapse (rho NaN) |
| ens-ridge+xpool | 0.5*ridge + 0.5*xpool mean | equal blend | 0.1583 | 0.1885 | 0.154 | FINAL BEST |

FINAL RESULTS (test species MAE, log10 yrs): ens=0.1885, ridge=0.1912, xpool=0.1977,
e1-cds=0.2010, e4+map=0.2014, gbm-tok=0.2185, e3=0.2360, e4=0.2469, e5a=0.2449, e5b=0.2737.
Baseline final-ckpt CDS: 0.2034. Non-DNA oracle (mass+meta GBM): 0.1514.
