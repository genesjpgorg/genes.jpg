import zlib

import pytest

torch = pytest.importorskip("torch", reason="model tests need `uv sync --extra model`")
import torch.nn.functional as F

from genesjpg import data
from genesjpg.align import AlignModel, contrastive_loss, retrieval_metrics, train_align
from genesjpg.data import Entry, label, synthetic_records, taxonomy_text
from genesjpg.encoders import MODERNGENA, DNAEncoder, HFTokenizer
from genesjpg.pipeline import GenomeToImage, load_align, load_prior, save_align, save_prior
from genesjpg.prior import DiffusionPrior, train_prior

TINY_SD = "hf-internal-testing/tiny-stable-diffusion-pipe"


def _species_embeddings(recs, dim=512, noise=0.3, seed=0):
    g = torch.Generator().manual_seed(seed)
    labels = sorted({label(r) for r in recs})
    protos = F.normalize(torch.randn(len(labels), dim, generator=g), dim=-1)
    idx = torch.tensor([labels.index(label(r)) for r in recs])
    return F.normalize(
        protos[idx] + noise * torch.randn(len(recs), dim, generator=g) / dim**0.5, dim=-1
    )


def test_moderngena_tokenizer_pads_and_masks():
    ids, mask = HFTokenizer(MODERNGENA)(["acgtacgtacgtttagcatcg", "ACGT"])
    assert ids.shape == mask.shape and ids[0, 0].item() == 1  # [CLS]
    assert mask[0].all() and not mask[1].all()
    assert (ids[1][mask[1] == 0] == 3).all()  # [PAD]


def test_align_checkpoint_round_trip(tmp_path):
    model = AlignModel(DNAEncoder.tiny())
    save_align(model, tmp_path / "align.pt")
    loaded = load_align(tmp_path / "align.pt", DNAEncoder.tiny())
    seqs = [r["dna_barcode"] for r in synthetic_records(4)]
    torch.testing.assert_close(loaded.dna.encode(seqs), model.dna.encode(seqs))


def test_contrastive_loss_prefers_aligned_pairs():
    a = F.normalize(torch.randn(16, 32), dim=-1)
    scale = torch.tensor(3.0)
    assert contrastive_loss(a, a, scale) < contrastive_loss(
        a, F.normalize(torch.randn(16, 32), dim=-1), scale
    )
    labels = torch.arange(16) % 4
    assert torch.isfinite(contrastive_loss(a, a, scale, labels))


def test_taxonomy_text():
    rec = synthetic_records(1)[0]
    assert taxonomy_text(rec).startswith("a photo of Animalia Arthropoda Insecta Diptera")


def test_alignment_learns_species():
    torch.manual_seed(0)
    recs = synthetic_records(96, n_species=6)
    img = _species_embeddings(recs)
    model = AlignModel(DNAEncoder.tiny())
    seqs, labels = [r["dna_barcode"] for r in recs], [label(r) for r in recs]
    train_align(model, seqs, labels, img, img, epochs=15, batch_size=32, lr=1e-3, head_lr=3e-3)
    m = retrieval_metrics(model.dna.encode(seqs), img, labels, [r["genus"] for r in recs])
    assert m["species_top1"] > 0.8


def test_prior_samples_unit_vectors_near_targets(tmp_path):
    torch.manual_seed(0)
    cond = F.normalize(torch.randn(128, 512), dim=-1)
    target = cond.clone()
    prior = DiffusionPrior(width=128, depth=2)
    train_prior(prior, cond, target, epochs=150, batch_size=64, lr=1e-3)
    sample = prior.sample(cond[:8], steps=20, guidance=1.0)
    assert sample.shape == (8, 512)
    assert torch.allclose(sample.norm(dim=-1), torch.ones(8), atol=1e-4)
    assert (sample * target[:8]).sum(-1).mean() > 0.5
    save_prior(prior, tmp_path / "prior.pt")
    assert torch.equal(load_prior(tmp_path / "prior.pt").null_cond, prior.null_cond)


def test_decoder_and_end_to_end_with_tiny_sd():
    pytest.importorskip("diffusers")
    from genesjpg.decoder import EmbeddingDecoder

    try:
        decoder = EmbeddingDecoder(TINY_SD, n_tokens=4)
    except OSError as e:  # offline
        pytest.skip(str(e))
    size = decoder.unet.config.sample_size * decoder.vae_scale
    emb = F.normalize(torch.randn(2, 512), dim=-1)
    loss = decoder.loss(torch.rand(2, 3, size, size) * 2 - 1, emb)
    loss.backward()
    assert torch.isfinite(loss)
    assert decoder.projector.proj.weight.grad is not None
    assert all(p.grad is None for p in decoder.vae.parameters())

    recs = synthetic_records(4)
    model = GenomeToImage(AlignModel(DNAEncoder.tiny()), DiffusionPrior(width=64, depth=1), decoder)
    images = model.generate(recs[0]["dna_barcode"], n=2, steps=2)
    assert len(images) == 2 and images[0].size == (size, size)


def test_prior_checkpoint_keeps_schedule(tmp_path):
    prior = DiffusionPrior(dim=8, cond_dim=8, width=16, depth=1, timesteps=50, cond_drop=0.3)
    save_prior(prior, tmp_path / "prior.pt")
    loaded = load_prior(tmp_path / "prior.pt")
    assert (loaded.timesteps, loaded.cond_drop) == (50, 0.3)
    torch.testing.assert_close(loaded.alphas_cumprod, prior.alphas_cumprod)


@pytest.mark.parametrize("n_entries,n,n_blocks", [(100, 25, 20), (100, 100, 7), (10, 50, 3)])
def test_select_blocks_returns_exactly_n_disjoint_entries(n_entries, n, n_blocks):
    entries = [Entry(f"{i}.jpg", i, 1, 0) for i in range(n_entries)]
    blocks = data._select_blocks(entries, n, n_blocks)
    picked = [e.offset for b in blocks for e in b]
    assert len(picked) == min(n, n_entries) == len(set(picked))
    assert all(b == list(entries[b[0].offset : b[-1].offset + 1]) for b in blocks)


def test_stream_metadata_keeps_last_row_without_newline(monkeypatch):
    csv_bytes = b"processid,genus\nA1,Aus\nB2,Bus"
    comp = zlib.compressobj(wbits=-15)
    raw = comp.compress(csv_bytes) + comp.flush()

    class Info:
        header_offset, compress_size = 0, len(raw)

    class FakeZip:
        def __init__(self, url):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def getinfo(self, name):
            return Info()

    class FakeResponse(FakeZip):
        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield raw

    monkeypatch.setattr(data, "RemoteZip", FakeZip)
    monkeypatch.setattr(data, "_range", lambda url, start, end: bytes(30))
    monkeypatch.setattr(data.requests, "get", lambda *a, **kw: FakeResponse(None))
    rows = data._stream_metadata({"A1", "B2"})
    assert rows["B2"]["genus"] == "Bus" and rows["A1"]["genus"] == "Aus"
