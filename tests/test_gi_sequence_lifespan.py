import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from longevity.gi_api import payload, request_hash
from longevity.gi_sequence_lifespan import CONFIG, infer_one, prepare, records, window_qc


def test_prepare_preserves_tss_on_both_strands_and_checks_reference_hash(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    pd.DataFrame({"model": ["all_genes_ridge"] * 2, "human_gene_id": ["G1", "G2"]}).to_csv(
        model / "coefficients.csv", index=False
    )
    sequence = "".join(np.random.default_rng(77).choice(list("ACGT"), 90000))
    genome = tmp_path / "genome.fa"
    genome.write_text(">chr1\n" + sequence + "\n")
    tss = tmp_path / "tss.csv"
    pd.DataFrame(
        {
            "human_gene_id": ["G1", "G2"],
            "contig": ["chr1"] * 2,
            "tss0": [45000, 45001],
            "strand": ["+", "-"],
        }
    ).to_csv(tss, index=False)
    args = SimpleNamespace(
        output=tmp_path / "prepared", genome=genome, tss_map=tss, model_dir=model, model="both"
    )
    prepare(args)
    windows = dict(records(args.output / "windows.fasta.gz"))
    assert windows["G1"] == sequence[4040:85960]
    assert windows["G1"][40960] == sequence[45000]
    assert windows["G2"][40960] == sequence[45001].translate(str.maketrans("ACGT", "TGCA"))
    assert all(len(s) == 81920 for s in windows.values())
    table = pd.read_csv(tss)
    table["sequence_sha256"] = "wrong"
    table.to_csv(tss, index=False)
    with pytest.raises(ValueError, match="checksum mismatch"):
        prepare(args)


def test_cache_only_inference_validates_context_and_never_calls_client(tmp_path):
    sequence = "A" * 81920
    row = {
        "sequence": sequence,
        "gene_id": "G1",
        "ensembl_species": "test_species",
        "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
    }
    key = request_hash(payload(CONFIG, sequence))
    response = {
        "meta": {
            "model": CONFIG["model"],
            "sequence_length": 81920,
            "task_specific_counts": {"tss_index": 40960, "scored_window": [20000, 70000]},
        },
        "data": {
            "prediction": {"expression_log_tpm": 2.5},
            "input": {
                "description": CONFIG["description"],
                "sequence_name": CONFIG["sequence_name"],
            },
        },
    }
    path = tmp_path / f"{key}.json"
    path.write_text(json.dumps(response))
    assert infer_one(row, None, tmp_path, True)["expression_log_tpm"] == 2.5
    response["data"]["input"]["description"] = "wrong tissue or species"
    path.write_text(json.dumps(response))
    with pytest.raises(ValueError, match="description mismatch"):
        infer_one(row, None, tmp_path, True)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        infer_one(row, None, tmp_path, True)


def test_window_qc_rejects_short_non_dna_and_unknown_tss():
    assert window_qc("A" * 81919) == "length_must_be_81920"
    assert window_qc("R" + "A" * 81919) == "unsupported_dna_alphabet"
    assert window_qc("A" * 40960 + "N" + "A" * 40959) == "N_in_central_9198_bp"
    assert window_qc("N" * 1000 + "A" * 80920) == "more_than_one_percent_N"
