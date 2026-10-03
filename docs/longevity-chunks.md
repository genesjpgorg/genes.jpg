# Controlled CDS versus random-genome comparison

The reference is the completed **longevity-anage100** CDS run. Its effective cohort
(after the original `--min-genes 200` filter) is frozen in
[`configs/longevity-anage100-comparison.json`](../configs/longevity-anage100-comparison.json):
98 assemblies/species, with 64 training, 16 validation and 18 test species.
The specification includes exact assembly accessions, taxonomy, labels, normalization,
split assignments, training settings, and hashes of the reference config and CDS pairs.

## Run the matched variant

```bash
uv sync --extra model

# Prepare only the assemblies actually used by the CDS reference.
uv run --extra model python -m longevity.chunks \
  --dataset data/datasets/anage-longevity \
  --anage-table data/datasets/_anage/anage_longevity_genomes.parquet \
  --comparison configs/longevity-anage100-comparison.json \
  --out data/datasets/longevity-anage100-chunks

# Inherit the frozen settings and exact splits; no manually copied hyperparameters.
uv run --extra model python -m longevity.train \
  --data data/datasets/longevity-anage100-chunks \
  --comparison configs/longevity-anage100-comparison.json \
  --out runs/longevity-anage100-chunks
```

The historical attention backend requires its FlashAttention/kernel dependencies
and a compatible GPU environment, as in the CDS run. The repository's model extra
alone does not recreate that environment. An incompatible backend must be resolved
in the environment; comparison mode rejects an override that changes the protocol.

Both models use **1,022 DNA BPE tokens maximum + CLS/SEP = 1,024 positions**.
Each H5 row already contains CLS/SEP and exactly 1,022 DNA tokens. Training consumes
that row unchanged, with no extra special tokens, padding, cropping, or resampling.
The older v1 H5 schema stored 1,024 DNA tokens without CLS/SEP and is rejected:
regenerate those files with this version.

| Aspect | Enforced comparison |
| --- | --- |
| Genomes | Exact 98 versioned assembly accessions from the effective CDS cohort |
| Labels / taxonomy | Match the CDS pairs; primary longevity labels must agree |
| Split | Exact saved train/validation/test taxids; no regenerated split |
| Normalization | Reference training-species mean and standard deviation |
| Model | Same ModernGENA ModernBERT encoder and CLS regression head |
| Optimization | Same AdamW parameter groups, learning rates, decay, clipping, seed, attention backend, precision policy |
| Duration | **18 epochs**, as requested |
| LR / validation | Same warmup fraction (5%), cosine schedule, one validation per epoch |
| Checkpoint / metrics | Final checkpoint, same species-mean predictions and species-level metrics |

The chunk run has 64,000 training examples, 2,000 updates per epoch, and **36,000
updates over 18 epochs**, versus 5,040 updates for CDS. The time cutoff is disabled
so the slower chunk run completes all 18 epochs. This is an epoch-matched comparison,
not a compute-matched comparison. Sequence counts, lengths, species weighting of
training examples, and observed token counts differ as consequences of using
1,000 fixed chunks per genome instead of variable numbers of CDSs. CDS inputs still
use the original random training crop / first evaluation crop when too long.

**Historical software match is unverified**, as agreed. The original report says
Transformers 5.18, whereas the repository's model extra pins `<5`. The old run did
not pin pretrained-model/tokenizer revisions or record its full environment; it
also did not snapshot original genome bytes. We can enforce versioned assembly
identity and recorded data/protocol, but cannot certify bitwise software, weights,
or genome-file equivalence to that historical run. New training configs record
Python, Torch, Transformers, CUDA and device information.

Preparation rejects missing/mismatched reference assemblies or labels. Training
also rejects extra assemblies, incorrect chunk counts, shards prepared against a
different specification, conflicting CLI settings, or reuse of an existing run
directory. Each run saves the specification and its fingerprint alongside its
config, metrics, predictions and checkpoint. Changing a specification requires
regenerating its shards; it cannot silently relabel an existing input set.

## Sampling and H5 format

