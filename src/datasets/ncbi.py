"""NCBI Taxonomy and RefSeq assemblies: resolve names to taxids, list assemblies (from
``assembly_summary*.txt`` files or the Datasets v2 REST API), pick one reference assembly
per species and download its genomic FASTA with MD5 verification.

API notes (verified live against https://api.ncbi.nlm.nih.gov/datasets/v2, 2026-10):
- ``POST /taxonomy`` with ``{"taxons": [names_or_taxids]}`` -> ``taxonomy_nodes[]``, each with
  ``query`` and either ``taxonomy{tax_id, organism_name, rank, lineage[taxids],
  genbank_common_name}`` or ``errors[]``. Synonyms ('Felis leo') resolve to the accepted node;
  homonyms ('Bacillus') yield several nodes; lookups are case-insensitive; ``rank`` is absent
  for 'no rank'. (The GET variant puts names in the URL path, where ',' splits a name into
  several queries and '/' makes the gateway 404; the POST body has neither problem.)
- ``GET /taxonomy/taxon/{taxids}/dataset_report`` -> ``reports[]`` echoing ``query`` with
  ``taxonomy.classification.species{id,name}`` (used to map subspecies/strain taxids to their
  species), or ``errors[]`` and no ``taxonomy`` for unknown ids. Merged ids echo the old id in
  ``query`` and report the new ``tax_id``.
- ``GET /genome/taxon/{taxid}/dataset_report`` -> ``reports[]`` (``accession``, ``organism``,
  ``assembly_info``, ``assembly_stats``, ``annotation_info``), ``total_count`` and
  ``next_page_token``. Reports carry no FTP path; ``GET /genome/accession/{accs}/links``
  does (``assembly_link_type == 'FTP_LINK'``).
- ``GET /taxonomy/taxon_suggest/{text}`` -> ``sci_name_and_ids[]`` with ``sci_name``, ``tax_id``,
  ``rank``, ``group_name``: prefix completion of names (no spelling correction).
- Unknown taxids/accessions return ``{}`` with HTTP 200, not an error.
- https://ftp.ncbi.nlm.nih.gov supports HTTP Range requests (``Accept-Ranges: bytes``); its
  416 responses carry no ``Content-Range``.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import re
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator
from tqdm import tqdm

from datasets.schema import AssemblyLevel, GenomeRecord

log = logging.getLogger(__name__)

API_URL = "https://api.ncbi.nlm.nih.gov/datasets/v2"
FTP_HOST = "ftp.ncbi.nlm.nih.gov"
USER_AGENT = "genes.jpg/0.1 (+https://github.com/genesjpgorg/genes.jpg)"
_CHUNK = 1 << 20
# NCBI's missing-value marker in assembly_summary files is lower-case 'na' only; upper-case
# 'NA' is a real value (asm_name of ~330 current RefSeq assemblies, e.g. GCF_019041315.1_NA/).
_NA = {"", "na"}


class NCBIError(RuntimeError):
    """Unrecoverable NCBI API or download failure."""


class ChecksumError(NCBIError):
    """A downloaded file does not match the MD5 published by NCBI."""


# --------------------------------------------------------------------------- HTTP client


class _Client:
    """``requests.Session`` with NCBI API-key support, a request-rate cap on the Datasets API
    (3 req/s without key, 10 req/s with ``NCBI_API_KEY``) and retries with backoff."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        key = os.environ.get("NCBI_API_KEY")
        if key:
            self.session.headers["api-key"] = key
        self.min_interval = 0.1 if key else 0.34
        self._lock = threading.Lock()
        self._next_at = 0.0

    def _throttle(self) -> None:
        with self._lock:
            wait = self._next_at - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next_at = time.monotonic() + self.min_interval

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: object | None = None,
        headers: dict | None = None,
        stream: bool = False,
        timeout: float = 60,
        retries: int = 4,
    ) -> requests.Response:
        """Request with retries on connection errors, 429 and 5xx. Returns 2xx and 416
        responses (416 = Range Not Satisfiable, handled by the download code); raises
        ``requests.HTTPError`` on other 4xx and ``NCBIError`` once the retries are spent."""
        error = "no attempt made"
        for attempt in range(retries):
            if url.startswith(API_URL):
                self._throttle()
            delay = float(2**attempt)
            try:
                r = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    stream=stream,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                if r.ok or r.status_code == 416:
                    return r
                if r.status_code not in (429, 500, 502, 503, 504):
                    r.raise_for_status()
                error = f"HTTP {r.status_code}"
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                r.close()
            if attempt < retries - 1:
                log.warning("%s %s failed (%s); retrying in %.0fs", method, url, error, delay)
                time.sleep(delay)
        raise NCBIError(f"{method} {url} failed after {retries} attempts: {error}")

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)


