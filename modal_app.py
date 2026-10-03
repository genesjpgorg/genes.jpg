"""Modal entrypoints for the genes.jpg DNA -> image pipeline.

Auth: MODAL_TOKEN_ID / MODAL_TOKEN_SECRET env vars (or `modal token new`).
Run:  modal run modal_app.py                                  # CPU: download + embed + align + prior
      modal run modal_app.py::train_decoder --max-steps 2000  # GPU (needs a payment method on Modal)
      modal run modal_app.py::generate --processid <BOLD id>  # GPU
      modal run modal_app.py::gpu_check                       # GPU smoke test
"""

import modal

app = modal.App("genes-jpg")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "torch",
        "torchvision",
        "transformers>=4.48,<5",
        "open_clip_torch",
        "diffusers",
        "accelerate",
        "safetensors",
        "pillow",
        "requests",
        "remotezip",
    )
    .add_local_python_source("genesjpg")
)

volume = modal.Volume.from_name("genes-jpg-data", create_if_missing=True)
VOLUME_DIR = "/data"
DATA_DIR = f"{VOLUME_DIR}/bioscan"
CPU = {"image": image, "volumes": {VOLUME_DIR: volume}, "cpu": 8.0, "memory": 16384}


def _cli(*args: str) -> None:
    from genesjpg.cli import main

    volume.reload()
    main(["--data", DATA_DIR, *args])
    volume.commit()


@app.function(**CPU, timeout=4 * 60 * 60)
def prepare(n_train: int = 20000, n_eval: int = 3000) -> None:
    """Download a paired BIOSCAN-5M subset and compute frozen BioCLIP embeddings."""
    _cli("download", "--n-train", str(n_train), "--n-eval", str(n_eval))
    _cli("embed")


@app.function(**CPU, timeout=4 * 60 * 60)
def train_cpu(
    align_epochs: int = 5, prior_epochs: int = 50, encoder: str = "moderngena", freeze_layers: int = 0
) -> None:
    """Steps 1-3: DNA encoder alignment and the diffusion prior (CPU is enough)."""
    _cli("train-align", "--epochs", str(align_epochs), "--encoder", encoder, "--freeze-layers", str(freeze_layers))
    _cli("train-prior", "--epochs", str(prior_epochs))


@app.function(image=image, volumes={VOLUME_DIR: volume}, gpu="A10G", timeout=8 * 60 * 60)
def train_decoder(max_steps: int = 2000, batch_size: int = 8, size: int = 512) -> None:
    """Step 4: Stable Diffusion decoder conditioned on BioCLIP image embeddings."""
    _cli("train-decoder", "--max-steps", str(max_steps), "--batch-size", str(batch_size), "--size", str(size))


@app.function(image=image, volumes={VOLUME_DIR: volume}, gpu="T4", timeout=30 * 60)
def generate(processid: str = "", dna: str = "", n: int = 4) -> None:
    target = ["--processid", processid] if processid else ["--dna", dna]
    _cli("generate", *target, "--n", str(n), "--out", f"{DATA_DIR}/samples/{processid or 'dna'}")


@app.function(image=image, gpu="T4", timeout=600)
def gpu_check() -> str:
    import torch

    if not torch.cuda.is_available():
        return "CUDA not available"
    return f"torch {torch.__version__}, GPU: {torch.cuda.get_device_name(0)}"


@app.local_entrypoint()
def main(n_train: int = 20000, n_eval: int = 3000, align_epochs: int = 5, prior_epochs: int = 50) -> None:
    prepare.remote(n_train, n_eval)
    train_cpu.remote(align_epochs, prior_epochs)
