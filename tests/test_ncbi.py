"""Tests for datasets.ncbi. Network tests use the NCBI Datasets API and a 48.5 kb phage
assembly (Escherichia phage Lambda, GCF_000840245.1, ~15 KB gzipped); nothing large."""

from __future__ import annotations

import gzip
import hashlib
import socket
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from datasets import ncbi
from datasets.schema import AssemblyLevel, GenomeRecord

MAMMALS = Path(
    "/mnt/filesystem-s8/genes.jpg/datasets/_ncbi/assembly_summary_vertebrate_mammalian_refseq.txt"
)
LAMBDA = "GCF_000840245.1"
LAMBDA_FTP = (
    "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/840/245/GCF_000840245.1_ViralProj14204/"
)

# A trimmed, real Datasets v2 genome report (GET /genome/taxon/9689/dataset_report).
LION_REPORT = {
    "accession": "GCF_018350215.1",
    "current_accession": "GCF_018350215.1",
    "paired_accession": "GCA_018350215.1",
    "source_database": "SOURCE_DATABASE_REFSEQ",
    "organism": {
        "tax_id": 9689,
        "organism_name": "Panthera leo",
        "common_name": "lion",
        "infraspecific_names": {"isolate": "Ple1", "sex": "male"},
    },
    "assembly_info": {
        "assembly_level": "Chromosome",
        "assembly_status": "current",
        "paired_assembly": {"accession": "GCA_018350215.1", "refseq_genbank_are_different": True},
        "assembly_name": "P.leo_Ple1_pat1.1",
        "assembly_type": "haploid",
        "bioproject_accession": "PRJNA684340",
        "release_date": "2021-05-13",
        "submitter": "Texas A&M University",
        "refseq_category": "reference genome",
        "biosample": {"accession": "SAMN17054329"},
    },
    "assembly_stats": {
        "total_sequence_length": "2297552363",
        "total_ungapped_length": "2297542863",
        "number_of_contigs": 108,
        "contig_n50": 77781637,
        "number_of_scaffolds": 53,
        "scaffold_n50": 147402474,
        "gc_percent": 41.5,
    },
    "annotation_info": {
        "name": "NCBI Panthera leo Annotation Release 100",
        "provider": "NCBI RefSeq",
        "release_date": "2021-08-06",
        "stats": {"gene_counts": {"total": 32109, "protein_coding": 19491, "non_coding": 8948}},
    },
}


def _row(acc: str, category: str | None, level: str, date: str | None) -> ncbi.AssemblySummaryRow:
    return ncbi.AssemblySummaryRow(
        assembly_accession=acc,
        refseq_category=category,
        taxid=1,
        species_taxid=1,
        organism_name="x",
        assembly_level=level,
        seq_rel_date=date,
        asm_name=acc,
    )


@pytest.fixture(scope="module")
def mammal_rows() -> list[ncbi.AssemblySummaryRow]:
    if not MAMMALS.exists():
        pytest.skip(f"{MAMMALS} not available")
    return ncbi.load_assembly_summary(MAMMALS)


# --------------------------------------------------------------------------- offline


def test_load_assembly_summary_mammals(mammal_rows):
    assert len(mammal_rows) == 277
    assert len({r.species_taxid for r in mammal_rows}) == 270
    assert all(r.assembly_accession.startswith("GCF_") for r in mammal_rows)
    assert Counter(r.assembly_level for r in mammal_rows) == {
        "Chromosome": 187,
        "Scaffold": 79,
        "Contig": 7,
        "Complete Genome": 4,
    }
    assert sum(r.refseq_category is None for r in mammal_rows) == 8  # 'na' -> None
    assert all(r.refseq_category in (None, "reference genome") for r in mammal_rows)


