import pytest
import torch
import torch.nn.functional as F

from genesjpg.align import AlignModel, contrastive_loss, retrieval_metrics, train_align
from genesjpg.data import label, synthetic_records, taxonomy_text
from genesjpg.encoders import MODERNGENA, DNAEncoder, HFTokenizer, KmerTokenizer
from genesjpg.pipeline import GenomeToImage, load_align, load_prior, save_align, save_prior
from genesjpg.prior import DiffusionPrior, train_prior

TINY_SD = "hf-internal-testing/tiny-stable-diffusion-pipe"


def _species_embeddings(recs, dim=512, noise=0.3, seed=0):
    g = torch.Generator().manual_seed(seed)
    labels = sorted({label(r) for r in recs})
    protos = F.normalize(torch.randn(len(labels), dim, generator=g), dim=-1)
    idx = torch.tensor([labels.index(label(r)) for r in recs])
    return F.normalize(protos[idx] + noise * torch.randn(len(recs), dim, generator=g) / dim**0.5, dim=-1)


def test_tokenizer_matches_barcodebert_vocab():
    tok = KmerTokenizer()
    assert len(tok.vocab) == 256 and min(tok.vocab.values()) == 2
    ids, mask = tok(["ACGTACGTNNNN", "ACGT"])
    assert ids.shape == (2, 3)
    assert ids[0, 2].item() == tok.unk_id
    assert mask.tolist() == [[1, 1, 1], [1, 0, 0]]


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
    assert contrastive_loss(a, a, scale) < contrastive_loss(a, F.normalize(torch.randn(16, 32), dim=-1), scale)
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
