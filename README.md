# genes.jpg

> Generate an image of a species from its DNA.

## Overview

genes.jpg maps a DNA barcode (COI) to a picture of the organism. The model has four stages: a BarcodeBERT DNA encoder,
contrastive alignment with frozen BioCLIP image/taxonomy embeddings, a diffusion prior that samples an image
embedding, and a Stable Diffusion decoder conditioned on that embedding. Details: [docs/model.md](docs/model.md).
Background: [docs/datasets.md](docs/datasets.md), [docs/architectures.md](docs/architectures.md).

## Getting Started

```bash
git clone https://github.com/genesjpgorg/genes.jpg.git
cd genes.jpg
pip install -e '.[dev]'
pytest
```

### Usage

```bash
python -m genesjpg --data data download        # paired BIOSCAN-5M subset (DNA + image + taxonomy)
python -m genesjpg --data data embed           # BioCLIP embeddings
python -m genesjpg --data data train-align     # steps 1-2 (CPU)
python -m genesjpg --data data train-prior     # step 3 (CPU)
python -m genesjpg --data data train-decoder   # step 4 (GPU)
python -m genesjpg --data data generate --processid <BOLD processid>
```

Modal: `modal run modal_app.py` runs the CPU stages on the `genes-jpg-data` Volume.

## Project Structure

```
genes.jpg/
├── genesjpg/          # data, encoders, align, prior, decoder, pipeline, cli
├── tests/
├── modal_app.py       # Modal entrypoints
├── docs/              # model, datasets, architecture review
└── .agents/skills/    # Amass API skill
```

## Contributing

1. Create a branch: `git checkout -b feature/my-change`
2. Commit your changes: `git commit -m "Describe change"`
3. Push the branch and open a pull request.

## License

Specify the license here (e.g. MIT).
