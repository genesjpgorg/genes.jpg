"""datasets.manifest / datasets.cli on a small synthetic dataset built under tmp_path."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from importlib.metadata import entry_points
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from PIL import Image
from pydantic_core import PydanticSerializationError
from typer.testing import CliRunner

from datasets import manifest as m
from datasets import schema as s
from datasets.cli import app

NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
LINEAGE = [1, 131567, 2759, 33208, 7711, 40674, 9443, 9604]

SPECIES = [
    s.SpeciesRecord(
        ncbi_taxid=9606,
        scientific_name="Homo sapiens",
        common_name="human",
        kingdom="Metazoa",
        phylum="Chordata",
        order="Primates",
        family="Hominidae",
        genus="Homo",
        lineage_taxids=[*LINEAGE, 9605, 9606],
        source_names=["Homo sapiens", "human"],
        n_images=2,
        n_genomes=1,
        split="train",
        **{"class": "Mammalia"},  # by alias
    ),
    s.SpeciesRecord(
        ncbi_taxid=9598,
        scientific_name="Pan troglodytes",
        common_name="chimpanzee",
        class_="Mammalia",  # by field name (populate_by_name)
        order="Primates",
        family="Hominidae",
        genus="Pan",
        lineage_taxids=[*LINEAGE, 9596, 9598],
        source_names=["Pan troglodytes"],
        n_images=2,
        n_genomes=1,
        split="test",
    ),
]


def _write_genome(
    root: Path, accession: str, taxid: int, organism: str, name: str, seqs: list[str]
) -> s.GenomeRecord:
    directory = root / "genomes" / accession
    directory.mkdir(parents=True)
    fasta = "".join(f">seq{i} {organism}\n{seq}\n" for i, seq in enumerate(seqs, 1)).encode()
    data = gzip.compress(fasta)
    fna = directory / f"{accession}_genomic.fna.gz"
    fna.write_bytes(data)
    report = directory / f"{accession}_assembly_report.txt"
    report.write_text(f"# Assembly name:  {name}\n# Taxid: {taxid}\n")
    md5 = hashlib.md5(data).hexdigest()
    checksums = directory / "md5checksums.txt"
    checksums.write_text(f"{md5}  ./{fna.name}\n")
    return s.GenomeRecord(
        assembly_accession=accession,
        ncbi_taxid=taxid,
        species_taxid=taxid,
        organism_name=organism,
        assembly_name=name,
        assembly_level=s.AssemblyLevel.chromosome,
        refseq_category="reference genome",
        release_date="2022-02-03",
        genome_size=sum(len(x) for x in seqs),
        gc_percent=41.0,
        scaffold_count=len(seqs),
        contig_count=len(seqs),
        ftp_path=f"https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/405/{accession}_{name}",
        sequence_file=fna.relative_to(root).as_posix(),
        sequence_md5=md5,
        sequence_bytes=len(data),
        n_sequences=len(seqs),
        extra_files=[report.relative_to(root).as_posix(), checksums.relative_to(root).as_posix()],
        downloaded_at=NOW,
    )


def _write_image(root: Path, n: int, taxid: int, size: tuple[int, int], fmt: str) -> s.ImageRecord:
    ext = {"JPEG": "jpg", "PNG": "png"}[fmt]
    path = root / "images" / str(taxid) / f"{n}.{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (n * 40 % 256, 90, 160)).save(path, format=fmt)
    data = path.read_bytes()
    return s.ImageRecord(
        image_id=f"synthetic:{n}",
        ncbi_taxid=taxid,
        source_dataset="synthetic",
        source_provider="test",
        source_id=str(n),
        observation_id=f"obs{(n + 1) // 2}",
        source_url=f"https://example.org/{n}.{ext}",
        original_label="Homo sapiens" if taxid == 9606 else "Pan troglodytes",
        image_type=s.ImageType.citizen_science if n % 2 else s.ImageType.unknown,
        license="CC0 1.0",
        file=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        width=size[0],
        height=size[1],
        format=fmt,
        downloaded_at=NOW,
    )


@dataclass
class Built:
    root: Path
    species: list[s.SpeciesRecord]
    genomes: list[s.GenomeRecord]
    images: list[s.ImageRecord]
    pairs: list[s.PairRecord]
    manifest: s.DatasetManifest

    def refresh_manifest(self) -> s.DatasetManifest:
        self.manifest = m.build_manifest(
            self.root, name=self.manifest.name, version=self.manifest.version
        )
        m.write_manifest(self.root, self.manifest)
        return self.manifest


@pytest.fixture
def built(tmp_path: Path) -> Built:
    root = tmp_path / "smoke"
    genomes = [
        _write_genome(
            root,
            "GCF_000001405.40",
            9606,
            "Homo sapiens",
            "GRCh38.p14",
            ["ACGT" * 50, "GGCCAATT" * 10],
        ),
        _write_genome(
            root,
            "GCF_028858775.2",
            9598,
            "Pan troglodytes",
            "NHGRI_mPanTro3-v2.0_pri",
            ["TTGACA" * 30],
        ),
    ]
    images = [
        _write_image(root, 1, 9606, (96, 64), "JPEG"),
        _write_image(root, 2, 9606, (120, 80), "PNG"),
        _write_image(root, 3, 9598, (64, 96), "JPEG"),
        _write_image(root, 4, 9598, (100, 100), "JPEG"),
    ]
    genome_of = {g.species_taxid: g.assembly_accession for g in genomes}
    pairs = [
        s.PairRecord(
            image_id=i.image_id, assembly_accession=genome_of[i.ncbi_taxid], ncbi_taxid=i.ncbi_taxid
        )
        for i in images
    ]
    tables = {"species": SPECIES, "genomes": genomes, "images": images, "pairs": pairs}
    for name, records in tables.items():
        m.write_table(root, name, records)
    manifest = m.build_manifest(
        root,
        name="smoke",
        version="0.0.1",
        description="synthetic test dataset",
        sources=[s.SourceInfo(name="synthetic", url="https://example.org", accessed_at=NOW)],
        selection={"n_species": 2, "seed": 0},
    )
    m.write_manifest(root, manifest)
    return Built(root, list(SPECIES), genomes, images, pairs, manifest)


# --------------------------------------------------------------------------- tables


def test_tables_round_trip(built: Built) -> None:
    for name in m.TABLE_NAMES:
        assert m.read_table(built.root, name) == getattr(built, name)
        assert m.table_path(built.root, name).is_file()
        assert (built.root / f"{name}.csv").is_file()


def test_species_class_alias_round_trips(built: Built) -> None:
    names = pq.read_schema(m.table_path(built.root, "species")).names
    assert "class" in names and "class_" not in names
    back = m.read_table(built.root, "species")
    assert [r.class_ for r in back] == ["Mammalia", "Mammalia"]
    assert back[0].model_dump(by_alias=True)["class"] == "Mammalia"


def test_arrow_schema_types() -> None:
    genomes = m.arrow_schema(s.GenomeRecord)
    assert genomes.field("downloaded_at").type == pa.timestamp("us", tz="UTC")
    assert genomes.field("extra_files").type == pa.list_(pa.string())
    assert genomes.field("assembly_level").type == pa.string()
    assert genomes.field("gc_percent").type == pa.float64()
    assert not genomes.field("assembly_accession").nullable
    assert genomes.field("gc_percent").nullable
    species = m.arrow_schema(s.SpeciesRecord)
    assert species.field("lineage_taxids").type == pa.list_(pa.int64())
    assert species.field("split").type == pa.string() and species.field("split").nullable
    assert species.names[5] == "class"


def test_csv_sibling(built: Built) -> None:
    with (built.root / "species.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["class"] == "Mammalia"
    assert rows[0]["lineage_taxids"] == "1|131567|2759|33208|7711|40674|9443|9604|9605|9606"
    assert rows[0]["source_names"] == "Homo sapiens|human"
    assert rows[1]["kingdom"] == ""
    with (built.root / "genomes.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["downloaded_at"] == "2026-10-03T12:00:00Z"
    assert rows[0]["assembly_level"] == "Chromosome"
    assert rows[0]["genome_size_ungapped"] == ""


def test_empty_table_round_trip(tmp_path: Path) -> None:
    path = m.write_table(tmp_path, "pairs", [])
    assert pq.read_schema(path).names == ["image_id", "assembly_accession", "ncbi_taxid", "pairing"]
    assert m.read_table(tmp_path, "pairs") == []


def test_write_table_rejects_wrong_model(tmp_path: Path, built: Built) -> None:
    with pytest.raises(TypeError, match="pairs row 0: expected PairRecord, got ImageRecord"):
        m.write_table(tmp_path, "pairs", built.images)


def test_naive_datetime_is_stored_as_utc(tmp_path: Path, built: Built) -> None:
    naive = datetime(2026, 1, 2, 3, 4, 5, 678901)  # noqa: DTZ001 - naive on purpose
    m.write_table(tmp_path, "images", [built.images[0].model_copy(update={"downloaded_at": naive})])
    back = m.read_table(tmp_path, "images")[0]
    assert back.downloaded_at == naive.replace(tzinfo=UTC)


def test_non_utc_datetime_round_trips(tmp_path: Path, built: Built) -> None:
    local = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    path = m.write_table(
        tmp_path, "images", [built.images[0].model_copy(update={"downloaded_at": local})]
    )
    back = m.read_table(tmp_path, "images")[0]
    assert back.downloaded_at == local
    assert back.downloaded_at.utcoffset() == timedelta(0)
    assert pq.read_table(path).to_pylist()[0]["downloaded_at"] == datetime(
        2026, 7, 1, 10, tzinfo=UTC
    )


def test_unicode_round_trip(built: Built) -> None:
    root = built.root
    bat = built.species[1].model_copy(
        update={"common_name": "Große Hufeisennase", "source_names": ["人", "Pan troglodytes"]}
    )
    old = root / built.images[2].file
    new = old.with_name("pic ü — Ménétries.jpg")
    old.rename(new)
    moved = built.images[2].model_copy(update={"file": new.relative_to(root).as_posix()})
    images = [*built.images[:2], moved, built.images[3]]
    m.write_table(root, "species", [built.species[0], bat])
    m.write_table(root, "images", images)
    man = m.build_manifest(
        root, name="smoke", version="0.0.1", description="Ménétries' Großer Test"
    )
    m.write_manifest(root, man)

    assert m.read_table(root, "species")[1] == bat
    assert m.read_table(root, "images") == images
    assert m.read_manifest(root) == man
    assert m.validate_dataset(root, check_hashes=True) == []
    with (root / "species.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[1]["common_name"] == "Große Hufeisennase"
    assert rows[1]["source_names"] == "人|Pan troglodytes"
    assert "Ménétries' Großer Test" in (root / "dataset.json").read_text(encoding="utf-8")


def _part_files(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if ".part" in p.name)


def test_write_table_failure_leaves_previous_files(
    built: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = built.root
    before_parquet = m.table_path(root, "images").read_bytes()
    before_csv = (root / "images.csv").read_bytes()

    real = pq.write_table

    def enospc(table: pa.Table, where: Path, **kwargs: object) -> None:
        real(table, where, **kwargs)  # the .part is (partially) written before the error
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(m.pq, "write_table", enospc)
    with pytest.raises(OSError, match="No space left"):
        m.write_table(root, "images", built.images[:1])
    monkeypatch.undo()

    too_big = built.genomes[0].model_copy(update={"genome_size": 2**63})
    with pytest.raises(OverflowError):
        m.write_table(root, "genomes", [too_big, built.genomes[1]])

    assert m.table_path(root, "images").read_bytes() == before_parquet
    assert (root / "images.csv").read_bytes() == before_csv
    assert m.read_table(root, "genomes") == built.genomes
    assert _part_files(root) == []
    assert m.validate_dataset(root) == []


def test_concurrent_writers_do_not_collide(built: Built) -> None:
    a = [p.model_copy(update={"pairing": "species_reference"}) for p in built.pairs] * 100
    b = [p.model_copy(update={"pairing": "same_specimen"}) for p in built.pairs] * 150
    errors: list[BaseException] = []

    def write(records: list[s.PairRecord]) -> None:
        try:
            m.write_table(built.root, "pairs", records)
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    for _ in range(5):
        threads = [threading.Thread(target=write, args=(recs,)) for recs in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        back = m.read_table(built.root, "pairs")
        assert back in (a, b)
        with (built.root / "pairs.csv").open(newline="", encoding="utf-8") as fh:
            assert len(list(csv.reader(fh))) - 1 in (len(a), len(b))
    assert _part_files(built.root) == []


# --------------------------------------------------------------------------- manifest


def test_manifest_counts_and_round_trip(built: Built) -> None:
    man = built.manifest
    assert man.schema_version == s.SCHEMA_VERSION
    assert man.counts == {
        "n_species": 2,
        "n_genomes": 2,
        "n_images": 4,
        "n_pairs": 4,
        "genome_bytes": sum(g.sequence_bytes for g in built.genomes),
        "image_bytes": sum(i.bytes for i in built.images),
    }
    assert {k: v.n_rows for k, v in man.tables.items()} == {
        "species": 2,
        "genomes": 2,
        "images": 4,
        "pairs": 4,
    }
    assert man.tables["images"].path == "images.parquet"
    assert m.read_manifest(built.root) == man
    raw = json.loads((built.root / "dataset.json").read_text())
    assert raw["created_at"].endswith("Z")
    assert raw["sources"][0]["accessed_at"] == "2026-10-03T12:00:00Z"
    assert raw["selection"] == {"n_species": 2, "seed": 0}


def test_dataset_counts_with_missing_tables(tmp_path: Path) -> None:
    assert m.dataset_counts(tmp_path) == dict.fromkeys(m.COUNT_KEYS, 0)
    assert m.build_manifest(tmp_path, name="x", version="0").tables == {}


def test_build_manifest_normalises_selection(built: Built) -> None:
    selection = {
        "path": Path("/x/y"),
        "when": datetime(2026, 1, 1, tzinfo=UTC),
        "caps": (1, 2),
        "tags": {"a"},
        "nested": {"seed": 0, "names": ["人"]},
    }
    man = m.build_manifest(built.root, name="x", version="1", selection=selection)
    assert man.selection == {
        "path": "/x/y",
        "when": "2026-01-01T00:00:00Z",
        "caps": [1, 2],
        "tags": ["a"],
        "nested": {"seed": 0, "names": ["人"]},
    }
    m.write_manifest(built.root, man)
    assert m.read_manifest(built.root) == man
    with pytest.raises(PydanticSerializationError):
        m.build_manifest(built.root, name="x", version="1", selection={"obj": object()})


# --------------------------------------------------------------------------- validation


def test_validate_clean(built: Built) -> None:
    assert m.validate_dataset(built.root) == []
    assert m.validate_dataset(built.root, check_hashes=True) == []


def test_validate_missing_root(tmp_path: Path) -> None:
    assert m.validate_dataset(tmp_path / "nope") == [f"{tmp_path / 'nope'}: not a directory"]


def test_validate_reports_missing_file(built: Built) -> None:
    (built.root / built.images[1].file).unlink()
    assert m.validate_dataset(built.root) == [
        "images.parquet synthetic:2: file 'images/9606/2.png' is missing"
    ]
    (built.root / built.genomes[0].extra_files[0]).unlink()
    assert m.validate_dataset(built.root) == [
        (
            "genomes.parquet GCF_000001405.40: file "
            "'genomes/GCF_000001405.40/GCF_000001405.40_assembly_report.txt' is missing"
        ),
        "images.parquet synthetic:2: file 'images/9606/2.png' is missing",
    ]


def test_validate_reports_wrong_byte_count(built: Built) -> None:
    g = built.genomes[0]
    bad = g.model_copy(update={"sequence_bytes": g.sequence_bytes + 1})
    m.write_table(built.root, "genomes", [bad, built.genomes[1]])
    expected = (
        f"genomes.parquet {g.assembly_accession}: file {g.sequence_file!r} has "
        f"{g.sequence_bytes} bytes, record says {g.sequence_bytes + 1}"
    )
    problems = m.validate_dataset(built.root)
    assert expected in problems
    assert any(p.startswith("dataset.json: counts[genome_bytes] is") for p in problems)
    built.refresh_manifest()
    assert m.validate_dataset(built.root) == [expected]


def test_validate_reports_pair_to_missing_image(built: Built) -> None:
    extra = s.PairRecord(
        image_id="synthetic:999", assembly_accession="GCF_000001405.40", ncbi_taxid=9606
    )
    m.write_table(built.root, "pairs", [*built.pairs, extra])
    built.refresh_manifest()
    assert m.validate_dataset(built.root) == [
        "pairs.parquet (synthetic:999, GCF_000001405.40): unknown image_id 'synthetic:999'"
    ]


def test_validate_reports_pair_inconsistencies(built: Built) -> None:
    pairs = [
        s.PairRecord(image_id="synthetic:1", assembly_accession="GCF_000000000.1", ncbi_taxid=9606),
        s.PairRecord(
            image_id="synthetic:3", assembly_accession="GCF_000001405.40", ncbi_taxid=9598
        ),
        s.PairRecord(
            image_id="synthetic:2", assembly_accession="GCF_000001405.40", ncbi_taxid=9598
        ),
    ]
    m.write_table(built.root, "pairs", pairs)
    built.refresh_manifest()
    assert m.validate_dataset(built.root) == [
        "pairs.parquet (synthetic:1, GCF_000000000.1): unknown assembly_accession 'GCF_000000000.1'",
        "pairs.parquet (synthetic:3, GCF_000001405.40): ncbi_taxid 9598 != genome species_taxid 9606",
        "pairs.parquet (synthetic:2, GCF_000001405.40): ncbi_taxid 9598 != image ncbi_taxid 9606",
        "pairs.parquet (synthetic:2, GCF_000001405.40): ncbi_taxid 9598 != genome species_taxid 9606",
    ]


def test_validate_reports_species_count_mismatch(built: Built) -> None:
    bad = built.species[0].model_copy(update={"n_images": 5, "n_genomes": 0})
    m.write_table(built.root, "species", [bad, built.species[1]])
    built.refresh_manifest()
    assert m.validate_dataset(built.root) == [
        "species.parquet 9606 (Homo sapiens): n_images is 5, images table has 2",
        "species.parquet 9606 (Homo sapiens): n_genomes is 0, genomes table has 1",
    ]


def test_validate_reports_unknown_species(built: Built) -> None:
    stray = built.images[3].model_copy(update={"ncbi_taxid": 9597})
    m.write_table(built.root, "images", [*built.images[:3], stray])
    built.refresh_manifest()
    problems = m.validate_dataset(built.root)
    assert "images.parquet synthetic:4: ncbi_taxid 9597 not in species table" in problems
    assert (
        "pairs.parquet (synthetic:4, GCF_028858775.2): ncbi_taxid 9598 != image ncbi_taxid 9597"
        in problems
    )
    assert "species.parquet 9598 (Pan troglodytes): n_images is 2, images table has 1" in problems
    assert len(problems) == 3


def test_validate_reports_duplicate_keys(built: Built) -> None:
    m.write_table(built.root, "images", [*built.images, built.images[0]])
    built.refresh_manifest()
    problems = m.validate_dataset(built.root)
    assert "images.parquet: duplicate image_id 'synthetic:1'" in problems
    assert "images.parquet: duplicate file 'images/9606/1.jpg'" in problems
    assert "species.parquet 9606 (Homo sapiens): n_images is 2, images table has 3" in problems
    assert len(problems) == 3


def test_validate_reports_duplicate_pair(built: Built) -> None:
    m.write_table(built.root, "pairs", [*built.pairs, built.pairs[0]])
    built.refresh_manifest()
    assert m.validate_dataset(built.root) == [
        (
            "pairs.parquet: duplicate (image_id, assembly_accession) "
            "('synthetic:1', 'GCF_000001405.40')"
        )
    ]


def test_validate_rejects_non_relative_paths(built: Built) -> None:
    absolute = built.images[2].model_copy(update={"file": "/abs/x.jpg"})
    dotdot = built.images[3].model_copy(update={"file": "images/9606/../9598/4.jpg"})
    m.write_table(built.root, "images", [*built.images[:2], absolute, dotdot])
    built.refresh_manifest()
    assert m.validate_dataset(built.root) == [
        "images.parquet synthetic:3: path '/abs/x.jpg' is not relative to the dataset root",
        (
            "images.parquet synthetic:4: path 'images/9606/../9598/4.jpg' is not relative to the "
            "dataset root"
        ),
    ]


def test_validate_reports_unreadable_tables(built: Built) -> None:
    root = built.root
    (root / "pairs.parquet").write_bytes(b"")
    (root / "genomes.parquet").write_bytes(b"definitely not parquet" * 50)
    data = (root / "species.parquet").read_bytes()
    (root / "species.parquet").write_bytes(data[: len(data) // 2])
    problems = m.validate_dataset(root)  # must not raise
    assert [p.split(":")[0] for p in problems] == [
        "species.parquet",
        "genomes.parquet",
        "pairs.parquet",
    ]
    assert all(": cannot read parquet: " in p for p in problems)
    result = runner.invoke(app, ["validate", str(root)])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # typer.Exit, not an Arrow traceback
    assert "pairs.parquet: cannot read parquet" in result.output
    assert "3 problem(s) found" in result.output


def test_validate_survives_foreign_table_schema(built: Built) -> None:
    root = built.root
    path = m.table_path(root, "images")
    pq.write_table(pq.read_table(path).drop_columns(["bytes"]), path)
    problems = m.validate_dataset(root)  # used to raise from the byte-sum over 'bytes'
    assert problems[:4] == [f"images.parquet row {i}: bytes: Field required" for i in range(4)]
    assert not any("counts[image_bytes]" in p for p in problems)  # not knowable, so skipped
    assert not any(p.startswith("dataset.json: tables[images]") for p in problems)  # 4 rows

    species = m.table_path(root, "species")
    table = pq.read_table(species)
    species.unlink()
    species.mkdir()
    pq.write_table(table, species / "part-0.parquet")
    problems = m.validate_dataset(root)
    assert "species.parquet: missing table" in problems
    assert "dataset.json: tables[species] refers to missing species.parquet" in problems


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_validate_reports_unreadable_files(built: Built) -> None:
    root = built.root
    image = root / built.images[0].file
    image.chmod(0)
    try:
        assert m.validate_dataset(root) == []  # size check only needs stat
        assert m.validate_dataset(root, check_hashes=True) == [
            "images.parquet synthetic:1: cannot read 'images/9606/1.jpg': Permission denied"
        ]
    finally:
        image.chmod(0o644)
    (root / "dataset.json").chmod(0)
    try:
        problems = m.validate_dataset(root)
    finally:
        (root / "dataset.json").chmod(0o644)
    assert len(problems) == 1 and problems[0].startswith("dataset.json: cannot read: ")


def test_validate_reports_leftover_part_files(built: Built) -> None:
    (built.root / "images.parquet.deadbeef.part").write_bytes(b"PAR1")
    assert m.validate_dataset(built.root) == [
        "images.parquet.deadbeef.part: leftover from an interrupted write"
    ]


def test_validate_reports_invalid_rows(built: Built) -> None:
    path = m.table_path(built.root, "images")
    pq.write_table(pq.read_table(path).drop_columns(["sha256"]), path)
    problems = m.validate_dataset(built.root)
    assert problems[:4] == [f"images.parquet row {i}: sha256: Field required" for i in range(4)]
    pq.write_table(pq.read_table(path).append_column("bogus", pa.array([1, 2, 3, 4])), path)
    problems = m.validate_dataset(built.root)
    assert (
        problems[0]
        == "images.parquet row 0: sha256: Field required; bogus: Extra inputs are not permitted"
    )


def test_validate_reports_manifest_problems(built: Built) -> None:
    path = built.root / "dataset.json"
    raw = json.loads(path.read_text())
    raw["schema_version"] = "9.9.9"
    raw["tables"]["pairs"]["n_rows"] = 1
    raw["counts"]["n_images"] = 40
    del raw["counts"]["image_bytes"]
    path.write_text(json.dumps(raw))
    assert m.validate_dataset(built.root) == [
        f"dataset.json: schema_version '9.9.9' != {s.SCHEMA_VERSION!r}",
        "dataset.json: tables[pairs].n_rows is 1, pairs.parquet has 4",
        "dataset.json: counts[n_images] is 40, actual 4",
        "dataset.json: counts lacks 'image_bytes'",
    ]
    path.write_text("{not json")
    assert m.validate_dataset(built.root)[0].startswith("dataset.json: invalid: ")


def test_validate_missing_manifest_and_table(built: Built) -> None:
    (built.root / "dataset.json").unlink()
    (built.root / "pairs.parquet").unlink()
    assert m.validate_dataset(built.root) == [
        "pairs.parquet: missing table",
        "dataset.json: missing",
    ]


def test_validate_check_hashes_detects_corruption(built: Built) -> None:
    for record, rel, algorithm, expected in (
        (built.images[2], built.images[2].file, "sha256", built.images[2].sha256),
        (built.genomes[1], built.genomes[1].sequence_file, "md5", built.genomes[1].sequence_md5),
    ):
        path = built.root / rel
        data = bytearray(path.read_bytes())
        data[-1] ^= 0xFF  # same size, different content
        path.write_bytes(data)
    assert m.validate_dataset(built.root) == []
    problems = m.validate_dataset(built.root, check_hashes=True)
    assert len(problems) == 2
    assert problems[0].startswith(
        "genomes.parquet GCF_028858775.2: md5 of 'genomes/GCF_028858775.2/"
    )
    assert problems[0].endswith(f", record says {built.genomes[1].sequence_md5}")
    assert problems[1].startswith("images.parquet synthetic:3: sha256 of 'images/9598/3.jpg' is ")
    assert problems[1].endswith(f", record says {built.images[2].sha256}")


def test_problem_lists_are_capped() -> None:
    capped = m._capped([str(i) for i in range(60)])
    assert len(capped) == 51 and capped[-1] == "... and 10 more like this"


# --------------------------------------------------------------------------- cli

runner = CliRunner()


def test_json_schema_is_a_valid_draft_2020_12_schema() -> None:
    """Strict validators (jsonschema check_schema, ajv) must accept the published schema: the
    $id may not carry a fragment (metaschema pattern ^[^#]*#?$) and every $ref must resolve."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = s.json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "#" not in schema["$id"] and schema["$id"] == s.SCHEMA_ID
    assert schema["version"] == s.SCHEMA_VERSION
    jsonschema.Draft202012Validator.check_schema(schema)  # raises SchemaError
    names = [model.__name__ for model in s.RECORD_MODELS.values()] + ["DatasetManifest"]
    assert set(names) <= set(schema["$defs"])
    for name in names:  # every nested $ref resolves (an unresolvable one raises here)
        ref = {"$ref": f"#/$defs/{name}", "$defs": schema["$defs"]}
        assert list(jsonschema.Draft202012Validator(ref).iter_errors({}))  # required missing


def test_exported_schema_file_is_in_sync() -> None:
    """schemas/dataset.schema.json is generated: re-export (genes-datasets schema-export) after
    changing the pydantic models."""
    path = Path(__file__).resolve().parents[1] / "schemas" / "dataset.schema.json"
    assert json.loads(path.read_text()) == s.json_schema()


def test_rows_validate_against_exported_json_schema(built: Built) -> None:
    """Rows written by write_table and the manifest validate against the exported JSON Schema
    (what an external consumer without pydantic would check)."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = s.json_schema()
    validator = jsonschema.Draft202012Validator
    tables = {"species": built.species, "genomes": built.genomes}
    tables |= {"images": built.images, "pairs": built.pairs}
    for table, records in tables.items():
        model = s.RECORD_MODELS[table]
        sub = {"$ref": f"#/$defs/{model.__name__}", "$defs": schema["$defs"]}
        v = validator(sub, format_checker=validator.FORMAT_CHECKER)
        for rec in m.read_table(built.root, table):
            errors = list(v.iter_errors(rec.model_dump(mode="json", by_alias=True)))
            assert errors == [], (table, errors)
        assert len(m.read_table(built.root, table)) == len(records)
    v = validator({"$ref": "#/$defs/DatasetManifest", "$defs": schema["$defs"]})
    manifest_json = json.loads((built.root / m.MANIFEST_FILE).read_text())
    assert list(v.iter_errors(manifest_json)) == []


def test_cli_schema_export(tmp_path: Path) -> None:
    out = tmp_path / "schemas" / "dataset.schema.json"
    result = runner.invoke(app, ["schema-export", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert str(out) in result.output
    assert json.loads(out.read_text()) == s.json_schema()


def test_cli_validate(built: Built) -> None:
    result = runner.invoke(app, ["validate", str(built.root), "--check-hashes"])
    assert result.exit_code == 0, result.output
    assert f"{built.root}: OK" in result.output
    (built.root / built.images[0].file).unlink()
    result = runner.invoke(app, ["validate", str(built.root)])
    assert result.exit_code == 1
    assert "images.parquet synthetic:1: file 'images/9606/1.jpg' is missing" in result.output
    assert "1 problem(s) found" in result.output


def test_cli_info(built: Built, tmp_path: Path) -> None:
    result = runner.invoke(app, ["info", str(built.root)])
    assert result.exit_code == 0, result.output
    for line in (
        "name: smoke",
        "version: 0.0.1",
        f"schema_version: {s.SCHEMA_VERSION}",
        "description: synthetic test dataset",
        "  species: species.parquet (2 rows)",
        "  n_images: 4",
        "  n_pairs: 4",
        "  - synthetic <https://example.org>",
        '"seed": 0',
    ):
        assert line in result.output, result.output
    assert runner.invoke(app, ["info", str(tmp_path)]).exit_code == 1


def test_cli_info_invalid_manifest(built: Built) -> None:
    path = built.root / "dataset.json"
    for text, expected in (
        ('{"name": "x"}', "invalid manifest: version: Field required"),
        ("{oops", "invalid manifest: "),
    ):
        path.write_text(text)
        result = runner.invoke(app, ["info", str(built.root)])
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)  # no traceback, a typer.Exit
        assert f"{path}: {expected}" in result.output
        assert "Traceback" not in result.output


def test_console_script_registered() -> None:
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert scripts.get("genes-datasets") == "datasets.cli:app"
