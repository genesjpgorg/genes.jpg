"""Validate and fingerprint every premade input before launching a comparison run."""

from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

import h5py
import numpy as np

from longevity.chunks import load_chunks
from longevity.comparison import read_spec, validate_cohort


def audit(data, comparison, out):
    data = Path(data)
    spec = read_spec(comparison)
    with ExitStack() as stack:
        df, _ = load_chunks(data, stack)
        validate_cohort(df, spec)
        if not df.groupby("assembly_accession").size().eq(1000).all():
            raise ValueError("Expected exactly 1000 inputs per genome")
        if not df.comparison_sha256.eq(spec["sha256"]).all():
            raise ValueError("H5 comparison fingerprint differs")
    fingerprints = {}
    for path in sorted(data.glob("*.h5")):
        with h5py.File(path, "r") as f:
            ids = f["input_ids"][:]
            if ids.dtype.kind not in "iu" or ids.shape != (1000, 1024):
                raise ValueError(f"Invalid IDs/shape: {path}")
            if ids.min() < 0 or ids.max() >= 32768:
                raise ValueError(f"Token outside the DNA vocabulary: {path}")
            if not (ids[:, 0] == 1).all() or not (ids[:, -1] == 2).all():
                raise ValueError(f"Missing CLS/SEP: {path}")
            if not np.isfinite(f["labels"][:]).all():
                raise ValueError(f"Nonfinite labels: {path}")
        with path.open("rb") as handle:
            fingerprints[path.name] = hashlib.file_digest(handle, "sha256").hexdigest()
    result = {
        "comparison_sha256": spec["sha256"],
        "n_assemblies": len(fingerprints),
        "n_inputs": len(df),
        "stored_positions": 1024,
        "dna_bpe_tokens": 1022,
        "chunks_per_genome": 1000,
        "h5_sha256": fingerprints,
        "inputs_per_split": {k: int(df.ncbi_taxid.isin(v).sum()) for k, v in spec["split"].items()},
    }
    Path(out).write_text(json.dumps(result, indent=2) + "\n")
    print(f"Validated {len(fingerprints)} H5 files and {len(df)} inputs", flush=True)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--comparison", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    audit(args.data, args.comparison, args.out)


if __name__ == "__main__":
    main()
