"""Adapter from a genes.jpg genome <-> image dataset (src/datasets) to the model's records.csv.

Two DNA inputs, chosen by ``prepare --dna``:

- ``genome`` (default): the species' nuclear reference genome. Each assembly is packed once into a flat
  byte array (``PackedGenome``); training draws a fresh random window per image per step, and a genome is
  embedded as the mean of ``K`` fixed windows (``embed_genome``). A window is ``WINDOW_BP`` bases, which
  ModernGENA's tokenizer truncates to its 1024-token context (~6.4 bp/token on mammal DNA).
- ``barcode``: the ~650 bp COI Folmer region (between the LCO1490 / HCO2198 primer sites), from NCBI's
  annotation of the species' mitochondrial genome (the one named in the assembly report, else the RefSeq one);
  unannotated mitochondria are searched for the primer sites directly.

Either way every image of a species gets that species' DNA (species-level pairing).
"""

from __future__ import annotations

import bisect
import csv
import gzip
import json
import random
import time
from pathlib import Path

import numpy as np
import requests
import torch
import torch.nn.functional as F

from .data import FIELDS

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
LCO1490 = "GGTCAACAAATCATAAAGATATTGG"
HCO2198 = "TAAACTTCAGGGTGACCAAAAAATCA"
COX1_NAMES = {"COX1", "COI", "CO1", "COXI", "MT-CO1"}
RECORD_FIELDS = [*FIELDS, "genome", "kingdom", "phylum", "class", "ncbi_taxid"]
WINDOW_BP = (
    10_000  # > 1024 tokens of DNA even in repeat-rich sequence; the tokenizer truncates the rest
)
MAX_N_FRAC = 0.01  # reject windows with more unknown bases than this
EVAL_WINDOWS = 32  # windows averaged into one genome embedding
_COMP = str.maketrans("ACGTN", "TGCAN")
_NORM = bytes(  # upper-case ACGT (soft-masked repeats included); anything else -> N
    b if chr(b) in "ACGT" else (b - 32 if chr(b) in "acgt" else ord("N")) for b in range(256)
)


def non_nuclear_accessions(report: Path) -> set[str] | None:
    """Accessions (RefSeq and GenBank) of the non-nuclear sequences in an NCBI assembly report, i.e. the
    ones to exclude; None if the report lists none."""
    out = set()
    for line in report.read_text().splitlines():
        f = line.split("\t")
        if line.startswith("#") or len(f) < 8:
            continue
        if f[7] == "non-nuclear" or f[3] in (
            "Mitochondrion",
            "Chloroplast",
            "Plastid",
            "Apicoplast",
        ):
            out |= {a for a in (f[4], f[6]) if a != "na"}
    return out or None


