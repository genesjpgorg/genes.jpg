"""Step 1 (ModernGENA DNA encoder) and the frozen BioCLIP image/text towers.

ModernGENA (AIRI-Institute/moderngena-base) is a 22-layer ModernBERT DNA language model (135M parameters) with
GENA-LM's 32k BPE vocabulary; a ~650 bp COI barcode becomes ~107 tokens. BioCLIP (imageomics/bioclip) is a CLIP
model trained on TreeOfLife-10M whose image and text embeddings define the 512-d target space.
"""

from __future__ import annotations

from pathlib import Path

import torch
import torch.nn.functional as F
from torch import nn

MODERNGENA = "AIRI-Institute/moderngena-base"
BIOCLIP = "hf-hub:imageomics/bioclip"


class HFTokenizer:
    """Wraps a Hugging Face tokenizer (e.g. GENA-LM's 32k DNA BPE) to return (input_ids, attention_mask)."""

    def __init__(self, model_id: str, max_len: int = 1024, shuffle: bool = False):
        from transformers import AutoTokenizer

        self.tok = AutoTokenizer.from_pretrained(model_id)
        self.max_len = max_len
        self.shuffle = shuffle  # control experiment: destroy token order, keep token composition

    def __call__(self, seqs: list[str]) -> tuple[torch.Tensor, torch.Tensor]:
        """Tokenise DNA strings (upper-cased, [CLS] ... [SEP], padded to the longest, truncated to max_len)."""
        enc = self.tok(
            [s.strip().upper() for s in seqs],
            padding=True,
            truncation=True,
            max_length=self.max_len,
            return_tensors="pt",
        )
        ids, mask = enc["input_ids"], enc["attention_mask"]
        if self.shuffle:
            ids = shuffle_tokens(ids, mask, seqs)
        return ids, mask


def shuffle_tokens(ids: torch.Tensor, mask: torch.Tensor, seqs: list[str]) -> torch.Tensor:
    """Randomly permute each sequence's tokens between [CLS] and [SEP] (padding untouched).

    The permutation is seeded by the sequence itself, so a given window always gets the same shuffle: training
    windows are random anyway, and the fixed evaluation windows of a genome keep a stable embedding."""
    import zlib

    ids = ids.clone()
    for i, s in enumerate(seqs):
        n = int(mask[i].sum())
        g = torch.Generator().manual_seed(zlib.crc32(s.encode()))
        perm = torch.randperm(n - 2, generator=g) + 1
        ids[i, 1 : n - 1] = ids[i, perm]
    return ids


class DNAEncoder(nn.Module):
    """ModernGENA + masked mean pooling + MLP head, L2-normalised into BioCLIP's 512-d space.

    Args:
        backbone: a ModernBERT-style model; defaults to pretrained ModernGENA.
        tokenizer: callable returning (input_ids, attention_mask); defaults to ModernGENA's tokenizer.
        embed_dim: output size (BioCLIP's embedding size).
        freeze_layers: freeze the token embeddings and the first N transformer layers (cheaper CPU training).
        shuffle_tokens: control experiment; shuffle token order inside every window (see ``shuffle_tokens``).
    """

    def __init__(
        self,
        backbone: nn.Module | None = None,
        tokenizer: HFTokenizer | None = None,
        embed_dim: int = 512,
        freeze_layers: int = 0,
        shuffle_tokens: bool = False,
    ):
        super().__init__()
        if backbone is None:
            from transformers import AutoModel

            backbone = AutoModel.from_pretrained(MODERNGENA)
        self.backbone = backbone
        self.tokenizer = tokenizer or HFTokenizer(MODERNGENA, shuffle=shuffle_tokens)
        hidden = backbone.config.hidden_size
        self.head = nn.Sequential(
            nn.Linear(hidden, hidden), nn.GELU(), nn.Linear(hidden, embed_dim)
        )
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
        """Token ids (B, L) -> unit-norm embeddings (B, embed_dim)."""
        h = self.backbone(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        m = attention_mask.unsqueeze(-1).to(h.dtype)
        pooled = (h * m).sum(1) / m.sum(1).clamp(min=1)
        return F.normalize(self.head(pooled), dim=-1)

    @torch.no_grad()
    def encode(self, seqs: list[str], batch_size: int = 256) -> torch.Tensor:
        """Embed raw DNA strings in eval mode; returns a CPU tensor (len(seqs), embed_dim)."""
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
        """PIL images -> unit-norm image embeddings (N, 512) on CPU."""
        x = torch.stack([self.preprocess(im) for im in images]).to(self.device)
        return F.normalize(self.model.encode_image(x).float(), dim=-1).cpu()

    @torch.no_grad()
    def encode_texts(self, texts: list[str]) -> torch.Tensor:
        """Captions -> unit-norm text embeddings (N, 512) on CPU."""
        return F.normalize(
            self.model.encode_text(self.tokenizer(texts).to(self.device)).float(), dim=-1
        ).cpu()

    def embed_files(self, paths: list[str | Path], batch_size: int = 64) -> torch.Tensor:
        """Embed image files in batches, printing progress."""
        from PIL import Image

        out = []
        for i in range(0, len(paths), batch_size):
            ims = [Image.open(p).convert("RGB") for p in paths[i : i + batch_size]]
            out.append(self.encode_images(ims))
            if (i // batch_size) % 20 == 0:
                print(f"  embedded {i + len(ims)}/{len(paths)} images", flush=True)
        return torch.cat(out)
