"""Build the gene -> longevity training table from a genes.jpg longevity dataset.

For every genome in the dataset, the human proteins of the HAGR ageing-signature genes are
spliced-aligned with miniprot, the best hit's CDS is cut out of the genome, and each
(species, gene) CDS is paired with the species' AnAge maximum longevity and tokenised with
ModernGENA's BPE tokenizer.

Stages (each writes into --out and is skipped when its output already exists, unless --force):
  1. proteins:  longest human isoform per gene            -> proteins.faa
  2. miniprot:  one job per genome, threads split by size  -> miniprot/<accession>.gff
  3. cds:       one process per genome                     -> cds/<accession>.parquet
  4. pairs:     join with longevity, tokenise in parallel  -> pairs.parquet

Usage:
  python -m longevity.prepare \
      --dataset /mnt/filesystem-c8/genes.jpg/datasets/anage-longevity-sample \
      --genes /mnt/filesystem-c8/genes.jpg/datasets/_longevity_genes/hagr_ageing_signature_611.csv \
      --human-proteins .../human_611/ncbi_dataset/data/protein.faa \
      --out /mnt/filesystem-c8/genes.jpg/datasets/_longevity_genes/sample5
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd

TOKENIZER = "AIRI-Institute/gena-lm-bert-base-t2t"
COMP = str.maketrans("ACGTNacgtn", "TGCANtgcan")


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}", flush=True)


# ---------------------------------------------------------------- 1. proteins


def read_fasta(text: str) -> dict[str, str]:
    """Parse FASTA text into {header: sequence}."""
    out, head, buf = {}, None, []
    for line in text.splitlines():
        if line.startswith(">"):
            if head is not None:
                out[head] = "".join(buf)
            head, buf = line[1:], []
        else:
            buf.append(line.strip())
    if head is not None:
        out[head] = "".join(buf)
    return out


def select_proteins(faa: Path, genes: pd.DataFrame, out: Path) -> pd.DataFrame:
    """Keep the longest isoform per GeneID; names become `<entrez_id>|<symbol>`."""
    best: dict[str, tuple[str, str]] = {}
    for head, seq in read_fasta(faa.read_text()).items():
        m = re.search(r"\[GeneID=(\d+)\]", head)
        if m and (m.group(1) not in best or len(seq) > len(best[m.group(1)][1])):
            best[m.group(1)] = (head.split()[0], seq)
    rows, lines = [], []
    for g in genes.itertuples():
        hit = best.get(str(g.entrez_id))
        if hit is None:
            continue
        lines += [f">{g.entrez_id}|{g.hgnc_symbol}", hit[1]]
        rows.append(dict(entrez_id=int(g.entrez_id), hgnc_symbol=g.hgnc_symbol,
                         protein_accession=hit[0], protein_len=len(hit[1])))
    out.write_text("\n".join(lines) + "\n")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 2. miniprot


class MemoryBudget:
    """Blocks a job until its estimated memory fits under the budget (in GB)."""

    def __init__(self, gb: float):
        import threading

        self.total = self.free = gb
        self.cv = threading.Condition()

    @contextmanager
    def hold(self, need: float):
        with self.cv:  # a job bigger than the whole budget runs alone
            self.cv.wait_for(lambda: self.free >= need or self.free == self.total)
            self.free -= need
        try:
            yield
        finally:
            with self.cv:
                self.free += need
                self.cv.notify_all()


def miniprot_gb(genome_size: int) -> float:
    """miniprot's index takes ~4 bytes per genome base (measured: 2.5 Gb genome -> ~10 GB)."""
    return 4.5 * genome_size / 1e9 + 1


def run_miniprot(miniprot: str, genome: Path, proteins: Path, gff: Path, threads: int,
                 budget: MemoryBudget, need_gb: float) -> str:
    tmp = gff.with_suffix(".gff.tmp")
    cmd = [miniprot, "-t", str(threads), "-I", "--gff", "--outn=1", "--outc=0.3",
           str(genome), str(proteins)]
    with budget.hold(need_gb):
        t0 = time.time()
        with open(tmp, "w") as fh, open(gff.with_suffix(".log"), "w") as err:
            subprocess.run(cmd, stdout=fh, stderr=err, check=True)
    tmp.rename(gff)
    return f"{gff.stem}: {time.time() - t0:.0f}s with {threads} threads"


