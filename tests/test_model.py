import json
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


def test_taxonomy_text_uses_record_lineage():
    rec = {
        "kingdom": "Animalia",
        "phylum": "Chordata",
        "class": "Mammalia",
        "order": "Carnivora",
        "family": "Canidae",
        "subfamily": "",
        "genus": "Vulpes",
        "species": "Vulpes vulpes",
    }
    assert (
        taxonomy_text(rec)
        == "a photo of Animalia Chordata Mammalia Carnivora Canidae Vulpes vulpes"
    )
    partial = {**rec, "kingdom": "", "phylum": ""}
    assert taxonomy_text(partial) == "a photo of Mammalia Carnivora Canidae Vulpes vulpes"
    bioscan = {k: v for k, v in rec.items() if k not in ("kingdom", "phylum", "class")}
    assert taxonomy_text(bioscan).startswith("a photo of Animalia Arthropoda Insecta Carnivora")


def test_folmer_barcode():
    from genesjpg.genomes import HCO2198, LCO1490, _revcomp, folmer_barcode

    inner = "ACGT" * 164 + "AC"
    assert folmer_barcode("AAA" + LCO1490 + inner + _revcomp(HCO2198) + "TTT") == inner


def _fake_assembly(tmp_path, rng):
    """Gzipped FASTA with two nuclear chromosomes, one short scaffold and a mitochondrion."""
    import gzip
    import random

    rng = random.Random(rng)
    chroms = {
        n: "".join(rng.choice("ACGTacgt") for _ in range(L))
        for n, L in [
            ("NC_1.1", 30_000),
            ("NC_2.1", 25_000),
            ("NW_3.1", 500),
            ("NT_ALT.1", 12_000),
            ("NC_MT.1", 16_000),
        ]
    }
    fa = tmp_path / "x_genomic.fna.gz"
    with gzip.open(fa, "wt") as f:
        for n, s in chroms.items():
            f.write(f">{n} Some species {'mitochondrion' if n == 'NC_MT.1' else 'chromosome'}\n")
            f.writelines(s[i : i + 80] + "\n" for i in range(0, len(s), 80))
    report = tmp_path / "x_assembly_report.txt"
    rows = [
        ["1", "assembled-molecule", "1", "Chromosome", "CM1.1", "=", "NC_1.1", "Primary Assembly"],
        ["2", "assembled-molecule", "2", "Chromosome", "CM2.1", "=", "NC_2.1", "Primary Assembly"],
        ["u", "unplaced-scaffold", "na", "na", "JA3.1", "=", "NW_3.1", "Primary Assembly"],
        ["MT", "assembled-molecule", "MT", "Mitochondrion", "na", "<>", "NC_MT.1", "non-nuclear"],
        [
            "HSCHR1_ALT",
            "alt-scaffold",
            "1",
            "Chromosome",
            "KI1.1",
            "=",
            "NT_ALT.1",
            "ALT_REF_LOCI_1",
        ],
    ]
    report.write_text("# Sequence-Name\tetc\n" + "".join("\t".join(r) + "\n" for r in rows))
    return fa, report, chroms


def test_pack_genome_nuclear_only_and_windows(tmp_path):
    from genesjpg.genomes import PackedGenome, pack_genome

    fa, report, chroms = _fake_assembly(tmp_path, 0)
    out = pack_genome(fa, report, tmp_path / "packed")
    g = PackedGenome(out, window=10_000)
    nuclear = {n: s.upper() for n, s in chroms.items() if n not in ("NC_MT.1", "NT_ALT.1")}
    assert g.seq.size == sum(map(len, nuclear.values()))
    assert len(g.spans) == 2  # the 500 bp scaffold is shorter than a window
    w = g.windows(8)
    assert w == PackedGenome(out, window=10_000).windows(8)  # fixed windows are deterministic
    assert all(len(x) == 10_000 and any(x in s for s in nuclear.values()) for x in w)
    assert not any(x in chroms["NC_MT.1"].upper() for x in w)
    assert sorted(json.loads((out / "index.json").read_text())["excluded"]) == [
        "NC_MT.1",
        "NT_ALT.1",
    ]