_CLIENT: _Client | None = None
_CLIENT_LOCK = threading.Lock()


def _client() -> _Client:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = _Client()
        return _CLIENT


def _api(method: str, path: str, *, params: dict | None = None, json: object | None = None) -> dict:
    """Datasets API call; every failure mode (HTTP 4xx, exhausted retries, non-JSON body such
    as a maintenance page) surfaces as ``NCBIError``."""
    url = f"{API_URL}/{path}"
    try:
        r = _client().request(
            method, url, params=params, json=json, headers={"Accept": "application/json"}
        )
    except requests.HTTPError as exc:
        raise NCBIError(f"{method} {url}: {exc}") from exc
    try:
        return r.json()
    except ValueError as exc:
        raise NCBIError(
            f"{method} {url}: non-JSON response ({r.headers.get('Content-Type')}, {r.text[:200]!r})"
        ) from exc


def _api_get(path: str, params: dict | None = None) -> dict:
    return _api("GET", path, params=params)


def _api_post(path: str, json: object) -> dict:
    return _api("POST", path, json=json)


# --------------------------------------------------------------------------- taxonomy


class LineageNode(NamedTuple):
    taxid: int
    name: str
    rank: str | None


class TaxonInfo(BaseModel):
    """An NCBI Taxonomy node with its full lineage."""

    model_config = ConfigDict(extra="forbid")

    taxid: int
    scientific_name: str = Field(description="NCBI accepted scientific name")
    rank: str | None = Field(
        default=None, description="lower-case NCBI rank, e.g. 'species'; None for 'no rank'"
    )
    common_name: str | None = None
    lineage: list[LineageNode] = Field(
        default_factory=list, description="(taxid, name, rank) from root to this taxon, inclusive"
    )
    query: str = Field(description="the name or taxid that was looked up")
    is_synonym: bool = Field(
        default=False, description="query name differs from the accepted scientific name"
    )
    n_matches: int = Field(
        default=1, description=">1 when the query is a homonym; the first match is returned"
    )

    @property
    def lineage_taxids(self) -> list[int]:
        return [node.taxid for node in self.lineage]

    def ancestor(self, rank: str) -> LineageNode | None:
        """Lineage node with the given rank ('kingdom', 'class', 'species', ...), if any."""
        return next((node for node in self.lineage if node.rank == rank), None)

    @property
    def species_taxid(self) -> int | None:
        node = self.ancestor("species")
        return node.taxid if node else None


def _rank(rank: str | None) -> str | None:
    return rank.lower() if rank and rank.upper() != "NO RANK" else None


def _taxonomy_nodes(queries: Sequence[str]) -> list[dict]:
    """Nodes with a ``taxonomy`` payload for the given names/taxids (``POST /taxonomy``, so a
    name is one query whatever characters it contains); error nodes are dropped."""
    data = _api_post("taxonomy", {"taxons": list(queries)})
    return [node for node in data.get("taxonomy_nodes", []) if "taxonomy" in node]


