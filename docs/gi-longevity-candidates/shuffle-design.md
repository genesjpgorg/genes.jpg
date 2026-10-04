# TSS sequence perturbation experiment

The six candidates are frozen before perturbation: ABHD4, TDO2, CCDC85A, LGI1, ACSS1 and ATG14. The experiment reuses exactly the 187 species–gene observations in [species_expression.csv](species_expression.csv). Their original sequences and API request hashes must match the source prediction manifest before preparation succeeds.

For every observation, predict the original sequence again and predict ten independently shuffled sequences. This gives **187 fresh native controls and 1,870 shuffled predictions**, or **2,057 requests**. Native controls use a separate experiment cache so the first execution actually calls the API again; subsequent resumes reuse completed requests.

The model is `g0-expression-8192`, the input is 81,920 bp, and the zero-based TSS index is 40,960. Every request has `sequence_name="orthologue_tss_window"` and the exact description `Homo sapiens primary hepatocytes, untreated, bulk RNA-seq.` The species name in the description is constant. **Only the DNA sequence varies.**

For each shuffle, split the 4,096 bp immediately upstream and the 4,096 bp immediately downstream of the TSS into non-overlapping 8-bp blocks. Permute blocks independently within each flank. Blocks containing N stay in place. This preserves the length, nucleotide composition and block multiset separately in each flank; the exterior of the 8,192-bp region remains unchanged. This is a block permutation, not an exact preservation of all overlapping 8-mer counts: new junctions create different overlapping words. The numeric TSS index is unchanged, but its nearby bases can change. Seeds are derived from the experiment seed, gene, species, replicate and flank size, making the perturbations reproducible.

Preparation checks original sequence hashes, rejects unchanged or duplicate shuffles, and records per-sequence QC. The runner validates GI response metadata, caches responses atomically, and writes a complete result table after each gene. A lock prevents simultaneous runners from writing to the same experiment. Predictions proceed gene by gene, allowing analysis before all six finish.

## Analysis

The outcome is recorded maximum species lifespan from AnAge. For each gene, compute Spearman correlation with fresh native expression and with the mean of ten shuffled **log(TPM+1)** predictions per species. Also report the correlation separately for each of the ten shuffle replicates. The inferential sample size is the number of species, not the number of requests. There is no adjustment for body mass or shared ancestry, matching the requested analysis.

Use 999,999 unrestricted permutations of lifespan ranks for two-sided correlation p values, retaining ties and applying the +1 correction. BH correction covers the six selected genes separately for native and shuffled-mean tests. These p values describe a perturbation of already selected candidates; they are not an independent genome-wide confirmation.

To compare correlations directly, calculate `sign(original rho) × (native rho − shuffled-mean rho)` and its exploratory 95% percentile interval from 20,000 paired species bootstrap samples. Positive values indicate attenuation in the original direction; a reversal can produce a value larger than one. The bootstrap is conditional on the ten-shuffle means and does not model candidate selection, phylogeny or uncertainty in lifespan records. Native-minus-original expression differences check API reproducibility.

Loss of correlation supports dependence on sequence organization within the perturbed region. It does not by itself demonstrate a causal effect on longevity or distinguish authentic regulatory sensitivity from a model response to artificial sequences. Persistence can involve retained composition or short motifs, the unperturbed sequence, or other model behavior. A decrease in predicted expression is recorded separately from a change in correlation.

## Reproduction and disconnected execution

The source dataset must contain `config.json`, `analyses/broad/gene_windows.parquet` and `analyses/broad/prediction_manifest.parquet`. Install analysis dependencies with `uv sync --extra analysis`. Set `GI_API_KEY` in the environment or a local `.env` file; credentials are never written to result tables.

```bash
gi_dataset=data/datasets/gi-hepatocytes-ensembl116
gi_experiment="$gi_dataset/experiments/top6_tss_8mer_flank4096_r10"
uv run python -m longevity.gi_tss_shuffle prepare \
  --dataset-root "$gi_dataset" --output "$gi_experiment" \
  --flank 4096 --replicates 10 --seed 20261004
tmux new-session -d -s gi-tss-shuffle \
  "env OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 GI_PYTHON=.venv/bin/python bash scripts/run_gi_tss_shuffle.sh '$gi_experiment' .env"
```

The script runs at 1.5 requests/second with four workers and automatically generates the final report. Monitor `progress.json` and `run.log`; `exit-code` is written when the script finishes. The experiment is independent of the conversation and can resume from its response cache by rerunning the script. Do not start a second runner while the first remains active.

For completed genes while inference continues:

```bash
OPENBLAS_NUM_THREADS=1 uv run python -m longevity.gi_tss_shuffle_report \
  --output "$gi_experiment" --partial
```

The final report is copied into `docs/gi-longevity-candidates/shuffle/`. Compact numeric tables, checksums and figures are suitable for Git; prepared DNA and raw API response caches remain in the dataset directory.
