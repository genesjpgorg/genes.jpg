# Which image dataset has the most mammal species? (survey, 2026-10-03)

Goal: choose the first image source for a genome ↔ image dataset restricted to **mammals with
a current NCBI RefSeq assembly**. Four candidates from [datasets.md](datasets.md) were measured
from their own metadata (no image bytes downloaded); BIOSCAN-5M was excluded up front (insects
only). Outcome: **TreeOfLife-200M**, now implemented as `datasets.sources.treeoflife200m` and
used for [`tol200m-mammals-smoke`](creating_datasets.md#5-the-generated-dataset-tol200m-mammals-smoke).

## Method

- **Genome side.** RefSeq `vertebrate_mammalian/assembly_summary.txt`, snapshot of 2026-10-03
  (`data/datasets/_ncbi/assembly_summary_vertebrate_mammalian_refseq.txt`): 277 assemblies,
  **270 `species_taxid`**, 269 distinct binomials (*Canis lupus* baileyi/dingo/familiaris share
  one binomial and one species taxid; *Bos indicus* and *Bos indicus x Bos taurus* share a
  binomial but not a taxid). 269 are flagged `reference genome`; levels: 187 Chromosome,
  79 Scaffold, 7 Contig, 4 Complete Genome.
- **Species-level rule** (shared by all four surveys). A label counts as a species if, after
  stripping parentheses and lower-casing, its first two whitespace tokens form a binomial whose
  second token matches `^[a-z]+$`: no `sp.`, rank words or authorship; subspecies and authority
  tails collapse into the binomial.
- **Intersection.** Exact binomial match between dataset labels and RefSeq `organism_name`;
  **no synonym resolution** (see caveats). When a binomial has several RefSeq assemblies the pick
  is `reference genome` > representative > other, then Complete Genome > Chromosome > Scaffold >
  Contig, then newest `seq_rel_date` (five such binomials: *Canis lupus* 5, *Homo sapiens*,
  *Rattus norvegicus*, *Bos indicus*, *Cricetulus griseus* 2 each).
- **Procedure.** Each dataset's metadata was downloaded in full at a pinned revision to
  `data/datasets/<key>/raw/`, counted with DuckDB / polars / pandas (`survey.py`), and every
  headline number was recomputed by a second agent with a different tool (`verify/`). All
  recomputations agreed exactly.

## Results

| Dataset (revision surveyed) | Raw metadata | Mammal species | Mammal images | ∩ RefSeq species (of 270) | Images in ∩ |
|---|---|---|---|---|---|
| **TreeOfLife-200M** (HF `imageomics/TreeOfLife-200M` @ `5f2dc493`, 2026-05-29) | 27 GB, 233.1 M rows | **5,537** (5,512 binomials) | 4.81 M | 250 | **2.25 M** |
| TreeOfLife-10M (HF `imageomics/TreeOfLife-10M` @ `91debffb`, 2026-01-15) | 7.8 GB | 4,089 | 0.21 M | 244 | 66 k |
| BioTrove (HF `BGLab/BioTrove` @ `4839f424`, 2024-12-13; full, not -Train) | 5.7 GB, 162.5 M rows | 3,725 | 4.19 M | **256** | 1.90 M |
| iNaturalist 2021 (competition release 2021-03-01) | 1.6 GB (annotations) | 246 | 71 k | 86 | 25 k |

Per dataset:

- **TreeOfLife-200M.** Metadata only (`catalog.parquet` 16.8 GB + `metadata/provenance.parquet`
  10.3 GB); bytes are fetched from each row's `catalog.source_url`. Mammalia: 4,806,057 rows,
  4,703,563 at species level. By `img_type`: 4.28 M Citizen Science (3,511 species), 251 k
  Camera-trap (104 species), 129 k Museum Specimen (3,699 species: the museum rows are what push
  the species count to 5,537), 144 k untyped (all EOL). Hosts: iNaturalist S3 78%,
  observation.org 7%, Agouti camera traps 5%, EOL 3%, Flickr, ALA, others. Across the 250
  matched species: 2,021,667 citizen-science, 164,828 camera-trap, 26,713 museum, 38,906
  untyped images; 206 species have >= 100 non-museum images, 128 have >= 1,000; 168 have a
  chromosome-level assembly. The revision is the BioCLIP 2.5 release, not the paper's `a8f38b4`.
- **TreeOfLife-10M.** 10,034,830 unique images once the `train_small` duplicate split is
  dropped; 207,334 Mammalia (EOL 138,417, iNat21 68,917; BIOSCAN 0). No image-type field; a
  heuristic flags 26,077 EOL images as museum-like. Image bytes are impractical: EOL images sit
  in 63 unindexed 32 GB `.tar.gz` archives (~2 TB, gzip stream, no id -> archive map),
  `content.eol.org` answers 403 to scripts, and iNat21 images need the 240 GB competition
  tarball. 11 of the 25 unmatched RefSeq species were deliberately moved to
  `imageomics/rare-species`.
- **BioTrove.** 3,251 parquet chunks, all rows iNaturalist photos at the `medium` (<= 500 px)
  URL on the same S3 bucket as TreeOfLife-200M, so larger variants are one path substitution
  away. 4,192,050 Mammalia rows, all `taxonRank = species`, no museum or camera-trap content.
  **No licence, observer or observation columns**: they were recovered by joining `photo_id` to
  the iNaturalist Open Data snapshot of 2026-09-27 (36 GB, `data/datasets/_inat_open_data/`),
  giving 79% CC BY-NC, 11% CC BY, 2.4% CC0, 98% research-grade, 0.8% of photos since deleted or
  re-licensed. Among the 256 matched species 1,902,012 images, 270,698 under CC0/BY/BY-SA.
- **iNaturalist 2021.** 246 mammal categories, 71,377 unique mammal images (<= 500 px), 86 match
  RefSeq (25,423 images). Bytes only inside the 240 GB `train.tar.gz` (val 8.9 GB, train_mini
  45 GB); no iNaturalist photo or observation ids, so no link back to the source record;
  competition terms: non-commercial research only, no redistribution of images; 90% CC BY-NC.

**Decision.** TreeOfLife-200M: the most mammal species, 2.25 M images for the RefSeq species,
per-image licences shipped in `provenance.parquet`, image bytes retrievable per row, and
observation ids (gbifID) to deduplicate photos of one animal. BioTrove is the practical
runner-up: highest RefSeq overlap, clean iNaturalist taxonomy, research-grade photos only, but
licences require the separate 36 GB join and its photos are largely a subset of
TreeOfLife-200M's iNaturalist rows. TreeOfLife-10M fails on image access; iNat2021 is too small
on mammals and forbids redistribution.

The five smoke-dataset species were taken from `refseq_intersect.csv` as visually distinct
animals from five orders with chromosome-level reference assemblies and 29 k–250 k
citizen-science photos each (*Odocoileus virginianus* 250,346, *Vulpes vulpes* 104,882,
*Castor canadensis* 56,285, *Tachyglossus aculeatus* 29,632, *Dasypus novemcinctus* 29,088).

## Caveats found during the survey

- **Catalog vs provenance URL.** `catalog.source_url` is unique per uuid and is the image's own
  URL. `provenance.source_url` is occurrence-level: it differs from the catalog URL for 39% of
  Mammalia rows (1,869,427), and in 1,760,102 of those it is the URL of a *sibling* photo of the
  same gbifID. Use provenance only for `license_name` / `license_link` / `copyright_owner`.
  Licences are effectively per occurrence (only 1 of 2,721,466 GBIF Mammalia occurrences has
  two licences among its photos), so the per-uuid join is reliable for licensing.
- **`img_type` is a record label, present only for GBIF rows.** EOL (144,200) and FathomNet (5)
  rows are NULL and include specimen photos. `Citizen Science` describes how the observation was
  published, not what the picture shows: the smoke-dataset audit found trail-camera frames,
  tracks, scat, remains and, for the beaver, 75% chewed wood
  ([audits/tol200m-mammals-smoke.md](audits/tol200m-mammals-smoke.md)). A content filter is a
  pipeline step still to be added. The Mammalia subset also contains fossil taxa
  (34,038 `FOSSIL_SPECIMEN` rows) and 92,774 `PRESERVED_SPECIMEN` rows.
- **Licences.** TreeOfLife-200M Mammalia: 77% CC BY-NC (any version), 12% CC BY, 3.9% NULL (all
  GBIF; mostly INBO/Agouti camera traps), 3.5% CC0/public domain, 2.6% `other` or
  all-rights-reserved (`other` rows often carry a CC URL in `license_link` but are not trusted),
  1.2% CC BY-SA. Across the 250 matched species only 349,070 images (15%) are CC0/BY/BY-SA.
  BioTrove ships no licences at all; iNat2021 adds competition-level non-commercial terms. The
  compilations themselves are CC0 (TreeOfLife, BioTrove metadata); the GBIF snapshot behind
  TreeOfLife-200M is CC BY-NC 4.0 (doi:10.15468/dl.bfv433).
- **Taxonomy backbones differ**: GBIF names in TreeOfLife-200M, iNaturalist names in BioTrove,
  EOL/ITIS names in TreeOfLife-10M, NCBI names in RefSeq. 19 RefSeq binomials are absent from
  TreeOfLife-200M under exact matching; the union of the four intersections is 264 of 269
  binomials, so **14 species are recoverable by resolving synonyms / backbone differences**
  (*Neogale vison* = *Neovison vison*, *Panthera uncia* = *Uncia uncia*, *Cervus canadensis*,
  *Bos mutus* = *Bos grunniens*, *Alexandromys fortis* = *Microtus fortis*, *Herpailurus
  yagouaroundi* = *Puma yagouaroundi*, *Leucopleurus acutus* = *Lagenorhynchus acutus*,
  *Notamacropus eugenii* = *Macropus eugenii*, *Camelus ferus*, *Bos indicus*, *Cricetulus
  griseus*, *Aotus nancymaae*, *Balaenoptera ricei*, *Pipistrellus hanaki*); five are in none
  of the four datasets. The source currently supports explicit `aliases`; taxid-based matching
  is the planned fix.
- **Multi-assembly species need a policy.** *Canis lupus* has 5 RefSeq assemblies; the mechanical
  rule picks the 2025 Mexican-wolf assembly (GCF_048164855.1) over the dog reference, while GBIF
  files domestic dogs under *Canis familiaris*, which RefSeq does not list.
- **URL oddities.** 509 iNaturalist URLs end in `original.` with no extension and one in `.txt`;
  the bucket serves the same suffix under every size variant (`large.`, `large.txt`), which
  `rewrite_inat_url` relies on. Deleted photos 404 under every variant, hence the builder's
  oversampling.
- `catalog.species` is the epithet only; `scientific_name` is the binomial, and ~5.7 k Mammalia
  rows carry authorship in it (`Urocitellus parryii (Richardson, 1825)`) with garbage in
  `genus`/`species`; names must be matched on `scientific_name` with authorship stripped.

## Artefacts

All on the shared disk (`data/datasets` = `/mnt/filesystem-s8/genes.jpg/datasets`):

| Path | Contents |
|---|---|
| `_survey/<key>/` for `treeoflife-200m`, `treeoflife-10m`, `biotrove`, `inat2021` | `survey.py` (exact script, rerunnable), `notes.md` (full write-up incl. column names and access tests), `summary.json` (`survey_summary.json` + `mammalia_breakdowns.json` for TreeOfLife-200M), `mammal_species.csv` (one row per binomial with per-type / per-licence counts), `refseq_intersect.csv` (matched species with the chosen assembly), `verify/` (second agent's scripts, logs and recomputed tables) |
| `_survey/biotrove/` extras | `license_join.py`, `refseq_intersect_license.csv`, `mammal_species_license.csv`, `license_summary.json` |
| `_survey/inat2021/per_image_mammals.csv` | every mammal image with split, licence, rights holder, date |
| `_survey/treeoflife-10m/mammalia_catalog.parquet`, `diagnostics/` | 207 k-row Mammalia table with licences; host/owner breakdowns |
| `treeoflife-200m/raw/` (26 GB), `treeoflife-10m/raw/` (8.3 GB), `biotrove/raw/` (5.4 GB), `inat2021/raw/` (1.6 GB) | pinned raw metadata as downloaded |
| `treeoflife-200m/mammalia_catalog.parquet`, `mammalia_provenance.parquet` | the `class = 'Mammalia'` subsets (4,806,057 rows each) that `datasets.sources.treeoflife200m` reads by default |
| `biotrove/mammalia_metadata.parquet`, `mammalia_photo_license.parquet` | BioTrove Mammalia rows, with the recovered per-photo licences |
| `_inat_open_data/` | iNaturalist Open Data snapshot 2026-09-27 (36 GB tarball + extracted CSVs), reusable for any iNaturalist-derived source |
| `_ncbi/assembly_summary_vertebrate_mammalian_refseq.txt` | the RefSeq snapshot all intersections used (also the builder's cross-check) |

`_survey/treeoflife-200m/refseq_intersect.csv` (250 rows: binomial, taxid, assembly, level,
genome size, per-`img_type` and per-licence image counts, order, family) is the natural
starting point for the species list of a full-scale config.