Preparation streams plain or gzipped FASTA in blocks of at most 1,000,000 bases
(configurable with `--block-bases`). BPE is applied independently per block;
samples never cross a contig or block boundary. Weighted reservoir sampling gives
1,000 independent samples **with replacement** over eligible block-local token
starts. Duplicates and overlaps are possible. Short blocks are excluded; genomes
with no eligible block fail rather than being padded. Ambiguous bases are retained
and handled by the shared DNA tokenizer. The seed combines the reference seed
with the assembly accession, independently of genome ordering.

| Dataset | Shape | Meaning |
| --- | --- | --- |
| `input_ids` | `(1000, 1024)` | int32 CLS + 1,022 DNA BPE tokens + SEP; LZF-compressed |
| `labels` | `(1000,)` | float64 log10 longevity in years, identical within genome |
| `contig` | `(1000,)` | Source FASTA record ID |
| `block_start_bp` | `(1000,)` | Zero-based source block offset |
| `token_start_in_block` | `(1000,)` | Zero-based DNA BPE start within source block |

Attributes record schema, tokenizer, dimensions, seed, block size, species metadata
and comparison fingerprint. Atomic per-genome writes prevent partial shards;
`--force` is required to replace an existing shard. Training leaves token arrays
on disk and reads only the batch rows. Standalone preparation/training without
`--comparison` remains available, but does **not** establish a matched experiment.

To freeze a different completed CDS reference:

```bash
uv run --extra model python -m longevity.comparison \
  --reference-run /path/to/cds-run --pairs /path/to/pairs.parquet \
  --budget epochs --out configs/another-comparison.json
```

The original gene preparation/training commands remain supported. The existing
`longevity.report` publication report still expects gene-specific metadata;
compare the shared species-level metrics in `metrics.jsonl` for the chunk variant.

## Cluster execution and automatic report

For the RTX PRO 6000 Blackwell run, preparation uses 16 independent genome workers
(`--workers 16`); sampling remains deterministic per assembly. The completed source
dataset now has newer NCBI taxonomy, so first make a snapshot that verifies the
selected FASTAs and labels and preserves the historical CDS taxonomy:

```bash
python -m longevity.snapshot \
  --dataset /shared/genes.jpg/datasets/anage-longevity \
  --comparison configs/longevity-anage100-comparison.json \
  --out /shared/genes.jpg/datasets/longevity-anage100-frozen --threads 16
python -m longevity.chunks \
  --dataset /shared/genes.jpg/datasets/longevity-anage100-frozen \
  --comparison configs/longevity-anage100-comparison.json \
  --out /shared/genes.jpg/datasets/longevity-anage100-chunks --workers 16
python -m longevity.audit_chunks \
  --data /shared/genes.jpg/datasets/longevity-anage100-chunks \
  --comparison configs/longevity-anage100-comparison.json --out input_audit.json
```

Use a separate training environment with Python development headers installed,
Torch 2.12.0, torchvision 0.27.0, Transformers 5.18.0 and kernels 0.17.2. The older
Transformers 4.x ModernBERT implementation does not dispatch the historical Hub
FlashAttention backend correctly; Torch 2.14 has no matching published kernel build.
The full tested environment is captured with `uv pip freeze --exclude-editable`.
This environment intentionally differs from the general `model` extra's `<5` pin.
Keep the preparation environment separate while its workers are active.

Training starts only after all 98 shards pass the input audit. Run it in a detached
session, with a fresh output directory, and preserve logs and checkpoints on the
shared disk. After successful completion, generate the report:

```bash
python -m longevity.compare_report \
  --run /shared/genes.jpg/runs/longevity-anage100-chunks \
  --reference /shared/genes.jpg/runs/longevity-anage100 \
  --job /shared/genes.jpg/runs/longevity-anage100-chunks-job \
  --out /shared/genes.jpg/runs/longevity-anage100-chunks-job/report
```

`scripts/publish_chunk_report.py` waits for the cluster pipeline's successful exit
marker, copies the completed report into an isolated Git worktree, commits/pushes
it, and creates a GitHub PR. It runs on the already authenticated workstation, so
GitHub credentials do not need to be copied to the cluster. The publisher records
its state and eventual PR URL in `publisher-status.json`; failed/incomplete runs
are never published as completed experiments.
