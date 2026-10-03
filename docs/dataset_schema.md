# Dataset schema (v0.1.0)

Machine-readable definition: [`schemas/dataset.schema.json`](../schemas/dataset.schema.json),
generated from the pydantic models in [`src/datasets/schema.py`](../src/datasets/schema.py)
(`genes-datasets schema-export`). This page explains the design.

## Directory layout

Built datasets live under `data/datasets/<name>/`. `data/datasets` is a symlink to the shared
disk (`/mnt/filesystem-s8/genes.jpg/datasets` on the current machine) and is git-ignored.

```
<name>/
  dataset.json        DatasetManifest — provenance, schema version, table paths, counts
  species.parquet     SpeciesRecord rows   (+ species.csv for humans)
  genomes.parquet     GenomeRecord rows    (+ genomes.csv)
  images.parquet      ImageRecord rows     (+ images.csv)
  pairs.parquet       PairRecord rows      (+ pairs.csv)
  genomes/<accession>/<accession>_<asm_name>_genomic.fna.gz   + assembly_report.txt, md5checksums.txt
  images/<taxid>/<image_id>.<ext>
  logs/               download failures (JSONL), selection logs
```

Raw upstream metadata (e.g. the TreeOfLife-200M catalog) is kept outside dataset roots under
`data/datasets/<source>/raw/` so several built datasets can share it.

## Tables

| Table | One row per | Primary key | Foreign keys |
|---|---|---|---|
| `species` | species | `ncbi_taxid` | — |
| `genomes` | genome assembly file | `assembly_accession` | `species_taxid → species.ncbi_taxid` |
| `images` | image file | `image_id` | `ncbi_taxid → species.ncbi_taxid` |
| `pairs` | image ↔ assembly link | (`image_id`, `assembly_accession`) | both ends |

### Key decisions

- **Identity is the NCBI Taxonomy ID.** Names from image sources are kept verbatim
  (`ImageRecord.original_label`, `SpeciesRecord.source_names`) but never used as a join key.
- **Genomes are species-level references**, not the genome of the photographed individual.
  `PairRecord.pairing` is `species_reference` for this; `same_specimen` is reserved for sources
  such as BIOSCAN where the sequence comes from the pictured specimen.
- **Splits are assigned per species** (`SpeciesRecord.split`), because the scientific claim is
  generalisation to unseen genomes. `ImageRecord.observation_id` additionally groups photos of the
  same individual/occurrence so no finer split leaks them.
- **Image context is explicit.** `ImageRecord.image_type` harmonises source labels into
  `citizen_science | camera_trap | museum_specimen | lab_specimen | illustration | unknown`;
  the source's own label stays in `source_image_type`. Museum skins and skulls should not be
  silently mixed with live-animal photos. The label describes the *record* (how the
  observation was made), not the picture: a `citizen_science` record can be a camera-trap frame
  uploaded by a volunteer, or show tracks, scat, remains or other sign instead of the animal.
  No content filter is applied at build time.
- **Per-record licensing.** Each image carries `license`, `license_url`, `rights_holder`,
  `publisher`; each dataset carries `sources[].license_notes`. Compilation licences do not
  cover individual images. `rights_holder` is `null` when the source recorded none; source
  placeholders (TreeOfLife-200M writes the literal `not provided`) are mapped to `null` rather
  than stored as if they were a name.
- **Genome size vs. FASTA content.** `GenomeRecord.genome_size` / `genome_size_ungapped` are
  NCBI's figures for the *primary* (nuclear) assembly. The FASTA in `sequence_file` is NCBI's
  `*_genomic.fna.gz`, which may also contain non-nuclear sequences (a RefSeq mitochondrion,
  e.g. `NC_015247.1` in GCF_023699985.2); `n_sequences` counts every FASTA record, so it can
  exceed `scaffold_count` by the number of organelle records and the FASTA can be a few tens of
  kb longer than `genome_size`.
- **Integrity.** Genomes store the NCBI-published MD5 (verified on download); images store
  SHA-256, byte size, width/height/format. `genes-datasets validate ROOT` checks row validity,
  referential integrity, file presence and sizes, and manifest counts.
- **Portability.** All paths are POSIX, relative to the dataset root. `dataset.json ->
  selection.config` repeats the build config with repo-relative paths (never the absolute
  path of the checkout).

## Versioning

`SCHEMA_VERSION` follows semver. Adding optional fields is a minor bump; renaming/removing
fields or changing semantics is a major bump. Every `dataset.json` records the schema version
it was written with. The exported file's `$id` is a plain URI (no `#v...` fragment, which
draft 2020-12 forbids and strict validators reject); the version is in its `version` key.