# ---------------------------------------------------------------- 3. cds


def parse_gff(gff: Path) -> pd.DataFrame:
    """Best (Rank=1) miniprot hit per protein with its CDS segments."""
    mrna, cds = {}, {}
    for line in gff.read_text().splitlines():
        if line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) < 9:
            continue
        attr = dict(kv.split("=", 1) for kv in f[8].split(";") if "=" in kv)
        if f[2] == "mRNA" and attr.get("Rank") == "1":
            target = attr["Target"].split()
            mrna[attr["ID"]] = dict(
                query=target[0], contig=f[0], strand=f[6],
                identity=float(attr.get("Identity", "nan")),
                positive=float(attr.get("Positive", "nan")),
                q_start=int(target[1]), q_end=int(target[2]),
                frameshifts=int(attr.get("Frameshift", 0)),
                stop_codons=int(attr.get("StopCodon", 0)),
            )
        elif f[2] == "CDS":
            cds.setdefault(attr["Parent"], []).append((int(f[3]), int(f[4])))
    rows = []
    for mid, m in mrna.items():
        segs = sorted(cds.get(mid, []))
        if segs:
            rows.append(dict(m, mrna_id=mid, segments=segs))
    return pd.DataFrame(rows)


def read_contigs(genome: Path, wanted: set[str]) -> dict[str, str]:
    """Load only the needed contigs (pigz for fast decompression)."""
    seqs, name, buf = {}, None, []
    proc = subprocess.Popen(["pigz", "-dc", str(genome)], stdout=subprocess.PIPE, text=True,
                            bufsize=1 << 24)
    for line in proc.stdout:
        if line[0] == ">":
            if name in wanted:
                seqs[name] = "".join(buf)
            name, buf = line[1:].split()[0], []
        elif name in wanted:
            buf.append(line.rstrip())
    if name in wanted:
        seqs[name] = "".join(buf)
    proc.wait()
    return seqs


def extract_cds(accession: str, genome: Path, gff: Path, out: Path, genomic_bp: int) -> str:
    """CDS (spliced) and genomic locus (start codon -> stop codon, introns included) per hit.

    Both are in the gene's own orientation; the locus is truncated to its first genomic_bp bases.
    """
    hits = parse_gff(gff)
    if hits.empty:
        pd.DataFrame().to_parquet(out)
        return f"{accession}: 0 hits"
    contigs = read_contigs(genome, set(hits.contig))
    seqs, loci, spans = [], [], []
    for h in hits.itertuples():
        contig = contigs[h.contig]
        s = "".join(contig[a - 1 : b] for a, b in h.segments).upper()
        lo, hi = h.segments[0][0], h.segments[-1][1]
        if h.strand == "-":
            seqs.append(s.translate(COMP)[::-1])
            loci.append(contig[max(lo - 1, hi - genomic_bp) : hi].upper().translate(COMP)[::-1])
        else:
            seqs.append(s)
            loci.append(contig[lo - 1 : min(hi, lo - 1 + genomic_bp)].upper())
        spans.append(hi - lo + 1)
    hits["cds"] = seqs
    hits["genomic"] = loci
    hits["genomic_span"] = spans
    hits["cds_len"] = hits.cds.str.len()
    hits["n_exons"] = hits.segments.map(len)
    hits["assembly_accession"] = accession
    hits.drop(columns="segments").to_parquet(out)
    return f"{accession}: {len(hits)} genes, median CDS {int(hits.cds_len.median())} bp"


# ---------------------------------------------------------------- 4. pairs + tokens

_tok = None


def _tokenize_chunk(seqs: list[str]) -> list[np.ndarray]:
    global _tok
    if _tok is None:
        from transformers import AutoTokenizer

        _tok = AutoTokenizer.from_pretrained(TOKENIZER)
    # no special tokens and no truncation here: the trainer adds [CLS]/[SEP] and crops windows
    return [np.asarray(x, dtype=np.int32) for x in _tok(seqs, add_special_tokens=False)["input_ids"]]


def tokenize(seqs: list[str], workers: int) -> list[np.ndarray]:
    n = max(1, math.ceil(len(seqs) / (workers * 4)))
    chunks = [seqs[i : i + n] for i in range(0, len(seqs), n)]
    with ProcessPoolExecutor(workers) as ex:
        return [ids for part in ex.map(_tokenize_chunk, chunks) for ids in part]