def _lineage(taxids: Sequence[int]) -> list[LineageNode]:
    found: dict[int, LineageNode] = {}
    for i in range(0, len(taxids), 100):
        for node in _taxonomy_nodes([str(t) for t in taxids[i : i + 100]]):
            tax = node["taxonomy"]
            found[tax["tax_id"]] = LineageNode(
                tax["tax_id"], tax["organism_name"], _rank(tax.get("rank"))
            )
    return [found[t] for t in taxids if t in found]


def _taxon_info(query: str) -> TaxonInfo | None:
    nodes = _taxonomy_nodes([query])
    if not nodes:
        return None
    if len(nodes) > 1:
        log.warning(
            "taxonomy query %r is a homonym (taxids %s); using the first",
            query,
            [n["taxonomy"]["tax_id"] for n in nodes],
        )
    tax = nodes[0]["taxonomy"]
    name = tax["organism_name"]
    own = LineageNode(tax["tax_id"], name, _rank(tax.get("rank")))
    return TaxonInfo(
        taxid=tax["tax_id"],
        scientific_name=name,
        rank=own.rank,
        common_name=tax.get("genbank_common_name") or tax.get("common_name"),
        lineage=[*_lineage(tax.get("lineage", [])), own],
        query=query,
        is_synonym=not query.isdigit() and query.casefold() != name.casefold(),
        n_matches=len(nodes),
    )


def resolve_taxon(name: str) -> TaxonInfo | None:
    """Resolve a scientific name (accepted name or NCBI synonym, case-insensitive) to its
    taxon, or None if NCBI does not know the name."""
    return _taxon_info(name.strip())


class TaxonHit(NamedTuple):
    """One NCBI Taxonomy node matching a batched name query (``resolve_taxa``)."""

    taxid: int
    scientific_name: str
    rank: str | None
    lineage_taxids: list[int]
    """ancestor taxids from root down to the parent (the node itself is not included)"""
    common_name: str | None = None


def resolve_taxa(queries: Sequence[str], *, batch_size: int = 100) -> dict[str, list[TaxonHit]]:
    """Batched ``resolve_taxon``: names (or taxids as strings) -> matching nodes, keyed by the
    query as given. Unknown names map to an empty list; homonyms to several hits (callers
    disambiguate, e.g. by kingdom via ``lineage_taxids``). Lineages are taxids only: no extra
    requests are made, so 4,000 names cost ~40 API calls."""
    out: dict[str, list[TaxonHit]] = {q: [] for q in queries}
    unique = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
    by_fold = {q.casefold(): q for q in unique}
    for i in range(0, len(unique), batch_size):
        for node in _taxonomy_nodes(unique[i : i + batch_size]):
            echoed = node.get("query") or []
            key = echoed[0] if echoed else None
            query = key if key in out else by_fold.get((key or "").casefold())
            if query is None:
                log.warning("taxonomy response for unknown query %r; ignored", key)
                continue
            tax = node["taxonomy"]
            out[query].append(
                TaxonHit(
                    taxid=tax["tax_id"],
                    scientific_name=tax["organism_name"],
                    rank=_rank(tax.get("rank")),
                    lineage_taxids=[int(t) for t in tax.get("lineage", [])],
                    common_name=tax.get("genbank_common_name") or tax.get("common_name"),
                )
            )
    for q in queries:  # a query given with surrounding whitespace shares its stripped twin
        if q != q.strip() and q.strip() in out:
            out[q] = out[q.strip()]
    return out


def get_taxon(taxid: int) -> TaxonInfo | None:
    return _taxon_info(str(int(taxid)))


def suggest_taxon_names(query: str) -> list[tuple[str, str | None]]:
    """``GET /taxonomy/taxon_suggest/{query}`` -> ``[(scientific name, rank), ...]`` (rank
    lower-cased, None for 'no rank'). The endpoint completes name *prefixes* (it does not
    correct spelling), so callers vary the query; viruses and other groups come back too."""
    data = _api_get(f"taxonomy/taxon_suggest/{quote(query.strip(), safe='')}")
    return [
        (item["sci_name"], _rank(item.get("rank")))
        for item in data.get("sci_name_and_ids", [])
        if item.get("sci_name")
    ]


