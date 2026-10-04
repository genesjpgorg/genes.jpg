# Methodology

## Data, orthologues and expression inference

The completed broad analysis contains **48 mammals, 3,036 human-anchored orthologues and 96,849 species–gene predictions**. Genes have at least 30 eligible species in the broad manifest; availability differs between genes and species. Lifespan labels are AnAge recorded maximum longevity, in years. The regression input tables preserve all 48 species; 47 are used for training and human is held out.

Source annotations and primary assemblies are Ensembl release 116. High-confidence one-to-one orthologues are anchored to human gene IDs. The transcript selection chooses an unambiguous Ensembl-canonical protein-coding transcript, excluding incomplete transcripts. These choices define the TSS and may omit genes without a usable transcript or adequate sequence. Existing preparation code is [gi_prepare.py](../../longevity/gi_prepare.py); numeric reference coordinates are in [reference_human_tss.csv](reference_human_tss.csv).

An **81,920-bp gene-sense DNA window** is centered at zero-based TSS offset **40,960**. For a zero-based genomic TSS `t` and flank `F=40960`, the plus-strand interval is `[t−F, t+F)`. The minus-strand interval is `[t−F+1, t+F+1)`, followed by reverse complementation. This offset difference places the TSS base at index F on either strand. Contig-edge windows are excluded rather than padded; the full window may contain at most 1% N and the central 9,198 bp must contain no N.

All requests use the frozen values:

```json
{
  "model": "g0-expression-8192",
  "tss_index": 40960,
  "sequence_name": "orthologue_tss_window",
  "options": {
    "description": "Homo sapiens primary hepatocytes, untreated, bulk RNA-seq."
  }
}
```

