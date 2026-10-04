"""Verify, decompress and index the exact GRCh38 reference used by the predictor."""

import argparse
import gzip
import hashlib
import json
import os
import shutil
from pathlib import Path

import pysam


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Ensembl 116 GRCh38 primary assembly .fa.gz")
    parser.add_argument("destination", type=Path, help="Uncompressed .fa output")
    args = parser.parse_args()
    provenance = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/gi-longevity-final/reference_human_provenance.json"
        ).read_text()
    )
    expected = next(x["sha256"] for x in provenance["sources"] if x["url"].endswith(".fa.gz"))
    with args.source.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise SystemExit(
            "Reference checksum mismatch; use the FASTA specified in reference_human_provenance.json"
        )
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.destination.with_suffix(".tmp")
    print("Verified compressed reference; decompressing...", flush=True)
    with gzip.open(args.source, "rb") as source, temporary.open("wb") as target:
        shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
    os.replace(temporary, args.destination)
    pysam.faidx(str(args.destination))
    marker = {"source_sha256": actual, "assembly": "GRCh38", "ensembl_release": 116}
    args.destination.with_suffix(".provenance.json").write_text(json.dumps(marker, indent=2) + "\n")
    print(f"Ready: {args.destination}", flush=True)


if __name__ == "__main__":
    main()
