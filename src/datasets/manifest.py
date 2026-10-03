"""Tables, manifest and validation of a built dataset directory.

Serialisation
- A table is ``<root>/<name>.parquet`` (canonical, zstd) plus ``<root>/<name>.csv`` for human
  inspection; ``read_table`` only ever reads the parquet file.
- Parquet columns are derived from the pydantic models (``arrow_schema``): column names are
  the field aliases (so the species table has a ``class`` column), enums and ``Literal``
  fields are strings, ``list[...]`` fields are arrow lists and ``datetime`` fields are
  ``timestamp[us, tz=UTC]`` columns (naive datetimes are taken to be UTC). All other values
  are the ``model_dump(mode="json")`` representation.
- In the CSV sibling lists are joined with ``|``, ``None`` is an empty cell and datetimes
  are ISO-8601 strings.
- ``dataset.json`` is the ``DatasetManifest`` as indented JSON with ISO-8601 datetimes.
- Text files are UTF-8. Every file is written to a uniquely named ``*.part`` sibling and
  renamed into place, so readers never see a partial file; a leftover ``*.part`` means an
  interrupted write and is reported by ``validate_dataset``.
"""

from __future__ import annotations

import contextlib
import csv
import functools
import hashlib
import os
import types
import typing
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python

from datasets.schema import (
    RECORD_MODELS,
    SCHEMA_VERSION,
    DatasetManifest,
    GenomeRecord,
    ImageRecord,
    PairRecord,
    SourceInfo,
    SpeciesRecord,
    TableInfo,
)

TableName = Literal["species", "genomes", "images", "pairs"]
TABLE_NAMES: tuple[TableName, ...] = ("species", "genomes", "images", "pairs")
MANIFEST_FILE = "dataset.json"
COUNT_KEYS = ("n_species", "n_genomes", "n_images", "n_pairs", "genome_bytes", "image_bytes")

_MAX_REPORTED = 50  # per check; keeps `validate` output readable on badly broken datasets


# --------------------------------------------------------------------------- arrow schema


def _arrow_type(annotation: object) -> pa.DataType:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        inner = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(inner) != 1:
            raise TypeError(f"unsupported union annotation {annotation!r}")
        return _arrow_type(inner[0])
    if origin is Literal:
        if not all(isinstance(a, str) for a in typing.get_args(annotation)):
            raise TypeError(f"only string Literals are supported, got {annotation!r}")
        return pa.string()
    if origin is list:
        return pa.list_(_arrow_type(typing.get_args(annotation)[0]))
    if isinstance(annotation, type):
        if issubclass(annotation, bool):
            return pa.bool_()
        if issubclass(annotation, int):
            return pa.int64()
        if issubclass(annotation, float):
            return pa.float64()
        if issubclass(annotation, str):  # includes str enums, stored by value
            return pa.string()
        if issubclass(annotation, datetime):
            return pa.timestamp("us", tz="UTC")
    raise TypeError(f"no arrow type for annotation {annotation!r}")


def _is_optional(annotation: object) -> bool:
    return typing.get_origin(annotation) in (typing.Union, types.UnionType) and type(
        None
    ) in typing.get_args(annotation)


@functools.cache
def arrow_schema(model: type[BaseModel]) -> pa.Schema:
    """pyarrow schema of a record model: aliases as column names, nullable iff ``X | None``."""
    return pa.schema(
        pa.field(
            info.alias or name, _arrow_type(info.annotation), nullable=_is_optional(info.annotation)
        )
        for name, info in model.model_fields.items()
    )


# --------------------------------------------------------------------------- tables


def table_path(root: str | Path, name: TableName) -> Path:
    return Path(root) / f"{name}.parquet"


