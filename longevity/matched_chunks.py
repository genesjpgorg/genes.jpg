"""Prepare reduced, per-genome budgets, reusing verified 1000-chunk pools when available."""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import h5py
import numpy as np
import pandas as pd

from longevity import chunks
from longevity.comparison import chunk_counts, read_spec, validate_cohort
from longevity.prepare import load_tables, log


def verify_shard(path, metadata, fingerprint, count, seed, block_bases):
    with h5py.File(path, "r") as f:
        attrs = f.attrs
        if (
            attrs.get("schema") != chunks.SCHEMA
            or attrs.get("tokenizer") != chunks.TOKENIZER
            or attrs.get("seed") != seed
            or attrs.get("block_bases") != block_bases
            or attrs.get("chunks_per_genome") != count
            or f["input_ids"].shape != (count, 1024)
            or f["labels"].shape != (count,)
        ):
            raise ValueError(f"Incompatible shard: {path}")
        actual = json.loads(attrs["metadata"])
        if actual.get("comparison_sha256") != fingerprint:
            raise ValueError(f"Shard fingerprint mismatch: {path}")
        for key, value in metadata.items():
            if key == "comparison_sha256":
                continue
            old = actual[key]
            if pd.isna(old) and pd.isna(value):
                continue
            if old != value:
                raise ValueError(f"Shard metadata mismatch for {key}: {path}")
        labels = f["labels"][:]
        if not np.isfinite(labels).all() or not np.allclose(
            labels, np.log10(metadata["max_longevity_yrs"])
        ):
            raise ValueError(f"Shard label mismatch: {path}")
        ids = f["input_ids"][:]
        if not ((ids[:, 0] == 1).all() and (ids[:, -1] == 2).all()):
            raise ValueError(f"Shard special tokens mismatch: {path}")
        if ids.dtype.kind not in "iu" or ids.min() < 0 or ids.max() >= 32768:
            raise ValueError(f"Invalid shard token IDs: {path}")
        if any(
            len(f[key]) != count for key in ("contig", "block_start_bp", "token_start_in_block")
        ):
            raise ValueError(f"Invalid provenance arrays: {path}")


def take_prefix(source, target, metadata, count):
    """Keep a fixed prefix; never select inputs using labels or held-out performance."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".h5.part")
    try:
        with h5py.File(source, "r") as src, h5py.File(temp, "w") as dst:
            if not 0 < count <= len(src["labels"]):
                raise ValueError("Prefix count exceeds source pool")
            dst.attrs.update(dict(src.attrs))
            dst.attrs["sampling_pool_size"] = len(src["labels"])
            dst.attrs["chunks_per_genome"] = count
            dst.attrs["metadata"] = json.dumps(metadata)
            for key in src:
                kwargs = {"chunks": (1, 1024), "compression": "lzf"} if key == "input_ids" else {}
                dst.create_dataset(key, data=src[key][:count], dtype=src[key].dtype, **kwargs)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)


def prepare_one(record, spec, out, reuse, resume):
    acc = record["assembly_accession"]
    metadata = {k: v for k, v in record.items() if k not in {"path", "log10_longevity"}}
    metadata["comparison_sha256"] = spec["sha256"]
    count = chunk_counts(spec)[acc]
    seed = int.from_bytes(
        hashlib.sha256(f"{spec['settings']['seed']}:{acc}".encode()).digest()[:8], "little"
    )
    target = out / f"{acc}.h5"
    block_bases = 1_000_000
    if target.exists():
        if not resume:
            raise FileExistsError(target)
        verify_shard(target, metadata, spec["sha256"], count, seed, block_bases)
        return f"{acc}: verified existing {count} chunks"
    source = reuse / f"{acc}.h5" if reuse else None
    reused = source is not None and source.exists()
    if reused:
        verify_shard(
            source,
            metadata,
            spec["parent_comparison_sha256"],
            spec["sampling_pool_size"],
            seed,
            block_bases,
        )
    else:
        source = out / ".pools" / f"{acc}.h5"
        # Preserve the original 1000-draw RNG stream, so reuse and fresh preparation agree.
        chunks.write_genome(
            source,
            record["path"],
            metadata,
            chunks._tokenizer,
            spec["sampling_pool_size"],
            seed,
            block_bases,
        )
    take_prefix(source, target, metadata, count)
    verify_shard(target, metadata, spec["sha256"], count, seed, block_bases)
    if not reused:
        source.unlink()
    return f"{acc}: {'reused' if reused else 'prepared'} {count} chunks"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--comparison", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--reuse-from", type=Path)
    ap.add_argument("--workers", type=int, default=96)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    if args.workers < 1:
        ap.error("workers must be positive")
    spec = read_spec(args.comparison)
    counts = chunk_counts(spec)
    if not 0 < max(counts.values()) <= spec["sampling_pool_size"]:
        ap.error("Invalid sampling pool size")
    genomes, _, _ = load_tables(args.dataset, None, 4, set(counts))
    paths = genomes.set_index("assembly_accession").path.to_dict()
    if set(paths) != set(counts):
        raise ValueError("Frozen genome paths do not match the cohort")
    records = [dict(row, path=paths[row["assembly_accession"]]) for row in spec["cohort"]]
    validate_cohort(pd.DataFrame(records), spec)
    extras = {p.stem for p in args.out.glob("*.h5")} - set(counts)
    if extras:
        raise ValueError(f"Unexpected output shards: {sorted(extras)}")
    log(
        f"Preparing {sum(counts.values())} chunks across {len(records)} genomes with {args.workers} workers"
    )
    with ProcessPoolExecutor(
        args.workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=chunks.init_worker,
    ) as pool:
        futures = [
            pool.submit(prepare_one, row, spec, args.out, args.reuse_from, args.resume)
            for row in records
        ]
        for i, future in enumerate(as_completed(futures), 1):
            log(f"[{i}/{len(records)}] {future.result()}")


if __name__ == "__main__":
    main()
