"""Rebuild the figures for the control B report from the two run directories.

usage: uv run --no-project --with matplotlib --with pillow make_figures.py
"""

import json
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image

RUNS = Path("/mnt/filesystem-a3/genes.jpg/runs")
MAIN = RUNS / "tol200m-refseq-diverse-genome"
CTRL = RUNS / "tol200m-refseq-diverse-genome-control-shuffle_tokens"
OUT = Path(__file__).parent
RUNSET = [
    ("main", MAIN, "Main run (intact DNA)", "#1f5f9e"),
    ("B", CTRL, "Control B (shuffled tokens)", "#c2571a"),
]
SPLITS = [
    ("seen", "Seen species"),
    ("new_species_known_family", "New species, known family"),
    ("new_family_known_class", "New family, known class"),
]
RANKS = ["species", "genus", "family", "order", "class"]

plt.rcParams.update(
    {
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.color": "#e3e3e3",
        "figure.dpi": 130,
    }
)


def align_curves():
    rows = ["run,epoch,loss,seen_species_top1,seen_pooled_species_top1,unseen_pooled_species_top1"]
    fig, ax = plt.subplots(1, 3, figsize=(13, 3.8))
    for key, run, label, col in RUNSET:
        ep, loss, seen, unseen = [], [], [], []
        for line in (run / "train_align.log").read_text().splitlines():
            m = re.match(r"align epoch (\d+)/\d+ loss ([\d.]+)", line)
            if not m:
                continue
            d = dict(re.findall(r"(\S+)=([\d.]+)", line))
            ep.append(int(m[1]))
            loss.append(float(m[2]))
            seen.append(float(d["val/pooled_species_top1"]))
            unseen.append(float(d["val_unseen/pooled_species_top1"]))
            rows.append(
                f"{key},{m[1]},{m[2]},{d['val/species_top1']},{d['val/pooled_species_top1']},"
                f"{d['val_unseen/pooled_species_top1']}"
            )
        ax[0].plot(ep, loss, color=col, label=label)
        ax[1].plot(ep, seen, color=col, marker="o", ms=3)
        ax[2].plot(ep, unseen, color=col, marker="o", ms=3)
    ax[0].set(title="Contrastive loss", xlabel="epoch", ylabel="loss")
    ax[1].set(title="Seen species: pooled species top-1", xlabel="epoch", ylim=(0, 0.6))
    ax[2].set(title="Held-out species: pooled species top-1", xlabel="epoch", ylim=(0, 0.6))
    ax[0].legend(frameon=False)
    for a in ax:
        a.set_xticks([1, 5, 10, 15, 20])
    fig.tight_layout()
    fig.savefig(OUT / "align_training.png")
    plt.close(fig)
    (OUT / "align.csv").write_text("\n".join(rows) + "\n")


def prior_curves():
    rows = ["run,epoch,loss"]
    fig, ax = plt.subplots(figsize=(7, 3.6))
    for key, run, label, col in RUNSET:
        ep, loss = [], []
        for line in (run / "train_prior.log").read_text().splitlines():
            m = re.match(r"prior epoch (\d+)/\d+ loss ([\d.]+)", line)
            if m:
                ep.append(int(m[1]))
                loss.append(float(m[2]))
                rows.append(f"{key},{m[1]},{m[2]}")
        ax.plot(ep, loss, color=col, label=label, lw=1.2)
    ax.set(
        title="Diffusion prior training loss",
        xlabel="epoch",
        ylabel="loss (log scale)",
        yscale="log",
    )
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(OUT / "prior_training.png")
    plt.close(fig)
    (OUT / "prior.csv").write_text("\n".join(rows) + "\n")


