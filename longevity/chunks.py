"""Prepare fixed genome BPE samples: python -m longevity.chunks --help."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import h5py
import numpy as np
import pandas as pd

from longevity.prepare import TOKENIZER, load_tables, log

CHUNK_TOKENS = 1024
DNA_TOKENS = CHUNK_TOKENS - 2
SCHEMA = "longevity-genome-chunks-v2"


def fasta_blocks(path: Path, block_bases: int = 1_000_000):
    """Stream bounded blocks without ever joining different FASTA records."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:
        contig, sequence, offset = None, "", 0
        for line in handle:
            if line.startswith(">"):
                if sequence:
                    yield contig, offset, sequence.upper()
                contig, sequence, offset = line[1:].split()[0], "", 0
            else:
                if contig is None and line.strip():
                    raise ValueError(f"Sequence before FASTA header: {path}")
                sequence += line.strip()
                while len(sequence) >= block_bases:
                    yield contig, offset, sequence[:block_bases].upper()
                    sequence = sequence[block_bases:]
                    offset += block_bases
        if sequence:
            yield contig, offset, sequence.upper()


def sample_genome(path, tokenizer, count=1000, seed=0, block_bases=1_000_000):
    """Independent uniform samples with replacement over valid block-local token starts.

    Weighted reservoir updates avoid holding a tokenized genome in memory. BPE is
    applied independently per block; windows never cross block/contig boundaries.
    """
    if count < 1 or block_bases < CHUNK_TOKENS:
        raise ValueError("count must be positive and block_bases must be >= 1024")
    rng = np.random.default_rng(seed)
    chunks = np.empty((count, CHUNK_TOKENS), dtype=np.int32)
    sources = [None] * count
    total = 0
    for contig, offset, sequence in fasta_blocks(Path(path), block_bases):
        ids = np.asarray(
            tokenizer(sequence, add_special_tokens=False, truncation=False)["input_ids"],
            dtype=np.int32,
        )
        starts = len(ids) - DNA_TOKENS + 1
        if starts <= 0:
            continue
        total += starts
        replace = np.flatnonzero(rng.random(count) < starts / total)
        for i, start in zip(replace, rng.integers(starts, size=len(replace))):
            chunks[i] = np.concatenate(([1], ids[start : start + DNA_TOKENS], [2]))
            sources[i] = (contig, offset, int(start))
    if not total:
        raise ValueError(f"{path}: no block contains 1022 DNA BPE tokens; cannot pad exact chunks")
    return chunks, sources


def write_genome(out, path, metadata, tokenizer, count=1000, seed=0, block_bases=1_000_000):
    """Write one atomic H5 shard with repeated, unnormalized log10(years) labels."""
    years = float(metadata["max_longevity_yrs"])
    if not np.isfinite(years) or years <= 0:
        raise ValueError("Longevity must be finite and positive")
    chunks, sources = sample_genome(path, tokenizer, count, seed, block_bases)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_suffix(".h5.part")
    try:
        with h5py.File(temp, "w") as f:
            f.attrs.update(
                schema=SCHEMA,
                tokenizer=TOKENIZER,
                chunk_tokens=CHUNK_TOKENS,
                chunks_per_genome=count,
                seed=seed,
                block_bases=block_bases,
                label="log10(max_longevity_yrs)",
                metadata=json.dumps(metadata),
            )
            f.create_dataset("input_ids", data=chunks, chunks=(1, CHUNK_TOKENS), compression="lzf")
            f.create_dataset("labels", data=np.full(count, np.log10(years), dtype=np.float64))
            f.create_dataset("contig", data=[s[0] for s in sources], dtype=h5py.string_dtype())
            f.create_dataset("block_start_bp", data=[s[1] for s in sources])
            f.create_dataset("token_start_in_block", data=[s[2] for s in sources])
        temp.replace(out)
    finally:
        temp.unlink(missing_ok=True)


class H5Rows:
    """Lazy sequence backed by open H5 datasets; subsets share handles owned by ExitStack."""

    def __init__(self, files, rows):
        self.files, self.rows = files, rows

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        shard, row = self.rows[index]
        return self.files[shard]["input_ids"][row]

    def subset(self, indices):
        return H5Rows(self.files, self.rows[np.asarray(indices, dtype=int)])


