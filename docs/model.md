# genes.jpg model

DNA barcode (COI) → species image. The design merges Difface (align, then diffuse), CLIBD (DNA–image–taxonomy
contrastive space) and unCLIP (prior + embedding-conditioned decoder). See [architectures.md](architectures.md) for
the literature review behind it.

```
COI barcode ─► [1] ModernGENA ─► [2] aligned DNA emb ─► [3] diffusion prior ─► BioCLIP image emb ─► [4] SD decoder ─► image
                                         │                                               ▲
                                         └── retrieval baseline: nearest real image ─────┘
```

| Step | Module | What is trained | Data | Compute |
|---|---|---|---|---|
| 1 DNA encoder | `genesjpg/encoders.py` `DNAEncoder` | ModernGENA (`AIRI-Institute/moderngena-base`, 22-layer ModernBERT, GENA-LM 32k BPE; fine-tuned) + mean-pool + MLP head → 512-d | — | CPU |
| 2 Alignment | `genesjpg/align.py` | DNA tower only; BioCLIP image/text towers frozen. Loss: label-aware InfoNCE DNA↔image + 0.5·DNA↔taxonomy text | paired DNA+image (BIOSCAN-5M) | CPU |
| 3 Prior | `genesjpg/prior.py` `DiffusionPrior` | MLP denoiser, x0-prediction, cosine schedule, CFG, DDIM sampling | paired | CPU |
| 4 Decoder | `genesjpg/decoder.py` `EmbeddingDecoder` | `EmbeddingProjector` (BioCLIP emb → 8 cross-attention tokens) into frozen SD 1.5; optional full UNet fine-tune | images only (any species photos) | GPU |

`genesjpg/pipeline.py` `GenomeToImage` chains the steps for inference and also exposes `retrieve()`.

## Data

`python -m genesjpg download` fetches a BIOSCAN-5M subset with HTTP range requests (metadata CSV streamed from its zip,
images in contiguous blocks of zip entries): `train` split for training, `val` (seen species) and `val_unseen`
(species absent from training) for evaluation. Records keep only specimens with a barcode and at least a genus label.

## Run

```bash
pip install -e '.[dev]'
python -m genesjpg --data data download --n-train 20000 --n-eval 3000
python -m genesjpg --data data embed                 # frozen BioCLIP image + taxonomy-text embeddings
python -m genesjpg --data data train-align --epochs 5
python -m genesjpg --data data train-prior --epochs 50
python -m genesjpg --data data train-decoder --max-steps 2000   # GPU
python -m genesjpg --data data generate --processid <BOLD processid>
```

On Modal (see `modal_app.py`): `modal run modal_app.py` runs steps 1–3 on CPU; `train_decoder` and `generate` need a
GPU, which requires a payment method on the Modal workspace.

## Metrics

`train-align` / `train-prior` print DNA→image retrieval on the eval splits: specimen top-1/top-5 (find the exact
specimen's photo), and species/genus top-1 (nearest photo has the right label). The prior also reports mean cosine
between sampled and true image embeddings. Checkpoints and `*_metrics.json` go to `<data>/checkpoints/`.

## Not yet done

- Decoder has not been trained (no GPU). Planned: train on all BIOSCAN-5M images, then add iNaturalist/TreeOfLife-10M.
- Whole-genome input: swap `DNAEncoder` for a long-context model (HyenaDNA / Caduceus).
- FID/KID and BioCLIP zero-shot species accuracy on generated images.