def rank_panels(fname, title, series):
    """series: list of (label, color, style, {split: {rank_top1: value}})."""
    fig, ax = plt.subplots(1, 3, figsize=(13, 3.8), sharey=True)
    x = range(len(RANKS))
    for a, (split, name) in zip(ax, SPLITS):
        for label, col, style, data in series:
            a.plot(
                x, [data[split][f"{r}_top1"] for r in RANKS], style, color=col, label=label, ms=5
            )
        a.set(title=name, xticks=list(x), xticklabels=RANKS, ylim=(0, 1.05))
    ax[0].set_ylabel("top-1 accuracy")
    ax[-1].legend(frameon=False, fontsize=8.5, loc="upper left")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(OUT / fname)
    plt.close(fig)


def evaluation():
    ev = {k: json.loads((r / "evaluation.json").read_text())["methods"] for k, r, _, _ in RUNSET}
    rank_panels(
        "retrieval_by_rank.png",
        "Nearest species for each genome embedding, scored at each taxonomic rank",
        [
            ("Main: aligned embedding", "#1f5f9e", "-o", ev["main"]["aligned"]),
            ("B: aligned embedding", "#c2571a", "-o", ev["B"]["aligned"]),
            ("Main: prior samples", "#1f5f9e", ":s", ev["main"]["prior"]),
            ("B: prior samples", "#c2571a", ":s", ev["B"]["prior"]),
            ("k-mer nearest neighbour", "#555555", "--^", ev["main"]["kmer_nn"]),
            ("chance", "#aaaaaa", "--", ev["main"]["chance"]),
        ],
    )
    gen = {k: json.loads((r / "generated_eval.json").read_text()) for k, r, _, _ in RUNSET}
    rank_panels(
        "generated_by_rank.png",
        "Generated images, classified by BioCLIP, scored at each taxonomic rank",
        [
            ("Main run", "#1f5f9e", "-o", gen["main"]),
            ("Control B", "#c2571a", "-o", gen["B"]),
            ("chance", "#aaaaaa", "--", ev["main"]["chance"]),
        ],
    )


def contact_sheets(width=900):
    for key, run, _, _ in RUNSET:
        for split, _ in SPLITS:
            im = Image.open(run / "samples" / f"contact_sheet_{split}.jpg").convert("RGB")
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
            im.save(OUT / f"{key}_{split}.jpg", quality=80, optimize=True)


def example_rows(width=900, row_h=220, split_x=800, gap=6):
    """Stack chosen contact-sheet rows (4 generated | 2 real) with a gap between generated and real photos."""
    picks = {
        "examples_work.jpg": [
            ("main", "seen", 5),
            ("main", "new_species_known_family", 1),
            ("main", "seen", 6),
            ("main", "new_species_known_family", 0),
            ("main", "new_species_known_family", 3),
            ("main", "new_species_known_family", 6),
        ],
        "examples_fail.jpg": [
            ("main", "seen", 1),
            ("main", "seen", 2),
            ("main", "new_species_known_family", 4),
            ("main", "new_family_known_class", 2),
            ("main", "new_family_known_class", 3),
        ],
        "examples_main_vs_b.jpg": [("main", "seen", 4), ("B", "seen", 4)],
    }
    runs = {key: run for key, run, _, _ in RUNSET}
    for fname, rows in picks.items():
        crops = []
        for key, split, i in rows:
            sheet = Image.open(runs[key] / "samples" / f"contact_sheet_{split}.jpg").convert("RGB")
            row = sheet.crop((0, i * row_h, sheet.width, (i + 1) * row_h))
            out = Image.new("RGB", (row.width + gap, row_h), "white")
            out.paste(row.crop((0, 0, split_x, row_h)), (0, 0))
            out.paste(row.crop((split_x, 0, row.width, row_h)), (split_x + gap, 0))
            crops.append(out)
        im = Image.new("RGB", (crops[0].width, row_h * len(crops)), "white")
        for k, c in enumerate(crops):
            im.paste(c, (0, k * row_h))
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        im.save(OUT / fname, quality=85, optimize=True)


if __name__ == "__main__":
    align_curves()
    prior_curves()
    evaluation()
    contact_sheets()
    example_rows()
