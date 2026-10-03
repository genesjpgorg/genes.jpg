# genes.jpg

> Generate an image of a species from its DNA.

genes.jpg takes a DNA barcode (the ~650 bp mitochondrial COI region used to identify animal species) and produces a
picture of the organism it came from. The first target is insects, using
[BIOSCAN-5M](https://huggingface.co/datasets/bioscan-ml/BIOSCAN-5M), where every specimen has both a barcode and a photo.

## Architecture

No published model generates whole-organism images from DNA, so the design combines three approaches (literature
review: [docs/architectures.md](docs/architectures.md)):

- **Difface**: first align DNA and images in a shared embedding space, then generate with diffusion.
- **CLIBD / BioCLIP**: contrastive DNA–image–taxonomy space for biodiversity data.
- **unCLIP (DALL-E 2)**: a *prior* that samples an image embedding from the condition, and a *decoder* that renders
  an image from that embedding.

```
                 ┌──────────────────────── trained on paired DNA + photo ────────────────────────┐
COI barcode ──► [1] DNA encoder ──► [2] aligned DNA embedding ──► [3] diffusion prior ──► BioCLIP image embedding
                 ModernGENA            (512-d, BioCLIP space)        samples one plausible           │
                                              │                      image embedding                 │
                                              │                                                      ▼
                                              └──► retrieval baseline:                 [4] decoder: Stable Diffusion
                                                   nearest real photos                 conditioned on the embedding
                                                                                       (trained on photos only)
                                                                                                     │
                                                                                                     ▼
                                                                                                   image
```

| Step | What it does | Model | Trained? | Code |
|---|---|---|---|---|
| 1. DNA encoder | Turns a barcode into a vector: tokenise, run the transformer, mean-pool, project to 512-d with an MLP | [ModernGENA](https://huggingface.co/AIRI-Institute/moderngena-base), 22-layer ModernBERT DNA LM, 135M params, 32k BPE tokens (~107 per barcode) | fine-tuned in step 2 | `genesjpg/encoders.py` `DNAEncoder` |
| 2. Alignment | Pulls each DNA vector next to the frozen [BioCLIP](https://huggingface.co/imageomics/bioclip) embedding of the same specimen's photo, plus (weight 0.5) the embedding of its taxonomy caption, e.g. *"a photo of Animalia Arthropoda Insecta Coleoptera Latridiidae Cortinicara gibbosa"*. Label-aware InfoNCE: specimens of the same species in a batch are all positives | DNA encoder + learned temperature; BioCLIP frozen | yes, CPU | `genesjpg/align.py` |
| 3. Prior | One barcode fits many photos (pose, sex, life stage), so instead of regressing an average it *samples* a BioCLIP image embedding given the DNA embedding | MLP diffusion model, x0-prediction, cosine schedule, classifier-free guidance, DDIM sampling | yes, CPU | `genesjpg/prior.py` `DiffusionPrior` |
| 4. Decoder | Renders an image from a BioCLIP image embedding, which is projected to 8 cross-attention tokens that replace the text prompt (IP-Adapter style) | Stable Diffusion 1.5 (frozen VAE + UNet) + `EmbeddingProjector`; optional UNet fine-tune | needs a GPU | `genesjpg/decoder.py` `EmbeddingDecoder` |

`genesjpg/pipeline.py` `GenomeToImage` chains all four steps for inference.

**Why this split**
- **The decoder never sees DNA.** It learns *image embedding → picture* from any species photos, so it can later use
  much larger image-only collections (iNaturalist, TreeOfLife-10M). Only steps 1–3, which are small, need the scarcer
  DNA–photo pairs.
- **There is a baseline before generation.** After step 2, *DNA → nearest real photos* already works on CPU.
- **Each step is evaluated on its own:**
  - retrieval accuracy for step 2;
  - how close sampled embeddings are to the real ones for step 3;
  - image quality and species accuracy for step 4.
  The `val_unseen` split holds species absent from training.

## Repository layout

```
genes.jpg/
├── genesjpg/
│   ├── data.py        # BIOSCAN-5M subset download (HTTP range requests into the remote zips), records, captions
│   ├── encoders.py    # step 1: ModernGENA DNAEncoder + HFTokenizer; frozen BioCLIP image/text towers
│   ├── align.py       # step 2: contrastive loss, AlignModel, train_align, retrieval_metrics
│   ├── prior.py       # step 3: DiffusionPrior, train_prior
│   ├── decoder.py     # step 4: EmbeddingProjector, EmbeddingDecoder, train_decoder
│   ├── pipeline.py    # GenomeToImage (end-to-end inference, retrieval) + checkpoint helpers
│   └── cli.py         # `python -m genesjpg <command>`
├── tests/             # unit tests with tiny random models (CPU, ~10 s)
├── modal_app.py       # the same stages as Modal functions (CPU stages + GPU decoder/generation)
├── docs/              # model.md (details), architectures.md (literature review), datasets.md (data survey)
└── .agents/skills/    # Amass API skill used for the literature/dataset research
```

## Getting started

```bash
git clone https://github.com/genesjpgorg/genes.jpg.git
cd genes.jpg
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu   # or a CUDA build
pip install -e '.[dev]'
pytest
```

### Pipeline

Every command reads and writes under `--data` (records, images, `embeddings.pt`, `checkpoints/`).

```bash
python -m genesjpg --data data download --n-train 20000 --n-eval 3000   # paired BIOSCAN-5M subset
python -m genesjpg --data data embed                                    # frozen BioCLIP image + caption embeddings
python -m genesjpg --data data train-align --epochs 5                   # steps 1-2 (CPU; --freeze-layers N to speed up)
python -m genesjpg --data data train-prior --epochs 50                  # step 3 (CPU)
python -m genesjpg --data data train-decoder --max-steps 2000           # step 4 (GPU)
python -m genesjpg --data data generate --processid <BOLD processid>    # nearest photos (+ images if decoder.pt exists)
```

From Python:

```python
from genesjpg.pipeline import GenomeToImage

model = GenomeToImage.from_checkpoints("data/checkpoints", gallery=(image_embeddings, records))
model.retrieve(barcode, k=5)  # [(record, cosine similarity), ...]
images = model.generate(barcode, n=4)  # list of PIL images (needs decoder.pt)
```

### Modal

`modal run modal_app.py` runs data prep and steps 1–3 on CPU, using the `genes-jpg-data` Volume.
`modal run modal_app.py::train_decoder` and `::generate` need a GPU, which requires a payment method on the Modal
workspace.

## Status

- Steps 1–3 are implemented and tested. They have not yet been trained on real data with ModernGENA.
- Step 4 is implemented and only tested with a tiny Stable Diffusion model. It is untrained until GPU access is
  available.
- Planned:
  - whole-genome input via a long-context DNA model;
  - FID/KID and BioCLIP species accuracy on generated images.

More detail: [docs/model.md](docs/model.md).

## Contributing

1. Create a branch: `git checkout -b feature/my-change`
2. Run `ruff check . && ruff format --check . && pytest`
3. Push the branch and open a pull request.

## License

Specify the license here (e.g. MIT).