def species_taxids(taxids: Sequence[int]) -> dict[int, int]:
    """Map taxids (species, subspecies, strains, ...) to their species-rank taxid, keyed by
    the taxid asked for (so merged/secondary ids map to the species of their current node).
    Taxids above species rank, or unknown ones, are absent from the result."""
    out: dict[int, int] = {}
    ids = sorted(set(taxids))
    for i in range(0, len(ids), 100):
        batch = ",".join(str(t) for t in ids[i : i + 100])
        data = _api_get(f"taxonomy/taxon/{batch}/dataset_report")
        for report in data.get("reports", []):
            tax = report.get("taxonomy")
            if not tax:  # unknown/deleted id: {'query': [...], 'errors': [...]}
                log.debug("taxonomy dataset_report: %s", report.get("errors"))
                continue
            species = tax.get("classification", {}).get("species")
            if not species:
                continue
            queried = [int(q) for q in report.get("query", []) if str(q).isdigit()]
            for t in queried or [tax["tax_id"]]:
                out[t] = species["id"]
    return out


# --------------------------------------------------------------------------- assemblies


class AssemblySummaryRow(BaseModel):
    """One assembly. Field names are the columns of NCBI ``assembly_summary*.txt``; rows
    built from Datasets v2 genome reports additionally carry the N50s and lack the few
    file-only columns (wgs_master, release_type, genome_rep, group, pubmed_id, ...)."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    assembly_accession: str
    bioproject: str | None = None
    biosample: str | None = None
    wgs_master: str | None = None
    refseq_category: str | None = Field(
        default=None, description="'reference genome', 'representative genome' or None"
    )
    taxid: int
    species_taxid: int
    organism_name: str
    infraspecific_name: str | None = Field(
        default=None, description="e.g. 'strain=K-12', 'breed=boxer'"
    )
    isolate: str | None = None
    version_status: str | None = Field(
        default=None, description="'latest', 'replaced' or 'suppressed'"
    )
    assembly_level: AssemblyLevel
    release_type: str | None = None
    genome_rep: str | None = None
    seq_rel_date: str | None = Field(default=None, description="YYYY-MM-DD")
    asm_name: str
    asm_submitter: str | None = None
    gbrs_paired_asm: str | None = None
    paired_asm_comp: str | None = None
    ftp_path: str | None = Field(
        default=None, description="NCBI FTP/HTTPS directory of the assembly"
    )
    excluded_from_refseq: str | None = None
    relation_to_type_material: str | None = None
    asm_not_live_date: str | None = None
    assembly_type: str | None = None
    group: str | None = None
    genome_size: int | None = None
    genome_size_ungapped: int | None = None
    gc_percent: float | None = None
    replicon_count: int | None = None
    scaffold_count: int | None = None
    contig_count: int | None = None
    annotation_provider: str | None = None
    annotation_name: str | None = None
    annotation_date: str | None = None
    total_gene_count: int | None = None
    protein_coding_gene_count: int | None = None
    non_coding_gene_count: int | None = None
    pubmed_id: list[int] = Field(default_factory=list)
    contig_n50: int | None = Field(default=None, description="API only")
    scaffold_n50: int | None = Field(default=None, description="API only")

    @field_validator("*", mode="before")
    @classmethod
    def _na_to_none(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() in _NA:
            return None
        return value

    @field_validator("pubmed_id", mode="before")
    @classmethod
    def _split_pubmed(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return [int(p) for p in value.split(";") if p.strip().isdigit()]
        return value

    @field_validator("seq_rel_date", "annotation_date", "asm_not_live_date", mode="before")
    @classmethod
    def _iso_date(cls, value: object) -> object:
        return value.replace("/", "-") if isinstance(value, str) else value


def load_assembly_summary(path: str | Path) -> list[AssemblySummaryRow]:
    """Parse an NCBI ``assembly_summary*.txt``: tab-separated, '#' comment lines, the last
    '#' line is the header. Columns the model does not know are ignored."""
    known = set(AssemblySummaryRow.model_fields)
    header: list[str] | None = None
    rows: list[AssemblySummaryRow] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if line.startswith("#"):
                header = line.lstrip("# ").split("\t")
                continue
            if not line:
                continue
            if header is None:
                raise ValueError(f"{path}:{lineno}: data before header line")
            data = {k: v for k, v in zip(header, line.split("\t"), strict=False) if k in known}
            try:
                rows.append(AssemblySummaryRow.model_validate(data))
            except ValueError as exc:
                raise ValueError(
                    f"{path}:{lineno}: bad row {data.get('assembly_accession')!r}: {exc}"
                ) from exc
    return rows


def _genome_reports(path: str, params: dict) -> Iterator[dict]:
    params = {**params, "page_size": 100}
    while True:
        data = _api_get(path, params)
        yield from data.get("reports", [])
        token = data.get("next_page_token")
        if not token:
            return
        params["page_token"] = token


def _ftp_links(accessions: Sequence[str]) -> dict[str, str]:
    links: dict[str, str] = {}
    for i in range(0, len(accessions), 50):
        data = _api_get(f"genome/accession/{','.join(accessions[i : i + 50])}/links")
        for link in data.get("assembly_links", []):
            if link.get("assembly_link_type") == "FTP_LINK":
                links[link["accession"]] = link["resource_link"]
    return links


def _row_from_report(
    report: dict, *, species_taxid: int, ftp_path: str | None
) -> AssemblySummaryRow:
    info = report.get("assembly_info", {})
    stats = report.get("assembly_stats", {})
    organism = report.get("organism", {})
    annotation = report.get("annotation_info", {})
    genes = annotation.get("stats", {}).get("gene_counts", {})
    infra = organism.get("infraspecific_names", {})
    paired = info.get("paired_assembly", {})
    accession = report["accession"]
    asm_name = info.get("assembly_name", "")
    infraspecific = next(
        (f"{k}={infra[k]}" for k in ("strain", "breed", "cultivar", "ecotype") if infra.get(k)),
        None,
    )
    paired_comp = None
    if "refseq_genbank_are_different" in paired:
        paired_comp = "different" if paired["refseq_genbank_are_different"] else "identical"
    status = info.get("assembly_status")
    return AssemblySummaryRow(
        assembly_accession=accession,
        bioproject=info.get("bioproject_accession"),
        biosample=info.get("biosample", {}).get("accession"),
        refseq_category=info.get("refseq_category"),
        taxid=organism["tax_id"],
        species_taxid=species_taxid,
        organism_name=organism["organism_name"],
        infraspecific_name=infraspecific,
        isolate=infra.get("isolate"),
        version_status="latest" if status == "current" else status,
        assembly_level=info["assembly_level"],
        seq_rel_date=info.get("release_date"),
        asm_name=asm_name,
        asm_submitter=info.get("submitter"),
        gbrs_paired_asm=report.get("paired_accession"),
        paired_asm_comp=paired_comp,
        ftp_path=ftp_path or construct_ftp_path(accession, asm_name),
        assembly_type=info.get("assembly_type"),
        genome_size=stats.get("total_sequence_length"),
        genome_size_ungapped=stats.get("total_ungapped_length"),
        gc_percent=stats.get("gc_percent"),
        scaffold_count=stats.get("number_of_scaffolds"),
        contig_count=stats.get("number_of_contigs"),
        annotation_provider=annotation.get("provider"),
        annotation_name=annotation.get("name"),
        annotation_date=annotation.get("release_date"),
        total_gene_count=genes.get("total"),
        protein_coding_gene_count=genes.get("protein_coding"),
        non_coding_gene_count=genes.get("non_coding"),
        contig_n50=stats.get("contig_n50"),
        scaffold_n50=stats.get("scaffold_n50"),
    )


def list_refseq_assemblies(species_taxid: int) -> list[AssemblySummaryRow]:
    """Current RefSeq assemblies of a species (including its subspecies/strains) from the
    Datasets API, with assembly stats and the authoritative FTP path. ``species_taxid`` must
    be a species-rank taxid: it is stored verbatim in ``AssemblySummaryRow.species_taxid``."""
    path = f"genome/taxon/{int(species_taxid)}/dataset_report"
    params = {"filters.assembly_source": "refseq", "filters.assembly_version": "current"}
    reports = list(_genome_reports(path, params))
    links = _ftp_links([r["accession"] for r in reports]) if reports else {}
    return [
        _row_from_report(r, species_taxid=species_taxid, ftp_path=links.get(r["accession"]))
        for r in reports
    ]


def get_assembly_report(accession: str) -> AssemblySummaryRow | None:
    """One assembly by accession (``GCF_...``/``GCA_...``, with or without version), or None.
    The species taxid is resolved through the taxonomy (falls back to the organism taxid)."""
    data = _api_get(f"genome/accession/{accession}/dataset_report")
    reports = data.get("reports", [])
    if not reports:
        return None
    report = reports[0]
    taxid = report["organism"]["tax_id"]
    species = species_taxids([taxid]).get(taxid, taxid)
    ftp_path = _ftp_links([report["accession"]]).get(report["accession"])
    return _row_from_report(report, species_taxid=species, ftp_path=ftp_path)


_CATEGORY_RANK = {"reference genome": 0, "representative genome": 1}
_LEVEL_RANK = {level.value: i for i, level in enumerate(AssemblyLevel)}


def pick_best_assembly(rows: Sequence[AssemblySummaryRow]) -> AssemblySummaryRow:
    """reference genome > representative genome > other; then Complete Genome > Chromosome
    > Scaffold > Contig; then newest release date; accession as a final tie-break."""
    if not rows:
        raise ValueError("no assemblies to choose from")

    def key(row: AssemblySummaryRow) -> tuple:
        date = -int(row.seq_rel_date.replace("-", "")) if row.seq_rel_date else 0
        return (
            _CATEGORY_RANK.get(row.refseq_category or "", 2),
            _LEVEL_RANK.get(row.assembly_level, len(_LEVEL_RANK)),
            date,
            row.assembly_accession,
        )

    return min(rows, key=key)


# --------------------------------------------------------------------------- FTP paths


def ftp_https_url(ftp_path: str) -> str:
    """'ftp://ftp.ncbi.nlm.nih.gov/...' or 'https://...' -> https URL without trailing '/'."""
    url = re.sub(r"^ftp://", "https://", ftp_path.strip())
    if not url.startswith("https://"):
        raise ValueError(f"not an NCBI ftp/https path: {ftp_path!r}")
    return url.rstrip("/")


def construct_ftp_path(accession: str, asm_name: str) -> str:
    """NCBI's directory layout, e.g. ``genomes/all/GCF/018/350/215/GCF_018350215.1_<asm_name>``
    with each run of characters outside ``[A-Za-z0-9._-]`` in ``asm_name`` replaced by '_'.
    Prefer the FTP link reported by the API when available: a few directories keep an older
    asm_name (e.g. GCF_033118175.1 'ASM3311817v2' lives in ..._ASM3311817v1/)."""
    prefix, rest = accession.split("_", 1)
    digits = rest.split(".", 1)[0]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", asm_name)
    return (
        f"https://{FTP_HOST}/genomes/all/{prefix}/{digits[0:3]}/{digits[3:6]}/{digits[6:9]}/"
        f"{accession}_{safe}"
    )


def genome_file_urls(ftp_path: str) -> dict[str, str]:
    """URLs of the genomic FASTA, assembly report and md5checksums.txt of an assembly dir."""
    base_url = ftp_https_url(ftp_path)
    base = base_url.rsplit("/", 1)[1]
    return {
        "sequence": f"{base_url}/{base}_genomic.fna.gz",
        "assembly_report": f"{base_url}/{base}_assembly_report.txt",
        "md5checksums": f"{base_url}/md5checksums.txt",
    }


# --------------------------------------------------------------------------- download


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def read_md5checksums(path: Path) -> dict[str, str]:
    """``md5checksums.txt`` ('<md5>  ./<file>' per line) -> {basename: md5}."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[1].strip().rsplit("/", 1)[-1]] = parts[0].lower()
    return out


