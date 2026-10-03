"""Adapter from a genes.jpg genome <-> image dataset (src/datasets) to the model's records.csv.

The DNA input is the species' nuclear reference genome. Each assembly is packed once into a flat byte array
(``PackedGenome``); training draws a fresh random window per image per step, and a genome is embedded as the mean
of ``K`` fixed windows (``embed_genome``). A window is ``WINDOW_BP`` bases, which ModernGENA's tokenizer truncates
to its 1024-token context (~6.4 bp/token on mammal DNA). Every image of a species gets that species' genome
(species-level pairing).
"""

from __future__ import annotations

import bisect
import csv
import gzip
import json
import random
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from .data import FIELDS

RECORD_FIELDS = [*FIELDS, "genome", "kingdom", "phylum", "class", "ncbi_taxid"]
# > 1024 tokens of DNA even in repeat-rich sequence; the tokenizer truncates the rest
WINDOW_BP = 10_000
MAX_N_FRAC = 0.01  # reject windows with more unknown bases than this
EVAL_WINDOWS = 32  # windows averaged into one genome embedding
_NORM = bytes(  # upper-case ACGT (soft-masked repeats included); anything else -> N
    b if chr(b) in "ACGT" else (b - 32 if chr(b) in "acgt" else ord("N")) for b in range(256)
)


# bump when the sequence filter changes; packs written by another version are rebuilt
PACK_VERSION = 2
ORGANELLES = ("Mitochondrion", "Chloroplast", "Plastid", "Apicoplast")
# alternate haplotypes and patches repeat regions already in the primary sequences (GRCh38: 199 Mb, GRCm39);
# filtered by role, not by assembly-unit name, which is not always "Primary Assembly" (GRCm39's is "C57BL/6J")
NON_PRIMARY_ROLES = ("alt-scaffold", "fix-patch", "novel-patch")


def excluded_accessions(report: Path) -> set[str]:
    """Accessions (RefSeq and GenBank) to leave out of a packed nuclear genome, from its NCBI assembly report:
    organelle sequences, and alternate loci / patches."""
    out = set()
    for line in report.read_text().splitlines():
        f = line.split("\t")
        if line.startswith("#") or len(f) < 8:
            continue
        if f[7] == "non-nuclear" or f[3] in ORGANELLES or f[1] in NON_PRIMARY_ROLES:
            out |= {a for a in (f[4], f[6]) if a != "na"}
    return out


def pack_genome(fasta_gz: Path, report: Path, out_dir: Path) -> Path:
    """Write the nuclear sequences of a gzipped FASTA as one byte array (``sequence.u8``, upper-case ACGTN)
    plus ``index.json`` (name, offset, length per sequence). Only primary nuclear sequences are kept (see
    ``excluded_accessions``). Idempotent: skips if a pack of the current PACK_VERSION exists."""
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "index.json"
    if index_path.exists() and json.loads(index_path.read_text()).get("version") == PACK_VERSION:
        return out_dir
    exclude = excluded_accessions(report)
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
    index = {
        "version": PACK_VERSION,
        "source": str(fasta_gz),
        "total_bp": offset,
        "excluded": skipped,
        "sequences": seqs,
    }
    index_path.write_text(json.dumps(index))
    print(
        f"packed {fasta_gz.name}: {len(seqs)} nuclear sequences, {offset:,} bp; "
        f"excluded {len(skipped)} ({', '.join(skipped[:3])}{', ...' if len(skipped) > 3 else ''})"
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


# NCBI names -> the iNaturalist-style names in BioCLIP's training captions (TreeOfLife-10M). NCBI-only clades
# (Viridiplantae, Streptophyta) are replaced, and groups NCBI ranks differently are read from the lineage taxids.
KINGDOM_NAMES = {"Metazoa": "Animalia", "Viridiplantae": "Plantae"}
CLASS_NAMES = {"Actinopteri": "Actinopterygii", "Hyperoartia": "Petromyzonti"}
PLANT_PHYLA = {  # lineage taxid -> phylum, for NCBI's phylum Streptophyta
    58023: "Tracheophyta",
    3208: "Bryophyta",
    3195: "Marchantiophyta",
    13809: "Anthocerotophyta",
}
CLASS_BY_LINEAGE = {  # lineage taxid -> class; NCBI ranks these as clade/subclass inside another class
    4447: "Liliopsida",  # monocots, inside NCBI class Magnoliopsida
    7778: "Elasmobranchii",  # sharks and rays, inside Chondrichthyes
    7863: "Holocephali",  # chimaeras, inside Chondrichthyes
    8504: "Reptilia",  # lizards, snakes, tuatara (NCBI class Lepidosauria)
    8459: "Reptilia",  # turtles (no class rank in NCBI)
    1294634: "Reptilia",  # crocodilians (no class rank in NCBI)
}


def bioclip_ranks(species: dict) -> tuple[str, str, str]:
    """(kingdom, phylum, class) of a dataset species row (NCBI taxonomy) in BioCLIP's caption vocabulary."""
    lineage = {int(t) for t in str(species.get("lineage_taxids") or "").split("|") if t}
    kingdom, phylum, cls = (species.get(k) or "" for k in ("kingdom", "phylum", "class"))
    kingdom = KINGDOM_NAMES.get(kingdom, kingdom)
    if phylum == "Streptophyta":
        phylum = next((n for t, n in PLANT_PHYLA.items() if t in lineage), "")
    cls = next((n for t, n in CLASS_BY_LINEAGE.items() if t in lineage), CLASS_NAMES.get(cls, cls))
    return kingdom, phylum, cls


def _rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def pack_dataset_genomes(dataset: Path, cache: Path, workers: int = 8) -> dict[str, dict]:
    """Pack every genome of a dataset into ``cache/<accession>`` (in parallel); accession -> record DNA fields.

    Keyed by assembly accession, not taxid: a genome's ``ncbi_taxid`` can be a subspecies (e.g. Peromyscus
    maniculatus bairdii) while species and pairs use the species taxid."""
    from concurrent.futures import ProcessPoolExecutor

    jobs = {}
    for g in _rows(dataset / "genomes.csv"):
        report = next(p for p in g["extra_files"].split("|") if p.endswith("_assembly_report.txt"))
        jobs[g["assembly_accession"]] = (
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
    genome_cache: str | Path | None = None,
    workers: int = 8,
) -> Path:
    """Write <out_dir>/records.csv (model format) from a genome <-> image dataset.

    Each assembly is packed into ``genome_cache`` (default ``<dataset>/../_packed_genomes``, shared across
    runs) and records point to it in ``genome``.
    Splits: species in ``unseen`` -> val_unseen; of the rest, ``val_frac`` of each species' images -> val,
    the remainder -> train. ``processid`` is the dataset's image_id; image paths are absolute.
    """
    dataset, out = Path(dataset).resolve(), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(genome_cache or dataset.parent / "_packed_genomes")
    by_accession = pack_dataset_genomes(dataset, cache, workers=workers)
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
        s = species[taxid]
        kingdom, phylum, cls = bioclip_ranks(s)
        rng.shuffle(ps)
        n_val = round(len(ps) * val_frac)
        for k, p in enumerate(ps):
            split = "val_unseen" if taxid in unseen else ("val" if k < n_val else "train")
            b = by_accession[p["assembly_accession"]]
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
                    "kingdom": kingdom,
                    "phylum": phylum,
                    "class": cls,
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