def pack_genome(fasta_gz: Path, report: Path, out_dir: Path) -> Path:
    """Write the nuclear sequences of a gzipped FASTA as one byte array (``sequence.u8``, upper-case ACGTN)
    plus ``index.json`` (name, offset, length per sequence). Idempotent: skips if index.json exists."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if (out_dir / "index.json").exists():
        return out_dir
    exclude = non_nuclear_accessions(report) or set()
    seqs, skipped, offset, name, keep = [], [], 0, None, False
    tmp = out_dir / "sequence.u8.tmp"
    with gzip.open(fasta_gz, "rb") as f, open(tmp, "wb") as out:

        def close():
            if name is not None and keep:
                seqs[-1]["length"] = offset - seqs[-1]["offset"]

        for line in f:
            if line.startswith(b">"):
                close()
                name = line[1:].split()[0].decode()
                desc = line.decode(errors="replace").lower()
                keep = name not in exclude and "mitochondrion" not in desc
                if keep:
                    seqs.append({"name": name, "offset": offset, "length": 0})
                else:
                    skipped.append(name)
            elif keep:
                chunk = line.rstrip().translate(_NORM)
                out.write(chunk)
                offset += len(chunk)
        close()
    tmp.rename(out_dir / "sequence.u8")
    index = {"source": str(fasta_gz), "total_bp": offset, "excluded": skipped, "sequences": seqs}
    (out_dir / "index.json").write_text(json.dumps(index))
    print(
        f"packed {fasta_gz.name}: {len(seqs)} nuclear sequences, {offset:,} bp; excluded {skipped}"
    )
    return out_dir


class PackedGenome:
    """Random access to a genome packed by ``pack_genome``."""

    def __init__(self, path: str | Path, window: int = WINDOW_BP):
        self.path = Path(path)
        index = json.loads((self.path / "index.json").read_text())
        self.seq = np.memmap(self.path / "sequence.u8", dtype=np.uint8, mode="r")
        self.window = window
        # windows never cross sequence boundaries; sequences shorter than a window are not sampled
        self.spans = [
            (s["offset"], s["length"]) for s in index["sequences"] if s["length"] >= window
        ]
        self.cum = np.cumsum([n - window + 1 for _, n in self.spans]).tolist()
        if not self.spans:
            raise ValueError(f"{self.path}: no sequence of at least {window} bp")

    def sample(self, rng: random.Random) -> str:
        """One random window (uniform over all valid start positions) with at most MAX_N_FRAC Ns."""
        for _ in range(1000):
            k = rng.randrange(self.cum[-1])
            i = bisect.bisect_right(self.cum, k)
            start = self.spans[i][0] + k - (self.cum[i - 1] if i else 0)
            w = self.seq[start : start + self.window].tobytes()
            if w.count(b"N") <= MAX_N_FRAC * self.window:
                return w.decode("ascii")
        raise ValueError(f"{self.path}: no window with <= {MAX_N_FRAC:.0%} N in 1000 draws")

    def windows(self, k: int = EVAL_WINDOWS, seed: int = 0) -> list[str]:
        """``k`` fixed windows: the same for a given genome and seed."""
        rng = random.Random(f"{seed}:{self.path.name}")
        return [self.sample(rng) for _ in range(k)]


def embed_genome(
    encoder, genome: PackedGenome, k: int = EVAL_WINDOWS, seed: int = 0
) -> torch.Tensor:
    """Genome embedding (512,): the re-normalised mean of the encoder's embeddings of ``k`` fixed windows."""
    return F.normalize(encoder.encode(genome.windows(k, seed), batch_size=32).mean(0), dim=-1)


def embed_records(encoder, records: list[dict], k: int = EVAL_WINDOWS) -> torch.Tensor:
    """DNA embedding per record (len(records), 512): its genome's embedding (computed once per genome) when
    the record has a ``genome``, else the embedding of its ``dna_barcode``."""
    cache: dict[str, torch.Tensor] = {}
    out = []
    for r in records:
        key = r.get("genome") or r["dna_barcode"]
        if key not in cache:
            g = r.get("genome")
            cache[key] = (
                embed_genome(encoder, PackedGenome(g), k) if g else encoder.encode([key])[0]
            )
        out.append(cache[key])
    return torch.stack(out)


def window_sampler(records: list[dict], seed: int = 0):
    """``f(indices) -> DNA strings``: a fresh random genome window for each record index, every call."""
    rng = random.Random(seed)
    genomes = {g: PackedGenome(g) for g in {r["genome"] for r in records}}
    return lambda idx: [genomes[records[i]["genome"]].sample(rng) for i in idx]


def _revcomp(s: str) -> str:
    return s.translate(_COMP)[::-1]


def _eutils(endpoint: str, **params) -> requests.Response:
    """GET an E-utilities endpoint, kept under NCBI's 3 requests/s, with retries."""
    for attempt in range(4):
        time.sleep(0.4)
        r = requests.get(EUTILS + endpoint, params=params, timeout=60)
        if r.status_code == 200:
            return r
        time.sleep(2**attempt)
    r.raise_for_status()
    return r


def mito_accession(report: Path) -> str | None:
    """Accession of the mitochondrion listed in an NCBI assembly report (RefSeq preferred), or None."""
    for line in report.read_text().splitlines():
        if line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) > 6 and f[3] == "Mitochondrion":
            return f[6] if f[6] != "na" else (f[4] if f[4] != "na" else None)
    return None


def refseq_mito_accession(taxid: int) -> str | None:
    """Accession of the RefSeq complete mitochondrial genome of a species taxid, or None."""
    term = f"txid{taxid}[Organism:noexp] AND mitochondrion[filter] AND refseq[filter]"
    ids = _eutils("esearch.fcgi", db="nuccore", term=term, retmode="json").json()
    ids = ids["esearchresult"]["idlist"]
    if not ids:
        return None
    summ = _eutils("esummary.fcgi", db="nuccore", id=ids[0], retmode="json").json()
    return summ["result"][ids[0]]["accessionversion"]


