# genes.jpg model

DNA (nuclear-genome windows; COI barcodes for BIOSCAN-5M insects) → species image. The design merges Difface (align, then diffuse), CLIBD (DNA–image–taxonomy
contrastive space) and unCLIP (prior + embedding-conditioned decoder). See [architectures.md](architectures.md) for
the literature review behind it.

```
DNA input ──► [1] ModernGENA ─► [2] aligned DNA emb ─► [3] diffusion prior ─► BioCLIP image emb ─► [4] SD decoder ─► image
                                         │                                               ▲
                                         └── retrieval baseline: nearest real image ─────┘
```

| Step | Module | What is trained | Data | Compute |
|---|---|---|---|---|
| 1 DNA encoder | `genesjpg/encoders.py` `DNAEncoder` | ModernGENA (`AIRI-Institute/moderngena-base`, 22-layer ModernBERT, GENA-LM 32k BPE; fine-tuned) + mean-pool + MLP head → 512-d | — | GPU (CPU feasible for short BIOSCAN barcodes) |
| 2 Alignment | `genesjpg/align.py` | DNA tower only; BioCLIP image/text towers frozen. Loss: label-aware InfoNCE DNA↔image + 0.5·DNA↔taxonomy text | paired DNA+image (BIOSCAN-5M, or a genes.jpg dataset via `prepare`) | GPU (CPU feasible for short BIOSCAN barcodes) |
| 3 Prior | `genesjpg/prior.py` `DiffusionPrior` | MLP denoiser, x0-prediction, cosine schedule, CFG, DDIM sampling | paired | CPU or GPU |
| 4 Decoder | `genesjpg/decoder.py` `EmbeddingDecoder` | `EmbeddingProjector` (BioCLIP emb → 8 cross-attention tokens) into frozen SD 1.5; optional full UNet fine-tune | images only (any species photos) | GPU |

`genesjpg/pipeline.py` `GenomeToImage` chains the steps for inference and also exposes `retrieve()`.

## Data

`python -m genesjpg download` fetches a BIOSCAN-5M subset with HTTP range requests (metadata CSV streamed from its zip,
images in contiguous blocks of zip entries): `train` split for training, `val` (seen species) and `val_unseen`
(species absent from training) for evaluation. Records keep only specimens with a barcode and at least a genus label.

### Genome datasets

`python -m genesjpg --data RUN prepare --dataset DATASET_DIR [--unseen TAXID ...]` writes `records.csv` from a
dataset built by `genes-datasets` (species/genomes/images/pairs tables), pairing every image with its species'
DNA. Species given with `--unseen` become `val_unseen`; `--val-frac` of the other species' images become `val`.

- Each assembly is packed once into `<dataset>/../_packed_genomes/<accession>/`
  (`sequence.u8`, upper-case ACGTN, plus `index.json`), **primary nuclear sequences only**: the assembly report's
  non-nuclear molecules, any `mitochondrion` FASTA record, and alternate loci / patches (roles `alt-scaffold`,
  `fix-patch`, `novel-patch`; 199 Mb in GRCh38) are dropped. Packs record a filter `version` and are rebuilt when
  it changes. Alignment training draws a fresh random
  10 kb window per image per batch (no window crosses a sequence boundary or has > 1% N), which the tokenizer
  truncates to ModernGENA's 1024-token context (~6.3 kb on mammal DNA). For evaluation, the prior and generation, a
  genome is embedded as the re-normalised mean of 32 fixed (seeded) windows. Code: `genesjpg/genomes.py`.
- Captions use each species' NCBI lineage, renamed to the iNaturalist-style names BioCLIP was trained on
  (`bioclip_ranks`): Metazoa → Animalia, Viridiplantae → Plantae, Streptophyta → Tracheophyta / Bryophyta /
  Marchantiophyta / Anthocerotophyta, Actinopteri → Actinopterygii, Hyperoartia → Petromyzonti, and from lineage
  taxids monocots → Liliopsida, sharks and rays → Elasmobranchii, chimaeras → Holocephali, Lepidosauria / turtles /
  crocodilians → Reptilia. Missing ranks are left out of the caption.

Smoke test on `tol200m-mammals-smoke` (2026-10-03, one RTX PRO 6000; 4 training species, armadillo held out):
alignment reaches species top-1 = 1.0 on seen-species `val` by epoch 15; a single 6 kb window identifies the
species 254/256 times; 13/16 generated images of seen species are classified correctly by BioCLIP; the unseen species
fails (4 training species cannot generalise). (A COI-barcode input mode, removed since, reached 1.0 by epoch 3 and
14/16 on the same split.)
With one held-out species the `val_unseen` split's own species metrics are trivially 1.0; the
`pooled_species_top1` / `pooled_genus_top1` metrics query against all held-out photos of seen and unseen species
together (armadillo: 0.0 with the aligned DNA embedding). `train-decoder` leaves `val_unseen` photos out unless
`--include-unseen` (the smoke-test decoder above was trained before this and saw them).

## Run

```bash
~/.local/bin/uv sync --extra model --python-preference only-managed   # see README Setup
python -m genesjpg --data data download --n-train 20000 --n-eval 3000   # or: prepare --dataset DIR (see above)
python -m genesjpg --data data embed                 # frozen BioCLIP image + taxonomy-text embeddings
python -m genesjpg --data data train-align --epochs 5
python -m genesjpg --data data train-prior --epochs 50
python -m genesjpg --data data train-decoder --max-steps 2000   # GPU
python -m genesjpg --data data generate --processid <BOLD processid>
```

On Modal (see `modal_app.py`): `modal run modal_app.py` runs steps 1–3 on CPU (BIOSCAN-5M only); `train_decoder` and `generate` need a
GPU, which requires a payment method on the Modal workspace.

## Metrics

`train-align`, `train-prior` and `train-decoder` take `--seed` (default 0), which seeds weight initialisation, batch
order, genome windows and diffusion noise: reruns with the same seed reproduce up to GPU kernel nondeterminism.

`train-align` / `train-prior` print DNA→image retrieval on the eval splits: specimen top-1/top-5 (find the exact
specimen's photo), and species/genus top-1 (nearest photo has the right label). The prior also reports mean cosine
between sampled and true image embeddings. Checkpoints and `*_metrics.json` go to `<data>/checkpoints/`.

## Not yet done

- Decoder has not been trained (no GPU). Planned: train on all BIOSCAN-5M images, then add iNaturalist/TreeOfLife-10M.
- Whole-genome input beyond window averaging: a long-context model (HyenaDNA / Caduceus) or learned pooling over
  many windows.
- FID/KID and BioCLIP zero-shot species accuracy on generated images.