def load_chunks(path: Path, stack):
    """Read labels/metadata, leaving token arrays on disk throughout training."""
    paths = sorted(path.glob("*.h5")) if path.is_dir() else [path]
    if not paths:
        raise ValueError(f"No H5 shards in {path}")
    files, frames, rows = [], [], []
    for shard, p in enumerate(paths):
        f = stack.enter_context(h5py.File(p, "r"))
        files.append(f)
        if f.attrs.get("schema") != SCHEMA or f.attrs.get("tokenizer") != TOKENIZER:
            raise ValueError(f"Unsupported chunk schema/tokenizer: {p}")
        n = len(f["labels"])
        if n == 0 or f["input_ids"].shape != (n, CHUNK_TOKENS) or n != f.attrs["chunks_per_genome"]:
            raise ValueError(f"Invalid chunk shape/count: {p}")
        if not (f["input_ids"][:, 0] == 1).all() or not (f["input_ids"][:, -1] == 2).all():
            raise ValueError(f"Missing CLS/SEP in premade H5 inputs: {p}")
        meta = json.loads(f.attrs["metadata"])
        labels = f["labels"][:]
        expected = np.log10(float(meta["max_longevity_yrs"]))
        if not np.isfinite(labels).all() or not np.allclose(labels, expected):
            raise ValueError(f"Labels disagree with genome longevity: {p}")
        frame = pd.DataFrame([meta] * n)
        frame["log10_longevity"] = labels
        frame["chunk_id"] = np.arange(n)
        frame["n_tokens"] = DNA_TOKENS
        frame["_row"] = np.arange(sum(map(len, frames)), sum(map(len, frames)) + n)
        frames.append(frame)
        rows.extend((shard, i) for i in range(n))
    df = pd.concat(frames, ignore_index=True)
    if df.groupby("ncbi_taxid").log10_longevity.nunique().max() > 1:
        raise ValueError("Conflicting longevity labels for the same species")
    if df.duplicated(["assembly_accession", "chunk_id"]).any():
        raise ValueError("Duplicate genome shards")
    return df, H5Rows(files, np.asarray(rows))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--anage-table", type=Path)
    ap.add_argument("--chunks-per-genome", type=int, default=1000)
    ap.add_argument("--block-bases", type=int, default=1_000_000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--workers", type=int, default=1, help="parallel genome tokenization processes")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--comparison", type=Path, help="Frozen CDS comparison specification")
    args = ap.parse_args()
    if args.chunks_per_genome < 1 or args.block_bases < CHUNK_TOKENS:
        ap.error("chunks-per-genome must be positive; block-bases must be >= 1024")
    if args.workers < 1:
        ap.error("--workers must be positive")
    spec, wanted = None, None
    if args.comparison:
        from longevity.comparison import read_spec

        spec = read_spec(args.comparison)
        wanted = {r["assembly_accession"] for r in spec["cohort"]}
    genomes, longevity, species = load_tables(args.dataset, args.anage_table, args.threads, wanted)
    labels = longevity[longevity.is_primary][["ncbi_taxid", "max_longevity_yrs"]]
    table = (
        genomes[["assembly_accession", "species_taxid", "path"]]
        .merge(labels, left_on="species_taxid", right_on="ncbi_taxid", validate="many_to_one")
        .merge(species, on="ncbi_taxid", validate="many_to_one")
    )
    if len(table) != len(genomes) or table.empty:
        raise ValueError("Every genome must have a primary longevity label and species metadata")
    if spec:
        from longevity.comparison import validate_cohort

        if args.seed != spec["settings"]["seed"]:
            ap.error("chunk preparation seed must match the comparison specification")
        wanted = {r["assembly_accession"] for r in spec["cohort"]}
        table = table[table.assembly_accession.isin(wanted)].copy()
        table["log10_longevity"] = np.log10(table.max_longevity_yrs)
        validate_cohort(table, spec)
        if args.chunks_per_genome != 1000:
            ap.error("CDS comparison requires 1000 chunks per genome")
        extras = {p.stem for p in args.out.glob("*.h5")} - wanted
        if extras:
            raise ValueError(f"Output directory contains non-comparison genomes: {sorted(extras)}")
    records = table.sort_values("assembly_accession").to_dict("records")
    for record in records:
        out = args.out / f"{record['assembly_accession']}.h5"
        if out.exists() and not args.force:
            raise FileExistsError(f"{out}: use --force to replace existing chunks")
    if args.workers == 1:
        init_worker()
        for record in records:
            log(prepare_one(record, args, spec))
    else:
        with ProcessPoolExecutor(
            args.workers, mp_context=multiprocessing.get_context("spawn"), initializer=init_worker
        ) as pool:
            futures = [pool.submit(prepare_one, record, args, spec) for record in records]
            for i, future in enumerate(as_completed(futures), 1):
                log(f"[{i}/{len(records)}] " + future.result())


def init_worker():
    global _tokenizer
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    from transformers import AutoTokenizer

    _tokenizer = AutoTokenizer.from_pretrained(TOKENIZER)


def prepare_one(record, args, spec):
    acc = record["assembly_accession"]
    out = args.out / f"{acc}.h5"
    metadata = {
        k: record[k]
        for k in (
            "assembly_accession",
            "ncbi_taxid",
            "scientific_name",
            "class",
            "order",
            "family",
            "max_longevity_yrs",
        )
    }
    seed = int.from_bytes(hashlib.sha256(f"{args.seed}:{acc}".encode()).digest()[:8], "little")
    if spec:
        metadata["comparison_sha256"] = spec["sha256"]
    write_genome(
        out, record["path"], metadata, _tokenizer, args.chunks_per_genome, seed, args.block_bases
    )
    return f"{acc}: wrote {args.chunks_per_genome} x {CHUNK_TOKENS} tokens to {out}"


if __name__ == "__main__":
    main()