def fetch_cox1(accession: str) -> str | None:
    """COX1 coding sequence of an annotated mitochondrial genome record, or None if not annotated."""
    fasta = _eutils("efetch.fcgi", db="nuccore", id=accession, rettype="fasta_cds_na").text
    for block in fasta.split(">")[1:]:
        header, *seq = block.splitlines()
        gene = header.split("[gene=")[1].split("]")[0] if "[gene=" in header else ""
        if gene.upper() in COX1_NAMES or "cytochrome c oxidase subunit I]" in header:
            return "".join(seq).upper()
    return None


def fetch_sequence(accession: str) -> str:
    """Nucleotide sequence of a record."""
    fasta = _eutils("efetch.fcgi", db="nuccore", id=accession, rettype="fasta").text
    return "".join(fasta.splitlines()[1:]).upper()


def _best_hit(seq: str, primer: str) -> tuple[int, int]:
    """(position, mismatches) of the best ungapped match of primer in seq."""
    n = len(primer)
    return min(
        ((i, sum(a != b for a, b in zip(seq[i : i + n], primer))) for i in range(len(seq) - n + 1)),
        key=lambda x: x[1],
    )


def folmer_barcode(seq: str, max_mismatches: int = 8, length: tuple[int, int] = (600, 700)) -> str:
    """The Folmer barcode region (between the LCO1490 and HCO2198 sites, primers excluded) of a COX1 or
    whole mitochondrial sequence; both strands are searched."""
    best = None
    for strand in (seq, _revcomp(seq)):
        fwd, mf = _best_hit(strand, LCO1490)
        rev, mr = _best_hit(strand, _revcomp(HCO2198))
        n = rev - fwd - len(LCO1490)
        ok = mf <= max_mismatches and mr <= max_mismatches and length[0] <= n <= length[1]
        if ok and (best is None or mf + mr < best[0]):
            best = (mf + mr, strand[fwd + len(LCO1490) : rev])
    if best is None:
        raise ValueError("Folmer primer sites not found")
    return best[1]


