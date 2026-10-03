"""Record schema for genes.jpg genome <-> image datasets.

Every built dataset is a directory with a ``dataset.json`` manifest and four tables
(species, genomes, images, pairs). The pydantic models below are the single source of
truth; ``export_json_schema`` writes them to ``schemas/dataset.schema.json``.

Design notes
- Species identity is the NCBI Taxonomy ID. Names from image sources are kept verbatim
  in ``ImageRecord.original_label`` and ``SpeciesRecord.source_names`` for traceability,
  but joins happen on ``ncbi_taxid`` only.
- A genome is a *species-level reference* (one RefSeq assembly per species), not the
  genome of the photographed individual. ``PairRecord`` makes that join explicit.
- ``split`` lives on the species, not the image: the intended evaluation is
  species-held-out (a species, and therefore its reference genome, is never in both
  train and test).
- All file paths in records are POSIX paths relative to the dataset root so the dataset
  can be moved between machines.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "0.1.0"

Split = Literal["train", "val", "test"]


class AssemblyLevel(str, Enum):
    complete_genome = "Complete Genome"
    chromosome = "Chromosome"
    scaffold = "Scaffold"
    contig = "Contig"


class ImageType(str, Enum):
    """Coarse image context, harmonised across sources (TreeOfLife-200M ``img_type``,
    Pl@ntNet organ labels, ...). Keep the source's own label in ``ImageRecord.source_image_type``."""

    citizen_science = "citizen_science"
    camera_trap = "camera_trap"
    museum_specimen = "museum_specimen"
    lab_specimen = "lab_specimen"
    illustration = "illustration"
    unknown = "unknown"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class SpeciesRecord(_Record):
    """One row per species present in the dataset."""

    ncbi_taxid: int = Field(description="NCBI Taxonomy ID at species rank")
    scientific_name: str = Field(description="NCBI accepted scientific name (binomial)")
    common_name: str | None = None
    kingdom: str | None = None
    phylum: str | None = None
    class_: str | None = Field(default=None, alias="class", description="taxonomic class")
    order: str | None = None
    family: str | None = None
    genus: str | None = None
    lineage_taxids: list[int] = Field(
        default_factory=list, description="NCBI taxids from root to this species, inclusive"
    )
    source_names: list[str] = Field(
        default_factory=list,
        description="verbatim names under which this species appeared in image sources",
    )
    n_images: int = 0
    n_genomes: int = 0
    split: Split | None = Field(default=None, description="species-held-out split assignment")

    model_config = ConfigDict(extra="forbid", use_enum_values=True, populate_by_name=True)


class GenomeRecord(_Record):
    """One row per genome assembly file stored in the dataset."""

    assembly_accession: str = Field(
        description="NCBI assembly accession incl. version, e.g. GCF_000001405.40"
    )
    ncbi_taxid: int = Field(
        description="taxid of the sequenced organism (may be a strain/subspecies)"
    )
    species_taxid: int = Field(description="species-rank taxid; join key to SpeciesRecord")
    organism_name: str
    assembly_name: str
    assembly_level: AssemblyLevel
    refseq_category: str | None = Field(
        default=None, description="'reference genome', 'representative genome' or None"
    )
    source: Literal["ncbi_refseq", "ncbi_genbank"] = "ncbi_refseq"
    release_date: str | None = Field(default=None, description="YYYY-MM-DD")
    genome_size: int | None = Field(
        default=None,
        description="NCBI genome_size / total_sequence_length: total length (bp, incl. gaps) of "
        "the top-level sequences of the *primary* (nuclear) assembly. Non-nuclear sequences "
        "that NCBI ships in sequence_file (e.g. a RefSeq mitochondrion) are not counted here",
    )
    genome_size_ungapped: int | None = Field(
        default=None, description="NCBI total_ungapped_length of the primary assembly (bp)"
    )
    gc_percent: float | None = None
    scaffold_count: int | None = None
    contig_count: int | None = None
    scaffold_n50: int | None = None
    contig_n50: int | None = None
    ftp_path: str = Field(description="NCBI FTP/HTTPS directory of the assembly")
    sequence_file: str = Field(
        description="path relative to dataset root of the genomic FASTA (.fna.gz)"
    )
    sequence_md5: str = Field(
        description="MD5 of sequence_file as published in NCBI md5checksums.txt, verified locally"
    )
    sequence_bytes: int
    n_sequences: int | None = Field(
        default=None,
        description="number of FASTA records in sequence_file: chromosomes/scaffolds plus any "
        "non-nuclear sequence NCBI ships with the assembly, so it can exceed scaffold_count "
        "by the number of organelle records",
    )
    extra_files: list[str] = Field(
        default_factory=list,
        description="other files kept, relative to dataset root (assembly report, md5checksums, ...)",
    )
    downloaded_at: datetime


