"""Command line: python -m genesjpg <command> --data DIR ...

Stages: download (BIOSCAN-5M) or prepare (a genes.jpg genome <-> image dataset) -> embed -> train-align
-> train-prior -> train-decoder -> generate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch

from .data import download_subset, label, read_records, taxonomy_text


def _paths(data: str) -> tuple[Path, Path]:
    """Data root and its checkpoints/ directory (created if missing)."""
    root = Path(data)
    ckpt = root / "checkpoints"
    ckpt.mkdir(parents=True, exist_ok=True)
    return root, ckpt


def _load(data: str):
    """Records, precomputed BioCLIP embeddings, and processid -> embedding row index."""
    root, _ = _paths(data)
    recs = read_records(root / "records.csv")
    emb = torch.load(root / "embeddings.pt")
    index = {pid: i for i, pid in enumerate(emb["processid"])}
    return recs, emb, index


def _split(recs, emb, index, split):
    """Records of one split with their image and taxonomy-text embeddings (row-aligned)."""
    rs = [r for r in recs if r["split"] == split and r["processid"] in index]
    ix = [index[r["processid"]] for r in rs]
    return rs, emb["image"][ix], emb["text"][ix]


def cmd_download(a):
    """Download the paired BIOSCAN-5M subset into --data."""
    download_subset(a.data, n_train=a.n_train, n_eval=a.n_eval, n_blocks=a.n_blocks)


def cmd_prepare(a):
    """Write records.csv into --data from a genome <-> image dataset (nuclear-genome windows)."""
    from .genomes import prepare_records

    prepare_records(
        a.dataset,
        a.data,
        unseen=a.unseen,
        val_frac=a.val_frac,
        seed=a.seed,
        genome_cache=a.genome_cache,
    )


def cmd_embed(a):
    """Compute frozen BioCLIP embeddings for every image and its taxonomy caption -> embeddings.pt."""
    from .encoders import BioCLIP

    root, _ = _paths(a.data)
    recs = read_records(root / "records.csv")
    clip = BioCLIP(device=a.device)
    img = clip.embed_files([root / r["image_path"] for r in recs], batch_size=a.batch_size)
    texts = [taxonomy_text(r) for r in recs]
    uniq = sorted(set(texts))
    uniq_emb = torch.cat([clip.encode_texts(uniq[i : i + 256]) for i in range(0, len(uniq), 256)])
    lookup = {t: i for i, t in enumerate(uniq)}
    txt = uniq_emb[[lookup[t] for t in texts]]
    torch.save(
        {"processid": [r["processid"] for r in recs], "image": img, "text": txt},
        root / "embeddings.pt",
    )
    print(f"saved {len(recs)} image and {len(uniq)} unique taxonomy-text embeddings")


def _eval_sets(recs, emb, index):
    return {s: _split(recs, emb, index, s) for s in ("val", "val_unseen")}


def _pooled_gallery(evals):
    """All held-out images (val + val_unseen) with their species and genus labels."""
    rs = [r for s in evals for r in evals[s][0]]
    im = torch.cat([evals[s][1] for s in evals])
    return im, [label(r) for r in rs], [r["genus"] for r in rs]


def _eval_metrics(q, rs, im, pooled):
    """Per-split retrieval metrics plus species/genus top-1 against the pooled held-out gallery.

    Every image of a species shares one DNA input, so specimen_top1/5 are at chance by construction; and with
    few species in a split its own species_top1 is easy (1.0 when it holds one species): read pooled_*."""
    from .align import pooled_metrics, retrieval_metrics

    labs, gens = [label(r) for r in rs], [r["genus"] for r in rs]
    return {**retrieval_metrics(q, im, labs, gens), **pooled_metrics(q, labs, gens, *pooled)}


def cmd_train_align(a):
    """Steps 1-2: fine-tune ModernGENA against BioCLIP embeddings; writes align.pt + align_metrics.json."""
    from .align import AlignModel, train_align
    from .encoders import DNAEncoder
    from .genomes import embed_records, window_sampler
    from .pipeline import save_align

    _, ckpt = _paths(a.data)
    recs, emb, index = _load(a.data)
    train, img, txt = _split(recs, emb, index, "train")
    evals = _eval_sets(recs, emb, index)
    pooled = _pooled_gallery(evals)
    model = AlignModel(DNAEncoder(freeze_layers=a.freeze_layers))

    def eval_fn(m):
        out = {}
        for s, (rs, im, _) in evals.items():
            if rs:
                met = _eval_metrics(embed_records(m.dna, rs), rs, im, pooled)
                out.update({f"{s}/{k}": v for k, v in met.items()})
        return out

    genome = bool(train[0].get("genome"))  # genome mode: fresh random windows every batch
    train_align(
        model,
        None if genome else [r["dna_barcode"] for r in train],
        [label(r) for r in train],
        img,
        txt,
        epochs=a.epochs,
        batch_size=a.batch_size,
        lr=a.lr,
        text_weight=a.text_weight,
        eval_fn=eval_fn,
        device=a.device,
        seed=a.seed,
        sample_dna=window_sampler(train, seed=a.seed) if genome else None,
    )
    save_align(model.cpu(), ckpt / "align.pt")
    metrics = eval_fn(model)
    (ckpt / "align_metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))


def cmd_train_prior(a):
    """Step 3: train the diffusion prior on (aligned DNA emb, image emb) pairs; writes prior.pt."""
    from .align import pooled_metrics, retrieval_metrics
    from .genomes import embed_records
    from .pipeline import load_align, save_prior
    from .prior import DiffusionPrior, train_prior

    _, ckpt = _paths(a.data)
    recs, emb, index = _load(a.data)
    align = load_align(ckpt / "align.pt").to(a.device)
    train, img, _ = _split(recs, emb, index, "train")
    cond = embed_records(align.dna, train)
    evals = {}
    eval_sets = _eval_sets(recs, emb, index)
    pooled = _pooled_gallery(eval_sets)
    for s, (rs, im, _) in eval_sets.items():
        if rs:
            q = embed_records(align.dna, rs)
            evals[s] = (q, im, [label(r) for r in rs], [r["genus"] for r in rs])
            base = _eval_metrics(q, rs, im, pooled)
            print(f"{s} baseline (aligned DNA emb, no prior): {base}")

    def eval_fn(p):
        out = {}
        for s, (q, im, labs, gens) in evals.items():
            g = torch.Generator(device=a.device).manual_seed(0)
            sampled = p.sample(
                q.to(a.device), steps=a.sample_steps, guidance=a.guidance, generator=g
            ).cpu()
            out[f"{s}/cos"] = (sampled * im).sum(-1).mean().item()
            met = retrieval_metrics(sampled, im, labs, gens)
            met.update(pooled_metrics(sampled, labs, gens, *pooled))
            keep = ("species_top1", "genus_top1", "pooled_species_top1", "pooled_genus_top1")
            out.update({f"{s}/{k}": v for k, v in met.items() if k in keep})
        return out

    prior = DiffusionPrior(width=a.width, depth=a.depth)
    train_prior(
        prior,
        cond,
        img,
        epochs=a.epochs,
        batch_size=a.batch_size,
        lr=a.lr,
        eval_fn=eval_fn,
        device=a.device,
    )
    save_prior(prior.cpu(), ckpt / "prior.pt")
    metrics = eval_fn(prior.to(a.device))
    (ckpt / "prior_metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))


def cmd_train_decoder(a):
    """Step 4: train the SD decoder on images + their BioCLIP embeddings; writes decoder.pt (GPU)."""
    from .decoder import EmbeddingDecoder, train_decoder

    root, ckpt = _paths(a.data)
    recs, emb, index = _load(a.data)
    # the decoder sees no DNA, but training it on val_unseen photos would leak the held-out species' looks
    # into "generate an unseen species"; --include-unseen trains on every image (e.g. a final model)
    recs = [
        r
        for r in recs
        if r["processid"] in index and (a.include_unseen or r["split"] != "val_unseen")
    ]
    print(f"decoder: training on {len(recs)} images")
    decoder = EmbeddingDecoder(a.model_id, n_tokens=a.n_tokens, train_unet=a.train_unet)
    train_decoder(
        decoder,
        [root / r["image_path"] for r in recs],
        emb["image"][[index[r["processid"]] for r in recs]],
        epochs=a.epochs,
        batch_size=a.batch_size,
        lr=a.lr,
        size=a.size,
        max_steps=a.max_steps,
        device=a.device,
    )
    decoder.save(ckpt / "decoder.pt")
    print(f"saved {ckpt / 'decoder.pt'}")


def cmd_generate(a):
    """Print the nearest real specimens for a barcode and, if decoder.pt exists, save generated images."""
    from .genomes import PackedGenome
    from .pipeline import GenomeToImage

    root, ckpt = _paths(a.data)
    recs, emb, _ = _load(a.data)
    seq = a.dna
    if a.processid:
        rec = next((r for r in recs if r["processid"] == a.processid), None)
        if rec is None:
            raise SystemExit(f"processid {a.processid!r} not found in records.csv")
        seq = PackedGenome(rec["genome"]) if rec.get("genome") else rec["dna_barcode"]
        print(f"{a.processid}: true label {label(rec)}")
    by_id = {r["processid"]: r for r in recs}
    gallery = (emb["image"], [by_id[pid] for pid in emb["processid"]])
    model = GenomeToImage.from_checkpoints(
        ckpt, gallery=gallery, model_id=a.model_id, device=a.device
    )
    print("nearest real specimens:")
    for r, s in model.retrieve(seq, k=a.k):
        print(f"  {s:.3f}  {label(r)}  {root / r['image_path']}")
    if model.decoder is None:
        print("no decoder.pt yet; run train-decoder (GPU) to generate images")
        return
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for i, im in enumerate(
        model.generate(seq, n=a.n, seed=a.seed, steps=a.steps, guidance=a.guidance)
    ):
        im.save(out / f"sample_{i}.png")
    print(f"saved {a.n} images to {out}")


def main(argv=None):
    """Parse arguments and run one pipeline command."""
    p = argparse.ArgumentParser(prog="genesjpg")
    p.add_argument("--data", default="data", help="data/checkpoint directory")
    p.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("download")
    s.add_argument("--n-train", type=int, default=20000)
    s.add_argument("--n-eval", type=int, default=3000)
    s.add_argument("--n-blocks", type=int, default=20)
    s.set_defaults(fn=cmd_download)

    s = sub.add_parser("prepare")
    s.add_argument(
        "--dataset", required=True, help="built dataset dir (species/genomes/images/pairs.csv)"
    )
    s.add_argument(
        "--unseen", type=int, nargs="*", default=[], help="species taxids held out as val_unseen"
    )
    s.add_argument("--val-frac", type=float, default=0.2)
    s.add_argument(
        "--genome-cache",
        default=None,
        help="packed genomes dir (default: <dataset>/../_packed_genomes)",
    )
    s.add_argument("--seed", type=int, default=0)
    s.set_defaults(fn=cmd_prepare)

    s = sub.add_parser("embed")
    s.add_argument("--batch-size", type=int, default=64)
    s.set_defaults(fn=cmd_embed)

    s = sub.add_parser("train-align")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--epochs", type=int, default=5)
    s.add_argument("--batch-size", type=int, default=128)
    s.add_argument("--lr", type=float, default=1e-4)
    s.add_argument("--text-weight", type=float, default=0.5)
    s.add_argument(
        "--freeze-layers",
        type=int,
        default=0,
        help="freeze embeddings + the first N backbone layers",
    )
    s.set_defaults(fn=cmd_train_align)

    s = sub.add_parser("train-prior")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--epochs", type=int, default=50)
    s.add_argument("--batch-size", type=int, default=256)
    s.add_argument("--lr", type=float, default=3e-4)
    s.add_argument("--width", type=int, default=1024)
    s.add_argument("--depth", type=int, default=4)
    s.add_argument("--sample-steps", type=int, default=50)
    s.add_argument("--guidance", type=float, default=2.0)
    s.set_defaults(fn=cmd_train_prior)

    s = sub.add_parser("train-decoder")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--model-id", default="stable-diffusion-v1-5/stable-diffusion-v1-5")
    s.add_argument("--epochs", type=int, default=1)
    s.add_argument("--batch-size", type=int, default=8)
    s.add_argument("--lr", type=float, default=1e-4)
    s.add_argument("--size", type=int, default=512)
    s.add_argument("--n-tokens", type=int, default=8)
    s.add_argument("--train-unet", action="store_true")
    s.add_argument("--max-steps", type=int, default=None)
    s.add_argument("--include-unseen", action="store_true", help="also train on val_unseen photos")
    s.set_defaults(fn=cmd_train_decoder)

    s = sub.add_parser("generate")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--dna")
    g.add_argument("--processid")
    s.add_argument("--model-id", default="stable-diffusion-v1-5/stable-diffusion-v1-5")
    s.add_argument("--n", type=int, default=4)
    s.add_argument("--k", type=int, default=5)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--steps", type=int, default=30)
    s.add_argument("--guidance", type=float, default=5.0)
    s.add_argument("--out", default="samples")
    s.set_defaults(fn=cmd_generate)

    a = p.parse_args(argv)
    # seeds weight init, prior/decoder noise and batch order; GPU kernels can still differ in the last bits
    torch.manual_seed(getattr(a, "seed", 0))
    a.fn(a)