@contextlib.contextmanager
def _staged(path: Path) -> Iterator[Path]:
    """Unique ``<path>.<id>.part`` to write into, then ``os.replace`` onto ``path``.

    The temporary file is removed on exit unless it was renamed away, so a failed write
    leaves the previous ``path`` untouched and no debris behind.
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "|".join(str(v) for v in value)
    return str(value)


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(_csv_cell(row[c]) for c in columns)


def write_table(root: str | Path, name: TableName, records: Sequence[BaseModel]) -> Path:
    """Write ``<root>/<name>.parquet`` and ``<root>/<name>.csv``; returns the parquet path.

    ``records`` must all be instances of ``RECORD_MODELS[name]``. An empty sequence writes an
    empty table with the full column schema. Both files are staged before either is replaced,
    so a conversion or I/O error leaves the previous pair in place.
    """
    model = RECORD_MODELS[name]
    rows: list[dict[str, object]] = []
    for i, record in enumerate(records):
        if not isinstance(record, model):
            raise TypeError(
                f"{name} row {i}: expected {model.__name__}, got {type(record).__name__}"
            )
        rows.append(record.model_dump(mode="json", by_alias=True))

    schema = arrow_schema(model)
    timestamp_columns = [f.name for f in schema if pa.types.is_timestamp(f.type)]
    arrow_rows = [
        {
            **row,
            **{
                c: datetime.fromisoformat(typing.cast(str, row[c]))
                for c in timestamp_columns
                if row[c] is not None
            },
        }
        for row in rows
    ]
    table = pa.Table.from_pylist(arrow_rows, schema=schema)

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = table_path(root, name)
    csv_path = path.with_suffix(".csv")
    with _staged(path) as tmp_parquet, _staged(csv_path) as tmp_csv:
        pq.write_table(table, tmp_parquet, compression="zstd")
        _write_csv(tmp_csv, schema.names, rows)
        os.replace(tmp_parquet, path)
        os.replace(tmp_csv, csv_path)
    return path


def read_table(root: str | Path, name: TableName) -> list[BaseModel]:
    """Read ``<root>/<name>.parquet`` back into validated records (``RECORD_MODELS[name]``)."""
    model = RECORD_MODELS[name]
    return [model.model_validate(row) for row in pq.read_table(table_path(root, name)).to_pylist()]


def _row_count(path: Path) -> int:
    return pq.read_metadata(path).num_rows


def _column_sum(path: Path, column: str) -> int:
    total = pc.sum(pq.read_table(path, columns=[column]).column(column)).as_py()
    return int(total or 0)


def dataset_counts(root: str | Path) -> dict[str, int]:
    """Actual ``COUNT_KEYS`` computed from the table files; a missing table counts as 0.

    Used when building a manifest, so an unreadable table raises (``validate_dataset`` has
    its own exception-free accounting).
    """
    root = Path(root)
    counts: dict[str, int] = {}
    for name in TABLE_NAMES:
        path = table_path(root, name)
        counts[f"n_{name}"] = _row_count(path) if path.is_file() else 0
    genomes, images = table_path(root, "genomes"), table_path(root, "images")
    counts["genome_bytes"] = _column_sum(genomes, "sequence_bytes") if genomes.is_file() else 0
    counts["image_bytes"] = _column_sum(images, "bytes") if images.is_file() else 0
    return counts


# --------------------------------------------------------------------------- manifest


def build_manifest(
    root: str | Path,
    *,
    name: str,
    version: str,
    description: str = "",
    sources: Sequence[SourceInfo] = (),
    selection: Mapping[str, object] | None = None,
) -> DatasetManifest:
    """Manifest describing the tables currently present under ``root`` (not written).

    ``selection`` is stored in its JSON form (paths and datetimes become strings, tuples and
    sets become lists) so the returned manifest equals what ``read_manifest`` gives back;
    values that cannot be serialised raise here rather than in ``write_manifest``.
    """
    root = Path(root)
    tables: dict[TableName, TableInfo] = {}
    for table in TABLE_NAMES:
        path = table_path(root, table)
        if path.is_file():
            tables[table] = TableInfo(path=path.name, n_rows=_row_count(path))
    return DatasetManifest(
        name=name,
        version=version,
        description=description,
        created_at=datetime.now(UTC),
        sources=list(sources),
        tables=tables,
        counts=dataset_counts(root),
        selection=to_jsonable_python(dict(selection or {})),
    )


def write_manifest(root: str | Path, manifest: DatasetManifest) -> Path:
    path = Path(root) / MANIFEST_FILE
    with _staged(path) as tmp:
        tmp.write_text(manifest.model_dump_json(indent=2, by_alias=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    return path


def read_manifest(root: str | Path) -> DatasetManifest:
    text = (Path(root) / MANIFEST_FILE).read_text(encoding="utf-8")
    return DatasetManifest.model_validate_json(text)


# --------------------------------------------------------------------------- validation


def _capped(problems: list[str]) -> list[str]:
    if len(problems) <= _MAX_REPORTED:
        return problems
    return problems[:_MAX_REPORTED] + [f"... and {len(problems) - _MAX_REPORTED} more like this"]


def format_validation_error(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" if e["loc"] else e["msg"]
        for e in exc.errors()
    )


def _read_rows(path: Path, model: type[BaseModel]) -> tuple[list, int | None, list[str]]:
    """Validated records, number of rows in the file (``None`` if unreadable) and problems."""
    try:
        raw = pq.read_table(path).to_pylist()
    except (pa.ArrowException, OSError) as exc:
        return [], None, [f"{path.name}: cannot read parquet: {exc}"]
    records, problems = [], []
    for i, row in enumerate(raw):
        try:
            records.append(model.model_validate(row))
        except ValidationError as exc:
            problems.append(f"{path.name} row {i}: {format_validation_error(exc)}")
    return records, len(raw), _capped(problems)


def _check_unique(table: str, key: str, values: Iterable[object]) -> list[str]:
    duplicates = [v for v, n in Counter(values).items() if n > 1]
    return _capped([f"{table}.parquet: duplicate {key} {v!r}" for v in duplicates])


def _check_references(
    present: set[str],
    species: list[SpeciesRecord],
    genomes: list[GenomeRecord],
    images: list[ImageRecord],
    pairs: list[PairRecord],
) -> list[str]:
    problems: list[str] = []
    if "species" in present:
        species_ids = {s.ncbi_taxid for s in species}
        problems += _capped(
            [
                f"genomes.parquet {g.assembly_accession}: species_taxid {g.species_taxid} "
                "not in species table"
                for g in genomes
                if g.species_taxid not in species_ids
            ]
        )
        problems += _capped(
            [
                f"images.parquet {i.image_id}: ncbi_taxid {i.ncbi_taxid} not in species table"
                for i in images
                if i.ncbi_taxid not in species_ids
            ]
        )
    image_by_id = {i.image_id: i for i in images}
    genome_by_acc = {g.assembly_accession: g for g in genomes}
    pair_problems: list[str] = []
    for p in pairs:
        label = f"pairs.parquet ({p.image_id}, {p.assembly_accession})"
        if "images" in present:
            image = image_by_id.get(p.image_id)
            if image is None:
                pair_problems.append(f"{label}: unknown image_id {p.image_id!r}")
            elif image.ncbi_taxid != p.ncbi_taxid:
                pair_problems.append(
                    f"{label}: ncbi_taxid {p.ncbi_taxid} != image ncbi_taxid {image.ncbi_taxid}"
                )
        if "genomes" in present:
            genome = genome_by_acc.get(p.assembly_accession)
            if genome is None:
                pair_problems.append(
                    f"{label}: unknown assembly_accession {p.assembly_accession!r}"
                )
            elif genome.species_taxid != p.ncbi_taxid:
                pair_problems.append(
                    f"{label}: ncbi_taxid {p.ncbi_taxid} != genome species_taxid "
                    f"{genome.species_taxid}"
                )
    return problems + _capped(pair_problems)


def _file_digest(path: Path, algorithm: str) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, algorithm).hexdigest()


def _check_file(
    root: Path,
    label: str,
    rel: str,
    *,
    expected_bytes: int | None = None,
    digest: tuple[str, str] | None = None,
) -> str | None:
    posix = PurePosixPath(rel)
    if posix.is_absolute() or ".." in posix.parts:
        return f"{label}: path {rel!r} is not relative to the dataset root"
    path = root / posix
    if not path.is_file():
        return f"{label}: file {rel!r} is missing"
    try:
        if expected_bytes is not None and (actual := path.stat().st_size) != expected_bytes:
            return f"{label}: file {rel!r} has {actual} bytes, record says {expected_bytes}"
        if digest is not None:
            algorithm, expected = digest
            if (actual := _file_digest(path, algorithm)) != expected.lower():
                return f"{label}: {algorithm} of {rel!r} is {actual}, record says {expected}"
    except OSError as exc:
        return f"{label}: cannot read {rel!r}: {exc.strerror}"
    return None


def _check_files(
    root: Path, genomes: list[GenomeRecord], images: list[ImageRecord], check_hashes: bool
) -> list[str]:
    problems: list[str] = []
    for g in genomes:
        label = f"genomes.parquet {g.assembly_accession}"
        digest = ("md5", g.sequence_md5) if check_hashes else None
        problems.append(
            _check_file(
                root, label, g.sequence_file, expected_bytes=g.sequence_bytes, digest=digest
            )
        )
        problems += (_check_file(root, label, extra) for extra in g.extra_files)
    for i in images:
        digest = ("sha256", i.sha256) if check_hashes else None
        problems.append(
            _check_file(
                root, f"images.parquet {i.image_id}", i.file, expected_bytes=i.bytes, digest=digest
            )
        )
    return _capped([p for p in problems if p is not None])


def _check_species_counts(
    present: set[str],
    species: list[SpeciesRecord],
    genomes: list[GenomeRecord],
    images: list[ImageRecord],
) -> list[str]:
    n_images = Counter(i.ncbi_taxid for i in images)
    n_genomes = Counter(g.species_taxid for g in genomes)
    problems: list[str] = []
    for s in species:
        label = f"species.parquet {s.ncbi_taxid} ({s.scientific_name})"
        if "images" in present and s.n_images != n_images[s.ncbi_taxid]:
            problems.append(
                f"{label}: n_images is {s.n_images}, images table has {n_images[s.ncbi_taxid]}"
            )
        if "genomes" in present and s.n_genomes != n_genomes[s.ncbi_taxid]:
            problems.append(
                f"{label}: n_genomes is {s.n_genomes}, genomes table has {n_genomes[s.ncbi_taxid]}"
            )
    return _capped(problems)


def _expected_counts(tables: Mapping[str, list], n_rows: Mapping[str, int | None]) -> dict:
    """``COUNT_KEYS`` from the rows read by ``_read_rows``; ``None`` where they cannot be known
    (table missing or unreadable; byte sums also when some rows failed validation)."""
    counts: dict[str, int | None] = {f"n_{name}": n_rows[name] for name in TABLE_NAMES}
    for key, name, attr in (
        ("genome_bytes", "genomes", "sequence_bytes"),
        ("image_bytes", "images", "bytes"),
    ):
        complete = n_rows[name] is not None and n_rows[name] == len(tables[name])
        counts[key] = sum(getattr(r, attr) for r in tables[name]) if complete else None
    return counts


def _check_manifest(
    root: Path, tables: Mapping[str, list], n_rows: Mapping[str, int | None]
) -> list[str]:
    path = root / MANIFEST_FILE
    if not path.is_file():
        return [f"{MANIFEST_FILE}: missing"]
    try:
        manifest = DatasetManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        return [f"{MANIFEST_FILE}: invalid: {format_validation_error(exc)}"]
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{MANIFEST_FILE}: cannot read: {exc}"]

    problems: list[str] = []
    if manifest.schema_version != SCHEMA_VERSION:
        problems.append(
            f"{MANIFEST_FILE}: schema_version {manifest.schema_version!r} != {SCHEMA_VERSION!r}"
        )
    for name in TABLE_NAMES:
        tpath = table_path(root, name)
        info = manifest.tables.get(name)
        actual = n_rows[name]
        if info is None:
            if tpath.is_file():
                problems.append(f"{MANIFEST_FILE}: tables lacks {name!r} but {tpath.name} exists")
        elif info.path != tpath.name:
            problems.append(
                f"{MANIFEST_FILE}: tables[{name}].path is {info.path!r}, expected {tpath.name!r}"
            )
        elif not tpath.is_file():
            problems.append(f"{MANIFEST_FILE}: tables[{name}] refers to missing {tpath.name}")
        elif actual is not None and info.n_rows != actual:
            problems.append(
                f"{MANIFEST_FILE}: tables[{name}].n_rows is {info.n_rows}, {tpath.name} has {actual}"
            )
    expected = _expected_counts(tables, n_rows)
    for key in COUNT_KEYS:
        if key not in manifest.counts:
            problems.append(f"{MANIFEST_FILE}: counts lacks {key!r}")
        elif (actual := expected[key]) is not None and manifest.counts[key] != actual:
            problems.append(
                f"{MANIFEST_FILE}: counts[{key}] is {manifest.counts[key]}, actual {actual}"
            )
    return problems


def validate_dataset(root: str | Path, *, check_hashes: bool = False) -> list[str]:
    """Return a list of human-readable problems; ``[]`` means the dataset is consistent.

    Checks: every table exists and every row validates against its record model; keys are
    unique; referential integrity between the tables; every referenced file exists with the
    recorded size (and hash, with ``check_hashes``); per-species counts; that ``dataset.json``
    matches the schema version, tables and counts on disk; and that no ``*.part`` file from an
    interrupted write is left in the root. Never raises for a broken dataset: unreadable
    tables and files are reported, and checks that would need them are skipped.
    """
    root = Path(root)
    if not root.is_dir():
        return [f"{root}: not a directory"]

    problems: list[str] = []
    tables: dict[str, list] = {}
    n_rows: dict[str, int | None] = {}  # None: table missing or unreadable
    for name in TABLE_NAMES:
        path = table_path(root, name)
        if not path.is_file():
            problems.append(f"{path.name}: missing table")
            tables[name], n_rows[name] = [], None
            continue
        tables[name], n_rows[name], row_problems = _read_rows(path, RECORD_MODELS[name])
        problems += row_problems
    present = {name for name, n in n_rows.items() if n is not None}
    species, genomes, images, pairs = (tables[n] for n in TABLE_NAMES)

    problems += _check_unique("species", "ncbi_taxid", (s.ncbi_taxid for s in species))
    problems += _check_unique(
        "genomes", "assembly_accession", (g.assembly_accession for g in genomes)
    )
    problems += _check_unique("genomes", "sequence_file", (g.sequence_file for g in genomes))
    problems += _check_unique("images", "image_id", (i.image_id for i in images))
    problems += _check_unique("images", "file", (i.file for i in images))
    problems += _check_unique(
        "pairs",
        "(image_id, assembly_accession)",
        ((p.image_id, p.assembly_accession) for p in pairs),
    )
    problems += _check_references(present, species, genomes, images, pairs)
    problems += _check_files(root, genomes, images, check_hashes)
    problems += _check_species_counts(present, species, genomes, images)
    problems += _check_manifest(root, tables, n_rows)
    problems += _capped(
        [f"{p.name}: leftover from an interrupted write" for p in sorted(root.glob("*.part"))]
    )
    return problems
