# Lifespan and predicted hepatocyte expression

These scatterplots show the six strongest Spearman candidates from the frozen 2,001-gene analysis: **ABHD4, TDO2, CCDC85A, LGI1, ACSS1 and ATG14**. Each point is one mammalian species. Lifespan is the recorded maximum from AnAge; expression is predicted from its orthologous DNA window using GI `g0-expression-8192`.

![Lifespan versus predicted expression for six candidate genes](lifespan_expression.png)

[Vector SVG](lifespan_expression.svg) · [PDF](lifespan_expression.pdf) · [Plotted observations](species_expression.csv) · [Candidate statistics](candidates.csv)

The x-axis uses a logarithmic scale. Dashed lines are ordinary regressions of predicted log(TPM+1) on log10(lifespan); panel statistics are Spearman rank correlations. Neither analysis adjusts for ancestry or body mass. Point colors identify mammalian orders for visualization only.

Spearman p values were estimated with 999,999 unrestricted permutations per gene, preserving ties. BH q values use all 3,036 planned genes as the multiple-testing family. The displayed six candidates were selected from that screen; they are exploratory associations in model predictions, not demonstrated lifespan interventions.

All species used exactly the same input context: `Homo sapiens primary hepatocytes, untreated, bulk RNA-seq.` The context and sequence name were held fixed across species. There are **187 species–gene observations** across these six genes. [Provenance](provenance.json) records the source snapshot and checksums.

To regenerate the figures from the committed CSV tables:

```bash
uv sync --extra analysis
uv run python scripts/plot_gi_candidates.py
```

An [8-bp block-shuffle experiment](shuffle-design.md) tests whether these associations persist after rearranging sequence near the TSS while retaining block composition. The candidate set and plotted baseline were fixed before that experiment.

The [completed shuffle experiment](shuffle/README.md) used ten independent shuffles per species within ±4,096 bp of the TSS. All six associations weakened, and none of the shuffled-mean correlations remained significant after BH correction across the six candidates (q ≥ 0.223). All 187 fresh native controls exactly reproduced their original predictions. The 2,057 API requests completed successfully in 22.9 minutes without retries.

![Lifespan correlations before and after shuffling](shuffle/correlations.png)

This result supports dependence of the model's lifespan-associated predictions on local sequence organization. It does not establish that these genes cause longer lifespan: the perturbed promoters are artificial, and the API's scored-window bounds also change slightly with the shuffled sequence. [Full numeric results and limitations](shuffle/README.md).

A separate [expression-to-lifespan prediction experiment](../gi-lifespan-predictor/README.md) uses the completed 3,036-gene dataset, with human excluded from gene selection and model fitting. Both selected-gene and all-gene regression improve prediction across held-out nonhuman species, but substantially underestimate human maximum lifespan.