class ImageRecord(_Record):
    """One row per image file stored in the dataset."""

    image_id: str = Field(
        description="stable id: '<source_dataset>:<source_id>' or a UUID from the source"
    )
    ncbi_taxid: int = Field(description="species-rank taxid; join key to SpeciesRecord")
    source_dataset: str = Field(
        description="dataset the metadata came from, e.g. 'treeoflife-200m'"
    )
    source_dataset_revision: str | None = Field(
        default=None, description="git sha / snapshot date of the source dataset"
    )
    source_provider: str | None = Field(
        default=None, description="upstream provider, e.g. 'gbif', 'eol', 'inaturalist'"
    )
    source_id: str | None = Field(
        default=None, description="provider record id (gbifID, observation id, ...)"
    )
    observation_id: str | None = Field(
        default=None,
        description="groups images of the same individual/occurrence; use for split grouping",
    )
    source_url: str = Field(description="URL the image bytes were downloaded from")
    original_label: str = Field(description="scientific name as given by the source")
    image_type: ImageType = Field(
        default=ImageType.unknown,
        description="the source's record-level context label (see ImageType), not a content "
        "classification: a citizen_science record may be a camera-trap frame or show tracks, "
        "sign or remains rather than the animal",
    )
    source_image_type: str | None = Field(
        default=None, description="source's own image-type/organ label, verbatim"
    )
    publisher: str | None = None
    license: str | None = Field(
        default=None, description="license name as given by the source, e.g. 'CC BY 4.0'"
    )
    license_url: str | None = None
    rights_holder: str | None = Field(
        default=None,
        description="rights holder / copyright owner as given by the source; None when the "
        "source records none (source placeholders such as 'not provided' are mapped to None)",
    )
    file: str = Field(description="path relative to dataset root")
    sha256: str
    bytes: int
    width: int
    height: int
    format: str = Field(description="PIL format name, e.g. 'JPEG'")
    downloaded_at: datetime


class PairRecord(_Record):
    """Explicit image <-> genome pairing. Species-level supervision: every image of a
    species is paired with that species' reference assembly."""

    image_id: str
    assembly_accession: str
    ncbi_taxid: int
    pairing: Literal["species_reference", "same_specimen"] = Field(
        default="species_reference",
        description="'same_specimen' only when the sequence comes from the photographed individual (e.g. BIOSCAN)",
    )


class SourceInfo(_Record):
    name: str
    url: str
    revision: str | None = None
    license_notes: str | None = None
    accessed_at: datetime | None = None


class TableInfo(_Record):
    path: str
    n_rows: int


class DatasetManifest(_Record):
    """``dataset.json`` at the dataset root."""

    name: str
    version: str
    schema_version: str = SCHEMA_VERSION
    description: str = ""
    created_at: datetime
    created_by: str = "genes.jpg/src/datasets"
    sources: list[SourceInfo] = Field(default_factory=list)
    tables: dict[Literal["species", "genomes", "images", "pairs"], TableInfo]
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="summary counts: n_species, n_genomes, genome_bytes, plus n_images, n_pairs, "
        "image_bytes",
    )
    selection: dict[str, object] = Field(
        default_factory=dict,
        description="parameters used to select species/images (filters, caps, seeds)",
    )


RECORD_MODELS: dict[str, type[BaseModel]] = {
    "species": SpeciesRecord,
    "genomes": GenomeRecord,
    "images": ImageRecord,
    "pairs": PairRecord,
}


SCHEMA_ID = "https://github.com/genesjpgorg/genes.jpg/schemas/dataset.schema.json"
"""``$id`` of the exported schema. A plain URI without a fragment: draft 2020-12 forbids
non-empty fragments in ``$id`` (``jsonschema.Draft202012Validator.check_schema`` rejects
them); the schema version lives in the ``version`` key."""


def json_schema() -> dict:
    """Single JSON Schema document with every record type under ``$defs``."""
    defs: dict[str, dict] = {}
    for model in (
        SpeciesRecord,
        GenomeRecord,
        ImageRecord,
        PairRecord,
        DatasetManifest,
    ):
        schema = model.model_json_schema(ref_template="#/$defs/{model}", by_alias=True)
        defs.update(schema.pop("$defs", {}))
        defs[model.__name__] = schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "genes.jpg genome <-> image dataset",
        "version": SCHEMA_VERSION,
        "description": (
            "A dataset directory holds dataset.json (DatasetManifest) and the tables "
            "species/genomes/images/pairs; rows of each table validate against the "
            "corresponding record schema in $defs."
        ),
        "$defs": defs,
    }


def export_json_schema(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_schema(), indent=2) + "\n")
    return path


if __name__ == "__main__":
    out = export_json_schema(
        Path(__file__).resolve().parents[2] / "schemas" / "dataset.schema.json"
    )
    print(f"wrote {out}")
