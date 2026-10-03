"""Contract between image-metadata sources (TreeOfLife-200M, BioTrove, ...) and the
dataset builder.

A source knows how to turn a list of species names into *candidate* images: metadata
rows with a URL, provenance and license, but no bytes yet. The builder resolves species to
NCBI taxids, fetches genomes, downloads the candidates and writes the tables. Sources never
download images and never talk to NCBI.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from datasets.schema import ImageType, SourceInfo


class ImageCandidate(BaseModel):
    """One selectable image, as described by a source's metadata. Field names mirror
    ``ImageRecord`` so the builder can forward them unchanged."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    candidate_id: str = Field(description="unique within the source, e.g. the TreeOfLife uuid")
    species_query: str = Field(description="the species name the builder asked for")
    original_label: str = Field(description="scientific name as given by the source")
    source_dataset: str
    source_dataset_revision: str | None = None
    source_provider: str | None = None
    source_id: str | None = None
    observation_id: str | None = Field(
        default=None, description="groups images of the same occurrence/individual"
    )
    source_url: str = Field(
        description="URL to fetch bytes from (already rewritten to the wanted size)"
    )
    fallback_urls: list[str] = Field(
        default_factory=list, description="tried in order if source_url fails"
    )
    image_type: ImageType = ImageType.unknown
    source_image_type: str | None = None
    publisher: str | None = None
    license: str | None = None
    license_url: str | None = None
    rights_holder: str | None = None


class ImageSource(Protocol):
    """Implemented by ``datasets.sources.<name>``."""

    name: str

    def source_info(self) -> SourceInfo:
        """Provenance of the metadata this source reads (repo, revision, license notes)."""
        ...

    def select(
        self,
        species: Sequence[str],
        *,
        per_species: int,
        seed: int = 0,
    ) -> dict[str, list[ImageCandidate]]:
        """Deterministically pick up to ``per_species`` candidates per species name, keyed
        by the query name. A species with no candidates maps to an empty list. Filters
        (image types, licenses, record basis, one-image-per-observation) are source
        configuration, set in the source's constructor, so that ``selection`` in
        ``dataset.json`` can record them."""
        ...

    def selection_params(self) -> dict[str, object]:
        """JSON-serialisable description of the filters/caps in effect, for the manifest."""
        ...
