# Full-dataset random-genome longevity comparison

The active full run uses a **CDS-matched DNA-token budget per genome**, prepared on the
96-core workstation, then trained on the GPU cluster. This supersedes the unstarted
1,000-chunks-per-genome full training plan. The completed pilot is unchanged.

The user-selected full protocol reference remains `longevity-agingatlas-all-genomic-10ep`
(10 epochs). That reference uses gene-genomic sequences; its train/validation/test taxids
are identical to the companion CDS dataset. At the user's request, the reduced input budget
is calculated from **`pairs_cds.parquet`**, not `pairs_genomic.parquet`. Thus cohort, splits,
labels and training settings come from the selected full reference; DNA volume comes from
the companion CDS inputs. This is recorded explicitly in the frozen specification.

| Setting | Completed pilot | Active full dataset |
| --- | ---: | ---: |
| Species | 98 | 1,871 |
| Assemblies | 98 | 1,872 |
| Train / validation / test species | 64 / 16 / 18 | 1,242 / 335 / 294 |
| Train assemblies | 64 | 1,243 |
| Chunks per assembly | 1,000 | 31–158, matched per genome |
| Total chunks | 98,000 | 249,465 |
| Train / validation / test chunks | 64,000 / 16,000 / 18,000 | 164,839 / 44,488 / 40,138 |
| Input length | 1,022 DNA BPE + CLS/SEP | 1,022 DNA BPE + CLS/SEP |
| Epochs | 18 | 10 |
| Chunk optimizer updates | 36,000 | 51,520 |

For each assembly, sum `min(n_tokens, 1022)` over its CDS examples, divide by 1,022, and
round to the nearest whole chunk (half up, minimum one). This matches the DNA tokens seen
per epoch after the model's length cap, excluding CLS/SEP. The CDS budget is **254,956,740**
DNA tokens; the random inputs contain **254,953,230**, a difference of 3,510 (0.0014%).
Per-genome differences are at most half a chunk. Example counts, padding and special-token
counts differ; this is DNA-volume matching, not identical optimizer-update counts.

The extra assembly belongs to a training species. Both assemblies stay in training, and each
gets its own CDS-derived budget. No new random split is generated. The frozen comparison
preserves exact assembly accessions, labels, taxonomy, training-species target normalization,
model and optimizer settings. Missing class annotations do not remove species from evaluation.

Active specification: `configs/longevity-full-cds-budget-comparison.json`, SHA256
`23f16d65332dd4d9616e32bb7049eb3817a295fff2fe3d1fd94768694d0dcb90`.
It records the original comparison fingerprint, CDS Parquet checksum, matching rule and
per-assembly counts. The original `configs/longevity-full-comparison.json` and completed
1,000-chunk H5 pools are preserved as provenance; they are not the active training inputs.

## Preparation and training

`longevity.chunk_budget` derives the new specification. `longevity.matched_chunks` prepares
one atomic H5 shard per genome, using the same seeded 1,000-draw sampling pool as the original
preparation, then keeping the first N rows. Completed original shards are validated and reused;
missing ones are sampled with the identical algorithm. Fresh and reused preparation produce
identical input/label/provenance arrays. Prefix selection never uses labels or model results.
`--resume` checks existing output shards before skipping them. All 1,872 source compressed
FASTA checksums were verified before the original preparation; both machines share those files.

Keeping a small sample still requires tokenizing the genome under the original uniform
sampling algorithm. Preparation is accelerated by moving from 20 cluster workers to **96 local
workers**, one per physical core (192 logical CPUs). There is no transfer of FASTA or H5 files:
the shared root is `/mnt/filesystem-s8/genes.jpg` locally and `/mnt/filesystem-n0/genes.jpg`
on the cluster.

Training reads only the reduced H5 directory. Cohort fingerprints, exact per-assembly counts,
labels and all shard shapes are checked before training. Full training retains the reference's
learning rates, AdamW settings, head, token batch budgets, seed, 5% warmup and cosine schedule,
and 10 epochs. It validates every 2,000 updates with a fixed cap of 50 examples per species
(or all available if fewer). Final validation/test use every prepared example. `best.pt` is
retained, while intact/shuffled reports use the same final **`model.pt`**.

Historical software/model-revision equivalence remains **unverified**, as agreed. The original
validation-subsampling implementation was not archived; the current deterministic sampler
matches its recorded cap and cadence. The cluster uses Torch 2.12.0+cu130, Transformers 5.18.0
and the flash-attn2 Hub kernel. The runtime package list accompanies the final report.

## Controls and metrics

`longevity.shuffle_eval` evaluates each final checkpoint on intact inputs and deterministic
within-chunk DNA-token permutations. CLS/SEP, labels, token multiplicities and splits remain
fixed; this is evaluation-time perturbation, not retraining. Both full and pilot reports include
species-level MAE and Pearson correlation in years and log10 years, plus Spearman correlation.
Predictions are averaged per species in log10 space, then exponentiated for year-scale metrics.
Per-chunk predictions remain on shared disk; per-species predictions and provenance go to GitHub.

- [Completed pilot intact/shuffled report](longevity-anage100-controls/README.md)
- Full results are added automatically at `docs/longevity-full-controls/README.md` after success.

## Jobs and recovery

All stages run in tmux. Active shared job directory: `runs/longevity-full-cds-budget-job/`.

- Workstation session `longevity-full-prepare-local`: `scripts/prepare_full_chunk_budget.sh`,
  96-worker H5 preparation, with safe resume.
- Cluster session `longevity-full-train`: `scripts/run_full_chunk_training.sh`, waits for
  successful preparation, audits H5 inputs, trains, then runs intact/shuffled evaluations.
- Workstation session `longevity-full-publisher`: `scripts/publish_longevity_controls.py`,
  waits for successful completion and publishes the full report to PR #16.

Active data: `datasets/longevity-full-cds-budget-chunks/`.
Active model: `runs/longevity-full-cds-budget-chunks/`.
Active controls: `runs/longevity-full-cds-budget-chunks-controls/`.
The old `longevity-full-chunks-job` was stopped intentionally before training; its interrupted
exit markers are historical, not the status of these replacement jobs.

```bash
tail -5 /mnt/filesystem-s8/genes.jpg/runs/longevity-full-cds-budget-job/prepare.log
ssh sber-cluster 'tmux ls'
ssh sber-cluster 'tail -5 /mnt/filesystem-n0/genes.jpg/runs/longevity-full-cds-budget-job/training.log'
```

The cluster launcher raises its open-file limit to 8,192 for the 1,872 H5 shards. A GPU lock
prevents overlap with other jobs using this experiment's lock. Publication checks success
markers and complete held-out coverage before committing/pushing results. Failure is recorded
in `controls-publisher-status.json` without publishing incomplete metrics.

Terminal/SSH disconnection is safe. **Keep the workstation powered on while preparation is
running here**; it also hosts the GitHub publisher. GPU training runs independently on the
cluster after preparation. Machine reboots require manual restart; tmux does not implement
training checkpoint recovery. No GitHub credentials are copied to the cluster.