# ---------------------------------------------------------------- dataset tables


def md5_ok(path: Path) -> bool:
    """Check a genome FASTA against the NCBI md5checksums.txt next to it."""
    sums = path.parent / "md5checksums.txt"
    if not sums.exists():
        return False
    want = next((ln.split()[0] for ln in sums.read_text().splitlines()
                 if ln.strip().endswith(path.name)), None)
    if want is None:
        return False
    got = subprocess.run(["md5sum", str(path)], capture_output=True, text=True).stdout.split()[0]
    return got == want


def load_tables(dataset: Path, anage_table: Path | None, threads: int,
                assembly_accessions: set[str] | None = None):
    """(genomes, longevity, species) tables.

    A finished dataset ships genomes/longevity/species parquet files. A dataset that is still
    downloading only has genomes/<accession>/; then completed FASTAs (no .part, MD5 verified) are
    joined with the AnAge <-> assembly table (`_anage/anage_longevity_genomes.parquet`).
    """
    if (dataset / "genomes.parquet").exists():
        genomes = pd.read_parquet(dataset / "genomes.parquet")
        if assembly_accessions is not None:
            genomes = genomes[genomes.assembly_accession.isin(assembly_accessions)].copy()
        genomes["path"] = genomes.sequence_file.map(lambda p: dataset / p)
        return (genomes, pd.read_parquet(dataset / "longevity.parquet"),
                pd.read_parquet(dataset / "species.parquet"))
    if anage_table is None:
        raise SystemExit(f"{dataset} has no genomes.parquet; pass --anage-table")
    files = sorted(dataset.glob("genomes/*/*_genomic.fna.gz"))
    if assembly_accessions is not None:
        files = [f for f in files if f.parent.name in assembly_accessions]
    with ThreadPoolExecutor(threads) as ex:
        ok = list(ex.map(md5_ok, files))
    bad = [f.name for f, o in zip(files, ok) if not o]
    if bad:
        log(f"skipping {len(bad)} genomes failing MD5: {bad[:5]}")
    found = pd.DataFrame({"assembly_accession": [f.parent.name for f, o in zip(files, ok) if o],
                          "path": [f for f, o in zip(files, ok) if o]})
    t = pd.read_parquet(anage_table)
    # one longevity row per species: the AnAge name that matches NCBI's, else the first
    t["is_primary"] = False
    t = t.sort_values(["ncbi_taxid", "hagrid"])
    exact = t.anage_name == t.ncbi_name
    first = t.assign(_e=exact).sort_values(["ncbi_taxid", "_e"], ascending=[True, False])
    t.loc[first.groupby("ncbi_taxid").head(1).index, "is_primary"] = True
    genomes = (found.merge(t[t.is_primary][["assembly_accession", "ncbi_taxid", "genome_size"]],
                           on="assembly_accession")
                    .rename(columns={"ncbi_taxid": "species_taxid"}))
    longevity = t[["ncbi_taxid", "is_primary", "max_longevity_yrs", "specimen_origin",
                   "sample_size", "data_quality"]]
    species = t[t.is_primary].rename(columns={"ncbi_name": "scientific_name", "anage_class": "class",
                                              "anage_order": "order", "anage_family": "family"})
    log(f"{len(files)} complete genome files, {len(genomes)} verified and matched to AnAge")
    return genomes, longevity, species[["ncbi_taxid", "scientific_name", "class", "order", "family"]]


def assign_splits(pairs: pd.DataFrame, by: str, val_frac: float, test_frac: float,
                  seed: int) -> pd.Series:
    """train/val/test label per pair: whole `by` groups (families) go to one split, and the
    assignment is done within each class so every class is represented in every split
    (classes with < 10 species stay entirely in train)."""
    sp = pairs.groupby("ncbi_taxid").agg(cls=("class", "first"), grp=(by, "first"))
    sp["grp"] = sp.grp.fillna("NA").astype(str)
    label = {}
    rng = np.random.default_rng(seed)
    for _, c in sp.groupby("cls", sort=True, dropna=False):
        groups = sorted(c.grp.unique())
        rng.shuffle(groups)
        n, n_test, n_val = len(c), 0, 0
        if n < 10:  # too few species to split meaningfully: keep the class in train
            label.update({t: "train" for t in c.index})
            continue
        for g in groups:
            members = c.index[c.grp == g]
            # Unknown families cannot be safely used as held-out family groups.
            if g == "NA":
                label.update({t: "train" for t in members})
                continue
            if n_test < test_frac * n:
                split, n_test = "test", n_test + len(members)
            elif n_val < val_frac * n:
                split, n_val = "val", n_val + len(members)
            else:
                split = "train"
            label.update({t: split for t in members})
    return pairs.ncbi_taxid.map(label)


