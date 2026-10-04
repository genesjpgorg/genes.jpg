from collections import Counter

import numpy as np
import pytest

from longevity.gi_api import payload, request_hash
from longevity.gi_tss_shuffle import sequence_hash, shuffle_blocks


def test_shuffle_preserves_each_flank_blocks_exterior_and_unknown_positions():
    rng = np.random.default_rng(123)
    sequence = "".join(rng.choice(list("ACGT"), size=1024))
    sequence = sequence[:320] + "ACGTNNNN" + sequence[328:]
    shuffled = shuffle_blocks(sequence, 512, 256, 8, 8192)
    assert shuffled != sequence
    assert shuffled[:256] == sequence[:256]
    assert shuffled[768:] == sequence[768:]
    assert shuffled[320:328] == "ACGTNNNN"
    assert Counter(shuffled) == Counter(sequence)
    for start, end in [(256, 512), (512, 768)]:
        assert Counter(shuffled[i : i + 8] for i in range(start, end, 8)) == Counter(
            sequence[i : i + 8] for i in range(start, end, 8)
        )
    assert shuffled == shuffle_blocks(sequence, 512, 256, 8, 8192)
    assert shuffled != shuffle_blocks(sequence, 512, 256, 8, 8193)


def test_only_sequence_changes_in_gi_payload():
    config = {
        "flank_bp": 512,
        "model": "g0-expression-8192",
        "sequence_name": "orthologue_tss_window",
        "description": "Homo sapiens primary hepatocytes, untreated, bulk RNA-seq.",
    }
    sequence = "".join(np.random.default_rng(7).choice(list("ACGT"), size=1024))
    shuffled = shuffle_blocks(sequence, 512, 256, 8, 101)
    before, after = payload(config, sequence), payload(config, shuffled)
    assert {k: v for k, v in before.items() if k != "sequence"} == {
        k: v for k, v in after.items() if k != "sequence"
    }
    assert request_hash(before) != request_hash(after)
    assert sequence_hash(sequence) != sequence_hash(shuffled)


@pytest.mark.parametrize("tss,flank", [(512, 257), (64, 256), (900, 256), (512, 0)])
def test_invalid_shuffle_geometry_is_rejected(tss, flank):
    with pytest.raises(ValueError):
        shuffle_blocks("A" * 1024, tss, flank, 8, 1)


def test_report_pairs_species_and_detects_correlation_reversal():
    from types import SimpleNamespace

    import pandas as pd

    from longevity.gi_tss_shuffle_report import summarize_gene

    rows = []
    for i in range(12):
        base = {
            "human_gene_id": "H1",
            "gene_id": f"G{i}",
            "ensembl_species": f"sp{i:02}",
            "max_longevity_yrs": float(2**i),
            "adult_weight_g": 10.0,
            "anage_order": "order",
            "original_expression_log_tpm": float(i),
        }
        rows.append(dict(base, condition="native", replicate=-1, expression_log_tpm=float(i)))
        for replicate in range(2):
            rows.append(
                dict(
                    base,
                    condition="shuffled",
                    replicate=replicate,
                    expression_log_tpm=20.0 - i + replicate,
                )
            )
    candidate = SimpleNamespace(
        human_gene_id="H1", gene_name="gene", n_species=12, spearman_rho=1.0
    )
    frame = pd.DataFrame(rows).sample(frac=1, random_state=1)
    result, _, species = summarize_gene(frame, candidate, 2, permutations=999)
    assert result["native_rho"] == pytest.approx(1)
    assert result["shuffled_mean_rho"] == pytest.approx(-1)
    assert result["signed_rho_attenuation"] == pytest.approx(2)
    assert result["native_retest_max_abs_difference"] == 0
    assert len(species) == 12
    with pytest.raises(ValueError, match="species or replicates"):
        summarize_gene(frame.iloc[:-1], candidate, 2, permutations=999)
