"""Step 2: align DNA embeddings with BioCLIP image and taxonomy-text embeddings (CLIBD-style).

The DNA encoder is fine-tuned so that a barcode's embedding lands next to the frozen BioCLIP embedding of the
same specimen's photo (main loss) and of its taxonomy caption (auxiliary loss, teaches the order/family/genus
hierarchy and helps on species never seen in training). After this step, nearest-neighbour search in BioCLIP
space already gives a DNA -> real photo retrieval baseline.
"""

from __future__ import annotations

import math
import random

import torch
import torch.nn.functional as F
from torch import nn

from .encoders import DNAEncoder


def contrastive_loss(
    a: torch.Tensor, b: torch.Tensor, logit_scale: torch.Tensor, labels: torch.Tensor | None = None
):
    """Symmetric InfoNCE between two batches of unit vectors.

    Args:
        a, b: (B, D) L2-normalised embeddings; row i of ``a`` is paired with row i of ``b``.
        logit_scale: log of the softmax temperature's inverse (learned, CLIP-style).
        labels: optional (B,) class ids. If given, every same-label pair counts as a positive, so several
            specimens of one species in a batch are not pushed apart as false negatives.
    """
    logits = logit_scale.exp() * a @ b.T
    if labels is None:
        targets = torch.eye(len(a), device=a.device)
    else:
        targets = (labels[:, None] == labels[None, :]).float()
    targets = targets / targets.sum(1, keepdim=True)
    loss_ab = -(targets * F.log_softmax(logits, dim=1)).sum(1).mean()
    loss_ba = -(targets * F.log_softmax(logits.T, dim=1)).sum(1).mean()
    return (loss_ab + loss_ba) / 2


class AlignModel(nn.Module):
    """DNA tower plus the learned contrastive temperature. BioCLIP towers stay frozen and outside the model;
    their embeddings are precomputed by ``python -m genesjpg embed``."""

    def __init__(self, dna_encoder: DNAEncoder):
        super().__init__()
        self.dna = dna_encoder
        self.logit_scale = nn.Parameter(torch.tensor(math.log(1 / 0.07)))

    def forward(self, input_ids, attention_mask):
        return self.dna(input_ids, attention_mask)


def retrieval_metrics(
    query: torch.Tensor, gallery: torch.Tensor, labels: list[str], genera: list[str]
) -> dict:
    """DNA -> image retrieval metrics, where query i's true image is gallery row i.

    Returns specimen top-1/top-5 (the exact specimen's photo is retrieved) and species/genus top-1
    (the nearest photo has the same species/genus label as the query).
    """
    sims = query @ gallery.T
    top5 = sims.topk(min(5, len(gallery)), dim=1).indices
    idx = torch.arange(len(query))
    nearest = top5[:, 0].tolist()
    return {
        "specimen_top1": (top5[:, 0] == idx).float().mean().item(),
        "specimen_top5": (top5 == idx[:, None]).any(1).float().mean().item(),
        "species_top1": sum(labels[j] == labels[i] for i, j in enumerate(nearest)) / len(query),
        "genus_top1": sum(genera[j] == genera[i] for i, j in enumerate(nearest)) / len(query),
    }


def train_align(
    model: AlignModel,
    seqs: list[str],
    labels: list[str],
    img_emb: torch.Tensor,
    txt_emb: torch.Tensor,
    epochs: int = 5,
    batch_size: int = 128,
    lr: float = 1e-4,
    head_lr: float = 1e-3,
    text_weight: float = 0.5,
    eval_fn=None,
    device: str = "cpu",
    seed: int = 0,
) -> AlignModel:
    """Fine-tune the DNA encoder against precomputed BioCLIP embeddings.

    Loss = InfoNCE(DNA, image) + ``text_weight`` * InfoNCE(DNA, taxonomy text), label-aware on ``labels``.
    The pretrained backbone uses ``lr``; the freshly initialised head and temperature use ``head_lr``.
    ``eval_fn(model) -> dict`` is called after every epoch and its metrics are printed.
    """
    model.to(device).train()
    head = [*model.dna.head.parameters(), model.logit_scale]
    head_ids = {id(p) for p in head}
    body = [p for p in model.parameters() if p.requires_grad and id(p) not in head_ids]
    opt = torch.optim.AdamW(
        [{"params": body, "lr": lr}, {"params": head, "lr": head_lr}], weight_decay=0.01
    )
    label_ids = {lab: i for i, lab in enumerate(sorted(set(labels)))}
    y = torch.tensor([label_ids[lab] for lab in labels])
    ids, mask = model.dna.tokenizer(seqs)
    order = list(range(len(seqs)))
    rng = random.Random(seed)
    for epoch in range(epochs):
        model.train()
        rng.shuffle(order)
        total, n = 0.0, 0
        for i in range(0, len(order), batch_size):
            b = torch.tensor(order[i : i + batch_size])
            if len(b) < 2:
                continue
            width = int(mask[b].sum(1).max())  # trim padding to the longest sequence in this batch
            dna = model(ids[b, :width].to(device), mask[b, :width].to(device))
            yb = y[b].to(device)
            loss = contrastive_loss(dna, img_emb[b].to(device), model.logit_scale, yb)
            if text_weight:
                loss = loss + text_weight * contrastive_loss(
                    dna, txt_emb[b].to(device), model.logit_scale, yb
                )
            opt.zero_grad()
            loss.backward()
            opt.step()
            with torch.no_grad():
                model.logit_scale.clamp_(0, math.log(100))
            total += loss.item() * len(b)
            n += len(b)
        msg = f"align epoch {epoch + 1}/{epochs} loss {total / max(n, 1):.4f}"
        if eval_fn:
            msg += " " + " ".join(f"{k}={v:.3f}" for k, v in eval_fn(model).items())
        print(msg, flush=True)
    return model
