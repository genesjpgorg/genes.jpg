"""Step 1 (DNA encoder) and the frozen BioCLIP image/text towers."""

from __future__ import annotations

from itertools import product
from pathlib import Path

import torch
import torch.nn.functional as F
from torch import nn

BARCODEBERT = "bioscan-ml/BarcodeBERT"
BIOCLIP = "hf-hub:imageomics/bioclip"


class KmerTokenizer:
    """Non-overlapping k-mer tokenizer using BarcodeBERT's vocabulary.

    ids: 0 = [MASK], 1 = [UNK] (also used for padding, masked out), then all ACGT k-mers.
    """

    def __init__(self, k: int = 4, stride: int = 4, max_len: int = 660):
        self.k, self.stride, self.max_len = k, stride, max_len
        self.vocab = {"".join(p): i + 2 for i, p in enumerate(product("ACGT", repeat=k))}
        self.unk_id = 1

    def encode(self, seq: str) -> list[int]:
        seq = seq.strip().upper()[: self.max_len]
        return [self.vocab.get(seq[i : i + self.k], self.unk_id) for i in range(0, len(seq) - self.k + 1, self.stride)]

    def __call__(self, seqs: list[str]) -> tuple[torch.Tensor, torch.Tensor]:
        ids = [self.encode(s) or [self.unk_id] for s in seqs]
        length = max(len(i) for i in ids)
        input_ids = torch.full((len(ids), length), self.unk_id, dtype=torch.long)
        mask = torch.zeros((len(ids), length), dtype=torch.long)
        for row, seq_ids in enumerate(ids):
            input_ids[row, : len(seq_ids)] = torch.tensor(seq_ids)
            mask[row, : len(seq_ids)] = 1
        return input_ids, mask


class DNAEncoder(nn.Module):
    """BarcodeBERT + mean pooling + MLP head, projected onto the unit sphere of BioCLIP's space."""

    def __init__(self, bert: nn.Module | None = None, embed_dim: int = 512, freeze_layers: int = 0):
        super().__init__()
        if bert is None:
            from transformers import BertModel

            bert = BertModel.from_pretrained(BARCODEBERT, add_pooling_layer=False)
        self.bert = bert
        hidden = bert.config.hidden_size
        self.head = nn.Sequential(nn.Linear(hidden, hidden), nn.GELU(), nn.Linear(hidden, embed_dim))
        self.tokenizer = KmerTokenizer()
        if freeze_layers:
            self.bert.embeddings.requires_grad_(False)
            for layer in self.bert.encoder.layer[:freeze_layers]:
                layer.requires_grad_(False)

    @classmethod
    def tiny(cls, embed_dim: int = 512) -> DNAEncoder:
        """Randomly initialised small BERT, for tests."""
        from transformers import BertConfig, BertModel

        cfg = BertConfig(
            vocab_size=258, hidden_size=32, num_hidden_layers=2, num_attention_heads=2, intermediate_size=64
        )
        return cls(BertModel(cfg, add_pooling_layer=False), embed_dim=embed_dim)

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        h = self.bert(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
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