def test_load_assembly_summary_typing(mammal_rows):
    platypus = next(r for r in mammal_rows if r.assembly_accession == "GCF_004115215.2")
    assert (platypus.taxid, platypus.species_taxid) == (9258, 9258)
    assert platypus.organism_name == "Ornithorhynchus anatinus"
    assert platypus.infraspecific_name is None and platypus.isolate == "Pmale09"
    assert platypus.genome_size == 1859264908 and isinstance(platypus.genome_size, int)
    assert platypus.genome_size_ungapped == 1844106806
    assert platypus.gc_percent == 46.0 and isinstance(platypus.gc_percent, float)
    assert (platypus.replicon_count, platypus.scaffold_count, platypus.contig_count) == (
        31,
        321,
        833,
    )
    assert platypus.total_gene_count == 30497
    assert platypus.pubmed_id == [8919867, 15008398, 33408411, 33910595, 33911273]
    assert platypus.seq_rel_date == "2020-11-06"
    assert platypus.assembly_level == AssemblyLevel.chromosome.value
    assert platypus.ftp_path.endswith("GCF_004115215.2_mOrnAna1.pri.v4/")
    assert platypus.contig_n50 is None  # not in assembly_summary files
    assert all(isinstance(r.gc_percent, float) for r in mammal_rows)
    assert all(r.seq_rel_date and len(r.seq_rel_date) == 10 for r in mammal_rows)


def test_load_assembly_summary_small_file(tmp_path):
    header = (
        "#assembly_accession\trefseq_category\ttaxid\tspecies_taxid\torganism_name\tassembly_level"
    )
    header += "\tseq_rel_date\tasm_name\tftp_path\tgenome_size\tgc_percent\tpubmed_id\tbogus_column"
    path = tmp_path / "assembly_summary.txt"
    path.write_text(
        "## comment\n"
        f"{header}\n"
        "GCF_1.1\tna\t10\t10\tFoo bar\tContig\t2020/11/06\tasm1\tftp://ftp.ncbi.nlm.nih.gov/x/GCF_1.1_asm1\t"
        "1234\t\tna\tignored\n"
        "GCF_2.1\trepresentative genome\t11\t10\tFoo baz\tScaffold\t2021-01-02\tasm 2\t"
        "https://ftp.ncbi.nlm.nih.gov/x/GCF_2.1_asm_2/\t5\t33.5\t1;2\tignored\n"
        # upper-case 'NA' is a real assembly name (328 current RefSeq rows), not a missing value
        "GCF_3.1\tna\t12\t12\tFoo qux\tContig\t2022-02-02\tNA\t"
        "https://ftp.ncbi.nlm.nih.gov/x/GCF_3.1_NA/\tna\tna\tna\tignored\n"
    )
    rows = ncbi.load_assembly_summary(path)
    assert [r.assembly_accession for r in rows] == ["GCF_1.1", "GCF_2.1", "GCF_3.1"]
    a, b, c = rows
    assert a.refseq_category is None and a.gc_percent is None and a.pubmed_id == []
    assert a.seq_rel_date == "2020-11-06" and a.genome_size == 1234
    assert b.refseq_category == "representative genome" and b.gc_percent == 33.5
    assert b.pubmed_id == [1, 2] and b.species_taxid == 10 and b.taxid == 11
    assert a.bioproject is None  # column absent from this file
    assert c.asm_name == "NA" and c.genome_size is None and c.refseq_category is None
    assert ncbi.construct_ftp_path(c.assembly_accession, c.asm_name).endswith("/GCF_3.1_NA")
    assert c.ftp_path.endswith("/GCF_3.1_NA/")


def test_load_assembly_summary_bad_row_is_located(tmp_path):
    path = tmp_path / "assembly_summary.txt"
    path.write_text(
        "#assembly_accession\ttaxid\tspecies_taxid\torganism_name\tassembly_level\tasm_name\n"
        "GCF_1.1\t10\t10\tFoo\tContig\tasm1\n"
        "GCF_2.1\tnot-a-taxid\t10\tFoo\tContig\tasm2\n"
    )
    with pytest.raises(ValueError, match=r"assembly_summary\.txt:3: bad row 'GCF_2\.1'"):
        ncbi.load_assembly_summary(path)