def pick_genomes(genomes: pd.DataFrame, species: pd.DataFrame, out: Path, n: int,
                 seed: int) -> pd.DataFrame:
    """Up to n genomes: already-aligned ones first, then round-robin over orders for diversity."""
    done = genomes.assembly_accession.map(lambda acc: (out / "miniprot" / f"{acc}.gff").exists())
    keep = list(genomes[done].index[:n])
    rest = genomes[~done].sample(frac=1, random_state=seed)
    order = rest.species_taxid.map(species.set_index("ncbi_taxid")["order"])
    queues = [list(g.index) for _, g in rest.groupby(order, sort=False)]
    while len(keep) < n and any(queues):
        for q in queues:
            if q and len(keep) < n:
                keep.append(q.pop())
    return genomes.loc[keep]


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, required=True, help="genes.jpg longevity dataset dir")
    ap.add_argument("--genes", type=Path, required=True, help="CSV with entrez_id,hgnc_symbol")
    ap.add_argument("--human-proteins", type=Path, required=True, help="NCBI protein.faa for the genes")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--anage-table", type=Path, default=None,
                    help="AnAge<->assembly parquet, for datasets still downloading (no genomes.parquet)")
    ap.add_argument("--threads", type=int, default=os.cpu_count())
    ap.add_argument("--jobs", type=int, default=12, help="max genomes aligned concurrently")
    ap.add_argument("--mem-gb", type=float, default=150, help="memory budget for miniprot jobs")
    ap.add_argument("--exclude-classes", nargs="*", default=[], help="e.g. Amphibia")
    ap.add_argument("--sequence", choices=["cds", "genomic"], default="cds",
                    help="model input: spliced CDS, or genomic locus start->stop codon with introns")
    ap.add_argument("--genomic-bp", type=int, default=16000,
                    help="keep the first N bp of each genomic locus (the trainer crops further)")
    ap.add_argument("--max-genomes", type=int, default=0, help="use at most N genomes (0 = all)")
    ap.add_argument("--assemblies", type=Path, default=None,
                    help="text file of assembly accessions to use (e.g. to reuse a genome set)")
    ap.add_argument("--seed", type=int, default=0, help="for --max-genomes sampling")
    ap.add_argument("--pairs-out", type=Path, default=None, help="default: <out>/pairs.parquet")
    ap.add_argument("--min-genes", type=int, default=0, help="drop species with fewer genes found")
    ap.add_argument("--val-frac", type=float, default=0.0, help="species fraction for val (0 = no split)")
    ap.add_argument("--test-frac", type=float, default=0.0)
    ap.add_argument("--split-by", default="family", help="taxonomic column kept within one split")
    ap.add_argument("--drop-sequences", action="store_true",
                    help="omit raw cds/genomic strings from the output (tokens only)")
    ap.add_argument("--max-genome-gb", type=float, default=8,
                    help="skip genomes larger than this many Gb (giant salamanders etc.)")
    ap.add_argument("--miniprot", default="miniprot")
    ap.add_argument("--min-identity", type=float, default=0.3, help="drop hits below this aa identity")
    ap.add_argument("--min-coverage", type=float, default=0.5, help="drop hits covering less of the protein")
    ap.add_argument("--force", action="store_true", help="recompute every stage")
    a = ap.parse_args()

    out = a.out
    (out / "miniprot").mkdir(parents=True, exist_ok=True)
    (out / "seqs").mkdir(exist_ok=True)
    t_start = time.time()

    log("STAGE proteins start")
    # 1. proteins
    genes = pd.read_csv(a.genes)
    proteins = out / "proteins.faa"
    prot = select_proteins(a.human_proteins, genes, proteins)
    prot.to_csv(out / "proteins.csv", index=False)
    log(f"proteins: {len(prot)}/{len(genes)} genes have a human protein")

    log("STAGE tables start")
    genomes, longevity, species = load_tables(a.dataset, a.anage_table, a.threads)
    if a.assemblies:
        want = set(a.assemblies.read_text().split())
        genomes = genomes[genomes.assembly_accession.isin(want)]
        log(f"restricted to {len(genomes)}/{len(want)} listed assemblies")

    if a.exclude_classes:
        cls = genomes.species_taxid.map(species.set_index("ncbi_taxid")["class"])
        drop = cls.isin(a.exclude_classes)
        log(f"skipping {int(drop.sum())} genomes of classes {a.exclude_classes}")
        genomes = genomes[~drop]
    big = genomes.genome_size > a.max_genome_gb * 1e9
    if big.any():
        log(f"skipping {int(big.sum())} genomes > {a.max_genome_gb} Gb")
        genomes = genomes[~big]
    if a.max_genomes and len(genomes) > a.max_genomes:
        genomes = pick_genomes(genomes, species, out, a.max_genomes, a.seed)
        log(f"limited to {len(genomes)} genomes (already aligned first, then round-robin "
            f"over {genomes.species_taxid.map(species.set_index('ncbi_taxid')['order']).nunique()} orders)")

    log("STAGE miniprot start")
    # 2. miniprot: up to --jobs genomes at a time within --mem-gb, largest first (short tail)
    todo = sorted((g for g in genomes.itertuples()
                   if a.force or not (out / "miniprot" / f"{g.assembly_accession}.gff").exists()),
                  key=lambda g: -int(g.genome_size))
    if todo:
        jobs = min(a.jobs, len(todo))
        per_job = max(1, a.threads // jobs)
        budget = MemoryBudget(a.mem_gb)
        log(f"miniprot: {len(todo)} genomes, up to {jobs} at a time x {per_job} threads, "
            f"{a.mem_gb:.0f} GB budget")
        with ThreadPoolExecutor(jobs) as ex:
            futs = [ex.submit(run_miniprot, a.miniprot, g.path, proteins,
                              out / "miniprot" / f"{g.assembly_accession}.gff", per_job,
                              budget, miniprot_gb(int(g.genome_size)))
                    for g in todo]
            for i, f in enumerate(futs, 1):
                try:
                    log(f"miniprot [{i}/{len(todo)}] " + f.result())
                except subprocess.CalledProcessError as e:
                    log(f"miniprot [{i}/{len(todo)}] FAILED {e}")
    genomes = genomes[genomes.assembly_accession.map(
        lambda acc: (out / "miniprot" / f"{acc}.gff").exists())]

    log("STAGE extraction start")
    # 3. CDS extraction: one process per genome
    todo = [g for g in genomes.itertuples()
            if a.force or not (out / "seqs" / f"{g.assembly_accession}.parquet").exists()]
    if todo:
        # Bound extraction concurrency by the same conservative per-genome RAM estimate.
        extraction_workers = min(len(todo), a.threads, a.jobs,
                                 max(1, int(a.mem_gb / max(miniprot_gb(int(g.genome_size)) for g in todo))))
        log(f"extraction: {len(todo)} genomes, {extraction_workers} workers within RAM budget")
        with ProcessPoolExecutor(extraction_workers) as ex:
            futs = [ex.submit(extract_cds, g.assembly_accession, g.path,
                              out / "miniprot" / f"{g.assembly_accession}.gff",
                              out / "seqs" / f"{g.assembly_accession}.parquet", a.genomic_bp)
                    for g in todo]
            for f in futs:
                log("cds " + f.result())

    log("STAGE join start")
    # 4. pairs
    hits = pd.concat([pd.read_parquet(out / "seqs" / f"{acc}.parquet")
                      for acc in genomes.assembly_accession], ignore_index=True)
    q = hits["query"].str.split("|", expand=True)
    hits["entrez_id"], hits["hgnc_symbol"] = q[0].astype(int), q[1]
    hits = hits.merge(prot[["entrez_id", "protein_len"]], on="entrez_id")
    hits["coverage"] = (hits.q_end - hits.q_start + 1) / hits.protein_len
    n0 = len(hits)
    hits = hits[(hits.identity >= a.min_identity) & (hits.coverage >= a.min_coverage)]
    log(f"pairs: {len(hits)}/{n0} hits pass identity>={a.min_identity}, coverage>={a.min_coverage}")

    lon = longevity[longevity.is_primary]
    pairs = (hits.merge(genomes[["assembly_accession", "species_taxid"]], on="assembly_accession")
                 .merge(lon[["ncbi_taxid", "max_longevity_yrs", "specimen_origin", "sample_size",
                             "data_quality"]], left_on="species_taxid", right_on="ncbi_taxid")
                 .merge(species[["ncbi_taxid", "scientific_name", "class", "order", "family"]],
                        on="ncbi_taxid"))
    pairs["log10_longevity"] = np.log10(pairs.max_longevity_yrs)

    log("STAGE tokenize start")
    t0 = time.time()
    pairs["input_ids"] = tokenize(pairs[a.sequence].tolist(), a.threads)
    pairs["n_tokens"] = pairs.input_ids.map(len)
    log(f"tokenised {len(pairs)} CDS in {time.time() - t0:.1f}s with {a.threads} workers, "
        f"{pairs[a.sequence].str.len().sum() / pairs.n_tokens.sum():.2f} bp/token ({a.sequence})")

    log("STAGE write start")
    cols = ["ncbi_taxid", "scientific_name", "class", "order", "family", "assembly_accession",
            "entrez_id", "hgnc_symbol", "contig", "strand", "identity", "positive", "coverage",
            "frameshifts", "stop_codons", "n_exons", "cds_len", "cds", "genomic_span", "genomic",
            "n_tokens", "input_ids",
            "max_longevity_yrs", "log10_longevity", "specimen_origin", "sample_size", "data_quality"]
    pairs = pairs[cols].sort_values(["ncbi_taxid", "entrez_id"]).reset_index(drop=True)
    if a.min_genes:
        n = pairs.groupby("ncbi_taxid").entrez_id.transform("size")
        log(f"dropping {pairs[n < a.min_genes].ncbi_taxid.nunique()} species with < {a.min_genes} genes")
        pairs = pairs[n >= a.min_genes].reset_index(drop=True)
    if a.val_frac + a.test_frac > 0:
        pairs["split"] = assign_splits(pairs, a.split_by, a.val_frac, a.test_frac, a.seed)
    if a.drop_sequences:
        pairs = pairs.drop(columns=["cds", "genomic"])
    pairs["input_ids"] = pairs.input_ids.map(lambda x: np.asarray(x, dtype=np.int32))
    pairs_out = a.pairs_out or out / "pairs.parquet"
    pairs.to_parquet(pairs_out)
    if "split" in pairs:
        sp = pairs.groupby("ncbi_taxid").agg(split=("split", "first"), cls=("class", "first"),
                                             family=("family", "first"))
        splits = {k: dict(species=int((sp.split == k).sum()),
                          pairs=int((pairs.split == k).sum()),
                          families=int(sp[sp.split == k].family.nunique()),
                          by_class=sp[sp.split == k].cls.value_counts().to_dict(),
                          taxids=sorted(map(int, sp.index[sp.split == k])))
                  for k in ("train", "val", "test")}
        pairs_out.with_name(pairs_out.stem + "_splits.json").write_text(json.dumps(splits, indent=1))
        log("splits: " + ", ".join(f"{k} {v['species']} species / {v['pairs']} pairs"
                                   for k, v in splits.items()))

    summary = (pairs.groupby(["ncbi_taxid", "scientific_name", "class"], dropna=False)
                    .agg(genes=("entrez_id", "size"), median_identity=("identity", "median"),
                         median_cds_bp=("cds_len", "median"), median_tokens=("n_tokens", "median"),
                         longevity_yrs=("max_longevity_yrs", "first"))
                    .reset_index())
    report = dict(
        dataset=str(a.dataset), n_genes_requested=len(genes), n_proteins=len(prot),
        n_pairs=len(pairs), n_species=int(pairs.ncbi_taxid.nunique()),
        min_identity=a.min_identity, min_coverage=a.min_coverage,
        frac_over_1022_tokens=float((pairs.n_tokens > 1022).mean()),
        species=summary.to_dict("records"), seconds=round(time.time() - t_start, 1),
    )
    pairs_out.with_name(pairs_out.stem + "_report.json").write_text(json.dumps(report, indent=2, default=str))
    log(f"wrote {pairs_out}\n{summary.to_string(index=False)}")

    log("STAGE write end")

if __name__ == "__main__":
    main()
