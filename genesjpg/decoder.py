"""Step 4: Stable Diffusion decoder conditioned on a BioCLIP image embedding.

The embedding is projected to a few cross-attention tokens (IP-Adapter-style image prompt)
that replace the text-encoder output. The decoder only needs images, so it can be trained
on any species photo collection, not just DNA-paired specimens.
"""

from __future__ import annotations

from pathlib import Path

import torch
import torch.nn.functional as F
from torch import nn

SD_MODEL = "stable-diffusion-v1-5/stable-diffusion-v1-5"


class EmbeddingProjector(nn.Module):
    """Maps one BioCLIP image embedding (B, emb_dim) to ``n_tokens`` UNet cross-attention tokens
    (B, n_tokens, cross_dim), standing in for CLIP text-encoder hidden states."""

    def __init__(self, emb_dim: int = 512, cross_dim: int = 768, n_tokens: int = 8):
        super().__init__()
        self.n_tokens, self.cross_dim = n_tokens, cross_dim
        self.proj = nn.Linear(emb_dim, n_tokens * cross_dim)
        self.norm = nn.LayerNorm(cross_dim)

    def forward(self, emb: torch.Tensor) -> torch.Tensor:
        return self.norm(self.proj(emb).view(-1, self.n_tokens, self.cross_dim))


