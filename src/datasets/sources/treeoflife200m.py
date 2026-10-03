"""TreeOfLife-200M (Hugging Face ``imageomics/TreeOfLife-200M``) as an image-candidate source.

The dataset ships metadata only: ``catalog.parquet`` (one row per image: taxonomy, URL,
GBIF/EOL ids, ``img_type``) and ``metadata/provenance.parquet`` (per-image license, rights
holder, title). Images are fetched from the upstream hosts. Selection is one DuckDB query
over the two parquets, so the full 233M-row catalog or a class-level subset (the
``mammalia_*.parquet`` files used by default) can be used interchangeably.

Facts verified against the parquets and the hosts (2026-10, revision 5f2dc493...):
- ``catalog.source_url`` is unique per uuid and is the image's own URL. ``provenance.source_url``
  is occurrence-level (often a sibling photo of the same gbifID): never use it for bytes;
  provenance is joined on ``uuid`` only for ``license_name``/``license_link``/``copyright_owner``.
- ``scientific_name`` is the binomial; ``species`` is only the epithet. ~5.7k Mammalia rows
  carry authorship (``Urocitellus parryii (Richardson, 1825)``, ``Myoprocta pratti Pocock,
  1913``) and their ``genus``/``species`` columns are garbage (``genus = '1827)'``), so names
  are matched on ``scientific_name`` with trailing authorship stripped (see ``match_key``).
- ``img_type`` is set for GBIF rows only ('Citizen Science', 'Camera-trap', 'Museum Specimen:
  ...'); EOL/FathomNet rows have NULL. ``source_id`` is the gbifID for GBIF rows (several
  photos share one), the media id for EOL rows (unique per image).
- 78% of mammal URLs are ``https://inaturalist-open-data.s3.amazonaws.com/photos/<id>/original.<ext>``
  (a handful are catalogued at ``medium.<ext>``). The bucket serves every size variant
  (``original`` / ``large`` <=1024 px / ``medium`` <=500 px / ``small`` / ``square``) for the
  *same suffix*, whatever it is: ``original.`` (no extension, 509 rows) -> ``large.`` serves
  JPEG, while ``large`` / ``large.jpg`` 404; ``original.txt`` and ``original.JPG_copy`` likewise
  serve JPEG at ``large.txt`` / ``large.JPG_copy``. Deleted photos 404 under every variant.
- ``content.eol.org`` (3% of mammal URLs) answers 403 to scripts and is dropped by default.
- Rows without a ``source_url`` (none in the Mammalia subset; all 5.15M BIOSCAN rows of the
  full catalog) are never selectable and are always dropped (``species_summary`` counts them).
- ``license_name`` values are lower-case slugs: ``cc-by-nc-4.0`` (68%), ``cc-by-4.0``,
  ``cc-by-nc-nd-4.0``, NULL (3.9%, all GBIF), ``cc-0-1.0``, ``other``, ``cc-by-nc-sa-*``,
  ``cc-by-sa-*``, ``all-rights-reserved``, ``cc-by-3.0``, ``cc-publicdomain``, ``cc-by-nc``,
  ``cc-by``, ``No known copyright restrictions``.
- ``provenance.copyright_owner`` is never NULL but is the literal ``'not provided'`` for 27% of
  Mammalia rows (``title`` for 100%): TreeOfLife-200M's fill value for a missing string. It
  is not a rights holder, so :func:`provenance_value` maps it to None (``rights_holder`` /
  ``license`` / ``license_url`` of a candidate are None when the source has nothing).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from datasets.schema import ImageType, SourceInfo
from datasets.sources.base import ImageCandidate

SOURCE_NAME = "treeoflife-200m"
SOURCE_URL = "https://huggingface.co/datasets/imageomics/TreeOfLife-200M"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "datasets" / "treeoflife-200m"
DEFAULT_CATALOG = "mammalia_catalog.parquet"
DEFAULT_PROVENANCE = "mammalia_provenance.parquet"
SUBSET_NOTE = (
    "mammalia_*.parquet are the class = 'Mammalia' rows of catalog.parquet / "
    "metadata/provenance.parquet at source_revision"
)
INAT_S3_HOST = "inaturalist-open-data.s3.amazonaws.com"
INAT_VARIANTS: tuple[str, ...] = ("original", "large", "medium", "small", "thumb", "square")
DEFAULT_EXCLUDE_HOSTS: tuple[str, ...] = ("content.eol.org",)
NULL_IMG_TYPE_KEY = "null"
"""Key used by :meth:`TreeOfLife200MSource.species_summary` for rows with NULL ``img_type``."""

LicensePolicy = Literal["cc_only", "permissive", "any"]
InatSizeVariant = Literal["large", "medium", "original"]

LICENSE_NOTES = (
    "TreeOfLife-200M metadata (catalog, provenance) is released under CC0-1.0; the images are "
    "not part of the Hugging Face release and each is licensed individually by its rights "
    "holder (see ImageRecord.license / license_url / rights_holder, taken from "
    "provenance.parquet). The GBIF occurrence snapshot the catalog derives from "
    "(doi:10.15468/dl.bfv433) is CC BY-NC 4.0; EOL and GBIF-mediated images carry their own "
    "licenses; FathomNet CC BY-NC-ND 4.0; BIOSCAN-5M CC BY 3.0."
)

# '<Genus> <epithet>' optionally followed by authorship (' (Richardson, 1825)', ' Pocock, 1913').
# Same pattern for Python (re) and DuckDB (RE2). A third lower-case token (hybrids, trinomials)
# does not match, so such names are matched in full.
_BINOMIAL_PATTERN = r"^([A-Za-z]+ [a-z][a-z-]*)(?: \(?[A-Z].*)?$"
_BINOMIAL = re.compile(_BINOMIAL_PATTERN)
_INAT_PHOTO = re.compile(
    rf"^(https?://{re.escape(INAT_S3_HOST)}/photos/\d+)/({'|'.join(INAT_VARIANTS)})(\.[^/]*)$"
)
_PERMISSIVE_PATTERN = r"^cc-(0|publicdomain|by|by-sa)(-[0-9.]+)?$"
_PERMISSIVE = re.compile(_PERMISSIVE_PATTERN)
_CATALOG_COLUMNS = (
    "uuid",
    "source_url",
    "scientific_name",
    "data_source",
    "publisher",
    "basis_of_record",
    "img_type",
    "source_id",
)
_PROVENANCE_COLUMNS = ("license_name", "license_link", "copyright_owner", "title")
MISSING_VALUE = "not provided"
"""TreeOfLife-200M's fill value for a missing provenance string (``copyright_owner``,
``title``); mapped to None by :func:`provenance_value`."""


def _has_url(column: str) -> str:
    return f"({column} IS NOT NULL AND trim({column}) <> '')"


# --------------------------------------------------------------------------- pure helpers


def match_key(name: str) -> str:
    """Lower-cased binomial with trailing authorship removed; the full trimmed name when it is
    not '<Genus> <epithet> [authorship]' (hybrids, trinomials, genus-only labels)."""
    name = name.strip()
    m = _BINOMIAL.fullmatch(name)
    return (m.group(1) if m else name).lower()


def rewrite_inat_url(url: str, variant: InatSizeVariant) -> str | None:
    """``.../photos/<id>/<any size variant><suffix>`` on the iNaturalist open-data bucket
    rewritten to ``variant`` (suffix kept verbatim, incl. the extension-less ``original.``
    case); None when ``url`` is not such a URL. The result equals ``url`` when it already is
    the requested variant."""
    m = _INAT_PHOTO.match(url)
    if not m:
        return None
    return f"{m.group(1)}/{variant}{m.group(3)}"


def license_allowed(license_name: str | None, policy: LicensePolicy) -> bool:
    """Python mirror of the SQL license filter (``'cc_only'``: any ``cc-*`` slug incl. cc-0 and
    cc-publicdomain; ``'permissive'``: cc-0, cc-publicdomain, cc-by, cc-by-sa; ``'any'``)."""
    if policy == "any":
        return True
    if license_name is None:
        return False
    slug = license_name.lower()
    if policy == "cc_only":
        return slug.startswith("cc-")
    if policy == "permissive":
        return _PERMISSIVE.match(slug) is not None
    raise ValueError(f"unknown license policy {policy!r}")


def image_type_from_img_type(img_type: str | None) -> ImageType:
    if img_type is None:
        return ImageType.unknown
    if img_type == "Citizen Science":
        return ImageType.citizen_science
    if img_type == "Camera-trap":
        return ImageType.camera_trap
    if img_type.startswith("Museum Specimen"):
        return ImageType.museum_specimen
    return ImageType.unknown


def provenance_value(value: object) -> str | None:
    """A provenance string with TreeOfLife-200M's missing-value marker (``'not provided'``,
    any case) and blanks turned into None; any other value is returned verbatim."""
    if value is None:
        return None
    text = str(value)
    if not text.strip() or text.strip().lower() == MISSING_VALUE:
        return None
    return text


def url_host(url: str | None) -> str | None:
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/:?#]+)", url or "", re.IGNORECASE)
    return m.group(1).lower() if m else None


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_match_key(column: str) -> str:
    """SQL twin of :func:`match_key` (trim first, like the Python helper)."""
    pattern = _sql_str(_BINOMIAL_PATTERN)
    col = f"trim({column})"
    return f"lower(coalesce(nullif(regexp_extract({col}, {pattern}, 1), ''), {col}))"


def _names(what: str, value: Sequence[str | None] | None) -> list[str | None] | None:
    """``value`` as a list, rejecting a bare string (which would be iterated per character)."""
    if value is None:
        return None
    if isinstance(value, str):
        raise TypeError(f"{what} must be a sequence of names, not a string: {value!r}")
    return list(value)


def _display_path(path: Path) -> str:
    """Repo-relative POSIX path when ``path`` lies inside the repo (portable in dataset.json)."""
    return path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else str(path)


def _parquet_fingerprint(path: Path) -> dict[str, object]:
    """Size, mtime and row count (footer only, instant) so a rebuild over a regenerated or
    different parquet is detectable from the manifest."""
    st = path.stat()
    return {
        "bytes": st.st_size,
        "modified_at": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds"),
        "n_rows": pq.ParquetFile(path).metadata.num_rows,
    }


# --------------------------------------------------------------------------- source


class TreeOfLife200MSource:
    """``datasets.sources.base.ImageSource`` over the TreeOfLife-200M catalog + provenance
    parquets. All filters are constructor configuration so ``selection_params()`` can record
    them in ``dataset.json``.

    Args:
        data_dir: directory holding the parquets (default: the repo's shared-disk copy).
        revision: Hugging Face dataset revision (git sha) the parquets were downloaded at.
        image_types: ``img_type`` values to keep; ``None`` keeps all. Include a ``None`` element
            to also keep rows with NULL ``img_type`` (EOL/FathomNet).
        exclude_basis_of_record: GBIF ``basis_of_record`` values to drop (NULL is kept).
        licenses: ``'cc_only'`` | ``'permissive'`` | ``'any'`` (see :func:`license_allowed`).
            Under ``'any'`` a catalog row without a provenance row is kept with NULL license.
        max_per_observation: cap per GBIF occurrence (``source_id``); ``None`` = no cap.
        inat_size_variant: size served by the iNaturalist bucket to download (catalogued iNat
            S3 URLs of any size are rewritten; the catalogued URL goes to ``fallback_urls``).
        aliases: query name -> alternate names also accepted as ``scientific_name``.
        exclude_hosts: URL hosts dropped outright (default: ``content.eol.org``, 403 to scripts).
        catalog_path / provenance_path: override the parquet files (fixtures, full catalog).

    Rows without a URL are always dropped. Two query names (or aliases) that reduce to the
    same :func:`match_key` are rejected by ``select``/``species_summary`` (ValueError) rather
    than silently assigned to the first; exact duplicates in ``species`` collapse.
    """

    name = SOURCE_NAME

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        *,
        revision: str,
        image_types: Sequence[str | None] | None = ("Citizen Science",),
        exclude_basis_of_record: Sequence[str] = (
            "FOSSIL_SPECIMEN",
            "PRESERVED_SPECIMEN",
            "MATERIAL_SAMPLE",
        ),
        licenses: LicensePolicy = "cc_only",
        max_per_observation: int | None = 1,
        inat_size_variant: InatSizeVariant = "large",
        aliases: Mapping[str, Sequence[str]] | None = None,
        exclude_hosts: Sequence[str] = DEFAULT_EXCLUDE_HOSTS,
        catalog_path: str | Path | None = None,
        provenance_path: str | Path | None = None,
    ) -> None:
        if licenses not in ("cc_only", "permissive", "any"):
            raise ValueError(f"licenses must be cc_only|permissive|any, got {licenses!r}")
        if inat_size_variant not in ("large", "medium", "original"):
            raise ValueError(
                f"inat_size_variant must be large|medium|original, got {inat_size_variant!r}"
            )
        if max_per_observation is not None and max_per_observation < 1:
            raise ValueError("max_per_observation must be >= 1 or None")
        self.data_dir = Path(data_dir)
        self.revision = revision
        self.image_types = _names("image_types", image_types)
        self.exclude_basis_of_record = _names("exclude_basis_of_record", exclude_basis_of_record)
        self.licenses: LicensePolicy = licenses
        self.max_per_observation = max_per_observation
        self.inat_size_variant: InatSizeVariant = inat_size_variant
        self.aliases = {k: _names(f"aliases[{k!r}]", v) for k, v in (aliases or {}).items()}
        self.exclude_hosts = [h.lower() for h in _names("exclude_hosts", exclude_hosts)]
        self.catalog_path = Path(catalog_path or self.data_dir / DEFAULT_CATALOG)
        self.provenance_path = Path(provenance_path or self.data_dir / DEFAULT_PROVENANCE)
        for path in (self.catalog_path, self.provenance_path):
            if not path.exists():
                raise FileNotFoundError(path)

    # ----------------------------------------------------------------- protocol

    def source_info(self) -> SourceInfo:
        return SourceInfo(
            name="TreeOfLife-200M",
            url=SOURCE_URL,
            revision=self.revision,
            license_notes=LICENSE_NOTES,
            accessed_at=datetime.now(UTC),
        )

    def selection_params(self) -> dict[str, object]:
        """Every filter in effect plus the parquet files used (repo-relative when inside the
        repo, with a size/mtime/row-count fingerprint each)."""
        is_default_subset = (self.catalog_path.name, self.provenance_path.name) == (
            DEFAULT_CATALOG,
            DEFAULT_PROVENANCE,
        )
        return {
            "source": SOURCE_NAME,
            "source_revision": self.revision,
            "catalog_path": _display_path(self.catalog_path),
            "provenance_path": _display_path(self.provenance_path),
            "parquet_fingerprints": {
                "catalog": _parquet_fingerprint(self.catalog_path),
                "provenance": _parquet_fingerprint(self.provenance_path),
            },
            "parquet_subset": SUBSET_NOTE if is_default_subset else None,
            "image_types": self.image_types,
            "exclude_basis_of_record": self.exclude_basis_of_record,
            "licenses": self.licenses,
            "max_per_observation": self.max_per_observation,
            "inat_size_variant": self.inat_size_variant,
            "exclude_hosts": self.exclude_hosts,
            "aliases": self.aliases,
            "name_matching": (
                "case-insensitive scientific_name with trailing authorship stripped "
                f"(pattern {_BINOMIAL_PATTERN})"
            ),
            "ordering": "md5('<seed>:' || uuid), then uuid; one row per (species, observation)"
            " chosen by the same key",
            "always_dropped": "rows with NULL/empty catalog.source_url",
            "missing_value_marker": f"provenance {MISSING_VALUE!r} (and blanks) -> NULL in "
            "license / license_url / rights_holder",
        }

    def select(
        self,
        species: Sequence[str],
        *,
        per_species: int,
        seed: int = 0,
    ) -> dict[str, list[ImageCandidate]]:
        """Up to ``per_species`` candidates per query name, in seeded pseudo-random order
        (``md5(seed:uuid)``), after the configured filters and the per-observation cap. A
        species' picks do not depend on which other names are in ``species``."""
        queries = list(dict.fromkeys(_names("species", species) or ()))
        out: dict[str, list[ImageCandidate]] = {q: [] for q in queries}
        if not queries or per_species <= 0:
            return out
        sql, params = self._ranked_sql(seed)
        cap = (
            ""
            if self.max_per_observation is None
            else f"WHERE obs_rank <= {int(self.max_per_observation)}"
        )
        sql += f"""
            , capped AS (
                SELECT *, row_number() OVER (PARTITION BY species_query ORDER BY rnd, uuid) AS rank
                FROM ranked {cap}
            )
            SELECT * FROM capped WHERE rank <= {int(per_species)} ORDER BY species_query, rank
        """
        for row in self._run(queries, sql, params):
            out[row["species_query"]].append(self._candidate(row))
        return out

    def species_summary(self, species: Sequence[str]) -> list[dict[str, object]]:
        """Per query name (JSON-serialisable): ``n_rows`` (name matches, no filters),
        ``image_types`` (img_type counts of those rows; NULL img_type under the key
        :data:`NULL_IMG_TYPE_KEY`), ``n_no_url`` (matches without a URL, never selectable),
        ``n_eligible`` (after filters), ``n_observations`` (distinct occurrences among eligible
        rows) and ``n_selectable`` (after the per-observation cap)."""
        queries = list(dict.fromkeys(_names("species", species) or ()))
        if not queries:
            return []
        summary = {
            q: {
                "species_query": q,
                "n_rows": 0,
                "image_types": {},
                "n_no_url": 0,
                "n_eligible": 0,
                "n_observations": 0,
                "n_selectable": 0,
            }
            for q in queries
        }
        sql, params = self._matched_sql()
        sql += f"""
            SELECT species_query, img_type, count(*) AS n,
                   count_if(NOT {_has_url("source_url")})::BIGINT AS n_no_url
            FROM matched GROUP BY ALL ORDER BY species_query, n DESC, img_type
        """
        for row in self._run(queries, sql, params):
            entry = summary[row["species_query"]]
            entry["n_rows"] += row["n"]
            entry["n_no_url"] += row["n_no_url"]
            key = NULL_IMG_TYPE_KEY if row["img_type"] is None else row["img_type"]
            entry["image_types"][key] = row["n"]
        sql, params = self._ranked_sql(seed=0)
        cap = (
            "TRUE"
            if self.max_per_observation is None
            else f"obs_rank <= {int(self.max_per_observation)}"
        )
        sql += f"""
            SELECT species_query, count(*) AS n_eligible,
                   count(DISTINCT obs_key) AS n_observations,
                   count_if({cap})::BIGINT AS n_selectable
            FROM ranked GROUP BY 1
        """
        for row in self._run(queries, sql, params):
            summary[row["species_query"]].update(
                n_eligible=row["n_eligible"],
                n_observations=row["n_observations"],
                n_selectable=row["n_selectable"],
            )
        return [summary[q] for q in queries]

    # ----------------------------------------------------------------- internals

    def _query_table(self, queries: Sequence[str]) -> pa.Table:
        """(match_key, species_query) pairs for the names and their aliases. A key claimed by
        two query names would make the later one silently empty, so that is an error."""
        keys: dict[str, str] = {}
        collisions: list[str] = []
        for q in queries:
            for name in (q, *self.aliases.get(q, ())):
                key = match_key(name)
                owner = keys.setdefault(key, q)
                if owner != q:
                    collisions.append(
                        f"{name!r} (key {key!r}) of {q!r} already matched by {owner!r}"
                    )
        if collisions:
            raise ValueError("query names/aliases collide: " + "; ".join(collisions))
        return pa.table({"match_key": list(keys), "species_query": list(keys.values())})

    def _matched_sql(self) -> tuple[str, list[object]]:
        cols = ", ".join(f"c.{c}" for c in _CATALOG_COLUMNS)
        sql = f"""
            WITH matched AS (
                SELECT {cols}, q.species_query,
                       CASE WHEN c.data_source = 'gbif' AND c.source_id IS NOT NULL
                            THEN c.source_id ELSE c.uuid END AS obs_key
                FROM read_parquet({_sql_str(str(self.catalog_path))}) c
                JOIN queries q ON {_sql_match_key("c.scientific_name")} = q.match_key
            )
        """
        return sql, []

    def _filters(self) -> tuple[str, list[object]]:
        """WHERE clause over ``matched m`` joined with ``provenance p``; a row without a URL
        is never a candidate, so that clause is unconditional."""
        clauses: list[str] = [_has_url("m.source_url")]
        params: list[object] = []
        if self.image_types is not None:
            values = [t for t in self.image_types if t is not None]
            parts = []
            if values:
                parts.append(f"m.img_type IN ({', '.join('?' * len(values))})")
                params.extend(values)
            if len(values) < len(self.image_types):
                parts.append("m.img_type IS NULL")
            clauses.append("(" + " OR ".join(parts) + ")" if parts else "FALSE")
        if self.exclude_basis_of_record:
            marks = ", ".join("?" * len(self.exclude_basis_of_record))
            clauses.append(f"(m.basis_of_record IS NULL OR m.basis_of_record NOT IN ({marks}))")
            params.extend(self.exclude_basis_of_record)
        if self.exclude_hosts:
            marks = ", ".join("?" * len(self.exclude_hosts))
            host = "lower(regexp_extract(m.source_url, '^[A-Za-z][A-Za-z0-9+.-]*://([^/:?#]+)', 1))"
            clauses.append(f"coalesce({host}, '') NOT IN ({marks})")
            params.extend(self.exclude_hosts)
        if self.licenses == "cc_only":
            clauses.append("lower(p.license_name) LIKE 'cc-%'")
        elif self.licenses == "permissive":
            clauses.append(
                f"regexp_matches(lower(p.license_name), {_sql_str(_PERMISSIVE_PATTERN)})"
            )
        return " AND ".join(clauses), params

    def _ranked_sql(self, seed: int) -> tuple[str, list[object]]:
        """``matched`` -> ``eligible`` (provenance left-joined, filters applied, one row per
        uuid even if a provenance file repeats a uuid) -> ``ranked`` (seeded order key ``rnd``
        and ``obs_rank`` within each observation)."""
        sql, params = self._matched_sql()
        where, where_params = self._filters()
        params.extend(where_params)
        pcols = ", ".join(f"p.{c}" for c in _PROVENANCE_COLUMNS)
        dedupe = ", ".join(f"p.{c} NULLS LAST" for c in _PROVENANCE_COLUMNS)
        rnd = f"md5({_sql_str(f'{int(seed)}:')} || uuid)"
        sql += f"""
            , eligible AS (
                SELECT m.*, {pcols}
                FROM matched m
                LEFT JOIN read_parquet({_sql_str(str(self.provenance_path))}) p USING (uuid)
                WHERE {where}
                QUALIFY row_number() OVER (
                    PARTITION BY m.species_query, m.uuid ORDER BY {dedupe}, m.source_url
                ) = 1
            )
            , ranked AS (
                SELECT *, {rnd} AS rnd,
                       row_number() OVER (PARTITION BY species_query, obs_key ORDER BY {rnd}, uuid)
                           AS obs_rank
                FROM eligible
            )
        """
        return sql, params

    def _run(self, queries: Sequence[str], sql: str, params: list[object]) -> list[dict]:
        con = duckdb.connect()
        try:
            con.register("queries", self._query_table(queries))
            cur = con.execute(sql, params)
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, values, strict=True)) for values in cur.fetchall()]
        finally:
            con.close()

    def _candidate(self, row: Mapping[str, object]) -> ImageCandidate:
        url = str(row["source_url"])
        fallback: list[str] = []
        rewritten = rewrite_inat_url(url, self.inat_size_variant)
        if rewritten and rewritten != url:
            fallback, url = [url], rewritten
        publisher = row["publisher"]
        data_source = row["data_source"]
        if publisher == "iNaturalist.org":
            provider = "inaturalist"
        else:
            provider = str(data_source).lower() if data_source else None
        source_id = row["source_id"]
        return ImageCandidate(
            candidate_id=str(row["uuid"]),
            species_query=str(row["species_query"]),
            original_label=str(row["scientific_name"]),
            source_dataset=SOURCE_NAME,
            source_dataset_revision=self.revision,
            source_provider=provider,
            source_id=source_id,
            observation_id=source_id if data_source == "gbif" else None,
            source_url=url,
            fallback_urls=fallback,
            image_type=image_type_from_img_type(row["img_type"]),
            source_image_type=row["img_type"],
            publisher=publisher,
            license=provenance_value(row["license_name"]),
            license_url=provenance_value(row["license_link"]),
            rights_holder=provenance_value(row["copyright_owner"]),
        )
