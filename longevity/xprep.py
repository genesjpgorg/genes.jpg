"""Prepare genome chunks for an explicit accession list (experiment variant of longevity.chunks).

Usage:
  python -m longevity.xprep --dataset data/datasets/anage-longevity \
      --accessions new_species.txt --out data/datasets/longevity-v2-chunks \
      --chunks-per-genome 1000 --workers 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from longevity.chunks import write_genome, CHUNK_TOKENS
from longevity.prepare import TOKENIZER, load_tables, log

_tokenizer = None


def init_worker():
    global _tokenizer
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    from transformers import AutoTokenizer

    _tokenizer = AutoTokenizer.from_pretrained(TOKENIZER)


def prepare_one(record, out_dir, count, seed, block_bases):
    acc = record["assembly_accession"]
    out = Path(out_dir) / f"{acc}.h5"
    if out.exists():
        return f"{acc}: exists, skipped"
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
    gseed = int.from_bytes(hashlib.sha256(f"{seed}:{acc}".encode()).digest()[:8], "little")
    write_genome(out, record["path"], metadata, _tokenizer, count, gseed, block_bases)
    return f"{acc}: wrote {count} x {CHUNK_TOKENS} tokens"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--accessions", type=Path, required=True, help="text file, one accession per line")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--anage-table", type=Path)
    ap.add_argument("--chunks-per-genome", type=int, default=1000)
    ap.add_argument("--block-bases", type=int, default=1_000_000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    wanted = {
        line.strip()
        for line in args.accessions.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    genomes, longevity, species = load_tables(args.dataset, args.anage_table, args.threads)
    labels = longevity[longevity.is_primary][["ncbi_taxid", "max_longevity_yrs"]]
    table = (
        genomes[["assembly_accession", "ncbi_taxid", "path"]]
        .merge(labels, on="ncbi_taxid", validate="many_to_one")
        .merge(species, on="ncbi_taxid", validate="many_to_one")
    )
    table = table[table.assembly_accession.isin(wanted)]
    missing = wanted - set(table.assembly_accession)
    if missing:
        log(f"WARNING: {len(missing)} accessions missing label/metadata: {sorted(missing)[:10]}")
    if table.empty:
        raise SystemExit("nothing to prepare")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "manifest.json").write_text(json.dumps({
        "accessions_file": str(args.accessions),
        "n_wanted": len(wanted), "n_matched": len(table),
        "chunks_per_genome": args.chunks_per_genome, "seed": args.seed,
    }, indent=2))
    records = table.sort_values("assembly_accession").to_dict("records")
    log(f"preparing {len(records)} genomes -> {args.out} with {args.workers} workers")
    with ProcessPoolExecutor(
        args.workers, mp_context=multiprocessing.get_context("spawn"), initializer=init_worker
    ) as pool:
        futures = [
            pool.submit(prepare_one, r, args.out, args.chunks_per_genome, args.seed, args.block_bases)
            for r in records
        ]
        done = failed = 0
        for i, future in enumerate(as_completed(futures), 1):
            try:
                log(f"[{i}/{len(records)}] " + future.result())
                done += 1
            except Exception as exc:  # noqa: BLE001 - keep going, report at end
                failed += 1
                log(f"[{i}/{len(records)}] FAILED: {exc}")
        log(f"finished: {done} ok, {failed} failed")


if __name__ == "__main__":
    main()