class EmbeddingDecoder(nn.Module):
    """Stable Diffusion (VAE + UNet) driven by a BioCLIP image embedding instead of a text prompt.

    By default only the ``EmbeddingProjector`` is trained (VAE and UNet frozen); ``train_unet=True`` also
    fine-tunes the UNet. The text encoder and tokenizer of the SD checkpoint are never loaded.
    """

    def __init__(
        self,
        model_id: str = SD_MODEL,
        emb_dim: int = 512,
        n_tokens: int = 8,
        train_unet: bool = False,
    ):
        super().__init__()
        from diffusers import AutoencoderKL, DDIMScheduler, DDPMScheduler, UNet2DConditionModel

        self.vae = AutoencoderKL.from_pretrained(model_id, subfolder="vae").requires_grad_(False)
        self.unet = UNet2DConditionModel.from_pretrained(model_id, subfolder="unet").requires_grad_(train_unet)
        self.noise_scheduler = DDPMScheduler.from_pretrained(model_id, subfolder="scheduler")
        self.sample_scheduler = DDIMScheduler.from_pretrained(model_id, subfolder="scheduler")
        self.projector = EmbeddingProjector(emb_dim, self.unet.config.cross_attention_dim, n_tokens)
        self.vae_scale = 2 ** (len(self.vae.config.block_out_channels) - 1)
        self.train_unet = train_unet

    def trainable_parameters(self):
        """Parameters to optimise: the projector, plus the UNet if ``train_unet``."""
        return [p for p in self.parameters() if p.requires_grad]

    def loss(self, pixels: torch.Tensor, emb: torch.Tensor, drop_prob: float = 0.1) -> torch.Tensor:
        """Latent-diffusion noise-prediction loss.

        Args:
            pixels: images in [-1, 1], shape (B, 3, H, W).
            emb: their BioCLIP image embeddings, shape (B, emb_dim).
            drop_prob: probability of zeroing an embedding, which trains the unconditional branch used by
                classifier-free guidance at sampling time.
        """
        with torch.no_grad():
            latents = self.vae.encode(pixels).latent_dist.sample() * self.vae.config.scaling_factor
        noise = torch.randn_like(latents)
        t = torch.randint(0, self.noise_scheduler.config.num_train_timesteps, (len(latents),), device=latents.device)
        noisy = self.noise_scheduler.add_noise(latents, noise, t)
        drop = torch.rand(len(emb), device=emb.device) < drop_prob
        emb = torch.where(drop[:, None], torch.zeros_like(emb), emb)
        pred = self.unet(noisy, t, encoder_hidden_states=self.projector(emb)).sample
        if self.noise_scheduler.config.prediction_type == "v_prediction":
            target = self.noise_scheduler.get_velocity(latents, noise, t)
        else:
            target = noise
        return F.mse_loss(pred.float(), target.float())

    @torch.no_grad()
    def generate(
        self,
        emb: torch.Tensor,
        steps: int = 30,
        guidance: float = 5.0,
        size: int | None = None,
        generator: torch.Generator | None = None,
    ):
        """Sample one PIL image per embedding with DDIM and classifier-free guidance (zero embedding = uncond)."""
        from PIL import Image

        device = emb.device
        size = size or self.unet.config.sample_size * self.vae_scale
        self.sample_scheduler.set_timesteps(steps, device=device)
        context = torch.cat([self.projector(torch.zeros_like(emb)), self.projector(emb)])
        shape = (len(emb), self.unet.config.in_channels, size // self.vae_scale, size // self.vae_scale)
        latents = torch.randn(shape, generator=generator, device=device) * self.sample_scheduler.init_noise_sigma
        for t in self.sample_scheduler.timesteps:
            inp = self.sample_scheduler.scale_model_input(torch.cat([latents] * 2), t)
            uncond, cond = self.unet(inp, t, encoder_hidden_states=context).sample.chunk(2)
            latents = self.sample_scheduler.step(uncond + guidance * (cond - uncond), t, latents).prev_sample
        images = self.vae.decode(latents / self.vae.config.scaling_factor).sample
        images = ((images.clamp(-1, 1) + 1) * 127.5).round().byte().permute(0, 2, 3, 1).cpu().numpy()
        return [Image.fromarray(im) for im in images]

    def save(self, path: str | Path) -> None:
        """Save trained weights only (projector, plus UNet if it was fine-tuned)."""
        state = {"projector": self.projector.state_dict()}
        if self.train_unet:
            state["unet"] = self.unet.state_dict()
        torch.save(state, path)

    def load(self, path: str | Path) -> None:
        """Load weights written by ``save`` on top of the pretrained SD checkpoint."""
        state = torch.load(path, map_location="cpu")
        self.projector.load_state_dict(state["projector"])
        if "unet" in state:
            self.unet.load_state_dict(state["unet"])


def image_transform(size: int):
    """Resize + centre-crop to ``size`` and scale to [-1, 1], as Stable Diffusion's VAE expects."""
    from torchvision import transforms

    return transforms.Compose(
        [
            transforms.Resize(size),
            transforms.CenterCrop(size),
            transforms.ToTensor(),
            transforms.Normalize([0.5], [0.5]),
        ]
    )


def train_decoder(
    decoder: EmbeddingDecoder,
    image_paths: list[str | Path],
    emb: torch.Tensor,
    epochs: int = 1,
    batch_size: int = 8,
    lr: float = 1e-4,
    size: int = 512,
    max_steps: int | None = None,
    device: str = "cpu",
) -> EmbeddingDecoder:
    """Train the decoder on images and their BioCLIP embeddings (row i of ``emb`` belongs to
    ``image_paths[i]``). Stops after ``epochs`` or ``max_steps`` optimiser steps, whichever comes first."""
    from PIL import Image

    decoder.to(device).train()
    tf = image_transform(size)
    opt = torch.optim.AdamW(decoder.trainable_parameters(), lr=lr)
    step = 0
    for epoch in range(epochs):
        perm = torch.randperm(len(image_paths))
        for i in range(0, len(perm), batch_size):
            b = perm[i : i + batch_size].tolist()
            pixels = torch.stack([tf(Image.open(image_paths[j]).convert("RGB")) for j in b]).to(device)
            loss = decoder.loss(pixels, emb[b].to(device))
            opt.zero_grad()
            loss.backward()
            opt.step()
            step += 1
            if step % 50 == 0 or step == 1:
                print(f"decoder epoch {epoch + 1} step {step} loss {loss.item():.4f}", flush=True)
            if max_steps and step >= max_steps:
                return decoder
    return decoder