def count_fasta_records(path: Path) -> int:
    """Number of '>' header lines in a (gzipped) FASTA, streamed in 1 MiB chunks."""
    n = 0
    prev = b"\n"
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            n += chunk.count(b"\n>")
            if prev.endswith(b"\n") and chunk.startswith(b">"):
                n += 1
            prev = chunk[-1:]
    return n


def _content_range_start(header: str | None) -> int | None:
    """'bytes 5000-19999/20000' -> 5000; None when absent or unparsable."""
    m = re.match(r"\s*bytes\s+(\d+)-", header or "")
    return int(m.group(1)) if m else None


def download_file(
    url: str, dest: Path, *, resume: bool = True, retries: int = 4, progress: bool = True
) -> int:
    """Stream ``url`` into ``dest`` via ``<dest>.part`` and an atomic rename; an existing
    ``.part`` is resumed with an HTTP Range request (a ``.part`` the server rejects with 416,
    or answers from an offset other than the requested one, is discarded and the download
    restarts from zero). Returns the file size in bytes. Completeness is checked against
    ``Content-Length`` only; this function gives no integrity guarantee by itself, callers
    that have a checksum should verify it (see ``_fetch_verified``)."""
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    client = _client()
    for attempt in range(retries):
        offset = part.stat().st_size if resume and part.exists() else 0
        # identity: NCBI gzips text files on the fly otherwise, which breaks Content-Length
        # bookkeeping and Range offsets (requests would transparently decompress).
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            with client.get(url, headers=headers, stream=True, timeout=120) as r:
                if r.status_code == 416:
                    # .part is longer than the remote file (stale or garbage); NCBI sends no
                    # Content-Range here, so we cannot tell "complete" apart: start over.
                    part.unlink(missing_ok=True)
                    raise OSError(f"HTTP 416 for Range bytes={offset}-; restarting")
                if r.status_code == 206:
                    start = _content_range_start(r.headers.get("Content-Range"))
                    if start in (None, offset):
                        mode = "ab"
                    elif start == 0:  # server ignored the offset but says so: full body
                        mode, offset = "wb", 0
                    else:
                        part.unlink(missing_ok=True)
                        raise OSError(
                            f"Content-Range starts at {start}, expected {offset}; restarting"
                        )
                else:
                    mode, offset = "wb", 0
                length = r.headers.get("Content-Length")
                total = offset + int(length) if length else None
                if total is None:
                    log.warning("%s: no Content-Length; cannot detect a truncated body", url)
                with (
                    open(part, mode) as fh,
                    tqdm(
                        total=total,
                        initial=offset,
                        unit="B",
                        unit_scale=True,
                        desc=dest.name,
                        disable=None if progress else True,
                        leave=False,
                    ) as bar,
                ):
                    for chunk in r.iter_content(_CHUNK):
                        fh.write(chunk)
                        bar.update(len(chunk))
            if total is not None and part.stat().st_size != total:
                raise OSError(f"short read: {part.stat().st_size} of {total} bytes")
            break
        except requests.HTTPError as exc:  # 4xx other than 416/429: retrying will not help
            raise NCBIError(f"download of {url} failed: {exc}") from exc
        except (requests.RequestException, OSError) as exc:
            if attempt == retries - 1:
                raise NCBIError(f"download of {url} failed: {exc}") from exc
            delay = 2.0**attempt
            log.warning("%s: %s; resuming in %.0fs", url, exc, delay)
            time.sleep(delay)
    os.replace(part, dest)
    return dest.stat().st_size


