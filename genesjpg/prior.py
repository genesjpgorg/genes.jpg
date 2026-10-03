"""Step 3: diffusion prior p(image embedding | DNA embedding), as in unCLIP / DALL-E 2.

A barcode is compatible with many photos (pose, sex, life stage), so instead of regressing
one mean embedding the prior samples a plausible BioCLIP image embedding.
"""

from __future__ import annotations

import math

import torch
import torch.nn.functional as F
from torch import nn


def cosine_alphas_cumprod(timesteps: int, s: float = 0.008) -> torch.Tensor:
    """Cumulative signal fraction alpha_bar_t of the cosine noise schedule (Nichol & Dhariwal 2021)."""
    t = torch.linspace(0, timesteps, timesteps + 1, dtype=torch.float64) / timesteps
    f = torch.cos((t + s) / (1 + s) * math.pi / 2) ** 2
    betas = (1 - f[1:] / f[:-1]).clamp(max=0.999)
    return torch.cumprod(1 - betas, 0).float()


def timestep_embedding(t: torch.Tensor, dim: int) -> torch.Tensor:
    """Sinusoidal embedding of integer diffusion timesteps, shape (B, dim)."""
    half = dim // 2
    freqs = torch.exp(-math.log(10000) * torch.arange(half, device=t.device) / half)
    args = t.float()[:, None] * freqs[None]
    return torch.cat([args.sin(), args.cos()], dim=-1)


class ResBlock(nn.Module):
    """Pre-norm residual MLP block; the timestep embedding is added before the MLP."""

    def __init__(self, width: int):
        super().__init__()
        self.norm = nn.LayerNorm(width)
        self.mlp = nn.Sequential(nn.Linear(width, width * 2), nn.SiLU(), nn.Linear(width * 2, width))

    def forward(self, h, temb):
        return h + self.mlp(self.norm(h) + temb)


class DiffusionPrior(nn.Module):
    """MLP denoiser over 512-d BioCLIP image embeddings, conditioned on the aligned DNA embedding.

    Training: x0-prediction MSE on embeddings scaled to per-dimension variance ~1; the condition is replaced by a
    learned null vector with probability ``cond_drop`` to enable classifier-free guidance.
    Sampling: DDIM (eta=0) with guidance; outputs are re-normalised to the unit sphere.
    """

    def __init__(
        self,
        dim: int = 512,
        cond_dim: int = 512,
        width: int = 1024,
        depth: int = 4,
        timesteps: int = 1000,
        cond_drop: float = 0.1,
    ):
        super().__init__()
        self.dim, self.width, self.timesteps, self.cond_drop = dim, width, timesteps, cond_drop
        self.scale = dim**0.5  # unit vectors -> per-dimension variance ~1, matching the noise
        self.null_cond = nn.Parameter(torch.zeros(cond_dim))
        self.time_mlp = nn.Sequential(nn.Linear(width, width), nn.SiLU(), nn.Linear(width, width))
        self.inp = nn.Linear(dim + cond_dim, width)
        self.blocks = nn.ModuleList(ResBlock(width) for _ in range(depth))
        self.out = nn.Sequential(nn.LayerNorm(width), nn.Linear(width, dim))
        self.register_buffer("alphas_cumprod", cosine_alphas_cumprod(timesteps))

    def denoise(self, x_t: torch.Tensor, t: torch.Tensor, cond: torch.Tensor) -> torch.Tensor:
        """Predict the clean (scaled) embedding x0 from noisy ``x_t`` at timesteps ``t`` given ``cond``."""
        temb = self.time_mlp(timestep_embedding(t, self.width))
        h = self.inp(torch.cat([x_t, cond], dim=-1))
        for block in self.blocks:
            h = block(h, temb)
        return self.out(h)

    def loss(self, target: torch.Tensor, cond: torch.Tensor) -> torch.Tensor:
        """Denoising loss for a batch of (image embedding, DNA embedding) pairs."""
        x0 = F.normalize(target, dim=-1) * self.scale
        t = torch.randint(0, self.timesteps, (len(x0),), device=x0.device)
        ab = self.alphas_cumprod[t][:, None]
        x_t = ab.sqrt() * x0 + (1 - ab).sqrt() * torch.randn_like(x0)
        drop = torch.rand(len(cond), device=cond.device) < self.cond_drop
        cond = torch.where(drop[:, None], self.null_cond.expand_as(cond), cond)
        return F.mse_loss(self.denoise(x_t, t, cond), x0)

    @torch.no_grad()
    def sample(
        self, cond: torch.Tensor, steps: int = 50, guidance: float = 2.0, generator: torch.Generator | None = None
    ) -> torch.Tensor:
        """DDIM (eta=0) sampling with classifier-free guidance; returns unit-norm embeddings."""
        x = torch.randn(len(cond), self.dim, generator=generator, device=cond.device)
        null = self.null_cond.expand_as(cond)
        ts = torch.linspace(self.timesteps - 1, 0, steps, device=cond.device).long()
        for i, t in enumerate(ts):
            tb = t.repeat(len(cond))
            x0 = self.denoise(x, tb, cond)
            if guidance != 1.0:
                x0_u = self.denoise(x, tb, null)
                x0 = x0_u + guidance * (x0 - x0_u)
            ab = self.alphas_cumprod[t]
            eps = (x - ab.sqrt() * x0) / (1 - ab).sqrt()
            ab_prev = self.alphas_cumprod[ts[i + 1]] if i + 1 < len(ts) else torch.tensor(1.0, device=x.device)
            x = ab_prev.sqrt() * x0 + (1 - ab_prev).sqrt() * eps
        return F.normalize(x, dim=-1)


def train_prior(
    prior: DiffusionPrior,
    cond: torch.Tensor,
    target: torch.Tensor,
    epochs: int = 50,
    batch_size: int = 256,
    lr: float = 3e-4,
    eval_fn=None,
    eval_every: int = 10,
    device: str = "cpu",
) -> DiffusionPrior:
    """Train the prior on (DNA embedding ``cond``, BioCLIP image embedding ``target``) pairs with AdamW and a
    cosine LR schedule. ``eval_fn(prior) -> dict`` runs every ``eval_every`` epochs and at the end."""
    prior.to(device)
    opt = torch.optim.AdamW(prior.parameters(), lr=lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    for epoch in range(epochs):
        prior.train()
        perm = torch.randperm(len(cond))
        total = 0.0
        for i in range(0, len(perm), batch_size):
            b = perm[i : i + batch_size]
            loss = prior.loss(target[b].to(device), cond[b].to(device))
            opt.zero_grad()
            loss.backward()
            opt.step()
            total += loss.item() * len(b)
        sched.step()
        msg = f"prior epoch {epoch + 1}/{epochs} loss {total / len(perm):.4f}"
        if eval_fn and ((epoch + 1) % eval_every == 0 or epoch + 1 == epochs):
            prior.eval()
            msg += " " + " ".join(f"{k}={v:.3f}" for k, v in eval_fn(prior).items())
        print(msg, flush=True)
    return prior
