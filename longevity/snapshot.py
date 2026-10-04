"""Freeze verified genome paths and CDS-era labels/taxonomy for comparison preparation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from longevity.comparison import read_spec
from longevity.prepare import log


def snapshot(dataset, comparison, out, threads=8):
    dataset, out = Path(dataset).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError(f"Snapshot already exists: {out}")
    spec = read_spec(comparison)
    expected = pd.DataFrame(spec["cohort"]).set_index("assembly_accession")
    genomes = pd.read_parquet(dataset / "genomes.parquet").set_index("assembly_accession")
    if not set(expected.index).issubset(genomes.index) or genomes.index.duplicated().any():
        raise ValueError("Reference assemblies missing or duplicated in source genomes")
    genomes = genomes.loc[expected.index].copy()
    if not np.array_equal(genomes.species_taxid, expected.ncbi_taxid):
        raise ValueError("Genome species identity differs from CDS reference")
    longevity = pd.read_parquet(dataset / "longevity.parquet")
    labels = longevity[longevity.is_primary].set_index("ncbi_taxid")
    actual = labels.loc[expected.ncbi_taxid, "max_longevity_yrs"].to_numpy()
    if actual.shape != expected.max_longevity_yrs.shape or not np.allclose(
        actual, expected.max_longevity_yrs, rtol=0, atol=1e-7
    ):
        raise ValueError("Current longevity labels differ from CDS reference")

    def verify(row):
        path = dataset / row.sequence_file
        with path.open("rb") as handle:
            got = hashlib.file_digest(handle, "md5").hexdigest()
        if got != row.sequence_md5:
            raise ValueError(f"Genome checksum mismatch: {row.Index}")
        return row.Index, got

    log(f"Verifying compressed FASTA checksums for {len(genomes)} reference assemblies")
    with ThreadPoolExecutor(threads) as pool:
        checksums = dict(pool.map(verify, genomes.itertuples()))
    species = (
        expected.reset_index()
        .drop(columns=["assembly_accession", "log10_longevity", "max_longevity_yrs"])
        .drop_duplicates()
    )
    current_species = pd.read_parquet(dataset / "species.parquet").set_index("ncbi_taxid")
    differences = []
    for row in species.to_dict("records"):
        for field in ("scientific_name", "class", "order", "family"):
            current = current_species.loc[row["ncbi_taxid"], field]
            current = None if pd.isna(current) else current
            old = None if pd.isna(row[field]) else row[field]
            if current != old:
                differences.append(
                    {
                        "ncbi_taxid": row["ncbi_taxid"],
                        "field": field,
                        "current": current,
                        "reference": row[field],
                    }
                )
    genomes["sequence_file"] = genomes.sequence_file.map(
        lambda p: os.path.relpath(dataset / p, out)
    )
    frozen_labels = (
        expected[["ncbi_taxid", "max_longevity_yrs"]].drop_duplicates().assign(is_primary=True)
    )
    out.mkdir(parents=True)
    genomes.reset_index().to_parquet(out / "genomes.parquet", index=False)
    frozen_labels.to_parquet(out / "longevity.parquet", index=False)
    species.to_parquet(out / "species.parquet", index=False)
    provenance = {
        "comparison_sha256": spec["sha256"],
        "source_dataset": str(dataset),
        "verified_sequence_md5": checksums,
        "taxonomy_source": "frozen CDS comparison specification",
        "taxonomy_differences_from_current_dataset": differences,
    }
    (out / "snapshot.json").write_text(json.dumps(provenance, indent=2) + "\n")
    log(
        f"Verified {len(genomes)} assemblies; preserved {len(differences)} CDS taxonomy annotations"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--comparison", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()
    snapshot(args.dataset, args.comparison, args.out, args.threads)


if __name__ == "__main__":
    main()