def test_construct_ftp_path_matches_summary(mammal_rows):
    mismatches = {
        r.assembly_accession
        for r in mammal_rows
        if ncbi.construct_ftp_path(r.assembly_accession, r.asm_name) != r.ftp_path.rstrip("/")
    }
    # NCBI renamed this assembly (v1 -> v2) without moving its directory; the API's FTP link
    # is authoritative, the constructed path is only a fallback.
    assert mismatches == {"GCF_033118175.1"}
    assert ncbi.construct_ftp_path("GCF_028564815.1", "mEubGla1.1.hap2.+ XY").endswith(
        "/GCF/028/564/815/GCF_028564815.1_mEubGla1.1.hap2._XY"
    )


def test_ftp_https_url_and_file_urls():
    ftp = "ftp://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/840/245/GCF_000840245.1_ViralProj14204/"
    assert ncbi.ftp_https_url(ftp) == LAMBDA_FTP.rstrip("/")
    assert ncbi.ftp_https_url(LAMBDA_FTP) == LAMBDA_FTP.rstrip("/")
    with pytest.raises(ValueError):
        ncbi.ftp_https_url("/local/path")
    urls = ncbi.genome_file_urls(ftp)
    assert urls == {
        "sequence": LAMBDA_FTP + "GCF_000840245.1_ViralProj14204_genomic.fna.gz",
        "assembly_report": LAMBDA_FTP + "GCF_000840245.1_ViralProj14204_assembly_report.txt",
        "md5checksums": LAMBDA_FTP + "md5checksums.txt",
    }


def test_pick_best_assembly():
    ref_contig = _row("GCF_1.1", "reference genome", "Contig", "2010-01-01")
    rep_complete = _row("GCF_2.1", "representative genome", "Complete Genome", "2024-01-01")
    none_complete = _row("GCF_3.1", None, "Complete Genome", "2025-01-01")
    assert ncbi.pick_best_assembly([none_complete, rep_complete, ref_contig]) is ref_contig
    assert ncbi.pick_best_assembly([none_complete, rep_complete]) is rep_complete

    scaffold_new = _row("GCF_4.1", None, "Scaffold", "2025-06-01")
    chromosome_old = _row("GCF_5.1", None, "Chromosome", "2015-06-01")
    assert ncbi.pick_best_assembly([scaffold_new, chromosome_old]) is chromosome_old

    old = _row("GCF_6.1", None, "Chromosome", "2020-03-10")
    new = _row("GCF_7.1", None, "Chromosome", "2025-02-27")
    undated = _row("GCF_8.1", None, "Chromosome", None)
    assert ncbi.pick_best_assembly([undated, old, new]) is new
    assert ncbi.pick_best_assembly([undated, old]) is old
    assert ncbi.pick_best_assembly([undated]) is undated
    with pytest.raises(ValueError):
        ncbi.pick_best_assembly([])


def test_row_from_report_offline():
    row = ncbi._row_from_report(LION_REPORT, species_taxid=9689, ftp_path=None)
    assert row.assembly_accession == "GCF_018350215.1"
    assert (row.taxid, row.species_taxid, row.organism_name) == (9689, 9689, "Panthera leo")
    assert row.refseq_category == "reference genome" and row.assembly_level == "Chromosome"
    assert row.genome_size == 2297552363 and row.genome_size_ungapped == 2297542863
    assert (row.contig_n50, row.scaffold_n50) == (77781637, 147402474)
    assert (row.scaffold_count, row.contig_count, row.gc_percent) == (53, 108, 41.5)
    assert row.seq_rel_date == "2021-05-13" and row.version_status == "latest"
    assert row.isolate == "Ple1" and row.infraspecific_name is None
    assert row.paired_asm_comp == "different" and row.gbrs_paired_asm == "GCA_018350215.1"
    assert row.bioproject == "PRJNA684340" and row.biosample == "SAMN17054329"
    assert row.total_gene_count == 32109 and row.annotation_provider == "NCBI RefSeq"
    assert row.ftp_path == (
        "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/018/350/215/GCF_018350215.1_P.leo_Ple1_pat1.1"
    )
    linked = ncbi._row_from_report(LION_REPORT, species_taxid=9689, ftp_path="https://x/y/")
    assert linked.ftp_path == "https://x/y/"


