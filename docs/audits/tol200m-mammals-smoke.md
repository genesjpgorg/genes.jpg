# Audit of `tol200m-mammals-smoke` (2026-10-03)

Two independent audits were run on the built dataset, by agents that had not seen the build
code and were told to re-derive everything with their own scripts rather than reuse
`genes-datasets validate`. Both passed. Per-image evidence for the visual audit is in
[tol200m-mammals-smoke.visual_audit.csv](tol200m-mammals-smoke.visual_audit.csv).

## Data audit — 17 checks, 0 failed, 3 warnings

| Area | What was checked | Result |
|---|---|---|
| Schema | All 510 table rows and `dataset.json` validated with `jsonschema` (draft 2020-12) against `schemas/dataset.schema.json`; no all-NULL columns among license/provenance fields | pass |
| Genomes | Files exist, sizes match; MD5 recomputed over 4.16 GB equals the record **and** a freshly downloaded NCBI `md5checksums.txt`; FASTA records and bases (`zcat`) equal `n_sequences` and the assembly report; assembly name/level/category/date/N50/GC equal the NCBI Datasets API; each accession is the current RefSeq reference for its taxid | pass |
| Images | 250/250 exist, bytes and sha256 match, PIL decodes with matching width/height/format; min side ≥ 224; each `image_id` matches exactly one catalog and one provenance row with the right species, `Citizen Science` type, allowed basis and identical license fields; ids, hashes, files, URLs and observation ids all unique; 50 per species; all licenses `cc-*` | pass |
| Selection | The seeded sample was re-implemented independently in DuckDB; the 250 ids are exactly ranks 1–50 per species | pass |
| Taxonomy | Names re-resolved against the live NCBI taxonomy API: taxids, rank, accepted names and full lineages identical; 5 distinct orders | pass |
| Pairs / manifest | One pair per image to the species' assembly; manifest counts equal recomputed counts; README numbers equal tables | pass |

Warnings (repo-level, not dataset content):

1. `schemas/dataset.schema.json` `$id` ends in `#v0.1.0`; the 2020-12 metaschema forbids a
   non-empty fragment in `$id`. Fix (drop the fragment in `schema.py`) in a follow-up commit.
2. `dataset.json → selection.config.root` and `assembly_summary_snapshot` are recorded as
   absolute machine paths; follow-up commit records them repo-relative as written in the config.
3. `GenomeRecord.genome_size` is NCBI's nuclear `total_sequence_length`, while the FASTA file
   (and `n_sequences`) also includes the RefSeq mitochondrion where one exists
   (*Odocoileus virginianus* +16,477 bp, *Dasypus novemcinctus* +17,056 bp). Both values are
   correct; the semantics will be documented on the field in the same follow-up.

## Visual audit — what the photos actually show

Method: within each species, `images.csv` sorted by `image_id`, every 4th image taken
(12 per species, 60 total) and viewed at full resolution; the remaining 190 were scanned on
contact sheets and 27 suspicious tiles opened individually. The 12-image samples are the
unbiased estimate.

| Species | Live animal, clear | Live, partial/distant | Tracks | Sign (chewed wood, burrows) | Remains | Unreadable | Wrong species? |
|---|---|---|---|---|---|---|---|
| *Vulpes vulpes* | 8 | 3 | 1 | 0 | 0 | 0 | 0 |
| *Odocoileus virginianus* | 7 | 3 | 2 | 0 | 0 | 0 | 0 |
| *Castor canadensis* | **0** | 2 | 0 | **8** | 1 | 0 | 1 (likely muskrat) |
| *Tachyglossus aculeatus* | 8 | 3 | 0 | 0 | 1 (carcass) | 0 | 0 |
| *Dasypus novemcinctus* | 7 | 4 | 0 | 0 | 0 | 1 | 0 |

Findings:

- **No systematic mislabels, placeholders, truncated files or captive/zoo scenes.** Live-animal
  rate is 92% / 83% / 92% / 92% for fox / deer / echidna / armadillo; the residue is tracks,
  prints, one skull, one tooth, one carcass and one unreadable night shot — ordinary
  iNaturalist label noise.
- **Beaver is the outlier: 17% live animals (none clearly), 75% beaver sign.** iNaturalist
  users document beavers mostly by chewed stumps and lodges. This is not a pipeline fault,
  but it means `image_type = citizen_science` must not be read as "shows the animal", and any
  training-scale build needs an image-content filter (e.g. zero-shot "live animal vs
  sign/tracks/remains" with a BioCLIP-class model) with the measured rate per species.
- About a fifth of the viewed `Citizen Science` photos are infrared trail-camera frames
  uploaded to iNaturalist (Reconyx/Browning overlays). Most show the animal; three are
  unusable. TreeOfLife-200M's `img_type` reflects the GBIF publisher, not image content.
- 62 of 250 `rights_holder` values are the literal string `not provided` from the provenance
  file; the license itself is always present.
- 207 of 250 images are exactly 1024 px on the long side (the `large` iNaturalist variant);
  the ALA-hosted ones are 650 px thumbnails.

## Consequences for the next build

1. Add a content filter step (or at least a per-image content label) before scaling to all
   RefSeq mammals; re-measure live-animal rate per species after filtering.
2. Consider excluding publishers/hosts that only provide small thumbnails (ALA 650 px) if
   resolution matters.
3. Keep `max_per_observation = 1`; the audit found no duplicated observations.
