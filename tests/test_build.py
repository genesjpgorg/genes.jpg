"""Offline tests for ``datasets.build``: NCBI functions are monkeypatched with fixtures, images
come from a fake ``ImageSource`` whose URLs point at a local HTTP server."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import threading
from collections import Counter
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError
from typer.testing import CliRunner

from datasets import build, manifest, ncbi
from datasets.build import BuildError, load_config
from datasets.cli import app
from datasets.ncbi import AssemblySummaryRow, LineageNode, TaxonInfo
from datasets.schema import GenomeRecord, SourceInfo
from datasets.sources.base import ImageCandidate

# --------------------------------------------------------------------------- fake NCBI


def _lineage(*nodes: tuple[int, str, str | None]) -> list[LineageNode]:
    return [LineageNode(*n) for n in nodes]


_BASE = ((1, "root", None), (33208, "Metazoa", "kingdom"), (7711, "Chordata", "phylum"))
_MAMMALIA = (*_BASE, (40674, "Mammalia", "class"))
VULPES = TaxonInfo(
    taxid=9627,
    scientific_name="Vulpes vulpes",
    rank="species",
    common_name="red fox",
    lineage=_lineage(
        *_MAMMALIA,
        (33554, "Carnivora", "order"),
        (9608, "Canidae", "family"),
        (9625, "Vulpes", "genus"),
        (9627, "Vulpes vulpes", "species"),
    ),
    query="Vulpes vulpes",
)
CASTOR = TaxonInfo(
    taxid=51338,
    scientific_name="Castor canadensis",
    rank="species",
    common_name="American beaver",
    lineage=_lineage(
        *_MAMMALIA,
        (9989, "Rodentia", "order"),
        (29132, "Castoridae", "family"),
        (10184, "Castor", "genus"),
        (51338, "Castor canadensis", "species"),
    ),
    query="Castor canadensis",
)
FULVA = TaxonInfo(
    taxid=494514,
    scientific_name="Vulpes vulpes fulva",
    rank="subspecies",
    lineage=[*VULPES.lineage, LineageNode(494514, "Vulpes vulpes fulva", "subspecies")],
    query="Vulpes vulpes fulva",
)
LEO = TaxonInfo(  # synonym query; no RefSeq assembly in the fake API
    taxid=9689,
    scientific_name="Panthera leo",
    rank="species",
    lineage=_lineage(*_MAMMALIA, (9689, "Panthera leo", "species")),
    query="Felis leo",
    is_synonym=True,
)
CARNIVORA = TaxonInfo(
    taxid=33554,
    scientific_name="Carnivora",
    rank="order",
    lineage=_lineage(*_MAMMALIA, (33554, "Carnivora", "order")),
    query="Carnivora",
)
TAXA = {t.query: t for t in (VULPES, CASTOR, FULVA, LEO, CARNIVORA)}


def _row(acc: str, taxon: TaxonInfo, asm: str, **kw) -> AssemblySummaryRow:
    fields = {
        "assembly_level": "Chromosome",
        "refseq_category": "reference genome",
        "seq_rel_date": "2025-03-05",
        "genome_size": 2_000_000,
        **kw,
    }
    return AssemblySummaryRow(
        assembly_accession=acc,
        taxid=taxon.taxid,
        species_taxid=taxon.taxid,
        organism_name=taxon.scientific_name,
        asm_name=asm,
        ftp_path=f"https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/000/000/{acc}_{asm}",
        **fields,
    )


ASSEMBLIES = {
    9627: [
        _row(
            "GCF_000000002.1", VULPES, "VulVulOld", refseq_category=None, assembly_level="Scaffold"
        ),
        _row("GCF_048418805.1", VULPES, "VulVul3"),
    ],
    51338: [_row("GCF_047511655.1", CASTOR, "mCasCan1.hap1v2")],
}

SNAPSHOT = (
    "## fake assembly_summary\n"
    "#assembly_accession\trefseq_category\ttaxid\tspecies_taxid\torganism_name\t"
    "version_status\tassembly_level\tseq_rel_date\tasm_name\tftp_path\n"
    "GCF_048418805.1\treference genome\t9627\t9627\tVulpes vulpes\tlatest\tChromosome\t"
    "2025/03/05\tVulVul3\thttps://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/048/418/805/x\n"
    # the snapshot still has an older beaver assembly -> cross-check warning expected
    "GCF_000000001.1\treference genome\t51338\t51338\tCastor canadensis\tlatest\tScaffold\t"
    "2017/01/01\tmCasCanOld\thttps://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/000/001/x\n"
)


class FakeNCBI:
    def __init__(self) -> None:
        self.genome_calls: Counter[str] = Counter()

    def resolve_taxon(self, name: str) -> TaxonInfo | None:
        return TAXA.get(" ".join(name.split()))

    def get_taxon(self, taxid: int) -> TaxonInfo | None:
        return next((t for t in TAXA.values() if t.taxid == taxid and t.rank == "species"), None)

    def list_refseq_assemblies(self, species_taxid: int) -> list[AssemblySummaryRow]:
        return list(ASSEMBLIES.get(species_taxid, []))

    def download_genome(
        self, row: AssemblySummaryRow, root, *, subdir: str = "genomes", progress: bool = True
    ) -> GenomeRecord:
        self.genome_calls[row.assembly_accession] += 1
        root = Path(root)
        folder = root / subdir / row.assembly_accession
        folder.mkdir(parents=True, exist_ok=True)
        base = f"{row.assembly_accession}_{row.asm_name}"
        seq = folder / f"{base}_genomic.fna.gz"
        if not seq.exists():  # idempotent like the real thing
            seq.write_bytes(
                gzip.compress(f">chr1 {row.organism_name}\nACGT\n>chr2\nGGCC\n".encode())
            )
        report = folder / f"{base}_assembly_report.txt"
        report.write_text("# fake assembly report\n")
        md5 = hashlib.md5(seq.read_bytes()).hexdigest()
        sums = folder / "md5checksums.txt"
        sums.write_text(f"{md5}  ./{seq.name}\n")
        return GenomeRecord(
            assembly_accession=row.assembly_accession,
            ncbi_taxid=row.taxid,
            species_taxid=row.species_taxid,
            organism_name=row.organism_name,
            assembly_name=row.asm_name,
            assembly_level=row.assembly_level,
            refseq_category=row.refseq_category,
            release_date=row.seq_rel_date,
            genome_size=row.genome_size,
            ftp_path=row.ftp_path or "",
            sequence_file=seq.relative_to(root).as_posix(),
            sequence_md5=md5,
            sequence_bytes=seq.stat().st_size,
            n_sequences=2,
            extra_files=[report.relative_to(root).as_posix(), sums.relative_to(root).as_posix()],
            downloaded_at=datetime.now(UTC),
        )


# --------------------------------------------------------------------------- image server


def make_jpeg(seed: int, size: tuple[int, int] = (160, 120)) -> bytes:
    im = Image.new("RGB", size, (seed * 37 % 256, seed * 59 % 256, seed * 83 % 256))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


class ImageServer:
    """Serves ``files[path]`` as JPEG, 404 otherwise; counts hits per path."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.hits: Counter[str] = Counter()
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                with server.lock:
                    server.hits[self.path] += 1
                body = server.files.get(self.path)
                status, ctype = (200, "image/jpeg") if body is not None else (404, "text/plain")
                body = body if body is not None else b"nope"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}{path}"

    @property
    def total_hits(self) -> int:
        return sum(self.hits.values())

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- fake source


