"""datasets.sources.treeoflife200m: the real Mammalia parquets on the shared disk (skipped when
absent) for the smoke species, small synthetic catalog/provenance fixtures for the edge cases,
and one network test against the iNaturalist open-data bucket."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from types import EllipsisType

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import requests

from datasets.schema import ImageType, SourceInfo
from datasets.sources import treeoflife200m as tol
from datasets.sources.base import ImageCandidate
from datasets.sources.treeoflife200m import (
    TreeOfLife200MSource,
    image_type_from_img_type,
    license_allowed,
    match_key,
    rewrite_inat_url,
)

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "configs" / "tol200m-mammals-smoke.json"
DATA_DIR = tol.DEFAULT_DATA_DIR
SMOKE = ["Vulpes vulpes", "Tachyglossus aculeatus", "Nonexistent species"]
S3 = f"https://{tol.INAT_S3_HOST}/photos"

CATALOG_COLUMNS = [
    "uuid", "source_url", "kingdom", "phylum", "class", "order", "family", "genus", "species",
    "scientific_name", "common", "data_source", "publisher", "basis_of_record", "img_type",
    "source_id", "shard_filename", "shard_file_path", "base_dataset_file_path",
]  # fmt: skip
PROVENANCE_COLUMNS = [
    "uuid", "source_id", "data_source", "source_url", "license_name", "copyright_owner",
    "license_link", "title", "bibliographicCitation",
]  # fmt: skip


def rnd_key(uuid: str, seed: int = 0) -> str:
    """Python mirror of the SQL ordering key."""
    return hashlib.md5(f"{seed}:{uuid}".encode()).hexdigest()


def row(
    uuid: str,
    *,
    name: str = "Vulpes vulpes",
    url: str | None | EllipsisType = ...,
    img_type: str | None = "Citizen Science",
    basis: str | None = "HUMAN_OBSERVATION",
    license: str | None = "cc-by-nc-4.0",
    source_id: str | None = None,
    data_source: str = "gbif",
    publisher: str | None = "iNaturalist.org",
    owner: str | None = "someone",
) -> dict:
    n = int(hashlib.md5(uuid.encode()).hexdigest()[:6], 16)
    return {
        "uuid": uuid,
        "source_url": f"{S3}/{n}/original.jpeg" if url is ... else url,
        "scientific_name": name,
        "data_source": data_source,
        "publisher": publisher,
        "basis_of_record": basis,
        "img_type": img_type,
        "source_id": source_id or f"obs-{uuid}",
        "license_name": license,
        "copyright_owner": owner,
        "license_link": f"http://creativecommons.org/licenses/{license}/" if license else None,
        "title": "not provided",
    }


def make_source(
    tmp_path: Path, rows: list[dict], prov_rows: list[dict] | None = None, **kwargs
) -> TreeOfLife200MSource:
    """Write catalog/provenance parquets with the real column names and build a source over
    them (``prov_rows`` defaults to ``rows``, i.e. 1:1 on uuid). ``provenance.source_url`` is
    deliberately a *different* URL so a test would notice if bytes were ever taken from
    provenance."""
    prov_rows = rows if prov_rows is None else prov_rows
    catalog = {c: [r.get(c) for r in rows] for c in CATALOG_COLUMNS}
    prov = {c: [r.get(c) for r in prov_rows] for c in PROVENANCE_COLUMNS}
    prov["source_url"] = [
        "https://example.invalid/occurrence-level/" + r["uuid"] for r in prov_rows
    ]
    for name, data in (("catalog", catalog), ("provenance", prov)):
        table = pa.table({k: pa.array(v, pa.string()) for k, v in data.items()})
        pq.write_table(table, tmp_path / f"{name}.parquet")
    kwargs.setdefault("revision", "test")
    return TreeOfLife200MSource(
        tmp_path,
        catalog_path=tmp_path / "catalog.parquet",
        provenance_path=tmp_path / "provenance.parquet",
        **kwargs,
    )


def ids(candidates: list[ImageCandidate]) -> list[str]:
    return [c.candidate_id for c in candidates]


# --------------------------------------------------------------------------- pure helpers


def test_match_key():
    assert match_key("Vulpes vulpes") == "vulpes vulpes"
    assert match_key("  VULPES Vulpes ") == "vulpes vulpes"
    assert match_key("Urocitellus parryii (Richardson, 1825)") == "urocitellus parryii"
    assert match_key("Myoprocta pratti Pocock, 1913") == "myoprocta pratti"
    assert match_key("Steno bredanensis (G. Cuvier in Lesson, 1828)") == "steno bredanensis"
    # trinomials, hybrids and genus-only labels are matched in full
    assert match_key("Canis lupus familiaris") == "canis lupus familiaris"
    assert match_key("Bos indicus x Bos taurus") == "bos indicus x bos taurus"
    assert match_key("Taphozous E. Geoffroy, 1818") == "taphozous e. geoffroy, 1818"
    assert match_key("Vulpes") == "vulpes"


def test_rewrite_inat_url():
    assert rewrite_inat_url(f"{S3}/123/original.jpeg", "large") == f"{S3}/123/large.jpeg"
    assert rewrite_inat_url(f"{S3}/123/original.JPG", "medium") == f"{S3}/123/medium.JPG"
    assert rewrite_inat_url(f"{S3}/123/original.png", "original") == f"{S3}/123/original.png"
    # extension-less and odd suffixes: the bucket serves 'large.<same suffix>' (verified live)
    assert rewrite_inat_url(f"{S3}/1212038/original.", "large") == f"{S3}/1212038/large."
    assert rewrite_inat_url(f"{S3}/791753/original.JPG_copy", "large") == (
        f"{S3}/791753/large.JPG_copy"
    )
    # catalogued at another size: rewritten too (the bucket serves every variant; verified live)
    assert rewrite_inat_url(f"{S3}/243631467/medium.jpeg", "large") == (
        f"{S3}/243631467/large.jpeg"
    )
    assert rewrite_inat_url(f"{S3}/243631467/medium.jpeg", "original") == (
        f"{S3}/243631467/original.jpeg"
    )
    assert rewrite_inat_url(f"{S3}/243631467/medium.jpeg", "medium") == (
        f"{S3}/243631467/medium.jpeg"
    )
    assert rewrite_inat_url(f"{S3}/9/square.jpg", "medium") == f"{S3}/9/medium.jpg"
    assert rewrite_inat_url(f"{S3}/9/huge.jpg", "medium") is None
    assert rewrite_inat_url("https://static.inaturalist.org/photos/1/original.jpg", "large") is None
    assert rewrite_inat_url("https://observation.org/photos/1.jpg", "large") is None
    assert rewrite_inat_url(f"{S3}/123/original.jpeg/extra", "large") is None


def test_license_allowed():
    permissive = {"cc-0-1.0", "cc-publicdomain", "cc-by-4.0", "cc-by-3.0", "cc-by", "cc-by-sa-4.0"}
    cc_nc = {"cc-by-nc-4.0", "cc-by-nc", "cc-by-nc-sa-4.0", "cc-by-nc-nd-4.0", "cc-by-nd-4.0"}
    other = {"other", "all-rights-reserved", "No known copyright restrictions", None}
    for name in permissive | cc_nc | other:
        assert license_allowed(name, "any")
        assert license_allowed(name, "cc_only") == (name is not None and name.startswith("cc-"))
        assert license_allowed(name, "permissive") == (name in permissive)
    assert license_allowed("CC-BY-4.0", "permissive")
    with pytest.raises(ValueError):
        license_allowed("cc-by-4.0", "bogus")  # type: ignore[arg-type]


def test_image_type_mapping():
    assert image_type_from_img_type("Citizen Science") is ImageType.citizen_science
    assert image_type_from_img_type("Camera-trap") is ImageType.camera_trap
    assert image_type_from_img_type("Museum Specimen: Vertebrate Zoology - Mammals") is (
        ImageType.museum_specimen
    )
    assert image_type_from_img_type("Museum Specimen: Plant") is ImageType.museum_specimen
    assert image_type_from_img_type(None) is ImageType.unknown
    assert image_type_from_img_type("Something new") is ImageType.unknown


def test_provenance_value():
    assert tol.provenance_value("not provided") is None
    assert tol.provenance_value(" Not Provided ") is None  # any case / surrounding blanks
    assert tol.provenance_value("") is None and tol.provenance_value("  ") is None
    assert tol.provenance_value(None) is None
    assert tol.provenance_value("someone") == "someone"
    assert tol.provenance_value("not provided by x") == "not provided by x"  # not the marker
    assert tol.provenance_value("Unknown") == "Unknown"  # a user-entered value stays verbatim


def test_constructor_validation(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        TreeOfLife200MSource(tmp_path, revision="x")
    src = make_source(tmp_path, [row("a")])
    with pytest.raises(ValueError):
        make_source(tmp_path, [row("a")], licenses="free")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        make_source(tmp_path, [row("a")], inat_size_variant="huge")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        make_source(tmp_path, [row("a")], max_per_observation=0)
    # a bare string would be iterated per character: rejected
    for bad in (
        {"image_types": "Citizen Science"},
        {"exclude_basis_of_record": "FOSSIL_SPECIMEN"},
        {"exclude_hosts": "content.eol.org"},
        {"aliases": {"Neogale vison": "Neovison vison"}},
    ):
        with pytest.raises(TypeError):
            make_source(tmp_path, [row("a")], **bad)
    with pytest.raises(TypeError):
        src.select("Vulpes vulpes", per_species=5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        src.species_summary("Vulpes vulpes")  # type: ignore[arg-type]
    assert src.name == "treeoflife-200m"
    assert src.select([], per_species=5) == {}
    assert src.select(["Vulpes vulpes"], per_species=0) == {"Vulpes vulpes": []}
    assert src.species_summary([]) == []


# --------------------------------------------------------------------------- fixtures


def test_fixture_max_per_observation_and_order(tmp_path: Path):
    rows = [row(f"a{i}", source_id="obs-A") for i in range(3)]
    rows += [row("b0", source_id="obs-B")]
    rows += [row(f"c{i}", source_id="obs-C") for i in range(2)]
    rows += [row("e0", source_id=None, data_source="eol", publisher=None, img_type=None)]
    rows += [row("e1", source_id=None, data_source="eol", publisher=None, img_type=None)]
    all_uuids = {r["uuid"] for r in rows}

    src = make_source(tmp_path, rows, max_per_observation=1, image_types=None)
    picked = src.select(["Vulpes vulpes"], per_species=100)["Vulpes vulpes"]
    assert len(picked) == 5  # A, B, C once each + the two EOL rows (no gbifID -> own key)
    gbif = [c for c in picked if c.source_provider == "inaturalist"]
    assert sorted(c.observation_id for c in gbif) == ["obs-A", "obs-B", "obs-C"]
    assert all(
        c.observation_id is None and c.source_provider == "eol" for c in picked if c not in gbif
    )
    # within an observation the row with the smallest md5('0:'||uuid) wins
    a_pick = next(c for c in picked if c.observation_id == "obs-A")
    assert a_pick.candidate_id == min((f"a{i}" for i in range(3)), key=rnd_key)
    # overall order is ascending by the same key
    keys = [rnd_key(c.candidate_id) for c in picked]
    assert keys == sorted(keys)

    src2 = make_source(tmp_path, rows, max_per_observation=2, image_types=None)
    picked2 = src2.select(["Vulpes vulpes"], per_species=100)["Vulpes vulpes"]
    assert len(picked2) == 7
    assert max(Counter(c.observation_id for c in picked2 if c.observation_id).values()) == 2

    src3 = make_source(tmp_path, rows, max_per_observation=None, image_types=None)
    picked3 = src3.select(["Vulpes vulpes"], per_species=100)["Vulpes vulpes"]
    assert set(ids(picked3)) == all_uuids
    assert ids(src3.select(["Vulpes vulpes"], per_species=3)["Vulpes vulpes"]) == ids(picked3)[:3]
    # the seed changes the order (and hence which photo of an observation is chosen)
    seeded = src3.select(["Vulpes vulpes"], per_species=100, seed=7)["Vulpes vulpes"]
    assert set(ids(seeded)) == all_uuids and ids(seeded) != ids(picked3)
    assert [rnd_key(c.candidate_id, 7) for c in seeded] == sorted(rnd_key(u, 7) for u in all_uuids)


def test_fixture_inat_url_rewrite(tmp_path: Path):
    rows = [
        row("jpeg", url=f"{S3}/1/original.jpeg"),
        row("noext", url=f"{S3}/2/original."),
        row("copy", url=f"{S3}/3/original.JPG_copy"),
        row("medium", url=f"{S3}/4/medium.jpeg"),
        row("static", url="https://static.inaturalist.org/photos/5/original.jpg"),
        row("obs", url="https://observation.org/photos/6.jpg", publisher="observation.org"),
    ]
    for variant in ("large", "medium"):
        src = make_source(tmp_path, rows, inat_size_variant=variant)
        by_id = {
            c.candidate_id: c
            for c in src.select(["Vulpes vulpes"], per_species=10)["Vulpes vulpes"]
        }
        assert len(by_id) == 6
        assert by_id["jpeg"].source_url == f"{S3}/1/{variant}.jpeg"
        assert by_id["jpeg"].fallback_urls == [f"{S3}/1/original.jpeg"]
        assert by_id["noext"].source_url == f"{S3}/2/{variant}."
        assert by_id["noext"].fallback_urls == [f"{S3}/2/original."]
        assert by_id["copy"].source_url == f"{S3}/3/{variant}.JPG_copy"
        # catalogued at 'medium': re-sized too, the catalogued URL kept as fallback
        assert by_id["medium"].source_url == f"{S3}/4/{variant}.jpeg"
        assert by_id["medium"].fallback_urls == (
            [] if variant == "medium" else [f"{S3}/4/medium.jpeg"]
        )
        for untouched in ("static", "obs"):
            c = by_id[untouched]
            assert c.source_url == next(r["source_url"] for r in rows if r["uuid"] == untouched)
            assert c.fallback_urls == []
    src = make_source(tmp_path, rows, inat_size_variant="original")
    for c in src.select(["Vulpes vulpes"], per_species=10)["Vulpes vulpes"]:
        catalogued = next(r["source_url"] for r in rows if r["uuid"] == c.candidate_id)
        if c.candidate_id == "medium":
            assert c.source_url == f"{S3}/4/original.jpeg" and c.fallback_urls == [catalogued]
        else:
            assert c.source_url == catalogued and c.fallback_urls == []
    # provider: iNaturalist.org publisher -> 'inaturalist', otherwise lower-cased data_source
    assert by_id["static"].source_provider == "inaturalist"
    assert by_id["obs"].source_provider == "gbif"


def test_fixture_license_policy(tmp_path: Path):
    licenses = [
        "cc-by-nc-4.0", "cc-by-4.0", "cc-0-1.0", "cc-publicdomain", "cc-by-sa-3.0", "cc-by",
        "cc-by-nd-4.0", "cc-by-nc-sa-4.0", "cc-by-nc", "other", "all-rights-reserved", None,
        "No known copyright restrictions", "CC-BY-4.0",
    ]  # fmt: skip
    rows = [row(f"u{i}", license=lic) for i, lic in enumerate(licenses)]
    for policy in ("cc_only", "permissive", "any"):
        src = make_source(tmp_path, rows, licenses=policy)
        picked = src.select(["Vulpes vulpes"], per_species=100)["Vulpes vulpes"]
        expected = {r["uuid"] for r in rows if license_allowed(r["license_name"], policy)}
        assert set(ids(picked)) == expected, policy
        assert all(c.license == rows[int(c.candidate_id[1:])]["license_name"] for c in picked)
    permissive = make_source(tmp_path, rows, licenses="permissive")
    got = {
        c.license for c in permissive.select(["Vulpes vulpes"], per_species=100)["Vulpes vulpes"]
    }
    assert got == {"cc-by-4.0", "cc-0-1.0", "cc-publicdomain", "cc-by-sa-3.0", "cc-by", "CC-BY-4.0"}
    assert not any(lic and "-nc" in lic for lic in got)


def test_fixture_eol_host_and_other_filters(tmp_path: Path):
    rows = [
        row("eol", url="https://content.eol.org/data/media/7f/66/6f/18.x.jpg", data_source="eol",
            publisher=None, img_type=None, basis=None, source_id="20279213", license="cc-by-nc-sa-3.0"),
        row("cit", img_type="Citizen Science"),
        row("cam", img_type="Camera-trap", basis="MACHINE_OBSERVATION", publisher="INBO", license=None),
        row("mus", img_type="Museum Specimen: Vertebrate Zoology - Mammals", basis="PRESERVED_SPECIMEN"),
        row("fos", img_type="Citizen Science", basis="FOSSIL_SPECIMEN"),
        row("nob", img_type="Citizen Science", basis=None),  # NULL basis is kept
        row("http", url="http://www.artportalen.se/x.jpg", publisher="SLU Artdatabanken"),
    ]  # fmt: skip
    everything = {"image_types": None, "licenses": "any", "exclude_basis_of_record": ()}
    src = make_source(tmp_path, rows, **everything)
    picked = src.select(["Vulpes vulpes"], per_species=100)["Vulpes vulpes"]
    assert set(ids(picked)) == {"cit", "cam", "mus", "fos", "nob", "http"}  # EOL host dropped
    types = {c.candidate_id: c.image_type for c in picked}
    assert types == {
        "cit": "citizen_science", "cam": "camera_trap", "mus": "museum_specimen",
        "fos": "citizen_science", "nob": "citizen_science", "http": "citizen_science",
    }  # fmt: skip
    cam = next(c for c in picked if c.candidate_id == "cam")
    assert cam.license is None and cam.license_url is None and cam.source_provider == "gbif"
    assert cam.source_image_type == "Camera-trap" and cam.publisher == "INBO"

    kept_eol = make_source(tmp_path, rows, exclude_hosts=(), **everything)
    picked = kept_eol.select(["Vulpes vulpes"], per_species=100)["Vulpes vulpes"]
    eol = next(c for c in picked if c.candidate_id == "eol")
    assert eol.source_provider == "eol" and eol.observation_id is None
    assert eol.source_id == "20279213" and eol.image_type == "unknown"
    assert eol.source_image_type is None and eol.publisher is None and eol.fallback_urls == []

    smoke = make_source(tmp_path, rows, image_types=["Citizen Science"], licenses="cc_only",
                        exclude_basis_of_record=["FOSSIL_SPECIMEN", "PRESERVED_SPECIMEN"])  # fmt: skip
    assert set(ids(smoke.select(["Vulpes vulpes"], per_species=100)["Vulpes vulpes"])) == {
        "cit",
        "nob",
        "http",
    }
    with_null = make_source(tmp_path, rows, image_types=["Camera-trap", None], licenses="any",
                            exclude_hosts=())  # fmt: skip
    assert set(ids(with_null.select(["Vulpes vulpes"], per_species=100)["Vulpes vulpes"])) == {
        "cam",
        "eol",
    }
    nothing = make_source(tmp_path, rows, image_types=[], licenses="any")
    assert nothing.select(["Vulpes vulpes"], per_species=100) == {"Vulpes vulpes": []}


def test_fixture_name_matching_and_aliases(tmp_path: Path):
    rows = [
        row("p1", name="Urocitellus parryii"),
        row("p2", name="Urocitellus parryii (Richardson, 1825)"),
        # untrimmed names (none in the current parquets): SQL must trim before the regex,
        # exactly like match_key()
        row("p3", name=" Urocitellus parryii (Richardson, 1825)"),
        row("p4", name="Urocitellus parryii (Richardson, 1825) "),
        row("m1", name="Myoprocta pratti Pocock, 1913"),
        row("v1", name="Neovison vison"),
        row("d1", name="Canis lupus familiaris"),
        row("w1", name="Canis lupus"),
    ]
    src = make_source(
        tmp_path, rows, aliases={"Neogale vison": ["Neovison vison", "Mustela vison"]}
    )
    queries = ["urocitellus PARRYII", "Myoprocta pratti", "Neogale vison", "Canis lupus", "Nope"]
    sel = src.select(queries, per_species=10)
    assert list(sel) == queries
    assert set(ids(sel["urocitellus PARRYII"])) == {"p1", "p2", "p3", "p4"}
    labels = {c.original_label for c in sel["urocitellus PARRYII"]}
    assert labels == {r["scientific_name"] for r in rows if r["uuid"].startswith("p")}
    assert {match_key(label) for label in labels} == {"urocitellus parryii"}
    assert all(c.species_query == "urocitellus PARRYII" for c in sel["urocitellus PARRYII"])
    assert ids(sel["Myoprocta pratti"]) == ["m1"]
    assert ids(sel["Neogale vison"]) == ["v1"]
    assert sel["Neogale vison"][0].original_label == "Neovison vison"
    assert ids(sel["Canis lupus"]) == ["w1"]  # the trinomial is not swept in
    assert sel["Nope"] == []
    # exact duplicates collapse; distinct names that reduce to the same key are an error
    # (not silently first-wins), as is an alias that equals another query name
    sel = src.select(["Canis lupus", "Canis lupus"], per_species=10)
    assert list(sel) == ["Canis lupus"] and ids(sel["Canis lupus"]) == ["w1"]
    with pytest.raises(ValueError, match="collide.*'Canis Lupus'"):
        src.select(["Canis lupus", "Canis Lupus"], per_species=10)
    with pytest.raises(ValueError, match="'Neovison vison'.*already matched by 'Neogale vison'"):
        src.species_summary(["Neogale vison", "Neovison vison"])
    # an alias list repeating its own query name is fine (the builder passes {q: [q, ...]})
    same = make_source(tmp_path, rows, aliases={"Canis lupus": ["Canis lupus", "canis LUPUS"]})
    assert ids(same.select(["Canis lupus"], per_species=10)["Canis lupus"]) == ["w1"]
    assert src.selection_params()["aliases"] == {
        "Neogale vison": ["Neovison vison", "Mustela vison"]
    }


def test_fixture_species_summary(tmp_path: Path):
    rows = [row(f"a{i}", source_id="obs-A") for i in range(3)]
    rows += [row("b0", source_id="obs-B"), row("b1", source_id="obs-B", license="other")]
    rows += [
        row(
            "m0",
            img_type="Museum Specimen: Vertebrate Zoology - Mammals",
            basis="PRESERVED_SPECIMEN",
        )
    ]
    rows += [row("x0", name="Other species")]
    rows += [row("n0", img_type=None), row("u0", url=None), row("u1", url="  ")]
    src = make_source(tmp_path, rows)
    summary = src.species_summary(["Vulpes vulpes", "Nope"])
    assert summary == [
        {
            "species_query": "Vulpes vulpes",
            "n_rows": 9,
            "image_types": {
                "Citizen Science": 7,
                "Museum Specimen: Vertebrate Zoology - Mammals": 1,
                tol.NULL_IMG_TYPE_KEY: 1,  # NULL img_type under a string key, not None
            },
            "n_no_url": 2,
            "n_eligible": 4,
            "n_observations": 2,
            "n_selectable": 2,
        },
        {
            "species_query": "Nope",
            "n_rows": 0,
            "image_types": {},
            "n_no_url": 0,
            "n_eligible": 0,
            "n_observations": 0,
            "n_selectable": 0,
        },
    ]
    assert tol.NULL_IMG_TYPE_KEY == "null"
    assert json.loads(json.dumps(summary, sort_keys=True)) == summary
    uncapped = make_source(tmp_path, rows, max_per_observation=None)
    assert uncapped.species_summary(["Vulpes vulpes"])[0]["n_selectable"] == 4


def test_fixture_missing_url_and_provenance(tmp_path: Path):
    rows = [row("a"), row("b"), row("nourl", url=None), row("blank", url=" ")]
    # provenance: uuid 'a' twice with different licenses, 'b' absent
    prov = [
        row("a", license="cc-by-nc-4.0"),
        row("a", license="cc-by-4.0"),
        row("nourl"),
        row("blank"),
    ]
    src = make_source(tmp_path, rows, prov_rows=prov, licenses="any", max_per_observation=None)
    picked = src.select(["Vulpes vulpes"], per_species=10)["Vulpes vulpes"]
    # url-less rows are never candidates (no literal 'None' URL); 'a' is returned once
    assert ids(picked) == sorted(["a", "b"], key=rnd_key)
    by_id = {c.candidate_id: c for c in picked}
    assert by_id["a"].license == "cc-by-4.0"  # deterministic: smallest license_name wins
    assert by_id["b"].license is None and by_id["b"].license_url is None  # kept under 'any'
    summary = src.species_summary(["Vulpes vulpes"])[0]
    assert summary["n_rows"] == 4 and summary["n_no_url"] == 2
    assert summary["n_eligible"] == summary["n_observations"] == summary["n_selectable"] == 2
    # without a provenance row there is no license to satisfy cc_only / permissive
    strict = make_source(tmp_path, rows, prov_rows=prov, licenses="cc_only")
    assert ids(strict.select(["Vulpes vulpes"], per_species=10)["Vulpes vulpes"]) == ["a"]
    permissive = make_source(tmp_path, rows, prov_rows=prov, licenses="permissive")
    got = permissive.select(["Vulpes vulpes"], per_species=10)["Vulpes vulpes"]
    assert ids(got) == ["a"] and got[0].license == "cc-by-4.0"
    assert permissive.species_summary(["Vulpes vulpes"])[0]["n_eligible"] == 1


def test_fixture_missing_value_marker_becomes_none(tmp_path: Path):
    """TreeOfLife-200M writes 'not provided' instead of NULL in copyright_owner (27% of the
    Mammalia rows); a candidate must carry None there, not the placeholder."""
    rows = [
        row("np", owner="not provided"),
        row("npc", owner="Not Provided "),
        row("blank", owner=""),
        row("real", owner="Jane Doe"),
        row("nolic", owner="not provided", license="not provided"),
    ]
    src = make_source(tmp_path, rows, licenses="any")
    by_id = {
        c.candidate_id: c for c in src.select(["Vulpes vulpes"], per_species=10)["Vulpes vulpes"]
    }
    assert set(by_id) == {"np", "npc", "blank", "real", "nolic"}
    assert by_id["np"].rights_holder is None and by_id["npc"].rights_holder is None
    assert by_id["blank"].rights_holder is None and by_id["real"].rights_holder == "Jane Doe"
    assert by_id["nolic"].license is None and by_id["real"].license == "cc-by-nc-4.0"
    assert "not provided" in src.selection_params()["missing_value_marker"]
    # the marker is not a CC licence: dropped under cc_only like a NULL licence
    strict = make_source(tmp_path, rows, licenses="cc_only")
    assert "nolic" not in ids(strict.select(["Vulpes vulpes"], per_species=10)["Vulpes vulpes"])


def test_source_info_and_selection_params(tmp_path: Path):
    src = make_source(tmp_path, [row("a")], revision="abc123", licenses="permissive")
    info = src.source_info()
    assert isinstance(info, SourceInfo)
    assert info.name == "TreeOfLife-200M" and info.revision == "abc123"
    assert info.url == tol.SOURCE_URL and info.accessed_at is not None
    assert "CC0" in info.license_notes and "CC BY-NC" in info.license_notes
    params = src.selection_params()
    assert json.loads(json.dumps(params, sort_keys=True)) == params
    assert params["catalog_path"] == str(tmp_path / "catalog.parquet")  # outside the repo
    assert params["provenance_path"] == str(tmp_path / "provenance.parquet")
    fingerprints = params["parquet_fingerprints"]
    assert set(fingerprints) == {"catalog", "provenance"}
    for name, fp in fingerprints.items():
        assert fp["n_rows"] == 1 and fp["bytes"] == (tmp_path / f"{name}.parquet").stat().st_size
        assert fp["modified_at"].endswith("+00:00")
    assert params["parquet_subset"] is None and "source_url" in params["always_dropped"]
    assert params["licenses"] == "permissive" and params["source_revision"] == "abc123"
    assert params["image_types"] == ["Citizen Science"] and params["max_per_observation"] == 1
    assert params["exclude_hosts"] == ["content.eol.org"] and params["inat_size_variant"] == "large"
    assert params["exclude_basis_of_record"] == [
        "FOSSIL_SPECIMEN",
        "PRESERVED_SPECIMEN",
        "MATERIAL_SAMPLE",
    ]


# --------------------------------------------------------------------------- real data


def real_source(**overrides) -> TreeOfLife200MSource:
    cfg = json.loads(CONFIG.read_text())["images"]
    assert cfg["source"] == "treeoflife-200m"
    kwargs: dict = {
        "revision": cfg["source_revision"],
        "image_types": cfg["image_types"],
        "exclude_basis_of_record": cfg["exclude_basis_of_record"],
        "licenses": cfg["licenses"],
        "max_per_observation": cfg["max_per_observation"],
        "inat_size_variant": cfg["inat_size_variant"],
        **overrides,
    }
    return TreeOfLife200MSource(DATA_DIR, **kwargs)


@pytest.fixture(scope="module")
def source() -> TreeOfLife200MSource:
    if not (DATA_DIR / "mammalia_catalog.parquet").exists():
        pytest.skip(f"{DATA_DIR} not available")
    return real_source()


@pytest.fixture(scope="module")
def smoke(source) -> dict[str, list[ImageCandidate]]:
    return source.select(SMOKE, per_species=5, seed=0)


def test_real_select_counts(source, smoke):
    assert list(smoke) == SMOKE
    assert [len(v) for v in smoke.values()] == [5, 5, 0]
    for query, candidates in smoke.items():
        for c in candidates:
            assert c.species_query == query and c.original_label == query
            assert c.source_dataset == "treeoflife-200m"
            assert c.source_dataset_revision == source.revision
            assert c.image_type == "citizen_science" and c.source_image_type == "Citizen Science"
            assert c.source_provider in {"inaturalist", "gbif"}
            assert c.observation_id == c.source_id and c.source_id
            assert c.license and c.license.startswith("cc-")
    all_ids = ids(smoke["Vulpes vulpes"]) + ids(smoke["Tachyglossus aculeatus"])
    assert len(set(all_ids)) == 10


def test_real_candidates_satisfy_filters(source, smoke):
    candidates = smoke["Vulpes vulpes"] + smoke["Tachyglossus aculeatus"]
    con = duckdb.connect()
    con.register("picked", pa.table({"uuid": ids(candidates)}))
    cur = con.execute(
        f"""
        SELECT c.*, p.license_name, p.license_link, p.copyright_owner
        FROM read_parquet('{source.catalog_path}') c
        JOIN read_parquet('{source.provenance_path}') p USING (uuid)
        JOIN picked USING (uuid)
        """
    )
    columns = [d[0] for d in cur.description]
    rows = [dict(zip(columns, values, strict=True)) for values in cur.fetchall()]
    con.close()
    assert len(rows) == len(candidates)
    by_uuid = {r["uuid"]: r for r in rows}
    for c in candidates:
        r = by_uuid[c.candidate_id]
        assert r["source_url"] and r["source_url"].strip()
        assert r["img_type"] in source.image_types
        assert r["basis_of_record"] not in source.exclude_basis_of_record
        assert license_allowed(r["license_name"], source.licenses)
        assert tol.url_host(r["source_url"]) not in source.exclude_hosts
        assert r["class"] == "Mammalia" and r["scientific_name"] == c.original_label
        assert (c.license, c.license_url, c.publisher, c.source_id) == (
            r["license_name"], r["license_link"], r["publisher"], r["source_id"]
        )  # fmt: skip
        assert c.rights_holder == tol.provenance_value(r["copyright_owner"])
        assert c.rights_holder != "not provided" and c.rights_holder != ""
        expected_url = (
            rewrite_inat_url(r["source_url"], source.inat_size_variant) or r["source_url"]
        )
        assert c.source_url == expected_url
        assert c.fallback_urls == ([r["source_url"]] if expected_url != r["source_url"] else [])
        if c.source_provider == "inaturalist":
            assert r["publisher"] == "iNaturalist.org"
        else:
            assert c.source_provider == r["data_source"].lower()
    assert any(c.source_url.startswith(S3) and "/large." in c.source_url for c in candidates)


def test_real_determinism_and_seed(source, smoke):
    again = real_source().select(SMOKE, per_species=5, seed=0)
    assert {k: ids(v) for k, v in again.items()} == {k: ids(v) for k, v in smoke.items()}
    other = source.select(["Vulpes vulpes"], per_species=5, seed=1)["Vulpes vulpes"]
    assert ids(other) != ids(smoke["Vulpes vulpes"])
    assert set(ids(other)) != set(ids(smoke["Vulpes vulpes"]))
    # the first k picks are a prefix of the first n > k picks, ordered by the seeded key
    more = source.select(["Vulpes vulpes"], per_species=20, seed=0)["Vulpes vulpes"]
    assert ids(more)[:5] == ids(smoke["Vulpes vulpes"])
    keys = [rnd_key(c.candidate_id, 0) for c in more]
    assert keys == sorted(keys)
    # a species' picks do not depend on which other names are in the list
    solo = source.select(["Tachyglossus aculeatus"], per_species=5, seed=0)
    assert ids(solo["Tachyglossus aculeatus"]) == ids(smoke["Tachyglossus aculeatus"])


def test_real_order_independent_of_duckdb_threads(source, smoke, monkeypatch):
    real_connect = duckdb.connect

    def single_threaded(*args, **kwargs):
        con = real_connect(*args, **kwargs)
        con.execute("SET threads = 1")
        return con

    monkeypatch.setattr(tol.duckdb, "connect", single_threaded)
    again = source.select(SMOKE, per_species=5, seed=0)
    assert {k: ids(v) for k, v in again.items()} == {k: ids(v) for k, v in smoke.items()}


def test_real_selection_params_are_portable(source):
    params = source.selection_params()
    assert params["catalog_path"] == "data/datasets/treeoflife-200m/mammalia_catalog.parquet"
    assert params["provenance_path"] == "data/datasets/treeoflife-200m/mammalia_provenance.parquet"
    assert not Path(params["catalog_path"]).is_absolute()
    fingerprints = params["parquet_fingerprints"]
    assert fingerprints["catalog"]["n_rows"] == fingerprints["provenance"]["n_rows"] == 4806057
    assert params["parquet_subset"] == tol.SUBSET_NOTE
    assert json.loads(json.dumps(params, sort_keys=True)) == params


def test_real_eol_host_filter(source):
    # the fox's 900 NULL-img_type rows are exactly its EOL rows, all on content.eol.org
    null_only = {"image_types": [None], "licenses": "any", "exclude_basis_of_record": ()}
    dropped = real_source(**null_only)
    assert dropped.select(["Vulpes vulpes"], per_species=10**6) == {"Vulpes vulpes": []}
    assert dropped.species_summary(["Vulpes vulpes"])[0]["n_eligible"] == 0
    kept = real_source(exclude_hosts=(), **null_only)
    picked = kept.select(["Vulpes vulpes"], per_species=10**6)["Vulpes vulpes"]
    assert len(picked) == 900 == kept.species_summary(["Vulpes vulpes"])[0]["n_eligible"]
    assert {tol.url_host(c.source_url) for c in picked} == {"content.eol.org"}
    assert {c.source_provider for c in picked} == {"eol"}
    assert {c.image_type for c in picked} == {"unknown"}
    assert all(c.observation_id is None and c.source_id for c in picked)
    assert all(c.source_image_type is None and c.fallback_urls == [] for c in picked)
    assert len({c.source_id for c in picked}) == 900  # EOL source_id is the media id


def test_real_max_per_observation(source):
    picked = source.select(["Vulpes vulpes"], per_species=300, seed=3)["Vulpes vulpes"]
    assert len(picked) == 300
    assert len({c.observation_id for c in picked}) == 300
    capped = real_source(max_per_observation=2).select(["Vulpes vulpes"], per_species=300, seed=3)
    assert max(Counter(c.observation_id for c in capped["Vulpes vulpes"]).values()) <= 2


def test_real_species_summary(source):
    summary = source.species_summary(SMOKE)
    fox, echidna, none = summary
    # totals as in _survey/treeoflife-200m/refseq_intersect.csv for this snapshot
    assert fox["n_rows"] == 111296 and fox["image_types"]["Citizen Science"] == 104882
    assert fox["image_types"]["Camera-trap"] == 3539 and fox["image_types"]["null"] == 900
    assert echidna["n_rows"] == 29966 and echidna["image_types"]["Citizen Science"] == 29632
    for entry in (fox, echidna):
        assert entry["n_no_url"] == 0
        assert 0 < entry["n_eligible"] <= entry["image_types"]["Citizen Science"]
        assert entry["n_selectable"] == entry["n_observations"] <= entry["n_eligible"]
    assert none == {
        "species_query": "Nonexistent species",
        "n_rows": 0,
        "image_types": {},
        "n_no_url": 0,
        "n_eligible": 0,
        "n_observations": 0,
        "n_selectable": 0,
    }
    assert json.loads(json.dumps(summary, sort_keys=True)) == summary


def test_real_permissive_excludes_nc(source):
    permissive = real_source(licenses="permissive")
    picked = permissive.select(["Vulpes vulpes"], per_species=50, seed=0)["Vulpes vulpes"]
    assert len(picked) == 50
    assert all(license_allowed(c.license, "permissive") for c in picked)
    assert not any("-nc" in c.license for c in picked)
    assert any(
        "-nc" in c.license
        for c in source.select(["Vulpes vulpes"], per_species=50)["Vulpes vulpes"]
    )


# --------------------------------------------------------------------------- network


@pytest.mark.network
def test_real_rewritten_large_urls_serve_images(source, smoke):
    inat = [c for c in smoke["Vulpes vulpes"] if c.source_url.startswith(S3)][:2]
    noext = (
        duckdb.connect()
        .execute(
            f"SELECT source_url FROM read_parquet('{source.catalog_path}') "
            "WHERE source_url LIKE '%/original.' ORDER BY source_url LIMIT 1"
        )
        .fetchone()[0]
    )
    urls = [c.source_url for c in inat] + [rewrite_inat_url(noext, "large")]
    assert len(urls) == 3 and urls[-1].endswith("/large.")
    headers = {"User-Agent": "genes.jpg/0.1 (+https://github.com/genesjpgorg/genes.jpg)"}
    for url in urls:
        r = requests.head(url, headers=headers, timeout=30, allow_redirects=True)
        assert r.status_code == 200, (url, r.status_code)
        assert r.headers.get("Content-Type", "").startswith("image/"), (url, r.headers)
        assert int(r.headers.get("Content-Length", "0")) > 1000
