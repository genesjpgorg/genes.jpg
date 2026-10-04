Run full-dataset random-genome longevity training with a **per-genome CDS DNA-token budget**: 249,465 chunks across 1,872 assemblies / 1,871 species. This replaces the unstarted 1,000-chunks-per-genome full plan. The exact 1,242/335/294 train/validation/test species split and 10-epoch protocol remain frozen from the selected `longevity-agingatlas-all-genomic-10ep` reference; the reduced DNA volume comes from its companion CDS dataset, per the user's clarification.

For each genome, sum CDS token lengths capped at 1,022, then round to the nearest complete 1,022-DNA-token chunk. Stored inputs include CLS/SEP for 1,024 positions total. The new budget contains 254,953,230 DNA tokens versus 254,956,740 for CDSs (0.0014% rounding difference), and requires 51,520 training updates. Preparation runs with 96 CPU workers on the workstation, reuses validated completed samples, and writes H5 files to the shared disk. GPU training starts automatically after all shards pass audit.

The same final checkpoint is evaluated on intact inputs and deterministic within-chunk token permutations, with CLS/SEP fixed, for both the pilot and full dataset. Reports include species-level MAE and Pearson correlation in years and log10 years, Spearman correlation, and per-species predictions. The completed pilot's 1,000-chunk budget is unchanged.

- Protocol and execution: [docs/longevity-full-chunks.md](docs/longevity-full-chunks.md)
- Pilot results: [docs/longevity-anage100-controls/README.md](docs/longevity-anage100-controls/README.md)
- Full results: [docs/longevity-full-controls/README.md](docs/longevity-full-controls/README.md) (added automatically when the pipeline finishes; PR stays draft until then).

Historical software equivalence remains unverified. The full reference uses gene-genomic sequences, and its splits match the companion CDS splits. The budget source and protocol reference are recorded separately. Missing class annotations do not drop species. Validation settings remain every 2,000 updates with up to 50 examples per species, followed by complete final evaluation.

Validation: focused tests cover variable-count training/audit, capped CDS budget arithmetic, identical fresh/reused samples, resume checks, H5 preparation, frozen cohorts, deterministic token permutations, metrics, missing taxonomy and publication checks. All jobs run in tmux; preparation and GitHub publication require the workstation to remain powered on.
