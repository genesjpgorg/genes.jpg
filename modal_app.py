"""Modal entrypoint for genes.jpg GPU jobs.

Auth: MODAL_TOKEN_ID / MODAL_TOKEN_SECRET env vars (or `modal token new`).
Run:  modal run modal_app.py                 # GPU smoke test
      modal run modal_app.py::train          # training stub
"""

import modal

app = modal.App("genes-jpg")

image = modal.Image.debian_slim(python_version="3.11").pip_install("torch", "numpy")

volume = modal.Volume.from_name("genes-jpg-data", create_if_missing=True)
DATA_DIR = "/data"


@app.function(image=image, gpu="T4", timeout=600)
def gpu_check() -> str:
    import torch

    if not torch.cuda.is_available():
        return "CUDA not available"
    return f"torch {torch.__version__}, GPU: {torch.cuda.get_device_name(0)}"


@app.function(image=image, gpu="A10G", volumes={DATA_DIR: volume}, timeout=60 * 60)
def train(epochs: int = 1) -> None:
    """Placeholder for the genome -> image model; checkpoints go to the volume."""
    import pathlib

    out = pathlib.Path(DATA_DIR) / "checkpoints"
    out.mkdir(parents=True, exist_ok=True)
    for epoch in range(epochs):
        (out / f"epoch_{epoch}.txt").write_text("placeholder checkpoint\n")
    volume.commit()


@app.local_entrypoint()
def main() -> None:
    print(gpu_check.remote())
