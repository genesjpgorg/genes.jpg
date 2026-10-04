"""Held-out-species evaluation of DNA -> image-embedding methods, baselines included.

Every method maps a species to one or more query vectors in BioCLIP image space. Each query is matched against the
centroids of all species' held-out photos (``val`` photos of training species, all photos of ``val_unseen`` species),
and scored by whether the nearest centroid (top-1) or any of the 5 nearest (top-5) shares the query species' species,
genus, family, order and class. A method that maps an unseen deer to another deer gets genus or family credit.

Methods:
- ``chance``: the expected score of a uniformly random candidate species.
- ``kmer_nn``: nearest training species by alignment-free k-mer composition of the genome (canonical 6-mers over
  fixed windows), answered with that species' training-photo centroid. The bar a DNA model has to clear.
- ``caption_<rank>``: BioCLIP text embedding of the taxonomy caption cut at that rank (``species`` = full name).
  An oracle: what the DNA would be worth if it told us the name, or only the family.
- ``frozen_linear``: pretrained (not fine-tuned) ModernGENA genome embedding, ridge-regressed onto training species'
  photo centroids.
- ``aligned``: the trained alignment model's genome embedding (steps 1-2).
- ``prior``: samples of the diffusion prior conditioned on that embedding (step 3), each scored separately.
"""

from __future__ import annotations

import random

import numpy as np
import torch
import torch.nn.functional as F

from .data import label, taxonomy_text

RANKS = ("species", "genus", "family", "order", "class")


def _ranks(r: dict) -> dict:
    return {
        "species": label(r),
        "genus": r["genus"],
        "family": r["family"],
        "order": r["order"],
        "class": r.get("class", ""),
    }


def species_table(records: list[dict], emb: dict, index: dict) -> dict:
    """Per-species lineage, representative record, and photo centroids of held-out and training photos."""
    out: dict[str, dict] = {}
    for r in records:
        if r["processid"] not in index:
            continue
        sp = out.setdefault(
            r["ncbi_taxid"],
            {"rec": r, "ranks": _ranks(r), "unseen": False, "held": [], "train": []},
        )
        sp["unseen"] |= r["split"] == "val_unseen"
        sp["held" if r["split"] != "train" else "train"].append(index[r["processid"]])
    for sp in out.values():
        for k in ("held", "train"):
            sp[k] = F.normalize(emb["image"][sp[k]].mean(0), dim=-1) if sp[k] else None
    return out


def score(queries: dict[str, torch.Tensor], table: dict, k: int = 5) -> dict:
    """Mean top-1 / top-k rank agreement of query vectors (taxid -> (n, 512) or (512,)) against the centroids of
    every species' held-out photos."""
    cands = [t for t in table if table[t]["held"] is not None]
    cent = torch.stack([table[t]["held"] for t in cands])
    hits = {f"{r}_top{n}": [] for r in RANKS for n in (1, k)}
    for t, q in queries.items():
        q = q.reshape(-1, cent.shape[1])
        top = (F.normalize(q, dim=-1) @ cent.T).topk(min(k, len(cands)), dim=1).indices
        for row in top.tolist():
            for r in RANKS:
                same = [table[cands[j]]["ranks"][r] == table[t]["ranks"][r] for j in row]
                hits[f"{r}_top1"].append(same[0])
                hits[f"{r}_top{k}"].append(any(same))
    return {m: float(np.mean(v)) for m, v in hits.items()}


def chance(targets: list[str], table: dict, k: int = 5) -> dict:
    """Expected score of picking candidate species uniformly at random."""
    cands = [t for t in table if table[t]["held"] is not None]
    out = {}
    for r in RANKS:
        p = [
            np.mean([table[c]["ranks"][r] == table[t]["ranks"][r] for c in cands]) for t in targets
        ]
        out[f"{r}_top1"] = float(np.mean(p))
        # P(at least one of k draws without replacement matches) for m matching candidates of n
        n = len(cands)
        topk = []
        for pt in p:
            m = round(pt * n)
            miss = np.prod([(n - m - i) / (n - i) for i in range(min(k, n))]) if m < n else 0.0
            topk.append(1 - miss)
        out[f"{r}_top{k}"] = float(np.mean(topk))
    return out


# --- k-mer composition --------------------------------------------------------------------------------------


def _canonical_index(k: int) -> np.ndarray:
    """Map each of the 4^k k-mer codes to the index of its canonical (strand-independent) form."""
    codes = np.arange(4**k)
    digits = (codes[:, None] >> (2 * np.arange(k)[::-1])) & 3
    rc = ((3 - digits[:, ::-1]) << (2 * np.arange(k)[::-1])).sum(1)
    canon = np.minimum(codes, rc)
    _, idx = np.unique(canon, return_inverse=True)
    return idx


def kmer_profile(seqs: list[str], k: int = 6) -> np.ndarray:
    """Canonical k-mer frequency vector of a set of DNA strings (k-mers containing N are skipped)."""
    lut = np.full(256, -1, dtype=np.int64)
    for i, b in enumerate(b"ACGT"):
        lut[b] = i
    canon = _canonical_index(k)
    counts = np.zeros(canon.max() + 1)
    for s in seqs:
        x = lut[np.frombuffer(s.encode(), dtype=np.uint8)]
        if len(x) < k:
            continue
        win = np.lib.stride_tricks.sliding_window_view(x, k)
        win = win[(win >= 0).all(1)]
        codes = (win << (2 * np.arange(k)[::-1])).sum(1)
        counts += np.bincount(canon[codes], minlength=len(counts))
    return counts / max(counts.sum(), 1)