class FakeSource:
    name = "fake"

    def __init__(self, catalog: dict[str, list[ImageCandidate]], **options) -> None:
        self.catalog = catalog
        self.options = options
        self.calls: list[dict] = []

    def source_info(self) -> SourceInfo:
        return SourceInfo(name="fake source", url="http://fake.test", revision="r1")

    def select(self, species, *, per_species: int, seed: int = 0, aliases=None):
        self.calls.append(
            {"species": list(species), "per_species": per_species, "seed": seed, "aliases": aliases}
        )
        out: dict[str, list[ImageCandidate]] = {}
        for name in species:
            names = [name, *(aliases or {}).get(name, [])]
            merged = {c.candidate_id: c for n in names for c in self.catalog.get(n, [])}
            out[name] = list(merged.values())[:per_species]
        return out

    def selection_params(self) -> dict[str, object]:
        return {"fake": True, **self.options}


class NoAliasSource(FakeSource):
    def select(self, species, *, per_species: int, seed: int = 0):  # protocol minimum
        return super().select(species, per_species=per_species, seed=seed)


class CtorAliasSource(FakeSource):
    """Aliases as a constructor argument kept in ``.aliases`` (like TreeOfLife200MSource)."""

    def __init__(self, catalog, *, aliases=None, revision: str = "", **options) -> None:
        super().__init__(catalog, **options)
        self.aliases = {k: list(v) for k, v in (aliases or {}).items()}
        self.revision = revision

    def select(self, species, *, per_species: int, seed: int = 0):
        return super().select(species, per_species=per_species, seed=seed, aliases=self.aliases)


def candidate(
    server: ImageServer, cid: str, path: str, label: str, *fallback: str
) -> ImageCandidate:
    return ImageCandidate(
        candidate_id=cid,
        species_query=label,
        original_label=label,
        source_dataset="fake",
        source_provider="gbif",
        source_id=cid.upper(),
        observation_id=f"obs-{cid}",
        source_url=server.url(path),
        fallback_urls=[server.url(p) for p in fallback],
        image_type="citizen_science",
        source_image_type="Citizen Science",
        publisher="iNaturalist.org",
        license="cc-by-4.0" if cid[0] == "v" else "cc-0-1.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        rights_holder="someone",
    )


# --------------------------------------------------------------------------- fixtures


class Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.ncbi = FakeNCBI()
        for name in ("resolve_taxon", "get_taxon", "list_refseq_assemblies", "download_genome"):
            monkeypatch.setattr(ncbi, name, getattr(self.ncbi, name))
        files = {
            f"/{n}.jpg": make_jpeg(i)
            for i, n in enumerate(["v1", "v3", "v4", "v5", "c1b", "c2", "c3", "c4"])
        }
        self.server = ImageServer(files)
        s = self.server
        self.catalog = {
            # v2 is dead (404) -> replaced by v4 from the oversample
            "Vulpes vulpes": [
                candidate(s, "v1", "/v1.jpg", "Vulpes vulpes"),
                candidate(s, "v2", "/v2.jpg", "Vulpes vulpes"),
                candidate(s, "v3", "/v3.jpg", "Vulpes vulpes"),
                candidate(s, "v4", "/v4.jpg", "Vulpes vulpes"),
                candidate(s, "v5", "/v5.jpg", "Vulpes vulpes"),
            ],
            # c1's primary URL is dead, its fallback works -> c4 is never needed
            "Castor canadensis": [
                candidate(s, "c1", "/c1.jpg", "Castor canadensis", "/c1b.jpg"),
                candidate(s, "c2", "/c2.jpg", "Castor canadensis"),
                candidate(s, "c3", "/c3.jpg", "Castor canadensis"),
                candidate(s, "c4", "/c4.jpg", "Castor canadensis"),
            ],
        }
        self.source = FakeSource(self.catalog, licenses="cc_only")
        monkeypatch.setitem(build.SOURCES, "fake", lambda cfg, aliases=None: self.source)
        monkeypatch.setattr(build, "PER_HOST_RPS", 1000.0)  # keep the local server fast
        self.snapshot = tmp_path / "assembly_summary.txt"
        self.snapshot.write_text(SNAPSHOT)

    def write_config(self, species=("Vulpes vulpes", "Castor canadensis"), **overrides) -> Path:
        cfg = {
            "name": "fake-smoke",
            "version": "0.1.0",
            "description": "two fake mammals",
            "root": str(self.tmp_path / "ds"),
            "species": list(species),
            "genomes": {"source": "ncbi_refseq", "assembly_summary_snapshot": str(self.snapshot)},
            "images": {
                "source": "fake",
                "source_revision": "r1",
                "per_species": 3,
                "seed": 0,
                "image_types": ["Citizen Science"],
                "exclude_basis_of_record": ["FOSSIL_SPECIMEN"],
                "licenses": "cc_only",
                "max_per_observation": 1,
                "inat_size_variant": "large",
                "min_side": 64,
            },
        }
        for key, value in overrides.items():
            section, _, leaf = key.partition("__")
            if leaf:
                cfg[section][leaf] = value
            else:
                cfg[section] = value
        path = self.tmp_path / "config.json"
        path.write_text(json.dumps(cfg, indent=2))
        return path

    def build(self, config_path: Path | None = None, **kw) -> build.BuildReport:
        cfg = load_config(config_path or self.write_config())
        return build.build(cfg, progress=False, **kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.server.close()


# --------------------------------------------------------------------------- config


def test_load_config_real_smoke_config():
    path = build.REPO_ROOT / "configs" / "tol200m-mammals-smoke.json"
    cfg = load_config(path)
    assert cfg.root == build.REPO_ROOT / "data" / "datasets" / "tol200m-mammals-smoke"
    assert cfg.genomes.assembly_summary_snapshot.is_absolute()
    assert cfg.images.source == "treeoflife-200m" and cfg.images.licenses == "cc_only"
    assert cfg.images.per_species == 50 and cfg.images.inat_size_variant == "large"
    assert len(cfg.species) == 5 and cfg.config_path == path.resolve()
    assert build._command(cfg) == "genes-datasets build configs/tol200m-mammals-smoke.json"


def test_load_config_paths_and_override(tmp_path):
    data = {
        "name": "x",
        "version": "1",
        "root": "data/datasets/x",
        "species": ["Vulpes vulpes"],
        "genomes": {"assembly_summary_snapshot": "snap.txt"},
        "images": {"source": "fake", "per_species": 2, "extra_option": 7},
    }
    path = tmp_path / "c.json"
    path.write_text(json.dumps(data))
    cfg = load_config(path, repo_root=tmp_path)
    assert cfg.root == tmp_path / "data" / "datasets" / "x"
    assert cfg.genomes.assembly_summary_snapshot == tmp_path / "snap.txt"
    assert cfg.images.model_dump()["extra_option"] == 7  # source-specific keys pass through
    assert cfg.images.licenses == "cc_only" and cfg.images.min_side == 224  # defaults
    assert "config_path" not in cfg.model_dump()

    cfg = load_config(path, root_override="/abs/elsewhere", repo_root=tmp_path)
    assert cfg.root == Path("/abs/elsewhere")
    assert build._command(cfg) == f"genes-datasets build {path} --root-override /abs/elsewhere"


def test_load_config_relative_root_override_is_cwd_relative(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    data = {
        "name": "x",
        "version": "1",
        "root": "r",
        "species": ["Vulpes vulpes"],
        "images": {"source": "fake", "per_species": 1},
    }
    path.write_text(json.dumps(data))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    cfg = load_config(path, root_override="scratch/out", repo_root=tmp_path / "repo")
    assert cfg.root == cwd / "scratch" / "out"  # not repo-relative
    assert build._command(cfg).endswith(f"--root-override {cwd / 'scratch' / 'out'}")
    assert load_config(path, repo_root=tmp_path / "repo").root == tmp_path / "repo" / "r"


@pytest.mark.parametrize(
    "patch",
    [
        {"images": {"source": "fake", "per_species": 2, "licenses": "cc_by_only"}},
        {"images": {"source": "fake", "per_species": 2, "inat_size_variant": "huge"}},
        {"images": {"source": "fake", "per_species": 0}},
        {"images": {"source": "fake"}},
        {"species": []},
        {"species": ["Vulpes vulpes", " Vulpes  vulpes "]},
        {"unexpected": 1},
        {"genomes": {"source": "ensembl"}},
    ],
)
def test_load_config_rejects_bad_values(tmp_path, patch):
    data = {
        "name": "x",
        "version": "1",
        "root": "r",
        "species": ["Vulpes vulpes"],
        "images": {"source": "fake", "per_species": 2},
        **patch,
    }
    path = tmp_path / "c.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValidationError):
        load_config(path, repo_root=tmp_path)


def test_load_config_not_an_object(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("[1, 2]")
    with pytest.raises(TypeError, match="JSON object"):
        load_config(path)


def test_construct_source_passes_matching_keys():
    cfg = build.ImagesConfig(source="x", per_species=5, seed=3, licenses="any", min_side=10, foo=1)

    class Narrow:
        def __init__(self, licenses, min_side=0):
            self.args = (licenses, min_side)

    class Wide:
        def __init__(self, **kw):
            self.kw = kw

    class WithAliases:
        def __init__(self, *, revision: str, aliases=None, licenses="cc_only"):
            self.kw = {"revision": revision, "aliases": aliases, "licenses": licenses}

    # a key the constructor cannot take (and no **kwargs) is a config error, not a silent drop
    with pytest.raises(BuildError, match=r"Narrow does not take .*\['foo'\]"):
        build.construct_source(Narrow, cfg)
    narrow_cfg = build.ImagesConfig(source="x", per_species=5, seed=3, licenses="any", min_side=10)
    assert build.construct_source(Narrow, narrow_cfg).args == ("any", 10)
    kw = build.construct_source(Wide, cfg).kw
    assert kw["foo"] == 1 and kw["licenses"] == "any"
    assert not {"source", "per_species", "seed"} & set(kw)
    built = build.construct_source(WithAliases, narrow_cfg, revision="abc", aliases={"a": ["b"]})
    assert built.kw == {"revision": "abc", "aliases": {"a": ["b"]}, "licenses": "any"}


def test_construct_source_leaves_unset_keys_to_source_defaults():
    """ImagesConfig's own defaults are never forwarded: a config that omits image_types /
    exclude_basis_of_record must get the source's (stricter) defaults."""

    class Strict:
        def __init__(
            self,
            *,
            revision,
            image_types=("Citizen Science",),
            exclude_basis_of_record=("FOSSIL_SPECIMEN", "PRESERVED_SPECIMEN"),
            licenses="cc_only",
            max_per_observation=1,
            min_side=0,
        ):
            self.kw = {
                "revision": revision,
                "image_types": image_types,
                "exclude_basis_of_record": exclude_basis_of_record,
                "licenses": licenses,
                "max_per_observation": max_per_observation,
                "min_side": min_side,
            }

    minimal = build.ImagesConfig(source="x", source_revision="r", per_species=5)
    assert build.construct_source(Strict, minimal, revision="r").kw == {
        "revision": "r",
        "image_types": ("Citizen Science",),
        "exclude_basis_of_record": ("FOSSIL_SPECIMEN", "PRESERVED_SPECIMEN"),
        "licenses": "cc_only",
        "max_per_observation": 1,
        "min_side": 0,
    }
    # an explicit null / empty list is "set" and does reach the source
    explicit = build.ImagesConfig(
        source="x", source_revision="r", per_species=5, image_types=None, exclude_basis_of_record=[]
    )
    kw = build.construct_source(Strict, explicit, revision="r").kw
    assert kw["image_types"] is None and kw["exclude_basis_of_record"] == []
    # a typo'd key cannot silently leave the intended policy unapplied
    typo = build.ImagesConfig(source="x", source_revision="r", per_species=5, licences="permissive")
    with pytest.raises(BuildError, match=r"\['licences'\]"):
        build.construct_source(Strict, typo, revision="r")


def test_make_source_unknown():
    with pytest.raises(BuildError, match="unknown image source 'nope'"):
        build.make_source(build.ImagesConfig(source="nope", per_species=1))


def test_treeoflife_factory_maps_config_keys(monkeypatch):
    """The real source takes ``revision=`` and ``aliases=`` in its constructor; check the
    mapping against a stub so the test touches neither duckdb nor the shared disk."""
    import sys
    import types

    class Stub:
        def __init__(
            self,
            data_dir="d",
            *,
            revision,
            image_types=None,
            exclude_basis_of_record=(),
            licenses="cc_only",
            max_per_observation=1,
            inat_size_variant="large",
            aliases=None,
            exclude_hosts=(),
            catalog_path=None,
            provenance_path=None,
        ):
            self.kw = {
                "revision": revision,
                "image_types": image_types,
                "exclude_basis_of_record": exclude_basis_of_record,
                "licenses": licenses,
                "inat_size_variant": inat_size_variant,
                "aliases": aliases,
                "catalog_path": catalog_path,
            }

    module = types.ModuleType("datasets.sources.treeoflife200m")
    module.TreeOfLife200MSource = Stub
    monkeypatch.setitem(sys.modules, "datasets.sources.treeoflife200m", module)
    cfg = build.ImagesConfig(
        source="treeoflife-200m",
        source_revision="5f2dc493",
        per_species=50,
        image_types=["Citizen Science", None],
        licenses="permissive",
        inat_size_variant="medium",
        min_side=224,
        catalog_path="/x/catalog.parquet",
    )
    source = build.make_source(cfg, aliases={"Felis leo": ["Felis leo", "Panthera leo"]})
    assert source.kw == {
        "revision": "5f2dc493",
        "image_types": ["Citizen Science", None],
        "exclude_basis_of_record": (),  # not in the config -> the source's own default
        "licenses": "permissive",
        "inat_size_variant": "medium",
        "aliases": {"Felis leo": ["Felis leo", "Panthera leo"]},
        "catalog_path": "/x/catalog.parquet",
    }
    with pytest.raises(BuildError, match="source_revision .* required"):
        build.make_source(build.ImagesConfig(source="treeoflife-200m", per_species=1))


# --------------------------------------------------------------------------- species


def test_resolve_species_variants(env):
    taxon, warnings = build.resolve_species("Vulpes vulpes")
    assert taxon.taxid == 9627 and warnings == []

    taxon, warnings = build.resolve_species("Vulpes vulpes fulva")
    assert taxon.taxid == 9627 and "subspecies" in warnings[0] and "9627" in warnings[0]

    taxon, warnings = build.resolve_species("Felis leo")
    assert taxon.taxid == 9689 and "synonym" in warnings[0]

    with pytest.raises(BuildError, match="does not know"):
        build.resolve_species("Nomen dubium")
    with pytest.raises(BuildError, match="not a species"):
        build.resolve_species("Carnivora")

    record = build.species_record(VULPES, "vulpes vulpes")
    assert record.model_dump(by_alias=True) | {} == {
        "ncbi_taxid": 9627,
        "scientific_name": "Vulpes vulpes",
        "common_name": "red fox",
        "kingdom": "Metazoa",
        "phylum": "Chordata",
        "class": "Mammalia",
        "order": "Carnivora",
        "family": "Canidae",
        "genus": "Vulpes",
        "lineage_taxids": [1, 33208, 7711, 40674, 33554, 9608, 9625, 9627],
        "source_names": ["vulpes vulpes"],
        "n_images": 0,
        "n_genomes": 0,
        "split": None,
    }


# --------------------------------------------------------------------------- build


def test_build_full_dataset(env):
    report = env.build()
    root = env.tmp_path / "ds"

    # species / assemblies
    assert [s.ncbi_taxid for s in report.species] == [9627, 51338]
    fox, beaver = report.species
    assert fox.assembly_accession == "GCF_048418805.1" and fox.n_refseq_assemblies == 2
    assert fox.snapshot_accession == "GCF_048418805.1" and fox.warnings == []
    assert beaver.snapshot_accession == "GCF_000000001.1"
    assert any("snapshot would pick GCF_000000001.1" in w for w in beaver.warnings)
    assert env.ncbi.genome_calls == {"GCF_048418805.1": 1, "GCF_047511655.1": 1}

    # candidates: ceil(1.3 * 3) = 4 requested, both names offered as aliases
    call = env.source.calls[0]
    assert call["per_species"] == 4 and call["seed"] == 0
    assert call["aliases"] == {
        "Vulpes vulpes": ["Vulpes vulpes"],
        "Castor canadensis": ["Castor canadensis"],
    }
    assert (fox.n_candidates, fox.n_downloaded, fox.n_reused, fox.n_failed) == (4, 3, 0, 1)
    assert (beaver.n_candidates, beaver.n_downloaded, beaver.n_failed) == (4, 3, 1)
    assert env.server.hits["/v2.jpg"] == 1 and env.server.hits["/v4.jpg"] == 1  # top-up
    assert env.server.hits["/c1.jpg"] == 1 and env.server.hits["/c1b.jpg"] == 1  # fallback
    assert env.server.hits["/c4.jpg"] == 0 and env.server.hits["/v5.jpg"] == 0  # never asked
    assert report.totals["n_images"] == 6 and report.totals["n_failed"] == 2
    assert report.totals["n_removed"] == 0
    assert report.problems == [] and report.finished_at is not None
    assert {
        "species",
        "assemblies",
        "select",
        "genomes",
        "images",
        "write",
        "validate",
        "total",
    } <= set(report.timings)

    # tables
    assert manifest.validate_dataset(root, check_hashes=True) == []
    species = manifest.read_table(root, "species")
    assert [(s.n_images, s.n_genomes, s.source_names) for s in species] == [
        (3, 1, ["Vulpes vulpes"]),
        (3, 1, ["Castor canadensis"]),
    ]
    images = {i.image_id: i for i in manifest.read_table(root, "images")}
    assert set(images) == {"v1", "v3", "v4", "c1", "c2", "c3"}
    assert images["c1"].source_url == env.server.url("/c1b.jpg")  # the URL that worked
    assert images["v1"].file == "images/9627/v1.jpg" and images["v1"].ncbi_taxid == 9627
    assert (
        images["v1"].source_dataset_revision == "r1"
        and images["v1"].image_type == "citizen_science"
    )
    assert images["c2"].license == "cc-0-1.0" and images["c2"].observation_id == "obs-c2"
    pairs = manifest.read_table(root, "pairs")
    assert len(pairs) == 6 and {p.pairing for p in pairs} == {"species_reference"}
    assert {(p.ncbi_taxid, p.assembly_accession) for p in pairs} == {
        (9627, "GCF_048418805.1"),
        (51338, "GCF_047511655.1"),
    }

    # manifest, logs, README
    m = manifest.read_manifest(root)
    assert m.name == "fake-smoke" and m.counts["n_images"] == 6 and m.counts["n_genomes"] == 2
    assert [s.name for s in m.sources] == ["fake source", "NCBI RefSeq"]
    assert "assembly_summary snapshot assembly_summary.txt" in m.sources[1].revision
    assert m.selection["image_source"] == {"fake": True, "licenses": "cc_only"}
    assert m.selection["config"]["images"]["per_species"] == 3
    assert m.selection["builder"]["candidates_per_species"] == 4
    assert m.selection["image_settings"]["images"]["min_side"] == 64
    assert "per_species" not in m.selection["image_settings"]["images"]
    assert "policy" not in m.selection["config"]["genomes"]  # config as written: no defaults
    assert m.selection["assemblies"]["Castor canadensis"]["snapshot_accession"] == "GCF_000000001.1"
    assert m.selection["downloads"]["Vulpes vulpes"]["n_failed"] == 1
    assert m.selection["command"] == f"genes-datasets build {env.tmp_path / 'config.json'}"
    failures = [
        json.loads(line) for line in (root / "logs/image_failures.jsonl").read_text().splitlines()
    ]
    assert sorted(f["image_id"] for f in failures) == ["c1", "v2"]
    assert all("HTTP 404" in f["error"] for f in failures)
    saved = json.loads((root / "logs/build_report.json").read_text())
    assert saved["totals"] == report.totals and len(saved["species"]) == 2
    readme = (root / "README.md").read_text()
    assert "# fake-smoke v0.1.0" in readme
    assert (
        "| 9627 | Vulpes vulpes | red fox | GCF_048418805.1 VulVul3 | Chromosome | 2,000,000 | 3 |"
        in readme
    )
    assert "| cc-by-4.0 | 3 |" in readme and "| cc-0-1.0 | 3 |" in readme
    assert f"genes-datasets build {env.tmp_path / 'config.json'}" in readme
    assert '"per_species": 3' in readme
    assert "snapshot would pick" in readme


def test_rerun_is_idempotent(env):
    first = env.build()
    root = env.tmp_path / "ds"
    before = [i.model_dump(exclude={"downloaded_at"}) for i in manifest.read_table(root, "images")]
    hits = dict(env.server.hits)
    mtimes = {p: p.stat().st_mtime_ns for p in (root / "images").rglob("*.jpg")}
    failures_log = (root / "logs/image_failures.jsonl").read_text()
    assert len(failures_log.splitlines()) == 2

    second = env.build()
    assert dict(env.server.hits) == hits  # no image was fetched again (not even the dead ones)
    assert {p: p.stat().st_mtime_ns for p in (root / "images").rglob("*.jpg")} == mtimes
    assert [(s.n_downloaded, s.n_reused, s.n_failed) for s in second.species] == [
        (3, 3, 0),
        (3, 3, 0),
    ]
    after = [i.model_dump(exclude={"downloaded_at"}) for i in manifest.read_table(root, "images")]
    assert after == before  # same rows in the same order, incl. c1 keeping its fallback URL
    assert [i["image_id"] for i in after] == ["v1", "v3", "v4", "c1", "c2", "c3"]
    assert second.totals["n_images"] == first.totals["n_images"] == 6
    assert second.totals["n_removed"] == 0 and second.warnings == first.warnings
    assert manifest.validate_dataset(root, check_hashes=True) == []
    assert env.ncbi.genome_calls["GCF_048418805.1"] == 2  # download_genome re-verifies, no refetch
    assert not list(root.glob("*.part"))
    # the failures log is cumulative: the dead URLs of run 1 are still on record
    assert (root / "logs/image_failures.jsonl").read_text() == failures_log


def test_rerun_after_url_change_refetches_stale_files(env):
    """The config's inat_size_variant changed: the source now hands out different primary
    URLs (different bytes) and the old URL is not a fallback any more. The old files must
    not be relabelled with the new URL: they are stale, removed and fetched again."""
    root = env.tmp_path / "ds"
    env.build()
    before = {i.image_id: i for i in manifest.read_table(root, "images")}
    s = env.server
    for name in ("v1", "v3", "v4", "v5", "c1b", "c2", "c3", "c4"):
        s.files[f"/{name}_orig.jpg"] = make_jpeg(100, size=(640, 480))
    env.catalog["Vulpes vulpes"] = [
        candidate(s, f"v{i}", f"/v{i}_orig.jpg", "Vulpes vulpes") for i in (1, 2, 3, 4, 5)
    ]
    env.catalog["Castor canadensis"] = [
        candidate(s, "c1", "/c1b_orig.jpg", "Castor canadensis"),
        candidate(s, "c2", "/c2_orig.jpg", "Castor canadensis"),
        candidate(s, "c3", "/c3_orig.jpg", "Castor canadensis"),
        candidate(s, "c4", "/c4_orig.jpg", "Castor canadensis"),
    ]
    report = env.build(env.write_config(images__inat_size_variant="original"))
    after = {i.image_id: i for i in manifest.read_table(root, "images")}
    assert set(after) == set(before) == {"v1", "v3", "v4", "c1", "c2", "c3"}
    for iid, rec in after.items():
        assert rec.source_url.endswith("_orig.jpg") and rec.width == 640
        assert rec.sha256 != before[iid].sha256
    assert env.server.hits["/v1_orig.jpg"] == 1 and env.server.hits["/v2_orig.jpg"] == 1
    fox, beaver = report.species
    assert (fox.n_downloaded, fox.n_reused, fox.n_failed) == (3, 0, 1)
    assert any("3 existing files were fetched from URLs" in w for w in fox.warnings)
    assert any("3 existing files were fetched from URLs" in w for w in beaver.warnings)
    assert any(
        "image settings changed" in w and "inat_size_variant: 'large' -> 'original'" in w
        for w in report.warnings
    )
    assert report.totals["n_removed"] == 6
    assert sorted(p.name for p in (root / "images" / "9627").iterdir()) == [
        "v1.jpg",
        "v3.jpg",
        "v4.jpg",
    ]
    assert manifest.validate_dataset(root, check_hashes=True) == []


def test_rerun_keeps_file_whose_old_url_is_still_a_fallback(env):
    """'large' -> 'medium' where the source lists the medium URL first and the large one as
    fallback: the existing file is reused under the URL it really came from, no request is
    made, and the settings change is flagged."""
    root = env.tmp_path / "ds"
    env.build()
    s = env.server
    for name in ("v1", "v3", "v4", "v5"):
        s.files[f"/medium/{name}.jpg"] = make_jpeg(50, size=(80, 60))
    env.catalog["Vulpes vulpes"] = [
        candidate(s, f"v{i}", f"/medium/v{i}.jpg", "Vulpes vulpes", f"/v{i}.jpg")
        for i in (1, 2, 3, 4, 5)
    ]
    hits = dict(env.server.hits)
    report = env.build(env.write_config(images__inat_size_variant="medium"))
    assert dict(env.server.hits) == hits
    fox = {i.image_id: i for i in manifest.read_table(root, "images") if i.ncbi_taxid == 9627}
    assert {i.source_url for i in fox.values()} == {s.url(f"/v{i}.jpg") for i in (1, 3, 4)}
    assert all(i.width == 160 for i in fox.values())
    assert report.species[0].n_reused == 3 and report.totals["n_removed"] == 0
    assert any("inat_size_variant: 'large' -> 'medium'" in w for w in report.warnings)
    assert not any("existing files" in w for w in report.species[0].warnings)


def test_rerun_with_stricter_min_side_tries_fallbacks(env):
    """Existing 160x120 files fail min_side=300: they are dropped (not kept at the dest where
    they would block the fallback) and the candidate's other URLs are fetched."""
    root = env.tmp_path / "ds"
    env.build()
    env.server.files["/v1_big.jpg"] = make_jpeg(9, size=(640, 480))
    env.catalog["Vulpes vulpes"][0] = candidate(
        env.server, "v1", "/v1.jpg", "Vulpes vulpes", "/v1_big.jpg"
    )
    report = env.build(env.write_config(images__min_side=300))
    fox, beaver = report.species
    assert env.server.hits["/v1_big.jpg"] == 1 and env.server.hits["/v1.jpg"] == 1  # not again
    assert (fox.n_downloaded, fox.n_reused) == (1, 0) and beaver.n_downloaded == 0
    assert any("3 existing files fail the current min_side=300" in w for w in fox.warnings)
    assert any("min_side: 64 -> 300" in w for w in report.warnings)
    rows = manifest.read_table(root, "images")
    assert [(i.image_id, i.width, i.source_url) for i in rows] == [
        ("v1", 640, env.server.url("/v1_big.jpg"))
    ]
    assert sorted(p.name for p in (root / "images" / "9627").iterdir()) == ["v1.jpg"]
    assert not (root / "images" / "51338").exists()  # all rejected, empty folder removed
    assert report.totals["n_removed"] == 6
    assert manifest.read_table(root, "species")[1].n_images == 0
    assert manifest.validate_dataset(root, check_hashes=True) == []


def test_rerun_removes_unreferenced_image_files(env):
    """A changed candidate set must not leave images without a table row (no licence, no
    provenance) under images/; leftovers of interrupted writes go too."""
    root = env.tmp_path / "ds"
    env.build()
    s = env.server
    s.files["/v6.jpg"], s.files["/v7.jpg"] = make_jpeg(6), make_jpeg(7)
    env.catalog["Vulpes vulpes"] = [
        candidate(s, "v6", "/v6.jpg", "Vulpes vulpes"),
        candidate(s, "v7", "/v7.jpg", "Vulpes vulpes"),
        candidate(s, "v1", "/v1.jpg", "Vulpes vulpes"),
        candidate(s, "v5", "/v5.jpg", "Vulpes vulpes"),
    ]
    (root / "images" / "9627" / "v9.abc123.part").write_bytes(b"x")
    (root / "images" / "999").mkdir()
    (root / "images" / "999" / "gone.jpg").write_bytes(make_jpeg(3))
    report = env.build(env.write_config(images__seed=1))
    assert sorted(p.name for p in (root / "images" / "9627").iterdir()) == [
        "v1.jpg",
        "v6.jpg",
        "v7.jpg",
    ]
    assert not (root / "images" / "999").exists()
    assert [i.image_id for i in manifest.read_table(root, "images") if i.ncbi_taxid == 9627] == [
        "v6",
        "v7",
        "v1",
    ]
    assert report.species[0].n_reused == 1 and env.server.hits["/v5.jpg"] == 0
    assert any("removed 4 file(s) under images/" in w for w in report.warnings)
    assert any("images.seed: 0 -> 1" in w for w in report.warnings)
    assert report.totals["n_removed"] == 4
    assert manifest.validate_dataset(root, check_hashes=True) == []


def test_files_without_recorded_url_are_reused_with_a_warning(env):
    """Images present but no images.parquet (a run that died before writing tables): the
    files are reused, their primary URL recorded and the missing provenance flagged."""
    root = env.tmp_path / "ds"
    env.build()
    manifest.table_path(root, "images").unlink()
    (root / "images.csv").unlink()
    hits = dict(env.server.hits)
    report = env.build()
    assert dict(env.server.hits) == hits
    fox, beaver = report.species
    assert (fox.n_reused, beaver.n_reused) == (3, 3)
    assert any("3 existing files have no recorded URL" in w for w in beaver.warnings)
    c1 = next(i for i in manifest.read_table(root, "images") if i.image_id == "c1")
    assert c1.source_url == env.server.url("/c1.jpg")  # primary; the fallback is not knowable
    assert manifest.validate_dataset(root, check_hashes=True) == []


def test_leftover_part_in_root_is_removed(env):
    root = env.tmp_path / "ds"
    root.mkdir()
    (root / "species.parquet.deadbeef.part").write_bytes(b"")
    report = env.build()
    assert not list(root.glob("*.part")) and report.problems == []
    assert any(
        "leftover *.part" in w and "species.parquet.deadbeef.part" in w for w in report.warnings
    )
    assert report.totals["n_removed"] == 1


def test_suffix_shaped_candidate_ids(env):
    """Ids ending in an image suffix must neither be re-requested on rerun nor collapse onto
    one file."""
    s = env.server
    env.catalog["Vulpes vulpes"] = [
        candidate(s, f"v{i}.jpg", f"/v{i}.jpg", "Vulpes vulpes") for i in (1, 2, 3, 4, 5)
    ]
    env.catalog["Castor canadensis"] = [
        candidate(s, "c2.jpg", "/c2.jpg", "Castor canadensis"),
        candidate(s, "c2.png", "/c3.jpg", "Castor canadensis"),
        candidate(s, "c1", "/c1.jpg", "Castor canadensis", "/c1b.jpg"),
    ]
    root = env.tmp_path / "ds"
    first = env.build()
    assert [(x.n_downloaded, x.n_failed) for x in first.species] == [(3, 1), (3, 1)]
    assert sorted(p.name for p in (root / "images" / "9627").iterdir()) == [
        "v1_jpg.jpg",
        "v3_jpg.jpg",
        "v4_jpg.jpg",
    ]
    assert sorted(p.name for p in (root / "images" / "51338").iterdir()) == [
        "c1.jpg",
        "c2_jpg.jpg",
        "c2_png.jpg",
    ]
    hits = dict(env.server.hits)
    second = env.build()
    assert dict(env.server.hits) == hits  # v2.jpg (dead) is not asked for again
    assert [(x.n_downloaded, x.n_reused, x.n_failed) for x in second.species] == [
        (3, 3, 0),
        (3, 3, 0),
    ]
    assert manifest.validate_dataset(root, check_hashes=True) == []


def test_dry_run_downloads_nothing(env):
    report = env.build(dry_run=True)
    assert report.dry_run and not (env.tmp_path / "ds").exists()
    assert env.server.total_hits == 0 and env.ncbi.genome_calls == {}
    assert [s.scientific_name for s in report.species] == ["Vulpes vulpes", "Castor canadensis"]
    assert [s.assembly_accession for s in report.species] == ["GCF_048418805.1", "GCF_047511655.1"]
    assert [s.n_candidates for s in report.species] == [4, 4]
    assert report.totals == {"n_species": 2, "n_candidates": 8}
    text = build.format_report(report)
    assert "dry run" in text and "GCF_047511655.1 mCasCan1.hap1v2 Chromosome 2,000,000 bp" in text
    assert "snapshot would pick" in text


def test_subspecies_alias_and_no_alias_source(env):
    # "Vulpes vulpes fulva" lifts to Vulpes vulpes; the catalog only knows the species name,
    # so candidates must come through the alias
    cfg_path = env.write_config(species=["Vulpes vulpes fulva"])
    report = env.build(cfg_path)
    assert report.species[0].ncbi_taxid == 9627 and report.species[0].n_downloaded == 3
    assert env.source.calls[-1]["aliases"] == {
        "Vulpes vulpes fulva": ["Vulpes vulpes fulva", "Vulpes vulpes"]
    }
    species = manifest.read_table(env.tmp_path / "ds", "species")[0]
    assert species.source_names == ["Vulpes vulpes fulva", "Vulpes vulpes"]
    assert any("is a subspecies" in w for w in report.warnings)

    # a source without the aliases keyword gets both names and the results are merged
    plain = NoAliasSource(env.catalog)
    report = build.build(load_config(cfg_path), progress=False, source=plain, dry_run=True)
    assert plain.calls[-1]["species"] == ["Vulpes vulpes fulva", "Vulpes vulpes"]
    assert report.species[0].n_candidates == 4
    assert any("takes no aliases" in w for w in report.warnings)

    # a source that takes aliases at construction (TreeOfLife200MSource) gets them from the
    # factory and is queried under the configured names only
    made: list[CtorAliasSource] = []

    def factory(cfg, *, aliases=None):
        made.append(CtorAliasSource(env.catalog, aliases=aliases, revision=cfg.source_revision))
        return made[-1]

    build.SOURCES["fake"] = factory  # monkeypatched dict entry, restored by the fixture
    report = build.build(load_config(cfg_path), progress=False, dry_run=True)
    assert made[-1].aliases == {"Vulpes vulpes fulva": ["Vulpes vulpes fulva", "Vulpes vulpes"]}
    assert made[-1].revision == "r1"
    assert made[-1].calls[-1]["species"] == ["Vulpes vulpes fulva"]
    assert report.species[0].n_candidates == 4
    assert not any("takes no aliases" in w for w in report.warnings)


def test_candidates_exhausted_is_a_warning(env):
    env.catalog["Castor canadensis"] = env.catalog["Castor canadensis"][:2]  # c1 (fallback), c2
    report = env.build()
    beaver = report.species[1]
    assert (beaver.n_candidates, beaver.n_downloaded) == (2, 2)
    assert any("only 2 candidates" in w for w in beaver.warnings)
    assert any("2 of 3 images downloaded" in w for w in beaver.warnings)
    assert manifest.validate_dataset(env.tmp_path / "ds", check_hashes=True) == []
    assert manifest.read_table(env.tmp_path / "ds", "species")[1].n_images == 2


@pytest.mark.parametrize(
    ("species", "message"),
    [
        (["Vulpes vulpes", "Nomen dubium"], "does not know"),
        (["Vulpes vulpes", "Vulpes vulpes fulva"], "both resolve to taxid 9627"),
        (["Carnivora"], "not a species"),
        (["Vulpes vulpes", "Felis leo"], "no current RefSeq assembly for: Felis leo"),
    ],
)
def test_build_errors(env, species, message):
    with pytest.raises(BuildError, match=message) as info:
        env.build(env.write_config(species=species))
    assert info.value.report is not None and info.value.report.finished_at is not None
    assert not (env.tmp_path / "ds").exists()


def test_build_errors_missing_snapshot_and_unknown_source(env):
    with pytest.raises(BuildError, match="snapshot .* does not exist"):
        env.build(
            env.write_config(genomes={"assembly_summary_snapshot": str(env.tmp_path / "no.txt")})
        )
    with pytest.raises(BuildError, match="unknown image source 'nope'"):
        env.build(env.write_config(images__source="nope"))
    # no snapshot configured: no cross-check, no warning
    report = env.build(env.write_config(genomes={}), dry_run=True)
    assert report.species[1].snapshot_accession is None and report.warnings == []


def test_validation_problems_raise_and_are_reported(env, monkeypatch):
    calls: list[dict] = []

    def fake_validate(root, **kw):
        calls.append({"root": Path(root), **kw})
        return ["bad row"]

    monkeypatch.setattr(manifest, "validate_dataset", fake_validate)
    with pytest.raises(BuildError, match="1 problem") as info:
        env.build()
    assert calls == [{"root": env.tmp_path / "ds", "check_hashes": True}]
    assert info.value.report.problems == ["bad row"]
    saved = json.loads((env.tmp_path / "ds" / "logs" / "build_report.json").read_text())
    assert saved["problems"] == ["bad row"]


def test_genome_download_failure(env, monkeypatch):
    def boom(row, root, **kw):
        raise ncbi.NCBIError(f"{row.assembly_accession}: ftp down")

    monkeypatch.setattr(ncbi, "download_genome", boom)
    with pytest.raises(BuildError, match="genome download failed: .*ftp down"):
        env.build()
    assert env.server.total_hits == 0  # images are not started when genomes fail


def test_image_source_exceptions_become_build_errors(env):
    """A missing parquet, a duckdb error, ... raised by the source factory or select() must
    surface as BuildError (with the report so far), not as a traceback."""
    missing = env.tmp_path / "nope.parquet"

    def broken_factory(cfg, *, aliases=None):
        raise FileNotFoundError(missing)

    build.SOURCES["fake"] = broken_factory  # monkeypatched entry, restored by the fixture
    with pytest.raises(BuildError, match="image source 'fake': construction failed") as info:
        env.build(dry_run=True)
    assert str(missing) in str(info.value) and "FileNotFoundError" in str(info.value)
    assert isinstance(info.value.__cause__, FileNotFoundError)
    assert [s.assembly_accession for s in info.value.report.species] == [
        "GCF_048418805.1",
        "GCF_047511655.1",
    ]

    class Boom(FakeSource):
        def select(self, *args, **kwargs):
            raise RuntimeError("duckdb: Out of Memory Error")

    build.SOURCES["fake"] = lambda cfg, aliases=None: Boom(env.catalog)
    with pytest.raises(BuildError, match="image source 'fake': select failed: RuntimeError"):
        env.build()
    assert not (env.tmp_path / "ds").exists()
    # a bad snapshot file is a clean error too
    env.snapshot.write_text("GCF_1\tno header line\n")
    with pytest.raises(BuildError, match="assembly summary snapshot .*data before header"):
        env.build(dry_run=True)


# --------------------------------------------------------------------------- CLI


def test_cli_build(env):
    runner = CliRunner()
    cfg_path = env.write_config()
    result = runner.invoke(app, ["build", str(cfg_path), "--no-progress", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output and not (env.tmp_path / "ds").exists()

    result = runner.invoke(app, ["build", str(cfg_path), "--no-progress"])
    assert result.exit_code == 0, result.output
    assert "fake-smoke v0.1.0 ->" in result.output and "n_images 6" in result.output
    assert "warnings (1):" in result.output and "snapshot would pick" in result.output
    assert manifest.validate_dataset(env.tmp_path / "ds", check_hashes=True) == []

    other = env.tmp_path / "elsewhere"
    result = runner.invoke(
        app, ["build", str(cfg_path), "--no-progress", "--root-override", str(other)]
    )
    assert result.exit_code == 0, result.output
    assert manifest.validate_dataset(other, check_hashes=True) == []
    assert f"--root-override {other}" in (other / "README.md").read_text()

    result = runner.invoke(app, ["validate", str(other), "--check-hashes"])
    assert result.exit_code == 0 and result.output.strip().endswith(": OK")


def test_cli_relative_root_override(env, monkeypatch):
    cwd = env.tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    result = CliRunner().invoke(
        app, ["build", str(env.write_config()), "--no-progress", "--root-override", "rel/out"]
    )
    assert result.exit_code == 0, result.output
    assert manifest.validate_dataset(cwd / "rel" / "out", check_hashes=True) == []
    assert not (build.REPO_ROOT / "rel").exists()
    assert f"--root-override {cwd / 'rel' / 'out'}" in (cwd / "rel/out/README.md").read_text()


def test_cli_source_error_is_a_clean_failure(env):
    missing = env.tmp_path / "nope.parquet"

    def broken_factory(cfg, *, aliases=None):
        raise FileNotFoundError(missing)

    build.SOURCES["fake"] = broken_factory
    result = CliRunner().invoke(
        app, ["build", str(env.write_config()), "--no-progress", "--dry-run"]
    )
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert (
        f"build failed: image source 'fake': construction failed: FileNotFoundError: {missing}"
        in (result.output)
    )
    assert "Traceback" not in result.output
    assert "fake-smoke v0.1.0 ->" in result.output and "GCF_048418805.1" in result.output


def test_cli_build_failures(env):
    runner = CliRunner()
    result = runner.invoke(
        app, ["build", str(env.write_config(species=["Nomen dubium"])), "--no-progress"]
    )
    assert result.exit_code == 1
    assert "build failed: species 'Nomen dubium': NCBI Taxonomy does not know" in result.output

    bad = env.tmp_path / "bad.json"
    bad.write_text('{"name": "x"}')
    result = runner.invoke(app, ["build", str(bad)])
    assert result.exit_code == 2 and "invalid config" in result.output

    result = runner.invoke(app, ["build", str(env.tmp_path / "missing.json")])
    assert result.exit_code == 2 and "cannot read config" in result.output
