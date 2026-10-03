# Which image dataset has the most mammal species? (survey, 2026-10-03)

Goal: pick the first image source for a genome ↔ image dataset restricted to **mammals with
NCBI RefSeq assemblies**. Four candidates from [datasets.md](datasets.md) were measured from
their own metadata; BIOSCAN-5M was excluded up front (insects only).

## Method

- **Genome side.** RefSeq `vertebrate_mammalian/assembly_summary.txt` snapshot of
  2026-10-03: 277 assemblies, **270 species** (269 distinct binomials; 269 flagged
  "reference genome"; 187 chromosome-level).
- **Species-level rule.** A label counts as a species if it is a binomial whose second token
  is a lowercase epithet (no `sp.`, ranks or authorship); the binomial is the first two tokens,
  lowercased, parentheses stripped.
- **Intersection.** Exact binomial match between dataset labels and RefSeq
  `organism_name`; no synonym resolution (see caveats).
- Each dataset's metadata was downloaded in full to `data/datasets/<key>/raw/`, counted
  with DuckDB/polars, and every headline number was independently recomputed by a second
  agent with a different tool. All four recomputations agreed exactly.

## Results

| Dataset (revision) | Mammal species | Mammal images | ∩ RefSeq species (of 270) | Images in ∩ |
|---|---|---|---|---|
| **TreeOfLife-200M** (HF `5f2dc493`) | **5,537** (5,512 binomials) | 4.81 M | 250 | 2.25 M |
| TreeOfLife-10M | 4,089 (~4,033 clean) | 0.21 M | 244 | 66 k |
| BioTrove (full, HF) | 3,725 | 4.19 M | **256** | 1.90 M |
| iNaturalist 2021 | 246 | 71 k | 86 | 25 k |

**Chosen: TreeOfLife-200M** — most mammal species, 2.25 M images for the RefSeq species,
per-image licenses via `provenance.parquet`, 168 of the 250 matched species with
chromosome-level assemblies. BioTrove is the practical runner-up: highest RefSeq overlap,
iNaturalist research-grade photos only, cleanest taxonomy; its photos are largely a subset of
TreeOfLife-200M's iNaturalist rows. TreeOfLife-10M images are impractical to retrieve
(EOL bytes in 63 unindexed 32 GB tarballs; `content.eol.org` returns 403). iNat2021 is too
small on mammals.

TreeOfLife-200M mammal images by type: 4.28 M citizen science (3,817 species), 251 k
camera-trap (117 species), 129 k museum specimens (5,028 species — this inflates the species
count), 144 k untyped EOL rows. Hosts: iNaturalist S3 78%, observation.org 7%, Agouti
camera traps 5%, EOL 3%, others. Licenses: 68% CC BY-NC 4.0, 12% CC BY 4.0, 3.5% CC0/PD,
3.9% NULL, 2.6% other/all-rights-reserved.

## Caveats found during the survey

- `catalog.source_url` is the per-image URL; `provenance.source_url` is occurrence-level and
  differs for 39% of mammal rows (sibling photos) — use provenance only for licenses.
- `img_type` exists only for GBIF rows; EOL rows are untyped and include specimen photos.
- Taxonomy backbones differ (GBIF names in TreeOfLife-200M, iNaturalist names in BioTrove,
  NCBI names in RefSeq): the union of the four intersections is 264/269 RefSeq binomials, so
  ~14 species are recoverable by resolving synonyms (e.g. *Neogale vison*, *Panthera uncia*,
  *Cervus canadensis*, *Bos mutus*).
- Multi-assembly species need a policy (*Canis lupus* has 5 RefSeq assemblies: wolf vs dog).
- 509 iNaturalist URLs end in `original.` without extension; one ends in `.txt`.
- The revision surveyed (BioCLIP 2.5, 2026-05) differs from the paper's revision.

## Artefacts

Under `data/datasets/_survey/<key>/` (shared disk): `mammal_species.csv`,
`refseq_intersect.csv`, the exact `survey.py`, `notes.md`, and the verifier's scripts in
`verify/`. Mammalia subsets of the TreeOfLife-200M catalog and provenance are at
`data/datasets/treeoflife-200m/mammalia_{catalog,provenance}.parquet`.
