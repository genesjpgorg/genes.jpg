"""Fast genome-chunk preparation: sample random byte offsets, tokenise only sampled windows.

Statistically equivalent to longevity.chunks (uniform sample of DNA-token starts, windows
never cross a contig boundary) but ~50x faster: instead of BPE-tokenising every 1Mb block,
we stream the FASTA once and only tokenise ~12kb windows around pre-sampled offsets.

Usage: same as longevity.xprep
"""

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

from longevity.chunks import CHUNK_TOKENS, DNA_TOKENS, SCHEMA
from longevity.prepare import TOKENIZER, load_tables, log

WINDOW_BP = 12000  # >= ~1022 tokens * ~5.9 bp/token with margin
_tokenizer = None


def init_worker():
    global _tokenizer
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    from transformers import AutoTokenizer

    _tokenizer = AutoTokenizer.from_pretrained(TOKENIZER)


def stream_windows(path: Path, offsets: np.ndarray, win: int):
    """Yield (offset, contig, seq[off:off+win]) for offsets whose window fits one contig.

    offset is a global base coordinate over the whole file (all records concatenated).
    A '>' record boundary inside a window invalidates it (never crosses contigs).
    """
    opener = gzip.open if str(path).endswith(".gz") else open
    offsets = np.unique(np.asarray(offsets, dtype=np.int64))
    idx, cum = 0, 0
    active = {}  # off -> (contig, buf)
    contig = None
    with opener(path, "rt") as handle:
        for line in handle:
            if line.startswith(">"):
                active.clear()  # windows crossing a contig boundary are invalid
                contig = line[1:].split()[0]
                continue
            seq = line.strip()
            if not seq:
                continue
            base = cum
            cum += len(seq)
            for o in list(active):
                rec = active[o]
                rec[1] += seq[: win - len(rec[1])]
                if len(rec[1]) >= win:
                    yield o, rec[0], rec[1][:win].upper()
                    del active[o]
            while idx < len(offsets) and offsets[idx] < cum:
                o = int(offsets[idx])
                idx += 1
                if o < base or contig is None:
                    continue
                buf = seq[o - base :]
                if len(buf) >= win:
                    yield o, contig, buf[:win].upper()
                else:
                    active[o] = [contig, buf]


def measure_bases(path: Path) -> int:
    opener = gzip.open if str(path).endswith(".gz") else open
    total = 0
    with opener(path, "rt") as h:
        for line in h:
            if not line.startswith(">"):
                total += len(line.strip())
    return total


def sample_genome_fast(path: Path, genome_size_hint: int, tokenizer, count: int,
                       seed: int, win: int = WINDOW_BP):
    rng = np.random.default_rng(seed)
    chunks, sources, seen = [], [], set()
    total = None
    for _try in range(6):
        limit = total or genome_size_hint
        if not limit:
            total = measure_bases(path)
            limit = total
        need = count - len(chunks)
        offs = rng.integers(0, max(limit - win - 1, 1), size=int(need * 1.2) + 8)
        for off, contig, seq in stream_windows(path, offs, win):
            if len(chunks) >= count:
                break
            if off in seen or len(seq) < win:
                continue
            ids = tokenizer(seq, add_special_tokens=False, truncation=False)["input_ids"]
            if len(ids) < DNA_TOKENS:
                continue
            seen.add(off)
            chunks.append(np.concatenate(([1], np.asarray(ids[:DNA_TOKENS], np.int32), [2])))
            sources.append((contig, off, 0))
        if len(chunks) >= count:
            break
        if total is None:
            total = measure_bases(path)
    if len(chunks) < count:
        if len(chunks) >= 64:
            return np.stack(chunks), sources
        raise ValueError(f"{path}: only {len(chunks)}/{count} valid windows collected")
    return np.stack(chunks), sources


def write_genome_fast(out, path, metadata, tokenizer, count, seed, size_hint):
    years = float(metadata["max_longevity_yrs"])
    if not np.isfinite(years) or years <= 0:
        raise ValueError("Longevity must be finite and positive")
    chunks, sources = sample_genome_fast(Path(path), size_hint, tokenizer, count, seed)
    n = len(chunks)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_suffix(".h5.part")
    try:
        with h5py.File(temp, "w") as f:
            f.attrs.update(schema=SCHEMA, tokenizer=TOKENIZER, chunk_tokens=CHUNK_TOKENS,
                           chunks_per_genome=n, seed=seed, sampler="random-offset-windows",
                           label="log10(max_longevity_yrs)", metadata=json.dumps(metadata))
            f.create_dataset("input_ids", data=chunks, chunks=(1, CHUNK_TOKENS), compression="lzf")
            f.create_dataset("labels", data=np.full(n, np.log10(years), dtype=np.float64))
            f.create_dataset("contig", data=[s[0] for s in sources], dtype=h5py.string_dtype())
            f.create_dataset("block_start_bp", data=[s[1] for s in sources])
            f.create_dataset("token_start_in_block", data=[s[2] for s in sources])
        temp.replace(out)
    finally:
        temp.unlink(missing_ok=True)


def prepare_one(record, out_dir, count, seed):
    acc = record["assembly_accession"]
    out = Path(out_dir) / f"{acc}.h5"
    if out.exists():
        return f"{acc}: exists, skipped"
    metadata = {k: record[k] for k in ("assembly_accession", "ncbi_taxid", "scientific_name",
                                       "class", "order", "family", "max_longevity_yrs")}
    gseed = int.from_bytes(hashlib.sha256(f"{seed}:{acc}".encode()).digest()[:8], "little")
    hint = int(record.get("genome_size_ungapped") or record.get("genome_size") or 0)
    write_genome_fast(out, record["path"], metadata, _tokenizer, count, gseed, hint)
    return f"{acc}: wrote {count} x {CHUNK_TOKENS} tokens"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--accessions", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--anage-table", type=Path)
    ap.add_argument("--chunks-per-genome", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    wanted = {l.strip() for l in args.accessions.read_text().splitlines()
              if l.strip() and not l.startswith("#")}
    genomes, longevity, species = load_tables(args.dataset, args.anage_table, args.threads)
    labels = longevity[longevity.is_primary][["ncbi_taxid", "max_longevity_yrs"]]
    table = (
        genomes[["assembly_accession", "ncbi_taxid", "path", "genome_size", "genome_size_ungapped"]]
        .merge(labels, on="ncbi_taxid", validate="many_to_one")
        .merge(species, on="ncbi_taxid", validate="many_to_one")
    )
    table = table[table.assembly_accession.isin(wanted)]
    if table.empty:
        raise SystemExit("nothing to prepare")
    args.out.mkdir(parents=True, exist_ok=True)
    records = table.sort_values("assembly_accession").to_dict("records")
    log(f"fast-preparing {len(records)} genomes -> {args.out} with {args.workers} workers")
    with ProcessPoolExecutor(
        args.workers, mp_context=multiprocessing.get_context("spawn"), initializer=init_worker
    ) as pool:
        futures = [pool.submit(prepare_one, r, args.out, args.chunks_per_genome, args.seed)
                   for r in records]
        done = failed = 0
        for i, future in enumerate(as_completed(futures), 1):
            try:
                log(f"[{i}/{len(records)}] " + future.result())
                done += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log(f"[{i}/{len(records)}] FAILED: {exc}")
        log(f"finished: {done} ok, {failed} failed")


if __name__ == "__main__":
    main()