def kmer_nn_queries(table: dict, targets: list[str], n_windows: int = 64, k: int = 6) -> dict:
    """For each target species, the training-photo centroid of its nearest training species by k-mer profile
    (z-scored frequencies, cosine similarity)."""
    from .genomes import PackedGenome

    train = [t for t in table if not table[t]["unseen"] and table[t]["train"] is not None]
    taxa = sorted(set(train) | set(targets))
    prof = np.stack(
        [kmer_profile(PackedGenome(table[t]["rec"]["genome"]).windows(n_windows), k) for t in taxa]
    )
    z = (prof - prof.mean(0)) / (prof.std(0) + 1e-12)
    z = torch.from_numpy(z).float()
    z = F.normalize(z, dim=-1)
    pos = {t: i for i, t in enumerate(taxa)}
    tr = torch.tensor([pos[t] for t in train])
    out, nearest = {}, {}
    for t in targets:
        sims = z[tr] @ z[pos[t]]
        if t in train:  # never answer with the species itself
            sims[train.index(t)] = -2
        best = train[int(sims.argmax())]
        nearest[t] = best
        out[t] = table[best]["train"]
    return out, nearest


# --- model-based queries ------------------------------------------------------------------------------------


def caption_queries(table: dict, targets: list[str], clip, rank: str) -> dict:
    """BioCLIP text embedding of each target's taxonomy caption, truncated after ``rank``."""
    keep = {"species": 4, "genus": 3, "family": 2, "order": 1, "class": 0}[
        rank
    ]  # of order/family/genus/species
    texts = {}
    for t in targets:
        r = dict(table[t]["rec"])
        for i, f in enumerate(("order", "family", "genus", "species")):
            if i >= keep:
                r[f] = ""
        r["subfamily"] = ""
        texts[t] = taxonomy_text(r)
    emb = clip.encode_texts(list(texts.values()))
    return dict(zip(texts, emb))


def frozen_linear_queries(
    table: dict, targets: list[str], device: str, alpha: float = 1.0, shuffle_tokens: bool = False
) -> dict:
    """Ridge regression from frozen ModernGENA genome embeddings (mean-pooled hidden states over fixed windows)
    to training species' photo centroids; predictions for the targets."""
    from transformers import AutoModel

    from .encoders import MODERNGENA, HFTokenizer
    from .genomes import PackedGenome

    tok = HFTokenizer(MODERNGENA, shuffle=shuffle_tokens)
    lm = AutoModel.from_pretrained(MODERNGENA).to(device).eval()

    @torch.no_grad()
    def embed(t):
        ids, mask = tok(PackedGenome(table[t]["rec"]["genome"]).windows())
        h = lm(input_ids=ids.to(device), attention_mask=mask.to(device)).last_hidden_state
        m = mask.to(device).unsqueeze(-1).float()
        return ((h * m).sum(1) / m.sum(1)).mean(0).cpu()

    train = [t for t in table if not table[t]["unseen"] and table[t]["train"] is not None]
    x_tr = torch.stack([embed(t) for t in train])
    mu, sd = x_tr.mean(0), x_tr.std(0) + 1e-6
    x = (x_tr - mu) / sd
    y = torch.stack([table[t]["train"] for t in train])
    w = torch.linalg.solve(x.T @ x + alpha * len(train) * torch.eye(x.shape[1]), x.T @ y)
    return {t: (embed(t) - mu) / sd @ w for t in targets}


def model_queries(table: dict, targets: list[str], align, prior=None, n_samples: int = 20, seed=0):
    """Aligned genome embeddings and, if a prior is given, ``n_samples`` prior samples per target."""
    from .genomes import PackedGenome, embed_genome

    aligned = {t: embed_genome(align.dna, PackedGenome(table[t]["rec"]["genome"])) for t in targets}
    sampled = {}
    if prior is not None:
        dev = next(prior.parameters()).device
        g = torch.Generator(device=dev).manual_seed(seed)
        for t in targets:
            cond = aligned[t].to(dev)[None].repeat(n_samples, 1)
            sampled[t] = prior.sample(cond, generator=g).cpu()
    return aligned, sampled


def held_out_groups(
    table: dict, unseen_tests: dict[str, str] | None = None
) -> dict[str, list[str]]:
    """Target species per test: each ``val_unseen`` species under its test name (default ``unseen``), plus a
    sample of training species (``seen``, scored on their held-out photos)."""
    groups: dict[str, list[str]] = {}
    for t, sp in table.items():
        if sp["unseen"]:
            groups.setdefault((unseen_tests or {}).get(t, "unseen"), []).append(t)
    seen = sorted(t for t, sp in table.items() if not sp["unseen"] and sp["held"] is not None)
    groups["seen"] = random.Random(0).sample(seen, min(60, len(seen)))
    return groups