def _fetch_verified(url: str, dest: Path, expected_md5: str, *, progress: bool) -> None:
    """Ensure ``dest`` exists with the expected MD5: reuse a valid file, otherwise download
    (at most twice: a resumed .part may itself be corrupt) and delete anything that fails."""
    if dest.exists():
        if md5_file(dest) == expected_md5:
            log.info("%s: up to date, skipping", dest)
            dest.with_name(dest.name + ".part").unlink(missing_ok=True)  # leftover
            return
        log.warning("%s: MD5 mismatch, re-downloading", dest)
        dest.unlink()
    for _ in range(2):
        download_file(url, dest, progress=progress)
        if md5_file(dest) == expected_md5:
            return
        dest.unlink()
        log.warning("%s: MD5 mismatch after download, retrying from scratch", dest)
    raise ChecksumError(f"{url}: MD5 does not match md5checksums.txt (expected {expected_md5})")


def download_genome(
    row: AssemblySummaryRow,
    dataset_root: str | Path,
    *,
    subdir: str = "genomes",
    progress: bool = True,
) -> GenomeRecord:
    """Download ``<asm>_genomic.fna.gz``, ``<asm>_assembly_report.txt`` and ``md5checksums.txt``
    into ``<dataset_root>/<subdir>/<accession>/``, verifying both files against the checksums
    (``ChecksumError`` if a file is still wrong after a fresh download). Resumable and
    idempotent: valid existing files are kept. ``md5checksums.txt`` is always re-fetched."""
    if not row.ftp_path:
        raise ValueError(f"{row.assembly_accession}: no ftp_path")
    root = Path(dataset_root)
    dest_dir = root / subdir / row.assembly_accession
    urls = genome_file_urls(row.ftp_path)

    md5_path = dest_dir / "md5checksums.txt"
    download_file(urls["md5checksums"], md5_path, resume=False, progress=False)
    checksums = read_md5checksums(md5_path)

    files: dict[str, Path] = {}
    for kind in ("sequence", "assembly_report"):
        name = urls[kind].rsplit("/", 1)[1]
        if name not in checksums:
            raise ChecksumError(f"{name} is not listed in {urls['md5checksums']}")
        files[kind] = dest_dir / name
        _fetch_verified(
            urls[kind], files[kind], checksums[name], progress=progress and kind == "sequence"
        )

    seq = files["sequence"]
    rel = {k: p.relative_to(root).as_posix() for k, p in files.items()}
    return GenomeRecord(
        assembly_accession=row.assembly_accession,
        ncbi_taxid=row.taxid,
        species_taxid=row.species_taxid,
        organism_name=row.organism_name,
        assembly_name=row.asm_name,
        assembly_level=row.assembly_level,
        refseq_category=row.refseq_category,
        source="ncbi_refseq" if row.assembly_accession.startswith("GCF_") else "ncbi_genbank",
        release_date=row.seq_rel_date,
        genome_size=row.genome_size,
        genome_size_ungapped=row.genome_size_ungapped,
        gc_percent=row.gc_percent,
        scaffold_count=row.scaffold_count,
        contig_count=row.contig_count,
        scaffold_n50=row.scaffold_n50,
        contig_n50=row.contig_n50,
        ftp_path=ftp_https_url(row.ftp_path),
        sequence_file=rel["sequence"],
        sequence_md5=checksums[seq.name],
        sequence_bytes=seq.stat().st_size,
        n_sequences=count_fasta_records(seq),
        extra_files=[rel["assembly_report"], md5_path.relative_to(root).as_posix()],
        downloaded_at=datetime.now(UTC),
    )