def test_list_refseq_assemblies_pagination_offline(monkeypatch):
    """Three report pages (page_token re-sent with the filters) and one batched links call."""
    calls: list[tuple[str, dict]] = []
    pages = {None: 1, "tok2": 2, "tok3": 3}

    def fake_api_get(path: str, params: dict | None = None) -> dict:
        calls.append((path, dict(params or {})))
        if path.endswith("/links"):
            accessions = path.split("/")[2].split(",")
            return {
                "assembly_links": [
                    {
                        "accession": a,
                        "assembly_link_type": "FTP_LINK",
                        "resource_link": f"ftp://x/{a}/",
                    }
                    for a in accessions
                ]
            }
        page = pages[params.get("page_token")]
        data = {
            "reports": [{**LION_REPORT, "accession": f"GCF_{page}{i}.1"} for i in range(2)],
            "total_count": 6,
        }
        if page < 3:
            data["next_page_token"] = f"tok{page + 1}"
        return data

    monkeypatch.setattr(ncbi, "_api_get", fake_api_get)
    rows = ncbi.list_refseq_assemblies(9689)
    accessions = ["GCF_10.1", "GCF_11.1", "GCF_20.1", "GCF_21.1", "GCF_30.1", "GCF_31.1"]
    assert [r.assembly_accession for r in rows] == accessions
    assert [r.ftp_path for r in rows] == [f"ftp://x/{a}/" for a in accessions]
    report_calls = [q for p, q in calls if p == "genome/taxon/9689/dataset_report"]
    assert [q.get("page_token") for q in report_calls] == [None, "tok2", "tok3"]
    assert all(
        q["filters.assembly_source"] == "refseq"
        and q["filters.assembly_version"] == "current"
        and q["page_size"] == 100
        for q in report_calls
    )
    assert [p for p, _ in calls if p.endswith("/links")] == [
        f"genome/accession/{','.join(accessions)}/links"
    ]


def test_read_md5checksums(tmp_path):
    p = tmp_path / "md5checksums.txt"
    p.write_text("AbC123  ./GCF_1_genomic.fna.gz\n0def  ./sub/GCF_1_assembly_report.txt\n\n")
    assert ncbi.read_md5checksums(p) == {
        "GCF_1_genomic.fna.gz": "abc123",
        "GCF_1_assembly_report.txt": "0def",
    }


def test_count_fasta_records(tmp_path, monkeypatch):
    fasta = b">chr1 a>b\nACGT\nAC\n>chr2\nGG\n\n>chr3 x\nT\n"
    gz = tmp_path / "x.fna.gz"
    gz.write_bytes(gzip.compress(fasta))
    assert ncbi.count_fasta_records(gz) == 3
    monkeypatch.setattr(ncbi, "_CHUNK", 1)  # every byte is a chunk boundary
    assert ncbi.count_fasta_records(gz) == 3
    plain = tmp_path / "x.fna"
    plain.write_bytes(fasta)
    assert ncbi.count_fasta_records(plain) == 3
    gz.write_bytes(gzip.compress(b""))
    assert ncbi.count_fasta_records(gz) == 0


# --------------------------------------------------------------------------- offline HTTP

BLOB = bytes(range(256)) * 80  # 20480 bytes
BLOB_MD5 = hashlib.md5(BLOB).hexdigest()
DROP_AFTER = 7000


