"""Build pairs.parquet from a (possibly partial) miniprot run — incremental version of
prepare.py stages 3-4, so training can start before all genomes are aligned.

Usage: python -m longevity.xmakepairs --dataset <ds> --miniprot-dir <dir> --genes <csv> \
       --human-proteins <faa> --out-dir <dir> [--min-species N] [--wait N]
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from longevity.prepare import extract_cds, select_proteins, tokenize, load_tables
from longevity.train import log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--miniprot-dir", type=Path, required=True)
    ap.add_argument("--genes", type=Path, required=True)
    ap.add_argument("--human-proteins", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--threads", type=int, default=20)
    ap.add_argument("--min-identity", type=float, default=0.3)
    ap.add_argument("--min-coverage", type=float, default=0.5)
    ap.add_argument("--wait", type=int, default=0,
                    help="wait until this many gffs exist (poll every 60s)")
    ap.add_argument("--pairs-out", type=Path, default=None)
    args = ap.parse_args()

    out = args.out_dir
    (out / "cds").mkdir(parents=True, exist_ok=True)

    genes = pd.read_csv(args.genes)
    prot = select_proteins(args.human_proteins, genes, out / "proteins.faa")
    prot.to_csv(out / "proteins.csv", index=False)
    genomes, longevity, species = load_tables(args.dataset, None, args.threads)

    while len(list(args.miniprot_dir.glob("*.gff"))) < args.wait:
        log(f"waiting: {len(list(args.miniprot_dir.glob('*.gff')))}/{args.wait} gffs")
        time.sleep(60)

    gffs = {p.stem for p in args.miniprot_dir.glob("*.gff")}
    genomes = genomes[genomes.assembly_accession.isin(gffs)]
    log(f"{len(genomes)} genomes with gff")

    todo = [g for g in genomes.itertuples()
            if not (out / "cds" / f"{g.assembly_accession}.parquet").exists()]
    if todo:
        with ProcessPoolExecutor(min(len(todo), args.threads)) as ex:
            futs = [ex.submit(extract_cds, g.assembly_accession, g.path,
                              args.miniprot_dir / f"{g.assembly_accession}.gff",
                              out / "cds" / f"{g.assembly_accession}.parquet") for g in todo]
            for f in futs:
                log("cds " + f.result())
    cds_files = sorted((out / "cds").glob("*.parquet"))
    accs = {p.stem for p in cds_files}
    genomes = genomes[genomes.assembly_accession.isin(accs)]
    hits = pd.concat([pd.read_parquet(p) for p in cds_files
                      if p.stem in set(genomes.assembly_accession)], ignore_index=True)
    q = hits["query"].str.split("|", expand=True)
    hits["entrez_id"], hits["hgnc_symbol"] = q[0].astype(int), q[1]
    hits = hits.merge(prot[["entrez_id", "protein_len"]], on="entrez_id")
    hits["coverage"] = (hits.q_end - hits.q_start + 1) / hits.protein_len
    n0 = len(hits)
    hits = hits[(hits.identity >= args.min_identity) & (hits.coverage >= args.min_coverage)]
    log(f"pairs: {len(hits)}/{n0} pass identity>={args.min_identity} coverage>={args.min_coverage}")

    lon = longevity[longevity.is_primary]
    pairs = (hits.merge(genomes[["assembly_accession", "species_taxid"]], on="assembly_accession")
                 .merge(lon[["ncbi_taxid", "max_longevity_yrs", "specimen_origin", "sample_size",
                             "data_quality"]], left_on="species_taxid", right_on="ncbi_taxid")
                 .merge(species[["ncbi_taxid", "scientific_name", "class", "order", "family"]],
                        on="ncbi_taxid"))
    pairs["log10_longevity"] = np.log10(pairs.max_longevity_yrs)
    t0 = time.time()
    pairs["input_ids"] = tokenize(pairs.cds.tolist(), args.threads)
    pairs["n_tokens"] = pairs.input_ids.map(len)
    log(f"tokenised {len(pairs)} pairs in {time.time()-t0:.0f}s")
    cols = ["ncbi_taxid", "scientific_name", "class", "order", "family", "assembly_accession",
            "entrez_id", "hgnc_symbol", "contig", "strand", "identity", "positive", "coverage",
            "frameshifts", "stop_codons", "n_exons", "cds_len", "cds", "n_tokens", "input_ids",
            "max_longevity_yrs", "log10_longevity", "specimen_origin", "sample_size", "data_quality"]
    pairs = pairs[cols].sort_values(["ncbi_taxid", "entrez_id"]).reset_index(drop=True)
    pairs_out = args.pairs_out or out / "pairs.parquet"
    pairs.to_parquet(pairs_out)
    log(f"wrote {pairs_out}: {len(pairs)} pairs, {pairs.ncbi_taxid.nunique()} species")


if __name__ == "__main__":
    main()
