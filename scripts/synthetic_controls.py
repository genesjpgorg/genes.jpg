"""Synthetic sanity test of the DNA controls (CPU, tiny model, ~2 min).

usage: python scripts/synthetic_controls.py OUT_DIR


World: 8 families x 4 species. A species' genome is random DNA whose GC content is set by its family (plus a
small per-species jitter); its "photo" embeddings are family direction + species direction + noise. One species
per family is held out. Expected: none and shuffle_tokens place held-out species in the right family (GC content
survives a token shuffle); permute_genomes falls to chance (1/8).
"""

import csv
import gzip
import random
import sys
from pathlib import Path

import torch
import torch.nn.functional as F

from genesjpg import evaluate as ev
from genesjpg.align import AlignModel, train_align
from genesjpg.data import label
from genesjpg.encoders import MODERNGENA, DNAEncoder, HFTokenizer
from genesjpg.genomes import embed_records, prepare_records, window_sampler

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
FAM, SP, IMG, BP = 8, 4, 20, 200_000
rng = random.Random(0)  # setup: GC jitter, held-out choice
seq_rng = random.Random(1)  # genome sequences only, so reruns (genomes cached) keep the same setup
torch.manual_seed(0)

# --- build the synthetic dataset ---------------------------------------------------------------------------
ds = root / "ds"
(ds / "genomes").mkdir(parents=True, exist_ok=True)
species, genomes, images, pairs = [], [], [], []
fam_dir = F.normalize(torch.randn(FAM, 512), dim=-1)
img_emb = {}
for f in range(FAM):
    for s in range(SP):
        t = str(1000 + f * 10 + s)
        gc = 0.30 + 0.40 * f / (FAM - 1) + rng.uniform(-0.01, 0.01)
        fa = ds / "genomes" / f"{t}.fna.gz"
        if not fa.exists():
            seq = "".join(
                seq_rng.choices("GCAT", weights=[gc / 2, gc / 2, (1 - gc) / 2, (1 - gc) / 2], k=BP)
            )
            with gzip.open(fa, "wt") as h:
                h.write(f">NC_{t}.1 chromosome\n")
                h.writelines(seq[i : i + 80] + "\n" for i in range(0, BP, 80))
        rep = ds / "genomes" / f"{t}_assembly_report.txt"
        rep.write_text(
            f"# x\n1\tassembled-molecule\t1\tChromosome\tCM{t}.1\t=\tNC_{t}.1\tPrimary Assembly\n"
        )
        genomes.append(
            {"assembly_accession": f"GCF_{t}.1", "ncbi_taxid": t, "species_taxid": t}
            | {"sequence_file": str(fa), "extra_files": str(rep)}
        )
        species.append(
            {
                "ncbi_taxid": t,
                "scientific_name": f"Gen{t} sp{t}",
                "genus": f"Gen{t}",
                "family": f"Fam{f}",
            }
            | {
                "order": f"Ord{f}",
                "class": "C",
                "kingdom": "Metazoa",
                "phylum": "P",
                "lineage_taxids": "1",
            }
        )
        sp_dir = F.normalize(torch.randn(512), dim=-1)
        for k in range(IMG):
            iid = f"{t}_{k}"
            images.append({"image_id": iid, "file": f"{iid}.jpg"})
            pairs.append({"image_id": iid, "assembly_accession": f"GCF_{t}.1", "ncbi_taxid": t})
            img_emb[iid] = F.normalize(fam_dir[f] + 0.5 * sp_dir + 0.3 * torch.randn(512), dim=-1)


def write(name, rows):
    with open(ds / name, "w", newline="") as h:
        w = csv.DictWriter(h, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


for name, rows in [
    ("species.csv", species),
    ("genomes.csv", genomes),
    ("images.csv", images),
    ("pairs.csv", pairs),
]:
    write(name, rows)
unseen = [int(1000 + f * 10 + rng.randrange(SP)) for f in range(FAM)]


# --- one run per control -------------------------------------------------------------------------------------
def run(control: str) -> dict:
    out = root / control
    prepare_records(
        ds, out, unseen=unseen, genome_cache=root / "packed", dna_control=control, workers=4
    )
    with open(out / "records.csv", newline="") as h:
        recs = list(csv.DictReader(h))
    emb = {
        "processid": [r["processid"] for r in recs],
        "image": torch.stack([img_emb[r["processid"]] for r in recs]),
    }
    emb["text"] = emb["image"]
    index = {p: i for i, p in enumerate(emb["processid"])}
    train = [r for r in recs if r["split"] == "train"]
    img = emb["image"][[index[r["processid"]] for r in train]]
    torch.manual_seed(0)
    tok = HFTokenizer(MODERNGENA, shuffle=control == "shuffle_tokens")
    enc = DNAEncoder.tiny()
    enc.tokenizer = tok
    model = AlignModel(enc)
    train_align(
        model,
        None,
        [label(r) for r in train],
        img,
        img,
        epochs=12,
        batch_size=32,
        lr=1e-3,
        head_lr=3e-3,
        sample_dna=window_sampler(train),
        text_weight=0.0,
    )
    table = ev.species_table(recs, emb, index)
    groups = {
        "unseen": [t for t in table if table[t]["unseen"]],
        "seen": [t for t in table if not table[t]["unseen"]],
    }
    out_scores = {}
    for g, ts in groups.items():
        q = embed_records(model.dna, [table[t]["rec"] for t in ts], k=8)
        out_scores[g] = ev.score(dict(zip(ts, q)), table)
    return out_scores


results = {c: run(c) for c in ("none", "shuffle_tokens", "permute_genomes")}
print("\nheld-out (8 species) and seen (24) top-1; chance: family 1/8, species 1/32")
for c, r in results.items():
    print(
        f"  {c:16s} held-out family {r['unseen']['family_top1']:.2f}  |  "
        f"seen family {r['seen']['family_top1']:.2f}, seen species {r['seen']['species_top1']:.2f}"
    )
