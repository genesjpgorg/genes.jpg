"""End-to-end smoke test: build and validate a complete tiny dataset with the public APIs of
``datasets.ncbi``, ``datasets.images`` and ``datasets.manifest``.

Species
- Panthera leo: taxonomy, best RefSeq assembly via ``list_refseq_assemblies`` +
  ``pick_best_assembly`` (recorded in the manifest, NOT downloaded: ~2.4 Gb), 3 GBIF images.
- Escherichia coli: taxonomy, reference assembly GCF_000005845.2 (K-12 MG1655, 4.6 Mb, ~1.4 MB
  gzipped) downloaded with ``download_genome``, 2 GBIF images, so the pairs table has rows.
  E. coli has >50,000 current RefSeq assemblies, so the reference is looked up by accession
  (``get_assembly_report``) instead of listing them all.

Usage::

    .venv/bin/python scripts/e2e_tiny_dataset.py [--root DIR] [--clean] [--no-progress]

Default root: data/datasets/_e2e_tiny (symlink to the shared disk). Re-running on an existing
root reuses verified genome and image files and must succeed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests

from datasets import images, manifest, ncbi
from datasets.schema import GenomeRecord, ImageRecord, PairRecord, SourceInfo, SpeciesRecord

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO / "data" / "datasets" / "_e2e_tiny"
GBIF_SEARCH = "https://api.gbif.org/v1/occurrence/search"
_CC_URL = re.compile(
    r"creativecommons\.org/(?:licenses/(?P<kind>[a-z-]+)|publicdomain/(?P<pd>zero|mark))/(?P<ver>\d\.\d)"
)
_IMAGE_TYPES = {
    "HUMAN_OBSERVATION": "citizen_science",
    "MACHINE_OBSERVATION": "camera_trap",
    "PRESERVED_SPECIMEN": "museum_specimen",
    "MATERIAL_SAMPLE": "lab_specimen",
}


@dataclass(frozen=True)
class Target:
    name: str
    gbif_key: int
    n_images: int
    reference_accession: str | None = None
    """Download this assembly; None = look the species' best RefSeq assembly up, no download."""


TARGETS = [
    Target("Panthera leo", gbif_key=5219404, n_images=3),
    Target(
        "Escherichia coli", gbif_key=11286021, n_images=2, reference_accession="GCF_000005845.2"
    ),
]
MAX_GENOME_SIZE_BP = 10_000_000  # genome_size guard: never fetch anything mammal-sized here


def log(msg: str) -> None:
    print(f"[e2e {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_license(value: str | None) -> tuple[str | None, str | None]:
    """GBIF license value -> (name, url): CC URLs become 'CC BY-NC 4.0' / 'CC0 1.0', anything
    else is kept verbatim as the name."""
    if not value:
        return None, None
    url = value if value.startswith("http") else None
    if m := _CC_URL.search(value):
        if m["pd"]:
            name = f"CC0 {m['ver']}" if m["pd"] == "zero" else f"Public Domain Mark {m['ver']}"
        else:
            name = f"CC {m['kind'].upper()} {m['ver']}"
        return name, url
    return value, url


def species_record(taxon: ncbi.TaxonInfo) -> SpeciesRecord:
    if taxon.rank != "species":
        raise SystemExit(f"{taxon.query!r} resolved to rank {taxon.rank!r}, expected species")

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
    )


def gbif_image_tasks(target: Target, taxid: int, root: Path, limit: int) -> list[images.ImageTask]:
    """One ImageTask per GBIF occurrence with a StillImage (first medium only), oldest
    gbifID first so re-runs pick the same images."""
    r = requests.get(
        GBIF_SEARCH,
        params={"taxonKey": target.gbif_key, "mediaType": "StillImage", "limit": limit},
        headers={"User-Agent": images.USER_AGENT},
        timeout=60,
    )
    r.raise_for_status()
    tasks: list[images.ImageTask] = []
    for occ in sorted(r.json()["results"], key=lambda o: int(o["key"])):
        medium = next(
            (
                m
                for m in occ.get("media", [])
                if m.get("type") == "StillImage" and m.get("identifier")
            ),
            None,
        )
        if medium is None:
            continue
        gbif_id = str(occ["key"])
        license_name, license_url = parse_license(medium.get("license") or occ.get("license"))
        basis = occ.get("basisOfRecord")
        tasks.append(
            images.ImageTask(
                image_id=f"gbif:{gbif_id}",
                url=medium["identifier"],
                dest=root / "images" / str(taxid) / f"gbif_{gbif_id}",
                meta={
                    "ncbi_taxid": taxid,
                    "source_dataset": "gbif",
                    "source_dataset_revision": datetime.now(UTC).date().isoformat(),
                    "source_provider": "gbif",
                    "source_id": gbif_id,
                    "observation_id": gbif_id,
                    "original_label": occ.get("scientificName")
                    or occ.get("species")
                    or target.name,
                    "image_type": _IMAGE_TYPES.get(basis or "", "unknown"),
                    "source_image_type": basis,
                    "publisher": medium.get("publisher")
                    or occ.get("publisher")
                    or occ.get("datasetName"),
                    "license": license_name,
                    "license_url": license_url,
                    "rights_holder": medium.get("rightsHolder") or occ.get("rightsHolder"),
                },
            )
        )
    return tasks