class _Server(ThreadingHTTPServer):
    """Serves ``BLOB`` as ``/<mode>/blob``; ``mode`` selects a misbehaviour (see handler)."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.log: list[dict] = []
        self.state: dict[str, int] = {}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.do_GET()

    def do_GET(self) -> None:
        mode, _, name = self.path.lstrip("/").partition("/")
        rng = self.headers.get("Range")
        self.server.log.append({"mode": mode, "name": name, "range": rng})
        state = self.server.state
        if mode == "html":  # maintenance page with HTTP 200
            return self._send(200, b"<html>NCBI is down</html>", {"Content-Type": "text/html"})
        if name != "blob":
            return self._send(404, b"")
        if mode == "retry_after" and not state.get("hit"):
            state["hit"] = 1
            return self._send(429, b"", {"Retry-After": "2"})
        start = int(rng.split("=")[1].split("-")[0]) if rng and mode != "ignore_range" else 0
        size = len(BLOB)
        if start >= size:
            return self._send(416, b"")  # like NCBI: no Content-Range
        if mode == "wrong206" and start:  # buggy server: 206 but the body starts at 0
            return self._send(206, BLOB, {"Content-Range": f"bytes 0-{size - 1}/{size}"})
        if mode == "shifted206" and start:  # 206 from a different offset than asked
            start -= 1
        body = BLOB[start:]
        status = 206 if start else 200
        headers = {"Content-Range": f"bytes {start}-{size - 1}/{size}"} if start else {}
        if mode == "nolength":  # chunked transfer encoding, no Content-Length
            self.send_response(status)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
            return
        if mode == "drop" and not state.get("dropped"):  # truncate the first response
            state["dropped"] = 1
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:DROP_AFTER])
            self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        self._send(status, body, headers)


@pytest.fixture
def server(monkeypatch):
    srv = _Server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    sleeps: list[float] = []
    monkeypatch.setattr(ncbi.time, "sleep", sleeps.append)  # no real backoff in tests
    srv.sleeps = sleeps
    yield srv
    srv.shutdown()
    srv.server_close()


def _ranges(srv: _Server) -> list[str | None]:
    return [e["range"] for e in srv.log]


def test_download_file_plain_and_404(server, tmp_path):
    dest = tmp_path / "blob"
    assert ncbi.download_file(f"{server.url}/ok/blob", dest, progress=False) == len(BLOB)
    assert dest.read_bytes() == BLOB and not dest.with_name("blob.part").exists()
    with pytest.raises(ncbi.NCBIError, match="404"):
        ncbi.download_file(f"{server.url}/ok/missing", tmp_path / "m", progress=False)
    assert not list(tmp_path.glob("m*")) and server.sleeps == []  # 4xx: no retries


def test_download_file_resume_after_drop(server, tmp_path, monkeypatch):
    monkeypatch.setattr(ncbi, "_CHUNK", 1024)  # flush partial bodies to the .part
    dest = tmp_path / "blob"
    assert ncbi.download_file(f"{server.url}/drop/blob", dest, progress=False) == len(BLOB)
    assert dest.read_bytes() == BLOB
    first, second = _ranges(server)
    assert first is None and second.startswith("bytes=")
    resumed_at = int(second.removeprefix("bytes=").rstrip("-"))
    assert 0 < resumed_at <= DROP_AFTER
    assert server.sleeps == [1.0]


def test_download_file_server_ignores_range(server, tmp_path):
    dest = tmp_path / "blob"
    dest.with_name("blob.part").write_bytes(BLOB[:5000])
    ncbi.download_file(f"{server.url}/ignore_range/blob", dest, progress=False)
    assert _ranges(server) == ["bytes=5000-"] and dest.read_bytes() == BLOB


def test_download_file_wrong_206_offsets(server, tmp_path):
    dest = tmp_path / "blob"
    dest.with_name("blob.part").write_bytes(BLOB[:5000])
    ncbi.download_file(f"{server.url}/wrong206/blob", dest, progress=False)
    assert _ranges(server) == ["bytes=5000-"] and dest.read_bytes() == BLOB  # no doubled prefix

    server.log.clear()
    dest.unlink()
    dest.with_name("blob.part").write_bytes(BLOB[:5000])
    ncbi.download_file(f"{server.url}/shifted206/blob", dest, progress=False)
    assert _ranges(server) == ["bytes=5000-", None] and dest.read_bytes() == BLOB


def test_download_file_oversized_part_restarts(server, tmp_path):
    dest = tmp_path / "blob"
    part = dest.with_name("blob.part")
    part.write_bytes(BLOB + b"junk" * 100)
    ncbi.download_file(f"{server.url}/ok/blob", dest, progress=False)
    assert _ranges(server) == ["bytes=20880-", None]  # 416, then from scratch
    assert dest.read_bytes() == BLOB and not part.exists()

    server.log.clear()
    dest.unlink()
    part.write_bytes(BLOB + b"junk")
    ncbi._fetch_verified(f"{server.url}/ok/blob", dest, BLOB_MD5, progress=False)
    assert dest.read_bytes() == BLOB and not part.exists()


def test_fetch_verified_reuses_and_cleans(server, tmp_path):
    dest = tmp_path / "blob"
    part = dest.with_name("blob.part")
    dest.write_bytes(BLOB)
    part.write_bytes(b"stale")
    ncbi._fetch_verified(f"{server.url}/ok/blob", dest, BLOB_MD5, progress=False)
    assert server.log == [] and dest.read_bytes() == BLOB and not part.exists()

    dest.write_bytes(b"corrupt")
    part.write_bytes(b"stale")
    ncbi._fetch_verified(f"{server.url}/ok/blob", dest, BLOB_MD5, progress=False)
    assert dest.read_bytes() == BLOB and not part.exists()
    with pytest.raises(ncbi.ChecksumError):
        ncbi._fetch_verified(f"{server.url}/ok/blob", dest, "0" * 32, progress=False)
    assert not dest.exists() and not part.exists()


def test_download_file_retry_after_and_no_length(server, tmp_path):
    dest = tmp_path / "blob"
    ncbi.download_file(f"{server.url}/retry_after/blob", dest, progress=False)
    assert dest.read_bytes() == BLOB and max(server.sleeps) >= 2.0
    ncbi.download_file(f"{server.url}/nolength/blob", tmp_path / "b2", progress=False)
    assert (tmp_path / "b2").read_bytes() == BLOB


def test_api_errors_are_ncbi_errors(server, monkeypatch):
    monkeypatch.setattr(ncbi, "API_URL", f"{server.url}/html")
    with pytest.raises(ncbi.NCBIError, match="non-JSON"):
        ncbi.resolve_taxon("Panthera leo")
    with pytest.raises(ncbi.NCBIError, match="non-JSON"):
        ncbi.list_refseq_assemblies(9689)
    monkeypatch.setattr(ncbi, "API_URL", f"{server.url}/ok")  # every API path 404s here
    with pytest.raises(ncbi.NCBIError, match="404"):
        ncbi.get_assembly_report("GCF_000840245.1")
    with pytest.raises(ncbi.NCBIError, match="404"):
        ncbi.species_taxids([9615])


# --------------------------------------------------------------------------- network


@pytest.mark.network
def test_resolve_taxon_lion():
    lion = ncbi.resolve_taxon("Panthera leo")
    assert lion is not None
    assert lion.taxid == 9689 and lion.scientific_name == "Panthera leo"
    assert lion.rank == "species" and lion.species_taxid == 9689
    assert lion.common_name == "lion" and lion.is_synonym is False and lion.n_matches == 1
    assert 40674 in lion.lineage_taxids
    assert lion.ancestor("class") == ncbi.LineageNode(40674, "Mammalia", "class")
    assert lion.ancestor("order").name == "Carnivora" and lion.ancestor("family").name == "Felidae"
    assert lion.lineage[0].taxid == 1 and lion.lineage[-1] == (9689, "Panthera leo", "species")
    assert lion.lineage_taxids.index(40674) < lion.lineage_taxids.index(33554)


@pytest.mark.network
def test_resolve_taxon_synonym_and_unknown():
    syn = ncbi.resolve_taxon("Felis leo")  # basionym of Panthera leo
    assert syn is not None
    assert syn.taxid == 9689 and syn.scientific_name == "Panthera leo"
    assert syn.is_synonym is True and syn.query == "Felis leo"
    assert ncbi.resolve_taxon("panthera leo").is_synonym is False  # case-insensitive match
    assert ncbi.resolve_taxon("Nonexistus bogusii") is None


@pytest.mark.network
def test_resolve_taxon_awkward_names():
    flu = ncbi.resolve_taxon("Influenza A virus (A/Puerto Rico/8/1934(H1N1))")  # '/' in name
    assert flu is not None and flu.taxid == 211044 and flu.rank is None
    # a ',' must not split the name into several lookups
    assert ncbi.resolve_taxon("Panthera leo, 1758") is None
    assert ncbi.resolve_taxon("Canis lupus familiaris, dog") is None


@pytest.mark.network
def test_get_taxon_and_species_taxids():
    dog = ncbi.get_taxon(9615)  # Canis lupus familiaris
    assert dog is not None and dog.rank == "subspecies" and dog.species_taxid == 9612
    assert dog.is_synonym is False and dog.query == "9615"
    assert ncbi.get_taxon(999999999) is None
    assert ncbi.species_taxids([9615, 2681611, 9689, 33554]) == {
        9615: 9612,
        2681611: 10710,  # Escherichia phage Lambda ('no rank') -> Lambdavirus lambda
        9689: 9689,
    }
    # unknown ids are skipped; merged ids are keyed by the id asked for
    assert ncbi.species_taxids([9615, 999999999, 11103]) == {9615: 9612, 11103: 3052230}


@pytest.mark.network
def test_list_refseq_assemblies_and_pick():
    rows = ncbi.list_refseq_assemblies(9612)  # Canis lupus: wolf, dog breeds, dingo
    assert rows and all(r.species_taxid == 9612 for r in rows)
    assert all(r.assembly_accession.startswith("GCF_") for r in rows)
    assert all(r.ftp_path.startswith("https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/") for r in rows)
    assert all(r.genome_size and r.genome_size > 1_000_000_000 for r in rows)
    assert all(r.contig_n50 and r.scaffold_n50 and r.gc_percent for r in rows)
    assert all(r.version_status == "latest" for r in rows)
    best = ncbi.pick_best_assembly(rows)
    assert best.refseq_category == "reference genome" and best.assembly_level == "Chromosome"
    assert ncbi.list_refseq_assemblies(999999999) == []


@pytest.mark.network
def test_get_assembly_report():
    row = ncbi.get_assembly_report(LAMBDA)
    assert row is not None
    assert (row.taxid, row.species_taxid) == (2681611, 10710)
    assert row.organism_name == "Escherichia phage Lambda"
    assert row.assembly_level == "Complete Genome" and row.genome_size == 48502
    assert row.ftp_path == LAMBDA_FTP and row.asm_name == "ViralProj14204"
    assert ncbi.get_assembly_report("GCF_000000000.1") is None


@pytest.mark.network
def test_download_genome_lambda(tmp_path, monkeypatch):
    row = ncbi.get_assembly_report(LAMBDA)
    assert row is not None and row.genome_size < 5_000_000

    fetched: list[tuple[str, dict | None]] = []
    real_request = ncbi._client().session.request

    def spy_request(method, url, **kw):
        fetched.append((url.rsplit("/", 1)[1], kw.get("headers")))
        return real_request(method, url, **kw)

    monkeypatch.setattr(ncbi._client().session, "request", spy_request)

    rec = ncbi.download_genome(row, tmp_path)
    assert isinstance(rec, GenomeRecord)
    assert rec.sequence_file == f"genomes/{LAMBDA}/GCF_000840245.1_ViralProj14204_genomic.fna.gz"
    seq = tmp_path / rec.sequence_file
    assert seq.exists() and rec.sequence_bytes == seq.stat().st_size > 0
    assert ncbi.md5_file(seq) == rec.sequence_md5 == "7e74fba2c9e1107f228dbb12bada5c1c"
    assert rec.n_sequences == 1
    assert rec.extra_files == [
        f"genomes/{LAMBDA}/GCF_000840245.1_ViralProj14204_assembly_report.txt",
        f"genomes/{LAMBDA}/md5checksums.txt",
    ]
    assert all((tmp_path / f).exists() for f in rec.extra_files)
    assert not list((tmp_path / "genomes" / LAMBDA).glob("*.part"))
    assert (rec.ncbi_taxid, rec.species_taxid) == (2681611, 10710)
    assert rec.assembly_level == "Complete Genome" and rec.source == "ncbi_refseq"
    assert rec.genome_size == 48502 and rec.contig_n50 == 48502 and rec.gc_percent == 50.0
    assert rec.ftp_path == LAMBDA_FTP.rstrip("/") and rec.release_date == "1993-04-28"
    assert rec.downloaded_at.tzinfo is not None
    assert [name for name, _ in fetched] == [
        "md5checksums.txt",
        seq.name,
        "GCF_000840245.1_ViralProj14204_assembly_report.txt",
    ]

    # idempotent: only md5checksums.txt is re-fetched, the sequence file is untouched
    fetched.clear()
    mtime = seq.stat().st_mtime_ns
    rec2 = ncbi.download_genome(row, tmp_path)
    assert [name for name, _ in fetched] == ["md5checksums.txt"]
    assert seq.stat().st_mtime_ns == mtime
    assert rec2.model_dump(exclude={"downloaded_at"}) == rec.model_dump(exclude={"downloaded_at"})

    # a corrupted file is detected and re-downloaded
    good = seq.read_bytes()
    seq.write_bytes(b"garbage" + good[7:])
    fetched.clear()
    rec3 = ncbi.download_genome(row, tmp_path)
    assert seq.name in [name for name, _ in fetched]
    assert seq.read_bytes() == good and rec3.sequence_md5 == rec.sequence_md5

    # a partial .part is resumed with a Range request and completes to the right checksum
    seq.unlink()
    part = seq.with_name(seq.name + ".part")
    part.write_bytes(good[:5000])
    fetched.clear()
    ncbi.download_genome(row, tmp_path)
    assert [h.get("Range") for name, h in fetched if name == seq.name] == ["bytes=5000-"]
    assert seq.read_bytes() == good and not part.exists()


@pytest.mark.network
def test_download_genome_checksum_error(tmp_path, monkeypatch):
    row = ncbi.get_assembly_report(LAMBDA)
    real = ncbi.read_md5checksums

    def wrong(path):
        sums = real(path)
        return {k: ("0" * 32 if k.endswith(".fna.gz") else v) for k, v in sums.items()}

    monkeypatch.setattr(ncbi, "read_md5checksums", wrong)
    with pytest.raises(ncbi.ChecksumError):
        ncbi.download_genome(row, tmp_path, progress=False)
    genome_dir = tmp_path / "genomes" / LAMBDA
    assert not list(genome_dir.glob("*.fna.gz*"))  # bad file (and .part) removed
    assert (genome_dir / "md5checksums.txt").exists()


@pytest.mark.network
def test_download_file_404_fails_fast(tmp_path):
    with pytest.raises(ncbi.NCBIError, match="404"):
        ncbi.download_file(LAMBDA_FTP + "does_not_exist.txt", tmp_path / "x", progress=False)
    assert not (tmp_path / "x").exists() and not (tmp_path / "x.part").exists()
