# genes.jpg

> Generate an image of a species from its DNA.

Datasets and models for **genome ↔ organism image** learning: given DNA, predict what the organism looks like; given
a photo, retrieve the matching genome. The repository has two parts:

- **Model** (`genesjpg/`): a DNA barcode (the ~650 bp mitochondrial COI region used to identify animal species) →
  image model, trained first on insects from [BIOSCAN-5M](https://huggingface.co/datasets/bioscan-ml/BIOSCAN-5M),
  where every specimen has both a barcode and a photo.
- **Dataset pipeline** (`src/datasets/`, CLI `genes-datasets`): builds validated species-level datasets that pair
  NCBI RefSeq reference genomes with licensed organism photos; the first is a 5-species mammal smoke dataset
  (`tol200m-mammals-smoke`).

Status (2026-10-03): model steps 1–3 are implemented and tested with tiny models; they have not been trained on real
data with ModernGENA yet, and the Stable Diffusion decoder is untrained (no GPU). The dataset pipeline is in place and
`tol200m-mammals-smoke` (5 RefSeq genomes, 250 images, 250 pairs) has been built, validated and independently audited.
The model does not yet consume the pipeline's genome datasets.

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

## Layout

```
genesjpg/                   DNA -> image model
  data.py                   BIOSCAN-5M subset download (HTTP range requests into the remote zips), records, captions
  encoders.py               step 1: ModernGENA DNAEncoder + HFTokenizer; frozen BioCLIP image/text towers
  align.py                  step 2: contrastive loss, AlignModel, train_align, retrieval_metrics
  prior.py                  step 3: DiffusionPrior, train_prior
  decoder.py                step 4: EmbeddingProjector, EmbeddingDecoder, train_decoder
  pipeline.py               GenomeToImage (end-to-end inference, retrieval) + checkpoint helpers
  cli.py                    `python -m genesjpg <command>`
src/datasets/               the `datasets` package (console script: genes-datasets)
  schema.py                 pydantic record models: SpeciesRecord, GenomeRecord, ImageRecord,
                            PairRecord, DatasetManifest, SourceInfo (the contract; versioned)
  ncbi.py                   NCBI taxonomy resolution, RefSeq assembly listing/choice, MD5-verified
                            resumable genome download (~3 req/s to the Datasets v2 API)
  images.py                 polite concurrent image downloader (per-host rate limit, retries,
                            PIL validation, sha256)
  manifest.py               parquet + CSV tables, dataset.json, validate_dataset()
  build.py                  config-driven builder: species -> genomes -> images -> tables -> validate
  sources/base.py           ImageSource protocol + ImageCandidate (what a new image source implements)
  sources/treeoflife200m.py TreeOfLife-200M source (DuckDB over catalog + provenance parquets)
  cli.py                    typer app: build, validate, info, schema-export
configs/                    dataset build configs, one JSON per dataset (configs/tol200m-mammals-smoke.json)
schemas/dataset.schema.json JSON Schema of the dataset format (generated; do not edit by hand)
scripts/e2e_tiny_dataset.py end-to-end exercise of ncbi/images/manifest on a tiny dataset
tests/                      pytest suite: test_model.py (model, tiny random models) + dataset tests
                            (tests marked `network` hit NCBI/GBIF/image hosts)
docs/
  model.md                  model details, run steps, metrics
  architectures.md          literature review behind the model architecture
  datasets.md               literature/resource survey of genome, image and phenotype datasets
  creating_datasets.md      how to build a dataset, config reference, what the smoke dataset contains
  dataset_schema.md         the dataset format: tables, fields, design decisions
  survey_mammals.md         why TreeOfLife-200M is the first image source (measured survey)
  audits/                   independent audits of built datasets
modal_app.py                Modal entrypoints for the model (CPU stages + GPU decoder/generation)
.agents/skills/             Amass API skill used for the literature/dataset research
data/datasets -> /mnt/filesystem-s8/genes.jpg/datasets   shared disk, git-ignored (see Setup)
```

## Setup

```bash
git clone https://github.com/genesjpgorg/genes.jpg.git && cd genes.jpg
~/.local/bin/uv sync                      # Python 3.12 env in .venv: dataset pipeline + dev tools (pytest, ruff)
~/.local/bin/uv sync --extra model        # also install the model stack (torch, transformers, diffusers, open_clip)

# Large artefacts (raw source metadata, genomes, images) live on the shared disk, outside git.
mkdir -p /mnt/filesystem-s8/genes.jpg/datasets data
ln -s /mnt/filesystem-s8/genes.jpg/datasets data/datasets

.venv/bin/python -m pytest -q             # add -m "not network" to stay offline; model tests skip without --extra model
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

`uv` and `uvx` are installed in `~/.local/bin`. The NCBI Datasets API needs no key; set
`NCBI_API_KEY` for a higher rate limit. The TreeOfLife-200M metadata (27 GB raw, plus the
0.5 GB `mammalia_*.parquet` subsets the source reads by default) is already on the shared
disk; the one-off commands to recreate it are in
[docs/creating_datasets.md](docs/creating_datasets.md#source-metadata-treeoflife-200m).

## Model pipeline

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

## Dataset format

A dataset is a directory under `data/datasets/<name>/` with four tables (parquet + CSV),
a manifest and the files:

```
dataset.json                 DatasetManifest: provenance, schema version, selection params, counts
species.parquet              one row per species           (NCBI taxid is the identity)
genomes.parquet              one row per genome assembly   (accession, NCBI stats, md5, file)
images.parquet               one row per image             (source, license, sha256, size, file)
pairs.parquet                image_id <-> assembly_accession (pairing = species_reference)
genomes/<accession>/         <asm>_genomic.fna.gz, <asm>_assembly_report.txt, md5checksums.txt
images/<taxid>/<image_id>.<ext>
logs/                        build.log, build_report.json, image_failures.jsonl
README.md                    generated summary with per-species table, licences and caveats
```

Design points: species identity is the NCBI Taxonomy ID (source names are kept only as
labels); the genome is a species-level reference, not the pictured individual's; splits are
assigned per species; every image carries its own licence fields; all paths are relative to
the dataset root. Field-by-field definitions: [docs/dataset_schema.md](docs/dataset_schema.md)
and [schemas/dataset.schema.json](schemas/dataset.schema.json)
(`genes-datasets schema-export` regenerates it from `schema.py`).

## Building a dataset

```bash
.venv/bin/genes-datasets build configs/tol200m-mammals-smoke.json --dry-run   # plan: taxids, assemblies, candidate counts
.venv/bin/genes-datasets build configs/tol200m-mammals-smoke.json             # build; idempotent, resumable
.venv/bin/genes-datasets validate data/datasets/tol200m-mammals-smoke --check-hashes
.venv/bin/genes-datasets info     data/datasets/tol200m-mammals-smoke
```

The config is the single input: species names, the genome source (NCBI RefSeq, with an
optional `assembly_summary` snapshot as cross-check) and the image source with its filters
(image types, record basis, licence policy, photos per observation, size variant, seed). The
builder resolves names to taxids, downloads one MD5-verified RefSeq assembly per species,
takes a seeded deterministic sample of image candidates (1.3x oversampled so dead URLs can be
replaced), downloads them at <= 4 requests/s per host, writes tables + `dataset.json` +
`README.md`, and fails unless `validate_dataset(check_hashes=True)` is clean. Re-running at
the same root reuses verified files. Exit codes: 0 ok, 1 build failed, 2 bad config.
Config reference and a step-by-step description:
[docs/creating_datasets.md](docs/creating_datasets.md).

### Extending the pipeline

- **New image source**: implement the `ImageSource` protocol from `src/datasets/sources/base.py`
  (`name`, `source_info()`, `select(species, *, per_species, seed)`, `selection_params()`),
  return `ImageCandidate`s (URL, fallback URLs, licence, observation id, image type), and
  register a factory in `datasets.build.SOURCES`. Constructor keyword arguments are filled from
  the `images` block of the config (`construct_source`), so filters are declared in the
  constructor and reported by `selection_params()` into `dataset.json`. Sources never download
  bytes and never talk to NCBI. `tests/test_source_treeoflife200m.py` shows the fixture style
  (tiny parquets, no network).
- **Schema changes**: edit `schema.py`, bump `SCHEMA_VERSION` (semver), run
  `genes-datasets schema-export`; `tests/test_manifest.py` checks the exported file is in sync
  and valid draft 2020-12.
- **Other genome sources**: only `ncbi_refseq` exists (`GenomesConfig.source`); the per-species
  choice is `ncbi.pick_best_assembly` (reference > representative; higher assembly level; newest).

## The data so far

`data/datasets/tol200m-mammals-smoke` (= `/mnt/filesystem-s8/genes.jpg/datasets/tol200m-mammals-smoke`),
built 2026-10-03 from [configs/tol200m-mammals-smoke.json](configs/tol200m-mammals-smoke.json):
five mammals from five orders, each with a chromosome-level RefSeq reference genome and 50
TreeOfLife-200M `Citizen Science` photos (one per observation, CC-licensed, iNaturalist
`large` variant <= 1024 px, shorter side >= 224 px).

| Species | TaxID | Order | RefSeq assembly | Level | Genome (bp) | Images |
|---|---|---|---|---|---|---|
| *Vulpes vulpes* (red fox) | 9627 | Carnivora | GCF_048418805.1 VulVul3 | Chromosome | 2,395,584,468 | 50 |
| *Odocoileus virginianus* (white-tailed deer) | 9874 | Artiodactyla | GCF_023699985.2 Ovbor_1.2 | Chromosome | 2,423,246,854 | 50 |
| *Castor canadensis* (American beaver) | 51338 | Rodentia | GCF_047511655.1 mCasCan1.hap1v2 | Chromosome | 2,814,832,999 | 50 |
| *Tachyglossus aculeatus* (Australian echidna) | 9261 | Monotremata | GCF_015852505.1 mTacAcu1.pri | Chromosome | 2,213,004,214 | 50 |
| *Dasypus novemcinctus* (nine-banded armadillo) | 9361 | Cingulata | GCF_030445035.2 mDasNov1.1.hap2 | Chromosome | 3,610,534,858 | 50 |

Totals: 5 genomes (4,162,803,657 bytes gzipped, all `reference genome`, MD5-verified against
NCBI), 250 images (95,716,305 bytes; 219 from iNaturalist's open-data bucket, 22 Atlas of
Living Australia, 9 observation.org), 250 pairs; 0 failed downloads. No split is assigned
(`species.split` is empty).

Two independent audits ([docs/audits/tol200m-mammals-smoke.md](docs/audits/tol200m-mammals-smoke.md))
re-derived every checksum, field and count from primary sources and found no defects. Known
caveats, also written into the dataset's own `README.md`:

- `image_type = citizen_science` is the source's label for the *record*, not for the picture, and
  no content filter is applied. A visual check found 83–92% live animals for four species but
  17% for the beaver (iNaturalist documents beavers mostly by chewed wood and dams); roughly a
  fifth of the photos are infrared trail-camera frames uploaded by volunteers.
- `rights_holder` is `null` for 62 of 250 images (TreeOfLife-200M stores the placeholder
  `not provided`, which the source maps to `null`); the licence itself is always present.
- `genome_size` is NCBI's nuclear primary-assembly length; two FASTA files also contain the
  RefSeq mitochondrion, so their `n_sequences` is `scaffold_count + 1`.

Why mammals and TreeOfLife-200M: RefSeq has current assemblies for only 270 mammal species, and
TreeOfLife-200M covers 250 of them with 2.25 M images, the largest overlap of the four datasets
measured in [docs/survey_mammals.md](docs/survey_mammals.md).

## Licensing

- **Images are licensed individually**, not by the dataset. Each `images` row records
  `license`, `license_url`, `rights_holder` and `publisher`; attribute per image. In the smoke
  dataset: 187 CC BY-NC 4.0, 46 CC BY 4.0, 10 CC BY-NC-ND 4.0, 5 CC0 1.0, 1 CC BY-SA 4.0,
  1 CC BY-NC-SA 4.0. Across all TreeOfLife-200M mammal images 77% are CC BY-NC, so anything
  commercial needs the `permissive` licence policy (CC0 / public domain / CC BY / CC BY-SA
  only) and far fewer images.
- Most bytes come from iNaturalist's open-data S3 bucket. The photos stay under their
  observers' CC licences and iNaturalist's terms of use; the builder fetches at most 4 requests/s
  per host and nothing beyond what the config asks for. Share the tables (URLs, licences,
  hashes) rather than redistributing image bytes unless every licence allows it.
- TreeOfLife-200M metadata is CC0-1.0; the GBIF occurrence snapshot it derives from is
  CC BY-NC 4.0 (cite doi:10.15468/dl.bfv433). NCBI genomes and taxonomy are public domain.
  `dataset.json -> sources[].license_notes` repeats this per dataset.

## Next

- Model:
  - train steps 1–3 with ModernGENA, then the decoder once a GPU is available;
  - add a standalone evaluation command, DNA-similarity/taxonomy baselines, and generated-image metrics
    (FID/KID, BioCLIP species accuracy);
  - whole-genome conditioning on the pipeline's RefSeq genomes via a long-context DNA model.
- Datasets:
  - scale the config to all ~250 RefSeq mammals with hundreds of images each
    (`data/datasets/_survey/treeoflife-200m/refseq_intersect.csv` lists them with per-type image counts);
    ~0.4 MB per `large` image, ~0.8 GB per genome;
  - match catalog names by NCBI taxid with synonym resolution instead of exact binomials
    (recovers ~14 RefSeq species such as *Neogale vison* / *Neovison vison*);
  - add an image-content filter at candidate selection (live animal vs. sign/tracks/remains);
  - assign species-held-out splits (`species.split`), then genus/family-held-out.

## Contributing

1. Branch from `main`.
2. Keep `pytest`, `ruff check` and `ruff format --check` clean.
3. Keep dataset builds deterministic (seeded), with provenance recorded in `dataset.json`.
4. Open a pull request.

## License

Code license to be decided. Data licences are per source/image; see Licensing above.