def download_n_images(
    tasks: list[images.ImageTask], n: int, root: Path, *, progress: bool
) -> tuple[list[ImageRecord], list[images.ImageResult]]:
    """Download candidates in order until ``n`` succeeded; returns records and all results."""
    records: list[ImageRecord] = []
    results: list[images.ImageResult] = []
    queue = list(tasks)
    while len(records) < n and queue:
        batch, queue = queue[: n - len(records)], queue[n - len(records) :]
        batch_results = images.download_images(
            batch, max_workers=4, per_host_rps=2.0, progress=progress
        )
        results += batch_results
        for res in batch_results:
            if res.ok:
                records.append(images.to_image_record(res, root))
            else:
                log(f"  image {res.task.image_id} failed: {res.error}")
    if len(records) < n:
        raise SystemExit(f"only {len(records)} of {n} images could be downloaded")
    return records, results


def assembly_summary(row: ncbi.AssemblySummaryRow, *, downloaded: bool) -> dict[str, object]:
    return {
        "assembly_accession": row.assembly_accession,
        "assembly_name": row.asm_name,
        "assembly_level": row.assembly_level,
        "refseq_category": row.refseq_category,
        "release_date": row.seq_rel_date,
        "genome_size": row.genome_size,
        "ftp_path": row.ftp_path,
        "downloaded": downloaded,
    }


