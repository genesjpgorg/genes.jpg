"""End-to-end inference: DNA barcode -> (aligned DNA embedding) -> prior -> decoder -> image."""

from __future__ import annotations

from pathlib import Path

import torch

from .align import AlignModel
from .decoder import SD_MODEL, EmbeddingDecoder
from .encoders import DNAEncoder
from .prior import DiffusionPrior


def save_align(model: AlignModel, path: str | Path) -> None:
    torch.save({"encoder": model.dna.name, "state_dict": model.state_dict()}, path)


def load_align(path: str | Path, dna_encoder: DNAEncoder | None = None) -> AlignModel:
    state = torch.load(path, map_location="cpu")
    model = AlignModel(dna_encoder or DNAEncoder(state.get("encoder", "barcodebert")))
    sd = {k.replace("dna.bert.", "dna.backbone.", 1): v for k, v in state["state_dict"].items()}
    model.load_state_dict(sd)
    return model.eval()


def save_prior(prior: DiffusionPrior, path: str | Path) -> None:
    cfg = {"dim": prior.dim, "cond_dim": prior.null_cond.numel(), "width": prior.width, "depth": len(prior.blocks)}
    torch.save({"config": cfg, "state_dict": prior.state_dict()}, path)


def load_prior(path: str | Path) -> DiffusionPrior:
    state = torch.load(path, map_location="cpu")
    prior = DiffusionPrior(**state["config"])
    prior.load_state_dict(state["state_dict"])
    return prior.eval()


class GenomeToImage:
    def __init__(
        self,
        align: AlignModel,
        prior: DiffusionPrior,
        decoder: EmbeddingDecoder | None = None,
        gallery: tuple[torch.Tensor, list[dict]] | None = None,
    ):
        self.align, self.prior, self.decoder, self.gallery = align.eval(), prior.eval(), decoder, gallery

    @classmethod
    def from_checkpoints(
        cls, ckpt_dir: str | Path, gallery=None, model_id: str = SD_MODEL, device: str = "cpu"
    ) -> GenomeToImage:
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
        return self.align.dna.encode(seqs).to(self.device)

    def image_embeddings(self, seq: str, n: int = 1, steps: int = 50, guidance: float = 2.0, seed: int = 0):
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

    def generate(self, seq: str, n: int = 1, seed: int = 0, steps: int = 30, guidance: float = 5.0, size=None):
        if self.decoder is None:
            raise ValueError("no decoder checkpoint loaded")
        emb = self.image_embeddings(seq, n=n, seed=seed)
        g = torch.Generator(device=self.device).manual_seed(seed)
        return self.decoder.generate(emb, steps=steps, guidance=guidance, size=size, generator=g)