The remaining request field is `sequence`. Only this sequence changes between species or perturbations. GI returns `data.prediction.expression_log_tpm`. Response validation checks model ID, context, sequence label, length, TSS offset, scored-window bounds and a finite prediction. The API is called through the existing [GI client](../../longevity/gi_api.py); its request hashes include the sequence and all fixed options. The [official expression API documentation](https://docs.genomicintelligence.ai/tasks#expression) describes the sequence orientation, TSS and response contract; this experiment deliberately retains its frozen model and input length rather than following future defaults.

## Original 30-hit Spearman screen

The requested result is the **2,001-gene, 63,906-observation snapshot frozen at 2026-10-04 10:48:42 UTC**, analyzed at 11:33:06 UTC. Later-completed genes are not added to this archive. Human is included in this association screen.

For each gene, correlate predicted log(TPM+1) with maximum lifespan across its observed species. Spearman rho is the Pearson correlation of average ranks, retaining ties. Using log10 lifespan gives identical ranks to lifespan in years. Species are treated as independent; there is no body-mass adjustment, tree, clade weighting or ancestry correction.

Each two-sided p value is based on **999,999 unrestricted permutations** of lifespan-rank pairings. Count draws with `abs(null rho) >= abs(observed rho) − 1e−12` and use `(extreme + 1)/(999999 + 1)`. Sort species lexicographically before testing. The NumPy RNG seed is `8192 XOR int.from_bytes(SHA256(human_gene_id)[:8], 'little')`; permutations are processed in blocks of 5,000. [The reproduction script](../../scripts/reproduce_gi_spearman.py) preserves these details.

BH correction uses the **3,036 planned tests**: the 1,035 genes not yet in the snapshot are assigned p=1. The resulting threshold `spearman_q_bh_planned < 0.05` selects exactly **30 genes**. BY correction adds the harmonic-number factor for the same 3,036-test family and selects five. Asymptotic Spearman p values are included for context; they yield 33 hits and do not define the requested list. Exported binomial tail intervals quantify Monte Carlo uncertainty; they do not represent biological uncertainty. Marginal genes can move around the threshold under simulation uncertainty. See the [full table](spearman/all_genes.csv), [30 hits](spearman/hits.csv) and [provenance](spearman/provenance.json).

## TSS block shuffling

The user selected **six** leading candidates: ABHD4, TDO2, CCDC85A, LGI1, ACSS1 and ATG14. Their gene/species pairs were fixed before perturbation. There are **187 pairs**, each with one freshly predicted native control and ten independently shuffled inputs: **2,057 total requests**.

Within each input, independently permute non-overlapping 8-bp blocks in the 4,096-bp upstream and 4,096-bp downstream flanks. Blocks containing N remain fixed. Preserve each flank's block multiset, nucleotide composition and all sequence outside the 8,192-bp region. This is not exact preservation of every overlapping 8-mer: words spanning new junctions change. The TSS index and all API options stay fixed. Duplicate or unchanged shuffle sequences are rejected. The [frozen protocol](../gi-longevity-candidates/shuffle/protocol.json) and [runner](../../longevity/gi_tss_shuffle.py) record the seeds and checks.

For each gene, use the same species before and after perturbation. The primary shuffled expression is the **mean of ten log(TPM+1) predictions per species**; compute its Spearman correlation with lifespan. Each individual shuffle also has a descriptive rho. The main native and shuffled-mean p values each use 999,999 unrestricted permutations. BH correction is performed **across the six selected genes**, separately for those two test families. These are selected-candidate perturbation results, not an independent genome-wide discovery screen.

Signed attenuation is `sign(original rho) × (native rho − shuffled-mean rho)`. Positive values mean weakening in the original direction. The exploratory 95% interval uses 20,000 paired species bootstrap samples, carrying lifespan and both expression values together. It is conditional on the estimated ten-shuffle means and does not account for gene selection or ancestry.

All fresh native predictions exactly match their original values. All six associations weaken, with shuffled-mean BH q ≥0.223432. Every API scoring window contains the perturbed region; the scored span itself changes by −889 to +122 bp after shuffling (mean −183.3 bp), despite fixed total input length. Artificial sequence behavior and this model-context change limit mechanistic interpretation. An expression decrease alone is not evidence of correlation loss; both are reported. The other 24 original hits have not been shuffled.

## Regression and human validation

The regressors use the **completed** matrix of 3,036 genes across 48 species. Human lifespan is excluded from every training, feature-selection and tuning step. The response is natural-log maximum lifespan. Features are GI log(TPM+1), standardized using training data. There are no mass, ancestry, species-ID or missingness-indicator predictors.

Two predeclared model families are fitted: ridge regression on training-only significant genes, and ridge regression on all eligible genes. In each training fold, retain genes observed in at least `max(10, ceil(0.6 × training species))` species. Median-impute from training species, standardize with training means/population SDs and exclude zero-variance features. The selected-gene model first uses observed-value Spearman tests with asymptotic t p values and BH q ≤0.05 over the 3,036-gene universe. Thus the final 57-gene set differs from the earlier 30-hit permutation screen.

Ridge minimizes `sum((log lifespan − intercept − X beta)^2) + alpha × sum(beta^2)`, leaving the intercept unpenalized. Five shuffled inner folds choose alpha from 0.01 through 100,000 in powers of ten, minimizing pooled log-scale squared error. Screening and preprocessing are refitted within every inner fold. Outer leave-one-species-out validation across 47 nonhuman mammals estimates error without using the held-out species in any step. The baseline is each training set's geometric-mean lifespan. Related species may occur in training and validation, consistent with the requested independent-species analysis.

The final models are fitted to all 47 nonhumans and then applied to human. Selected-gene ridge uses 57 genes and alpha=100; all-gene ridge uses 3,036 genes and alpha=0.01. Saved coefficients and preprocessing define `log prediction = intercept + sum(beta_j × (imputed_expression_j − mean_j)/scale_j)`; exponentiation gives years. There is no log-normal bias correction. [Exact parameters and inference instructions](USAGE.md#regression-parameters).

Human GRCh38 predictions are **32.527516 years** and **46.555582 years**, versus recorded maximum **122.5 years**. Nonhuman MAE is 12.848215 and 10.332788 years, versus baseline 14.962555. The models capture comparative signal but fail to recover human longevity accurately. Human is above the largest nonhuman training lifespan (110 years); validation includes other primates and does not establish transfer to a new mammalian order. These are exploratory comparative models, not individual lifespan forecasts.