def build(root: Path, *, progress: bool) -> None:
    started = datetime.now(UTC)
    root.mkdir(parents=True, exist_ok=True)
    species: list[SpeciesRecord] = []
    genomes: list[GenomeRecord] = []
    image_records: list[ImageRecord] = []
    all_results: list[images.ImageResult] = []
    assemblies: dict[str, dict[str, object]] = {}

    for target in TARGETS:
        taxon = ncbi.resolve_taxon(target.name)
        if taxon is None:
            raise SystemExit(f"NCBI does not know {target.name!r}")
        record = species_record(taxon)
        log(f"{target.name}: taxid {record.ncbi_taxid}, {len(record.lineage_taxids)} lineage nodes")

        if target.reference_accession is None:
            rows = ncbi.list_refseq_assemblies(record.ncbi_taxid)
            best = ncbi.pick_best_assembly(rows)
            log(
                f"  {len(rows)} RefSeq assemblies; best {best.assembly_accession} ({best.asm_name}, "
                f"{best.assembly_level}, {best.genome_size:,} bp) - not downloaded"
            )
            assemblies[target.name] = assembly_summary(best, downloaded=False)
        else:
            row = ncbi.get_assembly_report(target.reference_accession)
            if row is None:
                raise SystemExit(f"no assembly report for {target.reference_accession}")
            if (
                row.species_taxid != record.ncbi_taxid
                or (row.genome_size or 0) > MAX_GENOME_SIZE_BP
            ):
                raise SystemExit(
                    f"refusing {row.assembly_accession}: {row.species_taxid=} {row.genome_size=}"
                )
            genome = ncbi.download_genome(row, root, progress=progress)
            log(
                f"  downloaded {genome.assembly_accession}: {genome.sequence_bytes:,} bytes, "
                f"{genome.n_sequences} sequences, md5 {genome.sequence_md5}"
            )
            genomes.append(genome)
            assemblies[target.name] = assembly_summary(row, downloaded=True)

        tasks = gbif_image_tasks(target, record.ncbi_taxid, root, limit=4 * target.n_images)
        log(f"  {len(tasks)} GBIF image candidates, need {target.n_images}")
        recs, results = download_n_images(tasks, target.n_images, root, progress=progress)
        reused = sum(r.reused for r in results if r.ok)
        log(f"  {len(recs)} images ok ({reused} reused), {sum(not r.ok for r in results)} failed")
        image_records += recs
        all_results += results

        record.n_images = len(recs)
        record.n_genomes = sum(g.species_taxid == record.ncbi_taxid for g in genomes)
        record.source_names = sorted({r.original_label for r in recs})
        species.append(record)

    by_species = {g.species_taxid: g for g in genomes}
    pairs = [
        PairRecord(
            image_id=i.image_id,
            assembly_accession=by_species[i.ncbi_taxid].assembly_accession,
            ncbi_taxid=i.ncbi_taxid,
        )
        for i in image_records
        if i.ncbi_taxid in by_species
    ]

    for name, records in (
        ("species", species),
        ("genomes", genomes),
        ("images", image_records),
        ("pairs", pairs),
    ):
        manifest.write_table(root, name, records)
    failures = images.write_failures(all_results, root / "logs" / "image_failures.jsonl")
    log(
        f"tables written: {len(species)} species, {len(genomes)} genomes, {len(image_records)} images, "
        f"{len(pairs)} pairs; failures -> {failures.relative_to(root)}"
    )

    m = manifest.build_manifest(
        root,
        name="_e2e_tiny",
        version="0.1.0",
        description="End-to-end smoke dataset built by scripts/e2e_tiny_dataset.py: Panthera leo "
        "(images only; reference assembly recorded under selection.reference_assemblies) and "
        "Escherichia coli (reference genome + images).",
        sources=[
            SourceInfo(
                name="NCBI Taxonomy / RefSeq (Datasets API v2)",
                url=ncbi.API_URL,
                accessed_at=started,
            ),
            SourceInfo(
                name="GBIF occurrence API",
                url=GBIF_SEARCH,
                accessed_at=started,
                license_notes="per-image license in images.license / license_url",
            ),
        ],
        selection={
            "targets": [
                {"name": t.name, "gbif_taxon_key": t.gbif_key, "n_images": t.n_images}
                for t in TARGETS
            ],
            "gbif_query": {"mediaType": "StillImage", "first_medium_per_occurrence": True},
            "reference_assemblies": assemblies,
            "max_download_genome_size": MAX_GENOME_SIZE_BP,
        },
    )
    manifest.write_manifest(root, m)
    log(f"manifest written: counts {m.counts}")


def check(root: Path) -> None:
    problems = manifest.validate_dataset(root, check_hashes=True)
    for p in problems:
        log(f"  PROBLEM: {p}")
    if problems:
        raise SystemExit(f"validate_dataset: {len(problems)} problem(s)")
    log("validate_dataset(check_hashes=True): no problems")

    exe = shutil.which("genes-datasets", path=str(Path(sys.executable).parent))
    cmd = (
        [exe, "validate", str(root), "--check-hashes"]
        if exe
        else [sys.executable, "-m", "datasets.cli", "validate", str(root), "--check-hashes"]
    )
    cli = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if cli.returncode != 0 or not cli.stdout.rstrip().endswith(": OK"):
        raise SystemExit(f"{' '.join(cmd)} -> exit {cli.returncode}\n{cli.stdout}{cli.stderr}")
    log(f"{Path(cmd[0]).name} validate: {cli.stdout.strip()}")

    # read-back round trip and consistency with the manifest
    m = manifest.read_manifest(root)
    for name in manifest.TABLE_NAMES:
        rows = manifest.read_table(root, name)
        assert len(rows) == m.tables[name].n_rows == m.counts[f"n_{name}"], name
    assert m.counts["n_pairs"] > 0 and m.counts["genome_bytes"] > 0, m.counts
    log(f"read_table/read_manifest round trip OK: {json.dumps(m.counts)}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--clean", action="store_true", help="delete ROOT first (fresh build)")
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()
    root: Path = args.root
    if args.clean and root.exists():
        log(f"removing {root}")
        shutil.rmtree(root)
    log(f"dataset root: {root}" + (" (exists, re-run)" if root.exists() else ""))
    build(root, progress=not args.no_progress)
    check(root)
    log("PASSED")


if __name__ == "__main__":
    main()
