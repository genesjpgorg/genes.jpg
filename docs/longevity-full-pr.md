Run the full random-genome longevity experiment against the user-selected `longevity-agingatlas-all-genomic-10ep` reference: 1,872 assemblies / 1,871 species, exact 1,242/335/294 train/validation/test species split, and 10 epochs. The reference uses gene-genomic inputs; its splits match the companion CDS splits. Each assembly supplies 1,000 premade H5 inputs of 1,022 DNA BPE tokens plus CLS/SEP.

The same final checkpoint is evaluated on intact inputs and deterministic within-chunk token permutations, with CLS/SEP fixed, for both the 100-species pilot and full dataset. Reports include species-level MAE and Pearson correlation in years and log10 years, Spearman correlation, and per-species predictions.

- Protocol and execution: [docs/longevity-full-chunks.md](docs/longevity-full-chunks.md)
- Pilot results: [docs/longevity-anage100-controls/README.md](docs/longevity-anage100-controls/README.md)
- Full results: [docs/longevity-full-controls/README.md](docs/longevity-full-controls/README.md) (added automatically when the pipeline finishes; PR stays draft until then).

Epoch budgets are matched, not optimizer-update counts. Historical software equivalence remains unverified. The full reference was still training when its protocol was frozen. Adds fixed validation sampling/cadence and best-checkpoint retention from the full reference settings, retains species without class annotations, and audits all H5 files before training.

Validation: focused longevity tests cover H5 preparation/training, frozen cohorts, capped interim versus complete final evaluation, checkpoint selection, deterministic token permutations, correlation metrics, missing taxonomy, and publication coverage checks. Remote jobs run in tmux; final publication also runs in tmux on the authenticated workstation.
