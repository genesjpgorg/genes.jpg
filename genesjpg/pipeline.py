"""End-to-end inference: DNA barcode -> aligned DNA embedding -> prior -> decoder -> image.

Also holds checkpoint save/load helpers for the alignment model and the prior.
"""

from __future__ import annotations

from pathlib import Path

import torch

from .align import AlignModel
from .decoder import SD_MODEL, EmbeddingDecoder
from .encoders import DNAEncoder
from .prior import DiffusionPrior


def save_align(model: AlignModel, path: str | Path) -> None:
    """Save the full alignment model (fine-tuned ModernGENA + head + temperature)."""
    torch.save({"state_dict": model.state_dict()}, path)


def load_align(path: str | Path, dna_encoder: DNAEncoder | None = None) -> AlignModel:
    """Load an alignment checkpoint into ``dna_encoder`` (default: a fresh ModernGENA ``DNAEncoder``)."""
    model = AlignModel(dna_encoder or DNAEncoder())
    model.load_state_dict(torch.load(path, map_location="cpu")["state_dict"])
    return model.eval()


def save_prior(prior: DiffusionPrior, path: str | Path) -> None:
    """Save the prior with the config needed to rebuild it."""
    cfg = {
        "dim": prior.dim,
        "cond_dim": prior.null_cond.numel(),
        "width": prior.width,
        "depth": len(prior.blocks),
        "timesteps": prior.timesteps,
        "cond_drop": prior.cond_drop,
    }
    torch.save({"config": cfg, "state_dict": prior.state_dict()}, path)


def load_prior(path: str | Path) -> DiffusionPrior:
    """Rebuild and load a prior saved by ``save_prior``."""
    state = torch.load(path, map_location="cpu")
    prior = DiffusionPrior(**state["config"])
    prior.load_state_dict(state["state_dict"])
    return prior.eval()


class GenomeToImage:
    """The full DNA -> image model.

    Example:
        model = GenomeToImage.from_checkpoints("data/checkpoints", gallery=(image_emb, records))
        model.retrieve(barcode, k=5)      # nearest real photos (works without a decoder)
        images = model.generate(barcode, n=4)  # needs decoder.pt

    Args:
        align: trained ``AlignModel`` (steps 1-2).
        prior: trained ``DiffusionPrior`` (step 3).
        decoder: trained ``EmbeddingDecoder`` (step 4), or None for retrieval only.
        gallery: (BioCLIP image embeddings (N, 512), matching records) used by ``retrieve``.
    """

    def __init__(
        self,
        align: AlignModel,
        prior: DiffusionPrior,
        decoder: EmbeddingDecoder | None = None,
        gallery: tuple[torch.Tensor, list[dict]] | None = None,
    ):
        self.align, self.prior, self.decoder, self.gallery = (
            align.eval(),
            prior.eval(),
            decoder,
            gallery,
        )

    @classmethod
    def from_checkpoints(
        cls, ckpt_dir: str | Path, gallery=None, model_id: str = SD_MODEL, device: str = "cpu"
    ) -> GenomeToImage:
        """Load ``align.pt``, ``prior.pt`` and, if present, ``decoder.pt`` from ``ckpt_dir``."""
        ckpt = Path(ckpt_dir)
        decoder = None
        if (ckpt / "decoder.pt").exists():
            decoder = EmbeddingDecoder(model_id)
            decoder.load(ckpt / "decoder.pt")
            decoder.to(device).eval()
        align = load_align(ckpt / "align.pt").to(device)
        return cls(align, load_prior(ckpt / "prior.pt").to(device), decoder, gallery)

    @property
    def device(self):
        return next(self.prior.parameters()).device

    def embed_dna(self, seqs: list[str]) -> torch.Tensor:
        """Aligned, unit-norm DNA embeddings (len(seqs), 512)."""
        return self.align.dna.encode(seqs).to(self.device)

    def image_embeddings(
        self, seq: str, n: int = 1, steps: int = 50, guidance: float = 2.0, seed: int = 0
    ):
        """Sample ``n`` plausible BioCLIP image embeddings for one barcode with the prior."""
        g = torch.Generator(device=self.device).manual_seed(seed)
        cond = self.embed_dna([seq]).repeat(n, 1)
        return self.prior.sample(cond, steps=steps, guidance=guidance, generator=g)

    def retrieve(self, seq: str, k: int = 5) -> list[tuple[dict, float]]:
        """Nearest real gallery images to the DNA embedding (no generation needed)."""
        if self.gallery is None:
            raise ValueError("no gallery loaded")
        emb, records = self.gallery
        sims = (self.embed_dna([seq]).cpu() @ emb.T)[0]
        top = sims.topk(min(k, len(records)))
        return [(records[i], s) for s, i in zip(top.values.tolist(), top.indices.tolist())]

    def generate(
        self, seq: str, n: int = 1, seed: int = 0, steps: int = 30, guidance: float = 5.0, size=None
    ):
        """Generate ``n`` images for one barcode: prior samples ``n`` image embeddings, decoder renders each."""
        if self.decoder is None:
            raise ValueError("no decoder checkpoint loaded")
        emb = self.image_embeddings(seq, n=n, seed=seed)
        g = torch.Generator(device=self.device).manual_seed(seed)
        return self.decoder.generate(emb, steps=steps, guidance=guidance, size=size, generator=g)
