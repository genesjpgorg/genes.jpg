"""Offline coverage of chunk preparation, lazy H5 reads, and the shared training path."""

import gzip
import json
from contextlib import ExitStack

import numpy as np
import pytest

h5py = pytest.importorskip("h5py")
torch = pytest.importorskip("torch")

from longevity.chunks import fasta_blocks, load_chunks, sample_genome, write_genome
from longevity.train import collate, group_split, main


class Tokenizer:
    def __call__(self, sequence, **kwargs):
        assert kwargs == {"add_special_tokens": False, "truncation": False}
        return {"input_ids": [4 + "ACGTN".index(c) for c in sequence]}


def metadata(taxid=1):
    return dict(
        assembly_accession=f"GCF_{taxid}",
        ncbi_taxid=taxid,
        scientific_name=f"Species {taxid}",
        **{"class": "Mammalia"},
        order="Order",
        family=f"Family {taxid}",
        max_longevity_yrs=float(taxid * 10),
    )


def test_exact_chunks_seed_and_contig_boundaries(tmp_path):
    fasta = tmp_path / "genome.fna.gz"
    with gzip.open(fasta, "wt") as f:
        f.write(">first\n" + "A" * 2100 + "\n>second\n" + "C" * 3100 + "\n")
    blocks = list(fasta_blocks(fasta, 2048))
    assert [(c, s, len(x)) for c, s, x in blocks] == [
        ("first", 0, 2048),
        ("first", 2048, 52),
        ("second", 0, 2048),
        ("second", 2048, 1052),
    ]
    x, sources = sample_genome(fasta, Tokenizer(), seed=4, block_bases=2048)
    y, again = sample_genome(fasta, Tokenizer(), seed=4, block_bases=2048)
    assert x.shape == (1000, 1024)
    assert np.array_equal(x, y) and sources == again
    assert all(np.unique(row).size == 1 for row in x[:, 1:-1])
    assert set(x[:, 1]) == {4, 5}
    assert sources != sample_genome(fasta, Tokenizer(), seed=5, block_bases=2048)[1]


def test_short_contigs_are_not_joined_or_padded(tmp_path):
    fasta = tmp_path / "short.fna"
    fasta.write_text(">a\n" + "A" * 800 + "\n>b\n" + "C" * 800)
    with pytest.raises(ValueError, match="no block contains"):
        sample_genome(fasta, Tokenizer())


def make_shards(tmp_path, count=3):
    fasta = tmp_path / "genome.fna"
    fasta.write_text(">contig\n" + "ACGT" * 600)
    directory = tmp_path / "chunks"
    for taxid in range(1, 5):
        write_genome(directory / f"{taxid}.h5", fasta, metadata(taxid), Tokenizer(), count=count)
    return directory


def test_h5_round_trip_and_lazy_subsets(tmp_path):
    directory = make_shards(tmp_path)
    with ExitStack() as stack:
        df, rows = load_chunks(directory, stack)
        assert len(rows) == 12
        assert "input_ids" not in df
        assert df.groupby("ncbi_taxid").log10_longevity.nunique().eq(1).all()
        subset = rows.subset([11, 0, 5])
        assert np.array_equal(subset[0], rows[11])
        inp, mask, gidx = collate(
            [subset[i] for i in range(3)], 1024, True, __import__("random").Random(0), encoded=True
        )
        assert gidx.tolist() == [0, 1, 2]
        assert inp.shape == (3, 1024) and mask.all()
        assert (inp[:, 0] == 1).all() and (inp[:, -1] == 2).all()
        np.testing.assert_array_equal(inp[0].numpy(), rows[11])
        val, test = group_split(df, "family", 0.25, 0.25, 0)
        assert len(val) == len(test) == 1 and not set(val) & set(test)
    with h5py.File(directory / "1.h5", "r+") as f:
        f["labels"][0] = 42
    with ExitStack() as stack, pytest.raises(ValueError, match="Labels disagree"):
        load_chunks(directory, stack)


def test_shared_trainer_with_tiny_modernbert(tmp_path, monkeypatch):
    from transformers import AutoModel, ModernBertConfig, ModernBertModel

    torch.set_num_threads(2)
    config = ModernBertConfig(
        vocab_size=16,
        hidden_size=8,
        intermediate_size=16,
        num_hidden_layers=1,
        num_attention_heads=2,
        max_position_embeddings=2048,
        pad_token_id=3,
        cls_token_id=1,
        sep_token_id=2,
    )
    config._attn_implementation = "eager"
    monkeypatch.setattr(AutoModel, "from_pretrained", lambda *a, **kw: ModernBertModel(config))
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    directory = make_shards(tmp_path, count=2)
    out = tmp_path / "run"
    main(
        [
            "--data",
            str(directory),
            "--out",
            str(out),
            "--attn",
            "eager",
            "--val-species",
            "3",
            "--test-species",
            "4",
            "--max-steps",
            "1",
            "--tokens-per-batch",
            "2052",
            "--eval-tokens-per-batch",
            "2052",
        ]
    )
    saved = torch.load(out / "model.pt", weights_only=False)
    assert saved["config"]["split"] == {"train": [1, 2], "val": [3], "test": [4]}
    assert saved["config"]["max_len"] == 1024
    assert saved["config"]["mu"] == pytest.approx((1 + np.log10(20)) / 2)
    metrics = [json.loads(line) for line in (out / "metrics.jsonl").read_text().splitlines()]
    assert {m["kind"] for m in metrics} == {"train", "val", "test", "done"}
    assert all(np.isfinite(m["loss"]) for m in metrics if m["kind"] == "train")
    assert next(m for m in metrics if m["kind"] == "val")["species"][0]["n_chunks"] == 2
    with pytest.raises(SystemExit):
        main(["--data", str(directory), "--out", str(out), "--max-len", "1026"])


def test_prepare_cli_with_dataset_tables(tmp_path, monkeypatch):
    import pandas as pd
    from transformers import AutoTokenizer

    from longevity.chunks import main as prepare

    fasta = tmp_path / "genome.fna"
    fasta.write_text(">a\n" + "ACGT" * 1024)
    pd.DataFrame(
        [
            {
                "assembly_accession": "GCF_1",
                "ncbi_taxid": 999,
                "species_taxid": 1,
                "sequence_file": fasta.name,
            }
        ]
    ).to_parquet(tmp_path / "genomes.parquet")
    pd.DataFrame([metadata()]).drop(columns=["assembly_accession", "max_longevity_yrs"]).to_parquet(
        tmp_path / "species.parquet"
    )
    pd.DataFrame([{"ncbi_taxid": 1, "is_primary": True, "max_longevity_yrs": 10.0}]).to_parquet(
        tmp_path / "longevity.parquet"
    )
    monkeypatch.setattr(AutoTokenizer, "from_pretrained", lambda *a, **kw: Tokenizer())
    monkeypatch.setattr(
        "sys.argv", ["chunks", "--dataset", str(tmp_path), "--out", str(tmp_path / "output")]
    )
    prepare()
    with ExitStack() as stack:
        df, rows = load_chunks(tmp_path / "output", stack)
        assert len(rows) == 1000 and set(df.ncbi_taxid) == {1}
        assert rows[0].shape == (1024,)
    with pytest.raises(FileExistsError):
        prepare()
