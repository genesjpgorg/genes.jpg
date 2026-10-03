"""Step 1 (ModernGENA DNA encoder) and the frozen BioCLIP image/text towers."""

from __future__ import annotations

from pathlib import Path

import torch
import torch.nn.functional as F
from torch import nn

MODERNGENA = "AIRI-Institute/moderngena-base"
BIOCLIP = "hf-hub:imageomics/bioclip"


class HFTokenizer:
    """Wraps a Hugging Face tokenizer (e.g. GENA-LM's 32k DNA BPE) to return (input_ids, attention_mask)."""

    def __init__(self, model_id: str, max_len: int = 1024):
        from transformers import AutoTokenizer

        self.tok = AutoTokenizer.from_pretrained(model_id)
        self.max_len = max_len

    def __call__(self, seqs: list[str]) -> tuple[torch.Tensor, torch.Tensor]:
        enc = self.tok(
            [s.strip().upper() for s in seqs],
            padding=True,
            truncation=True,
            max_length=self.max_len,
            return_tensors="pt",
        )
        return enc["input_ids"], enc["attention_mask"]


class DNAEncoder(nn.Module):
    """ModernGENA + mean pooling + MLP head onto BioCLIP's unit sphere."""

    def __init__(
        self,
        backbone: nn.Module | None = None,
        tokenizer: HFTokenizer | None = None,
        embed_dim: int = 512,
        freeze_layers: int = 0,
    ):
        super().__init__()
        if backbone is None:
            from transformers import AutoModel

            backbone = AutoModel.from_pretrained(MODERNGENA)
        self.backbone = backbone
        self.tokenizer = tokenizer or HFTokenizer(MODERNGENA)
        hidden = backbone.config.hidden_size
        self.head = nn.Sequential(nn.Linear(hidden, hidden), nn.GELU(), nn.Linear(hidden, embed_dim))
        if freeze_layers:
            self.backbone.embeddings.requires_grad_(False)
            for layer in self.backbone.layers[:freeze_layers]:
                layer.requires_grad_(False)

    @classmethod
    def tiny(cls, embed_dim: int = 512) -> DNAEncoder:
        """Randomly initialised small ModernBERT with ModernGENA's tokenizer, for tests."""
        from transformers import ModernBertConfig, ModernBertModel

        cfg = ModernBertConfig(
            vocab_size=32768,
            hidden_size=32,
            intermediate_size=64,
            num_hidden_layers=2,
            num_attention_heads=2,
            pad_token_id=3,
            cls_token_id=1,
            sep_token_id=2,
        )
        return cls(ModernBertModel(cfg), embed_dim=embed_dim)

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        h = self.backbone(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        m = attention_mask.unsqueeze(-1).to(h.dtype)
        pooled = (h * m).sum(1) / m.sum(1).clamp(min=1)
        return F.normalize(self.head(pooled), dim=-1)

    @torch.no_grad()
    def encode(self, seqs: list[str], batch_size: int = 256) -> torch.Tensor:
        self.eval()
        device = next(self.parameters()).device
        out = []
        for i in range(0, len(seqs), batch_size):
            ids, mask = self.tokenizer(seqs[i : i + batch_size])
            out.append(self(ids.to(device), mask.to(device)).cpu())
        return torch.cat(out)


class BioCLIP:
    """Frozen BioCLIP image and text encoders (512-d, L2-normalised)."""

    def __init__(self, name: str = BIOCLIP, device: str = "cpu"):
        import open_clip

        self.model, _, self.preprocess = open_clip.create_model_and_transforms(name, device=device)
        self.model.eval().requires_grad_(False)
        self.tokenizer = open_clip.get_tokenizer(name)
        self.device = device

    @torch.no_grad()
    def encode_images(self, images) -> torch.Tensor:
        x = torch.stack([self.preprocess(im) for im in images]).to(self.device)
        return F.normalize(self.model.encode_image(x).float(), dim=-1).cpu()

    @torch.no_grad()
    def encode_texts(self, texts: list[str]) -> torch.Tensor:
        return F.normalize(self.model.encode_text(self.tokenizer(texts).to(self.device)).float(), dim=-1).cpu()

    def embed_files(self, paths: list[str | Path], batch_size: int = 64) -> torch.Tensor:
        from PIL import Image

        out = []
        for i in range(0, len(paths), batch_size):
            ims = [Image.open(p).convert("RGB") for p in paths[i : i + batch_size]]
            out.append(self.encode_images(ims))
            if (i // batch_size) % 20 == 0:
                print(f"  embedded {i + len(ims)}/{len(paths)} images", flush=True)
        return torch.cat(out)