def test_alignment_trains_on_sampled_windows():
    torch.manual_seed(0)
    recs = synthetic_records(48, n_species=4, seq_len=300)
    img = _species_embeddings(recs)
    model = AlignModel(DNAEncoder.tiny())
    labels = [label(r) for r in recs]
    calls = []

    def sample(idx):  # a random 200 bp slice of each record's sequence
        calls.append(len(idx))
        g = torch.randint(0, 100, (1,)).item()
        return [recs[i]["dna_barcode"][g : g + 200] for i in idx]

    train_align(model, None, labels, img, img, epochs=2, batch_size=16, sample_dna=sample)
    assert calls == [16, 16, 16] * 2


def test_pooled_metrics_unseen_species_must_beat_seen():
    from genesjpg.align import pooled_metrics

    e = torch.eye(3)
    gallery, g_labels, g_genera = e, ["a a", "b b", "c c"], ["a", "b", "c"]
    # a query for species "c c" that lands nearest to "a a": wrong in the pooled gallery
    q = F.normalize(torch.tensor([[1.0, 0.0, 0.5]]), dim=-1)
    m = pooled_metrics(q, ["c c"], ["c"], gallery, g_labels, g_genera)
    assert m == {"pooled_species_top1": 0.0, "pooled_genus_top1": 0.0}
    # against its own single-species split it would be trivially right
    assert retrieval_metrics(q, e[2:], ["c c"], ["c"])["species_top1"] == 1.0


def test_bioclip_ranks_from_ncbi_species():
    from genesjpg.genomes import bioclip_ranks

    def row(k, p, c, lineage):
        return {
            "kingdom": k,
            "phylum": p,
            "class": c,
            "lineage_taxids": "|".join(map(str, lineage)),
        }

    fox = row("Metazoa", "Chordata", "Mammalia", [1, 33208, 7711, 40674])
    assert bioclip_ranks(fox) == ("Animalia", "Chordata", "Mammalia")
    asparagus = row(
        "Viridiplantae", "Streptophyta", "Magnoliopsida", [33090, 35493, 58023, 3398, 4447]
    )
    assert bioclip_ranks(asparagus) == ("Plantae", "Tracheophyta", "Liliopsida")
    coffee = row("Viridiplantae", "Streptophyta", "Magnoliopsida", [33090, 35493, 58023, 3398])
    assert bioclip_ranks(coffee) == ("Plantae", "Tracheophyta", "Magnoliopsida")
    moss = row("Viridiplantae", "Streptophyta", "Bryopsida", [33090, 35493, 3208])
    assert bioclip_ranks(moss) == ("Plantae", "Bryophyta", "Bryopsida")
    shark = row("Metazoa", "Chordata", "Chondrichthyes", [7711, 7777, 7778])
    assert bioclip_ranks(shark) == ("Animalia", "Chordata", "Elasmobranchii")
    perch = row("Metazoa", "Chordata", "Actinopteri", [7711, 7898, 186623])
    assert bioclip_ranks(perch) == ("Animalia", "Chordata", "Actinopterygii")
    lizard = row("Metazoa", "Chordata", "Lepidosauria", [7711, 32561, 8504, 8509])
    assert bioclip_ranks(lizard) == ("Animalia", "Chordata", "Reptilia")
    turtle = row("Metazoa", "Chordata", "", [7711, 32561, 8459])
    assert bioclip_ranks(turtle) == ("Animalia", "Chordata", "Reptilia")
    fungus = row("Fungi", "Basidiomycota", "Agaricomycetes", [4751, 5204])
    assert bioclip_ranks(fungus) == ("Fungi", "Basidiomycota", "Agaricomycetes")
