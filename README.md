# genes.jpg

Datasets and models for **genome ↔ organism image** learning: given a species' reference
genome, predict what the organism looks like, and given a photo, retrieve the matching genome.

Status (2026-10-03): the dataset pipeline is in place and a first 5-species mammal dataset
(`tol200m-mammals-smoke`) has been built and validated. Model code has not started.

## What is here

```
configs/            build configs, one per dataset (JSON)
docs/
  creating_datasets.md   how to build a dataset + description of the built data  <-- start here
  dataset_schema.md      the dataset format (tables, fields, design decisions)
  survey_mammals.md      why TreeOfLife-200M was chosen as the first image source
  datasets.md            literature/resource survey of genome, image and phenotype datasets
schemas/dataset.schema.json   JSON Schema of the dataset format (generated)
src/datasets/       the `datasets` package (CLI: genes-datasets)
  schema.py           pydantic record models (species, genomes, images, pairs, manifest)
  ncbi.py             NCBI taxonomy resolution, RefSeq assembly lookup, verified genome download
  images.py           polite concurrent image downloader with validation
  manifest.py         parquet/CSV tables, dataset.json, integrity validation
  build.py            config-driven builder (species -> genomes -> images -> tables)
  sources/            image-metadata sources (treeoflife200m.py; base.py is the interface)
scripts/            e2e_tiny_dataset.py — end-to-end exercise of the modules
tests/              pytest suite (offline + a few live NCBI/GBIF tests marked `network`)
data/datasets -> /mnt/filesystem-s8/genes.jpg/datasets   shared disk, git-ignored
modal_app.py        Modal GPU entrypoint (placeholder for training)
```

## Setup

```bash
git clone https://github.com/genesjpgorg/genes.jpg.git && cd genes.jpg
uv sync                                   # Python 3.12 env in .venv
ln -s /mnt/filesystem-s8/genes.jpg/datasets data/datasets   # large artefacts live off-repo
.venv/bin/python -m pytest -q             # 164 tests, ~1 min (set NCBI_API_KEY to go faster)
```

## Building a dataset

```bash
.venv/bin/genes-datasets build configs/tol200m-mammals-smoke.json --dry-run   # plan
.venv/bin/genes-datasets build configs/tol200m-mammals-smoke.json             # build (idempotent)
.venv/bin/genes-datasets validate data/datasets/tol200m-mammals-smoke --check-hashes
.venv/bin/genes-datasets info data/datasets/tol200m-mammals-smoke
```

A config names the species, the genome source (NCBI RefSeq) and the image source with its
filters (image type, record basis, licenses, photos per observation, size, seed). The builder
resolves names to NCBI taxids, downloads one MD5-verified RefSeq assembly per species,
samples and downloads images deterministically, writes `species/genomes/images/pairs` tables
plus `dataset.json`, and refuses to finish unless the result validates. Details, config
reference and the source-metadata preparation steps: [docs/creating_datasets.md](docs/creating_datasets.md).

## The data so far

`data/datasets/tol200m-mammals-smoke` — 5 mammal species from 5 orders, each with a
chromosome-level RefSeq reference genome and 50 citizen-science photos from TreeOfLife-200M:

| Species | TaxID | Order | Assembly | Genome | Images |
|---|---|---|---|---|---|
| *Vulpes vulpes* | 9627 | Carnivora | GCF_048418805.1 | 2.40 Gb | 50 |
| *Odocoileus virginianus* | 9874 | Artiodactyla | GCF_023699985.2 | 2.42 Gb | 50 |
| *Castor canadensis* | 51338 | Rodentia | GCF_047511655.1 | 2.81 Gb | 50 |
| *Tachyglossus aculeatus* | 9261 | Monotremata | GCF_015852505.1 | 2.21 Gb | 50 |
| *Dasypus novemcinctus* | 9361 | Cingulata | GCF_030445035.2 | 3.61 Gb | 50 |

4.16 GB of genomes, 95.7 MB of images (≤1024 px), 250 image↔genome pairs; every image
carries its own license (187 CC BY-NC 4.0, 46 CC BY 4.0, 17 other CC). Images are
individually licensed and mostly non-commercial; see the licensing notes in
[docs/creating_datasets.md](docs/creating_datasets.md#5-the-generated-dataset-tol200m-mammals-smoke).
Independent audits ([docs/audits/](docs/audits/tol200m-mammals-smoke.md)) verified every file
and field against NCBI and the source catalog; a visual check found 83–92% of photos show the
live animal, except the beaver (17% — mostly chewed wood), so a content filter is the next
pipeline step.

Why mammals and TreeOfLife-200M: RefSeq has reference assemblies for only ~270 mammal
species, and TreeOfLife-200M covers 250 of them with 2.25 M images — the largest overlap of
the datasets surveyed ([docs/survey_mammals.md](docs/survey_mammals.md)).

## Next

Extend the config to all ~250 RefSeq mammals with hundreds of images each, match catalog
names by NCBI taxid (synonyms), assign species-held-out splits, and start on
genome-conditioned image models (see `docs/datasets.md` for baselines such as CLIBD and
G2PDiffusion).

## Contributing

Branch from `main`, keep `pytest` and `ruff check/format` clean, open a pull request.
