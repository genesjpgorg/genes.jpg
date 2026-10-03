# Full-dataset random-genome longevity comparison

The full random-chunk run matches the user-selected `longevity-agingatlas-all-genomic-10ep`
reference. That reference uses Aging Atlas **gene-genomic** sequences (`pairs_genomic.parquet`),
not CDS sequences. Its train/validation/test taxids are identical to the companion
`pairs_cds_splits.json`. The pilot still uses the `longevity-anage100` CDS reference.

| Setting | Pilot | Full dataset |
| --- | ---: | ---: |
| Species | 98 | 1,871 |
| Assemblies | 98 | 1,872 |
| Train / validation / test species | 64 / 16 / 18 | 1,242 / 335 / 294 |
| Train assemblies | 64 | 1,243 |
| Chunks per assembly | 1,000 | 1,000 |
| Input length | 1,022 DNA BPE + CLS/SEP | 1,022 DNA BPE + CLS/SEP |
| Epochs | 18 | 10 |
| Chunk optimizer updates | 36,000 | 388,440 |
| Reference optimizer updates | 5,040 | 149,590 |

The extra full-dataset assembly belongs to a training species. All of its chunks remain
in training. The frozen comparison includes exact assembly accessions, species assignments,
labels, taxonomy, training-species target normalization, model and optimizer settings.
No new random split is generated. The full reference was still running when its protocol
was frozen; this is recorded explicitly, rather than claiming its results were complete.

`configs/longevity-full-comparison.json` is immutable for this experiment. Its fingerprint is
`de5445e62589add1610bd610239fa65053ca3bc34ad4ae9bb271134302b873b2`.
The source compressed FASTA checksums for all 1,872 assemblies were verified before preparation.
The preparation algorithm is the same as the pilot: sample 1,000 token windows per genome,
with replacement, into H5; training reads those stored inputs and labels.

Full training inherits the reference's learning rates, AdamW settings, head, batch token
budgets, seed, 5% warmup and cosine schedule, and 10-epoch budget. Validation occurs every
2,000 updates using a fixed sample of 50 examples per species. Final validation and test
use all examples. The trainer saves a validation-selected `best.pt` as well as `model.pt`;
both intact and shuffled control reports use the **same final `model.pt`**, consistent with
the pilot report. The exact historical validation-subsampling implementation was not archived;
the current deterministic sampler enforces the reference's recorded cap and cadence.
Species lacking class annotations remain in evaluation (44 species in the full cohort).

Epoch matching does not imply equal compute or equal weighting: chunk counts and token
lengths differ from genes, and the species with two assemblies has twice as many training
chunks. Historical software/model-revision equivalence remains **unverified**, as agreed.
The cluster uses Torch 2.12.0+cu130, Transformers 5.18.0 and the flash-attn2 Hub kernel;
this differs from the repository's default model extra. The runtime package list is saved
with the report. See [pilot infrastructure](longevity-chunks.md) for the environment setup.

## Token-order controls and metrics

`python -m longevity.shuffle_eval --run RUN --data H5_DIRECTORY --out NEW_REPORT_DIRECTORY`
evaluates the trained model twice on each complete validation/test set. A SHA256-derived,
seeded permutation shuffles only the 1,022 DNA tokens within each chunk. CLS/SEP, token
multiplicities, labels and splits remain fixed. Permutations are independent of batch/access
order. This is evaluation-time perturbation, not retraining.

Reports include MAE and Pearson correlation in both **years** and **log10 years**, plus
Spearman correlation. Predictions are averaged in log10 space per species, then exponentiated
for year-scale metrics. Each species receives equal metric weight. Per-species predictions,
checkpoint SHA256, settings and frozen comparison are included. Per-chunk Parquet predictions
remain on the shared disk. Shuffling is out of distribution, so its effect alone does not
establish biological causality.

- [Completed pilot intact/shuffled report](longevity-anage100-controls/README.md)
- Full results are automatically added at `docs/longevity-full-controls/README.md` after success.

## Cluster execution and reporting

Host: `ssh sber-cluster` (`fishman@10.80.0.5`). Code: `/home/fishman/genesjpg-full`.
Shared root is `/mnt/filesystem-n0/genes.jpg` on the cluster and
`/mnt/filesystem-s8/genes.jpg` on the workstation. No data transfer between these mounts is needed.

Remote tmux sessions:

- `longevity-full-prepare`: checksum verification and 20-worker H5 preparation.
- `longevity-full-train`: waits for successful preparation, audits every H5 shard, trains,
  then evaluates intact/shuffled validation and test inputs.
- `longevity-shuffle-100`: pilot evaluations (session exits when completed).

The tracked launcher is `scripts/run_full_chunk_training.sh`. Preparation and pilot launcher
copies, logs and exit markers are under `runs/longevity-full-chunks-job/` on the shared disk.
All H5-loading jobs raise the open-file limit to 8,192; the default 1,024 cannot cover 1,872
shards. A GPU file lock prevents the new evaluation and training jobs overlapping.

Check progress:

```bash
ssh sber-cluster 'tmux ls'
ssh sber-cluster 'tail -5 /mnt/filesystem-n0/genes.jpg/runs/longevity-full-chunks-job/prepare.log'
ssh sber-cluster 'tail -5 /mnt/filesystem-n0/genes.jpg/runs/longevity-full-chunks-job/training.log'
```

The authenticated workstation runs `scripts/publish_longevity_controls.py` in tmux session
`longevity-full-publisher`. It waits for `pipeline.exit=0` and complete held-out coverage,
then commits/pushes the full report and marks the results PR ready. On failure, it records
an error in `controls-publisher-status.json` and does not publish incomplete metrics.
SSH/terminal disconnection is safe. Turning off the workstation does not affect remote
preparation/training/evaluation, but delays GitHub publication until the publisher is restarted.
No GitHub credentials were copied to the cluster. A cluster restart would require manual
recovery; these tmux jobs are not an automatic checkpoint-resume service.
