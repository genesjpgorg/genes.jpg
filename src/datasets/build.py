"""Config-driven dataset builder: species names -> NCBI taxids -> RefSeq genomes + source
images -> validated dataset directory.

Pipeline (``build``)
1. species: ``ncbi.resolve_taxon`` per configured name. Species rank is required; a
   subspecies is lifted to its species (warning); an unknown name, or two names resolving
   to one taxid, abort.
2. genomes: ``ncbi.list_refseq_assemblies`` + ``ncbi.pick_best_assembly`` (live API), cross-
   checked against the ``assembly_summary`` snapshot named in the config (warning when the
   two disagree; the live choice wins), then ``ncbi.download_genome`` with
   ``GENOME_WORKERS`` threads (existing verified files are reused).
3. images: the configured ``ImageSource`` selects ``ceil(OVERSAMPLE * per_species)``
   deterministic candidates per species, queried under both the configured and the NCBI
   accepted name. The first ``per_species`` candidates are downloaded with
   ``images.download_images``; a failed candidate is retried on its ``fallback_urls`` and
   then replaced from the remaining candidates until ``per_species`` succeeded or the
   candidates are exhausted. Failures are appended to ``logs/image_failures.jsonl``.
4. tables (``manifest.write_table``), ``dataset.json`` (provenance of both sources and the
   full selection: config, source filters, assembly choices, warnings, download stats),
   ``README.md`` and ``logs/build_report.json`` are written, then the result is checked with
   ``manifest.validate_dataset(check_hashes=True)``; problems raise ``BuildError``.

Re-running on an existing root reuses genome and image files (``download_genome`` /
``download_images`` re-validate them without a request) and rewrites tables and manifest.
``images/`` is owned by the builder: an existing file is reused only when the URL recorded
for it in the previous ``images.parquet`` is still one the current candidate lists (so a
changed ``inat_size_variant`` refetches instead of relabelling old bytes), a file that no
longer meets ``min_side`` is dropped so the candidate's other URLs can be tried, and files
the finished image table does not reference are removed. Each of these is a warning in the
report, as is any change of the image settings since the previous build at the same root.

Sources are looked up in ``SOURCES`` by ``config.images.source``; a factory imports its module
lazily and constructs the source from the ``ImagesConfig`` keys the config actually sets
(unset keys keep the source's own defaults; a key the constructor cannot take is a config
error) plus ``aliases`` (configured name -> [configured name, NCBI accepted name]). A source
may instead take ``aliases`` as a ``select`` keyword; one that supports neither is queried
under both names and the candidates are merged.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import math
import re
import shlex
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from datasets import images, manifest, ncbi
from datasets.schema import (
    SCHEMA_VERSION,
    GenomeRecord,
    ImageRecord,
    PairRecord,
    SourceInfo,
    SpeciesRecord,
)
from datasets.sources.base import ImageCandidate, ImageSource

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERSAMPLE = 1.3
"""Candidates requested per species, as a multiple of ``per_species`` (dead-URL reserve)."""
GENOME_WORKERS = 3
IMAGE_WORKERS = 16
PER_HOST_RPS = 4.0
NCBI_REFSEQ_URL = "https://www.ncbi.nlm.nih.gov/refseq/"
README_FILE = "README.md"
REPORT_FILE = "logs/build_report.json"
FAILURES_FILE = "logs/image_failures.jsonl"

Licenses = Literal["cc_only", "permissive", "any"]
SizeVariant = Literal["large", "medium", "original"]

_FORWARDED_FIELDS = {
    "source_dataset",
    "source_dataset_revision",
    "source_provider",
    "source_id",
    "observation_id",
    "original_label",
    "image_type",
    "source_image_type",
    "publisher",
    "license",
    "license_url",
    "rights_holder",
}
"""``ImageCandidate`` fields copied into the ``ImageRecord`` (``image_id`` and ``source_url``
come from the download task, the file fields from the result)."""


class BuildError(RuntimeError):
    """The build cannot produce a valid dataset; ``report`` holds the progress so far."""

    def __init__(self, message: str, report: BuildReport | None = None):
        super().__init__(message)
        self.report = report


# --------------------------------------------------------------------------- config


class GenomesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["ncbi_refseq"] = "ncbi_refseq"
    assembly_summary_snapshot: Path | None = Field(
        default=None,
        description="NCBI assembly_summary*.txt the live API choice is cross-checked against; "
        "repo-relative unless absolute",
    )
    policy: str = "pick_best_assembly"
    files: list[str] = Field(
        default_factory=lambda: ["genomic.fna.gz", "assembly_report.txt", "md5checksums.txt"]
    )


class ImagesConfig(BaseModel):
    """Image-source section. ``source``, ``per_species``, ``seed`` and ``min_side`` are used by
    the builder; every other key the config *sets* (including extra, source-specific ones) is
    passed to the source constructor, see ``construct_source``. A key left out of the config
    keeps the source's own default (the defaults below only validate the literals and are
    never forwarded); ``dataset.json -> selection.image_source`` records the effective
    filters."""

    model_config = ConfigDict(extra="allow")

    source: str = Field(description="key in ``SOURCES``, e.g. 'treeoflife-200m'")
    source_revision: str | None = None
    per_species: int = Field(gt=0, description="images to download per species")
    seed: int = 0
    image_types: list[str | None] | None = Field(
        default=None, description="source image-type labels to keep; None keeps all"
    )
    exclude_basis_of_record: list[str] = Field(default_factory=list)
    licenses: Licenses = "cc_only"
    max_per_observation: int | None = Field(default=1, ge=1)
    inat_size_variant: SizeVariant = "large"
    min_side: int = Field(default=224, ge=0, description="smaller images are rejected (builder)")


class BuildConfig(BaseModel):
    """Mirror of ``configs/*.json``; ``load_config`` resolves the paths."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = ""
    root: Path = Field(description="dataset directory; repo-relative unless absolute")
    species: list[str] = Field(min_length=1, description="scientific names (NCBI resolvable)")
    genomes: GenomesConfig = Field(default_factory=GenomesConfig)
    images: ImagesConfig
    config_path: Path | None = Field(default=None, exclude=True)
    root_override: Path | None = Field(default=None, exclude=True)
    repo_root: Path | None = Field(
        default=None,
        exclude=True,
        description="base the relative paths of the file were resolved against (load_config); "
        "paths under it are recorded repo-relative in dataset.json and README",
    )

    @field_validator("species")
    @classmethod
    def _clean_species(cls, names: list[str]) -> list[str]:
        cleaned = [" ".join(n.split()) for n in names]
        if any(not n for n in cleaned):
            raise ValueError("species names must not be empty")
        if duplicates := [n for n, c in Counter(cleaned).items() if c > 1]:
            raise ValueError(f"duplicate species names: {duplicates}")
        return cleaned


def _absolute(path: Path, base: Path) -> Path:
    return path if path.is_absolute() else base / path


def load_config(
    path: str | Path, *, root_override: str | Path | None = None, repo_root: Path = REPO_ROOT
) -> BuildConfig:
    """Read a build config; relative ``root`` and snapshot paths are taken from ``repo_root``,
    a relative ``root_override`` (a CLI argument) from the current directory. Raises
    ``OSError``, ``TypeError`` or ``ValueError`` (``pydantic.ValidationError`` is one) on bad
    input."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path}: expected a JSON object")
    config = BuildConfig.model_validate(data)
    override = Path(root_override).absolute() if root_override is not None else None
    update: dict[str, object] = {
        "root": override if override is not None else _absolute(config.root, repo_root),
        "config_path": path.resolve(),
        "root_override": override,
        "repo_root": Path(repo_root),
    }
    if (snapshot := config.genomes.assembly_summary_snapshot) is not None:
        update["genomes"] = config.genomes.model_copy(
            update={"assembly_summary_snapshot": _absolute(snapshot, repo_root)}
        )
    return config.model_copy(update=update)


def _portable(path: Path, config: BuildConfig) -> str:
    """``path`` as written in the config: repo-relative POSIX when it lies under the repo root
    the config was loaded against (``REPO_ROOT`` otherwise), else unchanged. Keeps machine-
    specific prefixes such as ``/home/<user>/genes.jpg`` out of ``dataset.json`` and README."""
    base = config.repo_root or REPO_ROOT
    return path.relative_to(base).as_posix() if path.is_relative_to(base) else str(path)


def config_as_written(config: BuildConfig) -> dict[str, object]:
    """The config for ``dataset.json -> selection.config`` and the README: the keys the file
    sets (defaults absent) with ``root`` and ``genomes.assembly_summary_snapshot`` repo-relative
    again (``load_config`` made them absolute), so the record reads the same on every machine.
    ``root`` is where the dataset was built: the configured root, or the ``--root-override``
    that ``selection.command`` also shows."""
    data = config.model_dump(mode="json", exclude_unset=True)
    data["root"] = _portable(config.root, config)
    genomes = data.get("genomes")
    snapshot = config.genomes.assembly_summary_snapshot
    if isinstance(genomes, dict) and snapshot is not None:
        genomes["assembly_summary_snapshot"] = _portable(snapshot, config)
    return data


# --------------------------------------------------------------------------- sources

Aliases = Mapping[str, Sequence[str]]
"""configured species name -> names under which the source should also match it (the
configured name itself and the NCBI accepted name)."""

SourceFactory = Callable[..., ImageSource]
"""``factory(config: ImagesConfig, *, aliases: Aliases | None) -> ImageSource``."""

_BUILDER_KEYS = {"min_side", "source_revision"}
"""``ImagesConfig`` keys the builder consumes itself: offered to a source constructor that
names them, silently dropped otherwise."""


def construct_source(
    cls: Callable[..., ImageSource], config: ImagesConfig, **overrides: object
) -> ImageSource:
    """``cls(**options)`` with the ``ImagesConfig`` keys the config explicitly sets plus
    ``overrides``; unset keys keep the constructor's own defaults. ``source``, ``per_species``
    and ``seed`` are never passed and ``_BUILDER_KEYS`` only to a constructor that names them;
    any other key the constructor cannot take (no ``**kwargs``) is a config error ->
    ``BuildError``."""
    params = inspect.signature(cls).parameters
    options = {
        **config.model_dump(exclude={"source", "per_species", "seed"}, exclude_unset=True),
        **overrides,
    }
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        if unknown := sorted(set(options) - set(params) - _BUILDER_KEYS):
            raise BuildError(
                f"image source {cls.__name__} does not take the images config key(s) "
                f"{unknown}; it accepts {sorted(params)}"
            )
        options = {k: v for k, v in options.items() if k in params}
    return cls(**options)


def _treeoflife200m(config: ImagesConfig, *, aliases: Aliases | None = None) -> ImageSource:
    from datasets.sources.treeoflife200m import TreeOfLife200MSource  # lazy: duckdb, parquet

    if not config.source_revision:
        raise BuildError("images.source_revision (TreeOfLife-200M git sha) is required")
    return construct_source(
        TreeOfLife200MSource, config, revision=config.source_revision, aliases=dict(aliases or {})
    )


SOURCES: dict[str, SourceFactory] = {"treeoflife-200m": _treeoflife200m}
"""``config.images.source`` -> factory; tests register fakes here."""


def make_source(config: ImagesConfig, *, aliases: Aliases | None = None) -> ImageSource:
    """Instantiate the source registered in ``SOURCES`` under ``config.source``."""
    try:
        factory = SOURCES[config.source]
    except KeyError:
        raise BuildError(
            f"unknown image source {config.source!r}; known: {sorted(SOURCES)}"
        ) from None
    return factory(config, aliases=aliases)


def _has_aliases(source: object, aliases: Aliases) -> bool:
    """True when the source already knows every alias, i.e. it took ``aliases`` at
    construction and keeps them as an ``.aliases`` mapping (as ``TreeOfLife200MSource`` does)."""
    installed = getattr(source, "aliases", None)
    if not isinstance(installed, Mapping):
        return False
    return all(set(names) - {q} <= set(installed.get(q, ())) for q, names in aliases.items())


# --------------------------------------------------------------------------- report


class SpeciesReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(description="name as written in the config")
    ncbi_taxid: int
    scientific_name: str = Field(description="NCBI accepted name")
    common_name: str | None = None
    assembly_accession: str | None = None
    assembly_name: str | None = None
    assembly_level: str | None = None
    genome_size: int | None = None
    n_refseq_assemblies: int = 0
    snapshot_accession: str | None = Field(
        default=None, description="what pick_best_assembly chooses from the snapshot file"
    )
    n_candidates: int = 0
    n_downloaded: int = 0
    n_reused: int = Field(default=0, description="of n_downloaded: files already present")
    n_failed: int = Field(default=0, description="failed download attempts (URLs) in this run")
    warnings: list[str] = Field(default_factory=list)


class BuildReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    root: str
    command: str
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None = None
    species: list[SpeciesReport] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)
    timings: dict[str, float] = Field(default_factory=dict, description="seconds per step")
    warnings: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list, description="validate_dataset output")


def format_report(report: BuildReport) -> str:
    """Human-readable summary (what ``genes-datasets build`` prints)."""
    head = f"{report.name} v{report.version} -> {report.root}"
    lines = [head + (" (dry run: nothing downloaded or written)" if report.dry_run else "")]
    for s in report.species:
        asm = "no assembly"
        if s.assembly_accession:
            size = f"{s.genome_size:,} bp" if s.genome_size is not None else "size unknown"
            asm = f"{s.assembly_accession} {s.assembly_name} {s.assembly_level} {size}"
        common = f" ({s.common_name})" if s.common_name else ""
        imgs = f"candidates {s.n_candidates}"
        if not report.dry_run:
            imgs += f", images {s.n_downloaded} ({s.n_reused} reused, {s.n_failed} failed)"
        lines.append(
            f"  {s.query}: taxid {s.ncbi_taxid} {s.scientific_name}{common}; {asm}; {imgs}"
        )
    if report.totals:
        lines.append("totals: " + ", ".join(f"{k} {v:,}" for k, v in report.totals.items()))
    if report.timings:
        lines.append("timings: " + ", ".join(f"{k} {v:.1f}s" for k, v in report.timings.items()))
    if report.warnings:
        lines.append(f"warnings ({len(report.warnings)}):")
        lines += [f"  - {w}" for w in report.warnings]
    if report.problems:
        lines.append(f"validation problems ({len(report.problems)}):")
        lines += [f"  - {p}" for p in report.problems]
    return "\n".join(lines)


# --------------------------------------------------------------------------- species


def resolve_species(name: str) -> tuple[ncbi.TaxonInfo, list[str]]:
    """Species-rank ``TaxonInfo`` for a configured name plus warnings (homonym, synonym,
    subspecies lifted to its species). ``BuildError`` if NCBI does not know the name or it is
    not at or below species rank."""
    taxon = ncbi.resolve_taxon(name)
    if taxon is None:
        raise BuildError(f"species {name!r}: NCBI Taxonomy does not know this name")
    warnings: list[str] = []
    if taxon.n_matches > 1:
        warnings.append(
            f"{name!r} is a homonym ({taxon.n_matches} NCBI matches); using taxid "
            f"{taxon.taxid} {taxon.scientific_name!r}"
        )
    if taxon.rank != "species":
        species_taxid = taxon.species_taxid
        if species_taxid is None or species_taxid == taxon.taxid:
            raise BuildError(
                f"species {name!r}: resolves to taxid {taxon.taxid} ({taxon.scientific_name}) "
                f"of rank {taxon.rank or 'no rank'}, which is not a species"
            )
        species = ncbi.get_taxon(species_taxid)
        if species is None:
            raise BuildError(f"species {name!r}: cannot fetch species taxid {species_taxid}")
        warnings.append(
            f"{name!r} is a {taxon.rank or 'no rank'} (taxid {taxon.taxid}); using its species "
            f"{species.scientific_name!r} (taxid {species.taxid})"
        )
        taxon = species
    elif taxon.is_synonym:
        warnings.append(f"{name!r} is an NCBI synonym of {taxon.scientific_name!r}")
    return taxon, warnings


def species_record(taxon: ncbi.TaxonInfo, query: str) -> SpeciesRecord:
    def rank(name: str) -> str | None:
        node = taxon.ancestor(name)
        return node.name if node else None

    return SpeciesRecord(
        ncbi_taxid=taxon.taxid,
        scientific_name=taxon.scientific_name,
        common_name=taxon.common_name,
        kingdom=rank("kingdom"),
        phylum=rank("phylum"),
        class_=rank("class"),
        order=rank("order"),
        family=rank("family"),
        genus=rank("genus"),
        lineage_taxids=taxon.lineage_taxids,
        source_names=[query],
    )


# --------------------------------------------------------------------------- helpers


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _some(items: Sequence[str], n: int = 3) -> str:
    """``a, b, c, ... (12 total)`` for messages."""
    head = ", ".join(items[:n])
    return head if len(items) <= n else f"{head}, ... ({len(items)} total)"


def _safe_stem(candidate_id: str) -> str:
    """File stem for a candidate: no dots, so ``images.download_images`` never mistakes the
    id's tail for an image suffix and ``<stem>.<ext>`` is the only file it can produce."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", candidate_id) or "image"


def _accepts_kwarg(fn: Callable, name: str) -> bool:
    params = inspect.signature(fn).parameters
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _previous_urls(root: Path) -> dict[str, str]:
    """``image_id -> source_url`` of an earlier build at ``root`` (the URL each existing file
    was really fetched from); empty when there is no readable images table."""
    if not manifest.table_path(root, "images").is_file():
        return {}
    try:
        return {r.image_id: r.source_url for r in manifest.read_table(root, "images")}
    except Exception as exc:  # noqa: BLE001 - a broken old table must not block a rebuild
        log.warning("%s: ignoring unreadable previous images table (%s)", root, exc)
        return {}


def image_settings(
    config_images: Mapping[str, object], builder: Mapping[str, object], image_source: object
) -> dict[str, object]:
    """Everything that decides which bytes end up in ``images/`` (not how many): the images
    config section as written minus ``per_species``, the builder's ``seed`` and ``min_side``
    and the source's effective filters. Derived the same way from a live build and from an
    earlier ``dataset.json -> selection`` (``config.images``, ``builder``, ``image_source``) so
    the two can be diffed on rerun without a copy of their own in the manifest."""
    section = {k: v for k, v in config_images.items() if k != "per_species"}
    knobs = {k: builder[k] for k in ("seed", "min_side") if k in builder}
    return {"images": {**section, **knobs}, "image_source": image_source}


def _previous_image_settings(root: Path) -> dict[str, object] | None:
    """``image_settings`` of the build whose manifest is at ``root``; None without one."""
    if not (root / manifest.MANIFEST_FILE).is_file():
        return None
    try:
        selection = manifest.read_manifest(root).selection
    except Exception as exc:  # noqa: BLE001 - informational only
        log.warning("%s: ignoring unreadable previous manifest (%s)", root, exc)
        return None
    config, builder = selection.get("config"), selection.get("builder")
    if not isinstance(config, Mapping) or not isinstance(config.get("images"), Mapping):
        return None
    knobs = builder if isinstance(builder, Mapping) else {}
    return image_settings(config["images"], knobs, selection.get("image_source"))


def _flatten(value: object, prefix: str, out: dict[str, object]) -> dict[str, object]:
    if isinstance(value, Mapping):
        for k, v in value.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    else:
        out[prefix] = value
    return out


def _settings_diff(old: Mapping[str, object], new: Mapping[str, object]) -> list[str]:
    """``key: old -> new`` for every leaf that differs (both sides JSON-normalised)."""
    a = _flatten(json.loads(json.dumps(old, default=str)), "", {})
    b = _flatten(json.loads(json.dumps(new, default=str)), "", {})
    return [
        f"{k}: {a.get(k, '<unset>')!r} -> {b.get(k, '<unset>')!r}"
        for k in sorted(set(a) | set(b))
        if a.get(k, "<unset>") != b.get(k, "<unset>")
    ]


def _urls(candidate: ImageCandidate, preferred: str | None) -> list[str]:
    urls = _unique([candidate.source_url, *candidate.fallback_urls])
    if preferred in urls:
        urls.remove(preferred)
        urls.insert(0, preferred)
    return urls


def _command(config: BuildConfig) -> str:
    """The CLI invocation that reproduces this build (run from the repo root)."""
    path = _portable(config.config_path, config) if config.config_path else "<config>"
    parts = ["genes-datasets", "build", path]
    if config.root_override is not None:
        parts += ["--root-override", str(config.root_override)]
    return shlex.join(parts)


@dataclass
class _Species:
    query: str
    taxon: ncbi.TaxonInfo
    record: SpeciesRecord
    report: SpeciesReport
    assembly: ncbi.AssemblySummaryRow | None = None
    genome: GenomeRecord | None = None
    candidates: list[ImageCandidate] = field(default_factory=list)
    images: list[ImageRecord] = field(default_factory=list)
    failed: list[images.ImageResult] = field(default_factory=list)


class _Builder:
    def __init__(self, config: BuildConfig, *, progress: bool, dry_run: bool):
        self.config = config
        self.root = Path(config.root)
        self.progress = progress
        self.dry_run = dry_run
        self.started = datetime.now(UTC)
        self.report = BuildReport(
            name=config.name,
            version=config.version,
            root=str(self.root),
            command=_command(config),
            dry_run=dry_run,
            started_at=self.started,
        )
        self.work: list[_Species] = []
        self.n_removed = 0
        """files removed from the root this run (stale/rejected/unreferenced images, *.part)"""

    # -- bookkeeping

    def warn(self, message: str, species: _Species | None = None) -> None:
        if species is not None:
            species.report.warnings.append(message)
            message = f"{species.query}: {message}"
        self.report.warnings.append(message)
        log.warning("%s", message)

    def fail(self, message: str) -> BuildError:
        self.report.finished_at = datetime.now(UTC)
        return BuildError(message, self.report)

    @contextlib.contextmanager
    def source_errors(self, step: str):
        """Turn whatever an image source raises (missing parquet, duckdb, ...) into a
        ``BuildError`` carrying the report so far; ``BuildError`` itself passes through."""
        name = self.config.images.source
        try:
            yield
        except BuildError as exc:
            raise self.fail(str(exc)) from None
        except Exception as exc:
            log.debug("image source %r: %s failed", name, step, exc_info=True)
            raise self.fail(
                f"image source {name!r}: {step} failed: {type(exc).__name__}: {exc}"
            ) from exc

    # -- steps

    def resolve_species(self) -> None:
        by_taxid: dict[int, _Species] = {}
        for name in self.config.species:
            try:
                taxon, warnings = resolve_species(name)
            except BuildError as exc:
                raise self.fail(str(exc)) from None
            if (other := by_taxid.get(taxon.taxid)) is not None:
                raise self.fail(
                    f"species {name!r} and {other.query!r} both resolve to taxid {taxon.taxid} "
                    f"({taxon.scientific_name})"
                )
            record = species_record(taxon, name)
            report = SpeciesReport(
                query=name,
                ncbi_taxid=taxon.taxid,
                scientific_name=taxon.scientific_name,
                common_name=taxon.common_name,
            )
            species = by_taxid[taxon.taxid] = _Species(name, taxon, record, report)
            self.work.append(species)
            self.report.species.append(report)
            for w in warnings:
                self.warn(w, species)
            log.info("%s -> taxid %d %s", name, taxon.taxid, taxon.scientific_name)

    def _snapshot_choices(self) -> dict[int, ncbi.AssemblySummaryRow] | None:
        path = self.config.genomes.assembly_summary_snapshot
        if path is None:
            return None
        if not path.is_file():
            raise self.fail(f"assembly summary snapshot {path} does not exist")
        try:
            rows = ncbi.load_assembly_summary(path)
        except (OSError, ValueError) as exc:
            raise self.fail(f"assembly summary snapshot {path}: {exc}") from exc
        by_species: dict[int, list[ncbi.AssemblySummaryRow]] = defaultdict(list)
        for row in rows:
            by_species[row.species_taxid].append(row)
        return {t: ncbi.pick_best_assembly(rows) for t, rows in by_species.items()}

    def choose_assemblies(self) -> None:
        snapshot = self._snapshot_choices()
        missing: list[str] = []
        for sp in self.work:
            try:
                rows = ncbi.list_refseq_assemblies(sp.taxon.taxid)
            except ncbi.NCBIError as exc:
                raise self.fail(f"{sp.query}: listing RefSeq assemblies failed: {exc}") from exc
            sp.report.n_refseq_assemblies = len(rows)
            if not rows:
                missing.append(f"{sp.query} (taxid {sp.taxon.taxid})")
                continue
            row = sp.assembly = ncbi.pick_best_assembly(rows)
            sp.report.assembly_accession = row.assembly_accession
            sp.report.assembly_name = row.asm_name
            sp.report.assembly_level = row.assembly_level
            sp.report.genome_size = row.genome_size
            log.info(
                "%s: %d RefSeq assemblies, using %s (%s, %s)",
                sp.query,
                len(rows),
                row.assembly_accession,
                row.asm_name,
                row.assembly_level,
            )
            if snapshot is None:
                continue
            snap = snapshot.get(sp.taxon.taxid)
            if snap is None:
                self.warn("species is not in the assembly summary snapshot", sp)
            else:
                sp.report.snapshot_accession = snap.assembly_accession
                if snap.assembly_accession != row.assembly_accession:
                    self.warn(
                        f"snapshot would pick {snap.assembly_accession} ({snap.asm_name}), "
                        f"live API picks {row.assembly_accession} ({row.asm_name}); using the "
                        "live choice",
                        sp,
                    )
        if missing:
            raise self.fail("no current RefSeq assembly for: " + ", ".join(missing))

    def image_source(self, source: ImageSource | None) -> ImageSource:
        if source is not None:
            return source
        with self.source_errors("construction"):
            return make_source(self.config.images, aliases=self.aliases())

    def check_image_settings(self, source: ImageSource) -> None:
        """Warn when the settings that decide which bytes land in ``images/`` differ from
        those of the previous build at this root."""
        previous = _previous_image_settings(self.root)
        if previous is None:
            return
        if diff := _settings_diff(previous, self.image_settings(source)):
            self.warn(
                "image settings changed since the previous build at this root ("
                + "; ".join(diff)
                + "); existing files are reused only where their recorded URL is still one "
                "the current settings would use"
            )

    def prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if leftovers := sorted(self.root.glob("*.part")):
            for path in leftovers:
                path.unlink()
            self.n_removed += len(leftovers)
            self.warn(
                f"removed {len(leftovers)} leftover *.part file(s) of an interrupted earlier "
                f"write: {_some([p.name for p in leftovers])}"
            )

    def download_genomes(self) -> None:
        def fetch(sp: _Species) -> GenomeRecord:
            assert sp.assembly is not None
            return ncbi.download_genome(sp.assembly, self.root, progress=self.progress)

        with ThreadPoolExecutor(max_workers=max(1, min(GENOME_WORKERS, len(self.work)))) as pool:
            futures = [(sp, pool.submit(fetch, sp)) for sp in self.work]
            errors: list[str] = []
            for sp, future in futures:
                try:
                    sp.genome = future.result()
                except (ncbi.NCBIError, OSError, ValueError) as exc:
                    errors.append(f"{sp.query}: {exc}")
                else:
                    log.info(
                        "%s: genome %s ready (%s bytes, %s sequences)",
                        sp.query,
                        sp.genome.assembly_accession,
                        f"{sp.genome.sequence_bytes:,}",
                        sp.genome.n_sequences,
                    )
        if errors:
            raise self.fail("genome download failed: " + "; ".join(errors))

    def aliases(self) -> dict[str, list[str]]:
        return {sp.query: _unique([sp.query, sp.record.scientific_name]) for sp in self.work}

    def select_candidates(self, source: ImageSource) -> None:
        cfg = self.config.images
        request = math.ceil(OVERSAMPLE * cfg.per_species)
        aliases = self.aliases()
        queries = list(aliases)

        def query(names: Sequence[str], **kw: object) -> dict[str, list[ImageCandidate]]:
            with self.source_errors("select"):
                return source.select(names, per_species=request, seed=cfg.seed, **kw)

        if _accepts_kwarg(source.select, "aliases"):
            selected = query(queries, aliases=aliases)
        elif _has_aliases(source, aliases):  # installed by make_source
            selected = query(queries)
        else:
            names = _unique(n for q in queries for n in aliases[q])
            if len(names) > len(queries):
                self.warn(
                    f"image source {source.name!r} takes no aliases; querying configured and "
                    "NCBI names separately and merging"
                )
            raw = query(names)
            selected = {}
            for q in queries:
                merged: dict[str, ImageCandidate] = {}
                for name in aliases[q]:
                    for c in raw.get(name, []):
                        merged.setdefault(c.candidate_id, c)
                selected[q] = list(merged.values())[:request]

        seen_ids: set[str] = set()
        for sp in self.work:
            stems: set[str] = set()
            for c in selected.get(sp.query, []):
                stem = _safe_stem(c.candidate_id)
                if c.candidate_id in seen_ids:
                    self.warn(f"candidate {c.candidate_id} already used by another species", sp)
                elif stem in stems:
                    self.warn(f"candidate {c.candidate_id} collides with file stem {stem}", sp)
                else:
                    seen_ids.add(c.candidate_id)
                    stems.add(stem)
                    sp.candidates.append(c)
            sp.report.n_candidates = len(sp.candidates)
            if len(sp.candidates) < cfg.per_species:
                self.warn(f"only {len(sp.candidates)} candidates for {cfg.per_species} images", sp)
            log.info("%s: %d image candidates", sp.query, len(sp.candidates))

    def download_images(self) -> None:
        cfg = self.config.images
        previous = _previous_urls(self.root)
        for sp in self.work:
            got = _download_candidates(
                self.root,
                sp.taxon.taxid,
                sp.candidates,
                cfg.per_species,
                min_side=cfg.min_side,
                progress=self.progress,
                previous=previous,
            )
            sp.failed = got.failed
            for candidate, result in got.ok:
                fields = candidate.model_dump(include=_FORWARDED_FIELDS)
                fields["source_dataset_revision"] = (
                    candidate.source_dataset_revision or cfg.source_revision
                )
                sp.images.append(
                    images.to_image_record(result, self.root, ncbi_taxid=sp.taxon.taxid, **fields)
                )
            sp.report.n_downloaded = len(got.ok)
            sp.report.n_reused = sum(r.reused for _, r in got.ok)
            sp.report.n_failed = len(got.failed)
            self.n_removed += len(got.stale) + len(got.rejected)
            if got.stale:
                self.warn(
                    f"{len(got.stale)} existing files were fetched from URLs the current image "
                    f"settings no longer use; removed and fetched again: {_some(got.stale)}",
                    sp,
                )
            if got.rejected:
                self.warn(
                    f"{len(got.rejected)} existing files fail the current min_side="
                    f"{cfg.min_side}; removed, other URLs tried: {_some(got.rejected)}",
                    sp,
                )
            if got.unverified:
                self.warn(
                    f"{len(got.unverified)} existing files have no recorded URL (no previous "
                    f"images table); reused with their primary URL: {_some(got.unverified)}",
                    sp,
                )
            if len(got.ok) < cfg.per_species:
                self.warn(
                    f"{len(got.ok)} of {cfg.per_species} images downloaded "
                    f"({len(got.failed)} failed attempts, candidates exhausted)",
                    sp,
                )
            log.info(
                "%s: %d images (%d reused, %d failed attempts)",
                sp.query,
                len(got.ok),
                sp.report.n_reused,
                len(got.failed),
            )

    def remove_unreferenced_images(self) -> None:
        """Delete files under ``images/`` that no record of this build points at (leftovers
        of earlier runs with other candidates, interrupted writes) and empty folders."""
        folder = self.root / "images"
        if not folder.is_dir():
            return
        referenced = {(self.root / i.file).resolve() for sp in self.work for i in sp.images}
        removed = []
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            if path.resolve() not in referenced:
                path.unlink()
                removed.append(path.relative_to(self.root).as_posix())
        for sub in sorted((p for p in folder.rglob("*") if p.is_dir()), reverse=True):
            with contextlib.suppress(OSError):  # only empty folders go
                sub.rmdir()
        if removed:
            self.n_removed += len(removed)
            self.warn(
                f"removed {len(removed)} file(s) under images/ not referenced by this build's "
                f"image table (left by earlier runs): {_some(removed)}"
            )

    def finish_records(self) -> tuple[list[SpeciesRecord], list[GenomeRecord], list, list]:
        species, genomes, image_records, pairs = [], [], [], []
        for sp in self.work:
            assert sp.genome is not None
            sp.record.n_images = len(sp.images)
            sp.record.n_genomes = 1
            sp.record.source_names = _unique(
                [sp.query, *sorted({i.original_label for i in sp.images})]
            )
            species.append(sp.record)
            genomes.append(sp.genome)
            image_records += sp.images
            pairs += [
                PairRecord(
                    image_id=i.image_id,
                    assembly_accession=sp.genome.assembly_accession,
                    ncbi_taxid=sp.taxon.taxid,
                    pairing="species_reference",
                )
                for i in sp.images
            ]
        return species, genomes, image_records, pairs

    def totals(self) -> dict[str, int]:
        return {
            "n_species": len(self.work),
            "n_genomes": sum(sp.genome is not None for sp in self.work),
            "n_images": sum(len(sp.images) for sp in self.work),
            "n_candidates": sum(len(sp.candidates) for sp in self.work),
            "n_reused": sum(sp.report.n_reused for sp in self.work),
            "n_failed": sum(len(sp.failed) for sp in self.work),
            "n_removed": self.n_removed,
            "genome_bytes": sum(sp.genome.sequence_bytes for sp in self.work if sp.genome),
            "image_bytes": sum(i.bytes for sp in self.work for i in sp.images),
        }

    def ncbi_source_info(self) -> SourceInfo:
        now = datetime.now(UTC)
        parts = []
        if (snapshot := self.config.genomes.assembly_summary_snapshot) is not None:
            mtime = datetime.fromtimestamp(snapshot.stat().st_mtime, UTC)
            parts.append(f"assembly_summary snapshot {snapshot.name} ({mtime.date().isoformat()})")
        parts.append(f"Datasets API v2 live {now.date().isoformat()}")
        return SourceInfo(
            name="NCBI RefSeq",
            url=NCBI_REFSEQ_URL,
            revision="; ".join(parts),
            license_notes="NCBI data: public domain / no restrictions",
            accessed_at=now,
        )

    def builder_params(self) -> dict[str, object]:
        cfg = self.config.images
        return {
            "per_species": cfg.per_species,
            "seed": cfg.seed,
            "min_side": cfg.min_side,
            "oversample": OVERSAMPLE,
            "candidates_per_species": math.ceil(OVERSAMPLE * cfg.per_species),
            "image_workers": IMAGE_WORKERS,
            "per_host_rps": PER_HOST_RPS,
        }

    def image_settings(self, source: ImageSource) -> dict[str, object]:
        """This build's ``image_settings`` (module-level function), compared on rerun."""
        section = config_as_written(self.config)["images"]
        assert isinstance(section, dict)
        return image_settings(section, self.builder_params(), source.selection_params())

    def selection(self, source: ImageSource) -> dict[str, object]:
        """``dataset.json -> selection``: ``config`` is the file as written (repo-relative
        paths, unset keys absent); ``image_source`` and ``builder`` are the effective
        parameters (with ``config.images`` they define the ``image_settings`` diffed on rerun)."""
        config_path = self.config.config_path
        return {
            "config": config_as_written(self.config),
            "config_path": _portable(config_path, self.config) if config_path else None,
            "command": self.report.command,
            "image_source": source.selection_params(),
            "builder": self.builder_params(),
            "assemblies": {
                sp.record.scientific_name: {
                    "ncbi_taxid": sp.taxon.taxid,
                    "assembly_accession": sp.assembly.assembly_accession,
                    "assembly_name": sp.assembly.asm_name,
                    "assembly_level": sp.assembly.assembly_level,
                    "refseq_category": sp.assembly.refseq_category,
                    "release_date": sp.assembly.seq_rel_date,
                    "genome_size": sp.assembly.genome_size,
                    "ftp_path": sp.assembly.ftp_path,
                    "n_refseq_assemblies": sp.report.n_refseq_assemblies,
                    "snapshot_accession": sp.report.snapshot_accession,
                }
                for sp in self.work
                if sp.assembly is not None
            },
            "warnings": list(self.report.warnings),
            "downloads": {
                sp.record.scientific_name: sp.report.model_dump(
                    include={"n_candidates", "n_downloaded", "n_reused", "n_failed"}
                )
                for sp in self.work
            },
        }


# --------------------------------------------------------------------------- images


@dataclass
class _Downloaded:
    """Outcome of ``_download_candidates`` for one species."""

    ok: list[tuple[ImageCandidate, images.ImageResult]] = field(default_factory=list)
    failed: list[images.ImageResult] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    """candidate ids whose existing file came from a URL the candidate no longer lists
    (removed, fetched again)"""
    rejected: list[str] = field(default_factory=list)
    """candidate ids whose existing file fails the current policy (removed, other URLs tried)"""
    unverified: list[str] = field(default_factory=list)
    """candidate ids whose existing file has no recorded URL (reused, primary URL recorded)"""


def _download_candidates(
    root: Path,
    taxid: int,
    candidates: Sequence[ImageCandidate],
    n_want: int,
    *,
    min_side: int,
    progress: bool,
    previous: Mapping[str, str] = {},
) -> _Downloaded:
    """Download ``candidates`` in order until ``n_want`` succeeded or none are left. A failed
    candidate is retried on its next URL (``fallback_urls``) before further candidates are
    started. ``previous`` maps candidate ids to the URL an earlier run fetched them from:
    that URL goes first, and a file whose previous URL the candidate no longer lists is
    stale and removed. Candidates whose (non-stale) file is present are taken first, so a
    rerun reuses them without touching the network; an existing file that
    ``download_images`` rejects under the current policy is removed so the remaining URLs
    get their turn. ``ok`` is in candidate order."""
    folder = root / "images" / str(taxid)
    out = _Downloaded()

    def existing(c: ImageCandidate) -> Path | None:
        stem = _safe_stem(c.candidate_id)
        paths = (folder / f"{stem}.{ext}" for ext in images.IMAGE_EXTENSIONS.values())
        return next((p for p in paths if p.is_file()), None)

    queue: list[tuple[ImageCandidate, list[str], bool]] = []
    for c in candidates:
        fetched = previous.get(c.candidate_id)
        urls = _urls(c, fetched)
        path = existing(c)
        if path is not None and fetched is None:
            out.unverified.append(c.candidate_id)
        elif path is not None and fetched not in urls:
            path.unlink()
            out.stale.append(c.candidate_id)
            path = None
        queue.append((c, urls, path is not None))
    queue.sort(key=lambda item: not item[2])  # stable: present first
    pending: deque[tuple[ImageCandidate, list[str]]] = deque((c, urls) for c, urls, _ in queue)

    while len(out.ok) < n_want and pending:
        batch = [pending.popleft() for _ in range(min(n_want - len(out.ok), len(pending)))]
        tasks = [
            images.ImageTask(
                image_id=c.candidate_id,
                url=urls[0],
                dest=folder / _safe_stem(c.candidate_id),
                meta={**c.model_dump(), "ncbi_taxid": taxid},
            )
            for c, urls in batch
        ]
        results = images.download_images(
            tasks,
            max_workers=IMAGE_WORKERS,
            per_host_rps=PER_HOST_RPS,
            min_side=min_side,
            progress=progress,
        )
        retry: list[tuple[ImageCandidate, list[str]]] = []
        for (c, urls), result in zip(batch, results, strict=True):
            if result.ok:
                out.ok.append((c, result))
                continue
            out.failed.append(result)
            if (path := existing(c)) is not None:
                # download_images leaves no file behind a failed fetch, so this is an intact
                # earlier file it kept because it fails the current min_side/format policy;
                # fetching urls[0] again would only reproduce it
                path.unlink()
                out.rejected.append(c.candidate_id)
            if len(urls) > 1:
                retry.append((c, urls[1:]))
        pending.extendleft(reversed(retry))  # fallbacks first, in batch order
    order = {c.candidate_id: i for i, c in enumerate(candidates)}
    out.ok.sort(key=lambda pair: order[pair[0].candidate_id])
    return out


# --------------------------------------------------------------------------- README


def _readme(
    config: BuildConfig,
    report: BuildReport,
    species: Sequence[SpeciesRecord],
    genomes: Sequence[GenomeRecord],
    image_records: Sequence[ImageRecord],
    sources: Sequence[SourceInfo],
) -> str:
    genome_by_taxid = {g.species_taxid: g for g in genomes}
    lines = [
        f"# {config.name} v{config.version}",
        "",
        config.description,
        "",
        (
            f"Built {report.started_at.date().isoformat()} by genes.jpg `datasets.build` "
            f"(schema {SCHEMA_VERSION}); {len(species)} species, {len(genomes)} genomes, "
            f"{len(image_records)} images. See `dataset.json` for provenance and selection "
            "parameters and `logs/build_report.json` for the build report."
        ),
        "",
        "## Species",
        "",
        "| taxid | species | common name | assembly | level | genome size (bp) | images |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in species:
        g = genome_by_taxid.get(s.ncbi_taxid)
        size = f"{g.genome_size:,}" if g and g.genome_size is not None else ""
        lines.append(
            f"| {s.ncbi_taxid} | {s.scientific_name} | {s.common_name or ''} | "
            f"{g.assembly_accession + ' ' + g.assembly_name if g else ''} | "
            f"{g.assembly_level if g else ''} | {size} | {s.n_images} |"
        )
    licenses = Counter(i.license or "(none recorded)" for i in image_records)
    lines += ["", "## Image licences", "", "| licence | images |", "|---|---|"]
    lines += [f"| {name} | {n} |" for name, n in licenses.most_common()]
    lines += [
        "",
        (
            "Every image row carries its own `license`, `license_url`, `rights_holder` and "
            "`publisher`; the compilation licence of a source does not cover individual images."
        ),
        "",
        "## Sources",
        "",
    ]
    for src in sources:
        rev = f" @ {src.revision}" if src.revision else ""
        notes = f" - {src.license_notes}" if src.license_notes else ""
        lines.append(f"- {src.name}{rev} <{src.url}>{notes}")
    n_no_holder = sum(i.rights_holder is None for i in image_records)
    extra_records = [
        f"{g.assembly_accession} ({g.n_sequences} records, {g.scaffold_count} scaffolds)"
        for g in genomes
        if g.n_sequences is not None
        and g.scaffold_count is not None
        and g.n_sequences > g.scaffold_count
    ]
    lines += [
        "",
        "## Caveats",
        "",
        (
            "- `image_type` / `source_image_type` are the source's labels for the *record* (how "
            "the observation was made), not a description of the picture, and the builder "
            "applies no content filter: citizen-science uploads include camera-trap frames, "
            "tracks, scat, remains and habitat or sign-only shots (e.g. a gnawed stump for a "
            "beaver), so a species-dependent share of images does not show the animal. "
            "Spot-check or add a content filter before treating an image as a photo of the "
            "organism."
        ),
        (
            f"- `rights_holder` is empty for {n_no_holder} of {len(image_records)} images: the "
            "source recorded no rights holder (source placeholders such as 'not provided' are "
            "stored as null). Attribute those by `publisher` and `source_url`."
        ),
        (
            "- `genome_size` is NCBI's primary-assembly length (nuclear top-level sequences, "
            "incl. gaps). The FASTA in `sequence_file` may additionally carry non-nuclear "
            "sequences (e.g. a RefSeq mitochondrion), which `n_sequences` counts and "
            "`genome_size` does not"
            + (
                "; here `n_sequences` exceeds `scaffold_count` for " + ", ".join(extra_records)
                if extra_records
                else ""
            )
            + "."
        ),
    ]
    config_label = _portable(config.config_path, config) if config.config_path else "<config>"
    lines += [
        "",
        "## How it was built",
        "",
        "```",
        report.command,
        "```",
        "",
        f"Config (`{config_label}`, as written; relative paths are repo-relative):",
        "",
        "```json",
        json.dumps(config_as_written(config), indent=2),
        "```",
        "",
        (
            "Species are resolved to NCBI Taxonomy IDs; one RefSeq assembly per species "
            "(reference > representative; highest assembly level; newest) is downloaded from "
            "NCBI and MD5-verified; images are a seeded, deterministic sample of the source's "
            "candidates (effective filters in `dataset.json` -> `selection.image_source`), "
            f"oversampled by {OVERSAMPLE:g}x so dead URLs can be replaced. Failed downloads "
            f"are appended to `{FAILURES_FILE}`. Re-running the command at this root reuses "
            "verified files, rewrites the tables and removes files under `images/` the "
            "tables no longer reference."
        ),
    ]
    if report.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in report.warnings]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- build


def build(
    config: BuildConfig,
    *,
    progress: bool = True,
    dry_run: bool = False,
    source: ImageSource | None = None,
) -> BuildReport:
    """Build the dataset described by ``config`` (see the module docstring for the steps).

    ``dry_run`` resolves species, chooses assemblies and selects candidates (network and
    source reads, no downloads, nothing written or removed) and returns the plan. ``source``
    overrides the ``SOURCES`` lookup. Raises ``BuildError`` (with ``.report``) when a species
    does not resolve, has no RefSeq assembly, the image source cannot be built or queried, a
    genome download fails or the finished dataset does not validate; any dataset problems
    are also listed in ``report.problems``.
    """
    b = _Builder(config, progress=progress, dry_run=dry_run)
    timings = b.report.timings
    t0 = time.perf_counter()

    def lap(step: str, since: float) -> float:
        now = time.perf_counter()
        timings[step] = round(now - since, 3)
        return now

    log.info("building %s v%s at %s", config.name, config.version, b.root)
    t = time.perf_counter()
    b.resolve_species()
    t = lap("species", t)
    b.choose_assemblies()
    t = lap("assemblies", t)
    source = b.image_source(source)
    b.select_candidates(source)
    b.check_image_settings(source)
    t = lap("select", t)
    if dry_run:
        b.report.totals = {
            "n_species": len(b.work),
            "n_candidates": sum(len(sp.candidates) for sp in b.work),
        }
        lap("total", t0)
        b.report.finished_at = datetime.now(UTC)
        return b.report

    b.prepare_root()
    b.download_genomes()
    t = lap("genomes", t)
    b.download_images()
    b.remove_unreferenced_images()
    t = lap("images", t)

    species, genomes, image_records, pairs = b.finish_records()
    for name, records in (
        ("species", species),
        ("genomes", genomes),
        ("images", image_records),
        ("pairs", pairs),
    ):
        manifest.write_table(b.root, name, records)
    images.write_failures(
        (r for sp in b.work for r in sp.failed), b.root / FAILURES_FILE, append=True
    )
    sources = [source.source_info(), b.ncbi_source_info()]
    m = manifest.build_manifest(
        b.root,
        name=config.name,
        version=config.version,
        description=config.description,
        sources=sources,
        selection=b.selection(source),
    )
    manifest.write_manifest(b.root, m)
    b.report.totals = b.totals()
    (b.root / README_FILE).write_text(
        _readme(config, b.report, species, genomes, image_records, sources), encoding="utf-8"
    )
    t = lap("write", t)

    problems = manifest.validate_dataset(b.root, check_hashes=True)
    b.report.problems = problems
    lap("validate", t)
    lap("total", t0)
    b.report.finished_at = datetime.now(UTC)
    report_path = b.root / REPORT_FILE
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(b.report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if problems:
        raise BuildError(
            f"{b.root}: built dataset has {len(problems)} problem(s): " + "; ".join(problems[:5]),
            b.report,
        )
    log.info(
        "done: %s",
        ", ".join(f"{k} {v:,}" for k, v in b.report.totals.items() if k.startswith("n_")),
    )
    return b.report


__all__ = [
    "SOURCES",
    "BuildConfig",
    "BuildError",
    "BuildReport",
    "GenomesConfig",
    "ImagesConfig",
    "SpeciesReport",
    "build",
    "config_as_written",
    "construct_source",
    "format_report",
    "image_settings",
    "load_config",
    "make_source",
    "resolve_species",
    "species_record",
]
