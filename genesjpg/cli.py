"""Command line: python -m genesjpg <command> --data DIR ...

Stages: download -> embed -> train-align -> train-prior -> train-decoder -> generate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch

from .data import download_subset, label, read_records, taxonomy_text


def _paths(data: str) -> tuple[Path, Path]:
    root = Path(data)
    ckpt = root / "checkpoints"
    ckpt.mkdir(parents=True, exist_ok=True)
    return root, ckpt


def _load(data: str):
    root, _ = _paths(data)
    recs = read_records(root / "records.csv")
    emb = torch.load(root / "embeddings.pt")
    index = {pid: i for i, pid in enumerate(emb["processid"])}
    return recs, emb, index


def _split(recs, emb, index, split):
    rs = [r for r in recs if r["split"] == split and r["processid"] in index]
    ix = [index[r["processid"]] for r in rs]
    return rs, emb["image"][ix], emb["text"][ix]


def cmd_download(a):
    download_subset(a.data, n_train=a.n_train, n_eval=a.n_eval, n_blocks=a.n_blocks)


def cmd_embed(a):
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
    torch.save({"processid": [r["processid"] for r in recs], "image": img, "text": txt}, root / "embeddings.pt")
    print(f"saved {len(recs)} image and {len(uniq)} unique taxonomy-text embeddings")


def _eval_sets(recs, emb, index):
    return {s: _split(recs, emb, index, s) for s in ("val", "val_unseen")}


def cmd_train_align(a):
    from .align import AlignModel, retrieval_metrics, train_align
    from .encoders import DNAEncoder
    from .pipeline import save_align

    _, ckpt = _paths(a.data)
    recs, emb, index = _load(a.data)
    train, img, txt = _split(recs, emb, index, "train")
    evals = _eval_sets(recs, emb, index)
    model = AlignModel(DNAEncoder(freeze_layers=a.freeze_layers))

    def eval_fn(m):
        out = {}
        for s, (rs, im, _) in evals.items():
            if rs:
                q = m.dna.encode([r["dna_barcode"] for r in rs])
                met = retrieval_metrics(q, im, [label(r) for r in rs], [r["genus"] for r in rs])
                out.update({f"{s}/{k}": v for k, v in met.items()})
        return out

    train_align(
        model,
        [r["dna_barcode"] for r in train],
        [label(r) for r in train],
        img,
        txt,
        epochs=a.epochs,
        batch_size=a.batch_size,
        lr=a.lr,
        text_weight=a.text_weight,
        eval_fn=eval_fn,
        device=a.device,
    )
    save_align(model.cpu(), ckpt / "align.pt")
    metrics = eval_fn(model)
    (ckpt / "align_metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))


def cmd_train_prior(a):
    from .align import retrieval_metrics
    from .pipeline import load_align, save_prior
    from .prior import DiffusionPrior, train_prior

    _, ckpt = _paths(a.data)
    recs, emb, index = _load(a.data)
    align = load_align(ckpt / "align.pt").to(a.device)
    train, img, _ = _split(recs, emb, index, "train")
    cond = align.dna.encode([r["dna_barcode"] for r in train])
    evals = {}
    for s, (rs, im, _) in _eval_sets(recs, emb, index).items():
        if rs:
            q = align.dna.encode([r["dna_barcode"] for r in rs])
            evals[s] = (q, im, [label(r) for r in rs], [r["genus"] for r in rs])
            base = retrieval_metrics(q, im, evals[s][2], evals[s][3])
            print(f"{s} baseline (aligned DNA emb, no prior): {base}")

    def eval_fn(p):
        out = {}
        for s, (q, im, labs, gens) in evals.items():
            g = torch.Generator().manual_seed(0)
            sampled = p.sample(q.to(a.device), steps=a.sample_steps, guidance=a.guidance, generator=g).cpu()
            out[f"{s}/cos"] = (sampled * im).sum(-1).mean().item()
            met = retrieval_metrics(sampled, im, labs, gens)
            out.update({f"{s}/{k}": v for k, v in met.items() if k in ("species_top1", "genus_top1")})
        return out

    prior = DiffusionPrior(width=a.width, depth=a.depth)
    train_prior(prior, cond, img, epochs=a.epochs, batch_size=a.batch_size, lr=a.lr, eval_fn=eval_fn, device=a.device)
    save_prior(prior.cpu(), ckpt / "prior.pt")
    metrics = eval_fn(prior.to(a.device))
    (ckpt / "prior_metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))


def cmd_train_decoder(a):
    from .decoder import EmbeddingDecoder, train_decoder

    root, ckpt = _paths(a.data)
    recs, emb, index = _load(a.data)
    recs = [r for r in recs if r["processid"] in index]
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
    from .pipeline import GenomeToImage

    root, ckpt = _paths(a.data)
    recs, emb, _ = _load(a.data)
    seq = a.dna
    if a.processid:
        rec = next(r for r in recs if r["processid"] == a.processid)
        seq = rec["dna_barcode"]
        print(f"{a.processid}: true label {label(rec)}")
    by_id = {r["processid"]: r for r in recs}
    gallery = (emb["image"], [by_id[pid] for pid in emb["processid"]])
    model = GenomeToImage.from_checkpoints(ckpt, gallery=gallery, model_id=a.model_id, device=a.device)
    print("nearest real specimens:")
    for r, s in model.retrieve(seq, k=a.k):
        print(f"  {s:.3f}  {label(r)}  {root / r['image_path']}")
    if model.decoder is None:
        print("no decoder.pt yet; run train-decoder (GPU) to generate images")
        return
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for i, im in enumerate(model.generate(seq, n=a.n, seed=a.seed, steps=a.steps, guidance=a.guidance)):
        im.save(out / f"sample_{i}.png")
    print(f"saved {a.n} images to {out}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="genesjpg")
    p.add_argument("--data", default="data", help="data/checkpoint directory")
    p.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("download")
    s.add_argument("--n-train", type=int, default=20000)
    s.add_argument("--n-eval", type=int, default=3000)
    s.add_argument("--n-blocks", type=int, default=20)
    s.set_defaults(fn=cmd_download)

    s = sub.add_parser("embed")
    s.add_argument("--batch-size", type=int, default=64)
    s.set_defaults(fn=cmd_embed)

    s = sub.add_parser("train-align")
    s.add_argument("--epochs", type=int, default=5)
    s.add_argument("--batch-size", type=int, default=128)
    s.add_argument("--lr", type=float, default=1e-4)
    s.add_argument("--text-weight", type=float, default=0.5)
    s.add_argument("--freeze-layers", type=int, default=0, help="freeze embeddings + the first N backbone layers")
    s.set_defaults(fn=cmd_train_align)

    s = sub.add_parser("train-prior")
    s.add_argument("--epochs", type=int, default=50)
    s.add_argument("--batch-size", type=int, default=256)
    s.add_argument("--lr", type=float, default=3e-4)
    s.add_argument("--width", type=int, default=1024)
    s.add_argument("--depth", type=int, default=4)
    s.add_argument("--sample-steps", type=int, default=50)
    s.add_argument("--guidance", type=float, default=2.0)
    s.set_defaults(fn=cmd_train_prior)

    s = sub.add_parser("train-decoder")
    s.add_argument("--model-id", default="stable-diffusion-v1-5/stable-diffusion-v1-5")
    s.add_argument("--epochs", type=int, default=1)
    s.add_argument("--batch-size", type=int, default=8)
    s.add_argument("--lr", type=float, default=1e-4)
    s.add_argument("--size", type=int, default=512)
    s.add_argument("--n-tokens", type=int, default=8)
    s.add_argument("--train-unet", action="store_true")
    s.add_argument("--max-steps", type=int, default=None)
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
    a.fn(a)