def _check_coding(barcode: str) -> None:
    """The barcode must translate (vertebrate mitochondrial code) without stops in one of its frames."""
    from Bio.Seq import Seq

    for frame in range(3):
        sub = barcode[frame : frame + (len(barcode) - frame) // 3 * 3]
        if "*" not in str(Seq(sub).translate(table=2)):
            return
    raise ValueError("barcode has stop codons in every frame")


def _rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def species_barcodes(dataset: Path) -> list[dict]:
    """One COI barcode per genome of the dataset, with its provenance."""
    out = []
    for g in _rows(dataset / "genomes.csv"):
        extras = g["extra_files"].split("|")
        report = next(dataset / p for p in extras if p.endswith("_assembly_report.txt"))
        acc, source = mito_accession(report), "assembly_report"
        if acc is None:
            acc, source = refseq_mito_accession(int(g["species_taxid"])), "refseq_search"
        if acc is None:
            raise ValueError(f"{g['organism_name']}: no mitochondrial genome found")
        cox1 = fetch_cox1(acc)
        if cox1 is None:  # unannotated mitochondrion (e.g. a GenBank assembly molecule)
            source += "+primer_search"
        barcode = folmer_barcode(cox1 or fetch_sequence(acc))
        _check_coding(barcode)
        out.append(
            {
                "ncbi_taxid": g["ncbi_taxid"],
                "assembly_accession": g["assembly_accession"],
                "organism_name": g["organism_name"],
                "mito_accession": acc,
                "mito_source": source,
                "cox1_length": len(cox1) if cox1 else "",
                "barcode_length": len(barcode),
                "dna_barcode": barcode,
            }
        )
        print(f"{g['organism_name']}: {acc} ({source}) -> barcode {len(barcode)} bp")
    return out


def pack_dataset_genomes(dataset: Path, cache: Path, workers: int = 8) -> dict[str, dict]:
    """Pack every genome of a dataset into ``cache/<accession>`` (in parallel); taxid -> record DNA fields."""
    from concurrent.futures import ProcessPoolExecutor

    jobs = {}
    for g in _rows(dataset / "genomes.csv"):
        report = next(p for p in g["extra_files"].split("|") if p.endswith("_assembly_report.txt"))
        jobs[g["ncbi_taxid"]] = (
            dataset / g["sequence_file"],
            dataset / report,
            cache / g["assembly_accession"],
        )
    with ProcessPoolExecutor(min(workers, len(jobs))) as ex:
        paths = dict(zip(jobs, ex.map(pack_genome, *zip(*jobs.values()))))
    return {t: {"dna_barcode": "", "genome": str(p.resolve())} for t, p in paths.items()}


def prepare_records(
    dataset: str | Path,
    out_dir: str | Path,
    unseen: list[int] = (),
    val_frac: float = 0.2,
    seed: int = 0,
    dna: str = "genome",
    genome_cache: str | Path | None = None,
) -> Path:
    """Write <out_dir>/records.csv (model format) from a genome <-> image dataset.

    ``dna="genome"``: each assembly is packed into ``genome_cache`` (default ``<dataset>/../_packed_genomes``,
    shared across runs) and records point to it in ``genome``. ``dna="barcode"``: records carry the species'
    COI barcode in ``dna_barcode`` (provenance in barcodes.csv).
    Splits: species in ``unseen`` -> val_unseen; of the rest, ``val_frac`` of each species' images -> val,
    the remainder -> train. ``processid`` is the dataset's image_id; image paths are absolute.
    """
    dataset, out = Path(dataset).resolve(), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if dna == "genome":
        by_taxid = pack_dataset_genomes(
            dataset, Path(genome_cache or dataset.parent / "_packed_genomes")
        )
    elif dna == "barcode":
        barcodes = species_barcodes(dataset)
        with open(out / "barcodes.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(barcodes[0]))
            w.writeheader()
            w.writerows(barcodes)
        by_taxid = {
            b["ncbi_taxid"]: {"dna_barcode": b["dna_barcode"], "genome": ""} for b in barcodes
        }
    else:
        raise ValueError(f"dna must be 'genome' or 'barcode', not {dna!r}")
    species = {s["ncbi_taxid"]: s for s in _rows(dataset / "species.csv")}
    images = {i["image_id"]: i for i in _rows(dataset / "images.csv")}
    pairs = _rows(dataset / "pairs.csv")

    rng = random.Random(seed)
    by_species: dict[str, list[dict]] = {}
    for p in sorted(pairs, key=lambda p: p["image_id"]):
        by_species.setdefault(p["ncbi_taxid"], []).append(p)
    unseen = {str(t) for t in unseen}
    records = []
    for taxid, ps in sorted(by_species.items()):
        s, b = species[taxid], by_taxid[taxid]
        rng.shuffle(ps)
        n_val = round(len(ps) * val_frac)
        for k, p in enumerate(ps):
            split = "val_unseen" if taxid in unseen else ("val" if k < n_val else "train")
            records.append(
                {
                    "processid": p["image_id"],
                    "split": split,
                    "dna_barcode": b["dna_barcode"],
                    "genome": b["genome"],
                    "dna_bin": p["assembly_accession"],
                    "order": s["order"],
                    "family": s["family"],
                    "subfamily": "",
                    "genus": s["genus"],
                    "species": s["scientific_name"],
                    "image_path": str(dataset / images[p["image_id"]]["file"]),
                    "kingdom": "Animalia" if s["kingdom"] == "Metazoa" else s["kingdom"],
                    "phylum": s["phylum"],
                    "class": s["class"],
                    "ncbi_taxid": taxid,
                }
            )
    path = out / "records.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RECORD_FIELDS)
        w.writeheader()
        w.writerows(records)
    counts = {sp: sum(r["split"] == sp for r in records) for sp in ("train", "val", "val_unseen")}
    (out / "prepare.json").write_text(
        json.dumps(
            {
                "dataset": str(dataset),
                "dna": dna,
                "unseen": sorted(unseen),
                "val_frac": val_frac,
                "seed": seed,
                "counts": counts,
            },
            indent=2,
        )
    )
    print(f"wrote {len(records)} records to {path}: {counts}")
    return path
