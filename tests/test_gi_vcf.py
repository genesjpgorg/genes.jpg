"""Scientific regression tests: genotype semantics, indel anchors and model deltas."""

import gzip
import math
from pathlib import Path

import pytest

pytest.importorskip("pysam")
pytest.importorskip("scipy")
import pysam

from longevity.gi_vcf import (
    FrozenPredictor,
    InputError,
    Variant,
    WindowIndex,
    aggregate_expression,
    apply_haplotype,
    compare_predictions,
    reverse_complement,
    scan_variants,
    vcf_header,
)

ROOT = Path(__file__).resolve().parents[1]


def variant(pos, ref, alt, gt=(1, 1), phased=False, phase_set="implicit"):
    return Variant("1", pos, ref, tuple(alt.split(",")), gt, phased, phase_set)


def test_indels_keep_surviving_tss_centered_on_both_strands():
    sequence = "ACGTACTGACGTAGCTAGCATCGATGCA"
    # TSS is base 12, A. Delete two bases before it, insert immediately before it.
    edits = [variant(6, sequence[6:9], sequence[6]), variant(11, sequence[11], sequence[11] + "GG")]
    edited = sequence[:7] + sequence[9:12] + "GG" + sequence[12:]
    # Net shift zero: -2 deleted then +2 inserted, preserving reference base 12.
    plus = apply_haplotype(sequence, 0, 12, "+", edits, 0, 0, flank=4)
    minus = apply_haplotype(sequence, 0, 12, "-", edits, 0, 0, flank=4)
    assert plus == edited[8:16]
    assert minus == reverse_complement(edited[9:17])
    assert plus[4] == sequence[12]
    assert minus[4] == reverse_complement(sequence[12])


def test_deletion_after_tss_retains_shared_anchor():
    sequence = "AAAACCCCGGGGTTTTAAAA"
    edits = [variant(8, "GGG", "G")]
    actual = apply_haplotype(sequence, 0, 8, "+", edits, 0, 0, flank=4)
    assert actual == "CCCCGGTT"
    assert actual[4] == "G"


def test_tss_deletion_and_conflicting_alleles_are_rejected():
    sequence = "AAAACCCCGGGGTTTTAAAA"
    with pytest.raises(InputError, match="TSS"):
        apply_haplotype(sequence, 0, 8, "+", [variant(7, "CGG", "C")], 0, 0, flank=4)
    with pytest.raises(InputError, match="Overlapping"):
        apply_haplotype(
            sequence, 0, 8, "+", [variant(8, "G", "A"), variant(8, "G", "C")], 0, 0, flank=4
        )


def test_unphased_multiallelic_and_phase_blocks():
    first = variant(5, "A", "C,G", (1, 2), True, "block-1")
    second = variant(9, "T", "A", (0, 1), True, "block-1")
    for draw in range(8):
        assert {first.allele(draw, h) for h in (0, 1)} == {"C", "G"}
        for haplotype in (0, 1):
            assert (first.allele(draw, haplotype) == "C") == (second.allele(draw, haplotype) == "T")
    unphased = variant(12, "T", "C", (0, 1))
    assert {unphased.allele(0, h) for h in (0, 1)} == {"T", "C"}
    assert unphased.allele(3, 0) == unphased.allele(3, 0)
    haploid = variant(1, "A", "G", (1,))
    assert haploid.allele(0, 0) == haploid.allele(0, 1) == "G"


def test_interval_index_includes_all_overlapping_genes():
    rows = [
        {"contig": "1", "window_start0": a, "window_end0": b, "human_gene_id": name}
        for a, b, name in [(10, 50, "A"), (20, 30, "B"), (40, 60, "C")]
    ]
    index = WindowIndex(rows, padding=0)
    assert index.overlap("1", 29, 41) == ["A", "B", "C"]
    assert index.overlap("1", 60, 61) == []
    assert index.overlap("1", 5, 10) == []


def write_vcf(path, records, assembly="GRCh38", samples=("ONE", "TWO")):
    content = (
        f"##fileformat=VCFv4.3\n##reference={assembly}\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t"
        + "\t".join(samples)
        + "\n"
        + "\n".join(records)
        + "\n"
    )
    path.write_text(content)
    return path


def test_vcf_filtering_sample_selection_gzip_and_reference_matching(tmp_path):
    fa = tmp_path / "reference.fa"
    fa.write_text(">1\n" + "A" * 10000 + "\n")
    pysam.faidx(str(fa))
    windows = [{"contig": "1", "window_start0": 4000, "window_end0": 5000, "human_gene_id": "G1"}]
    records = [
        "chr1\t4200\t.\tA\tC,G\t.\tPASS\t.\tGT\t0/0\t1/2",
        "1\t4201\t.\tA\tC\t.\tq10\t.\tGT\t1/1\t1/1",
        "1\t4202\t.\tA\tG\t.\tPASS\t.\tGT\t0/1\t./.",
        "1\t9000\t.\tA\tG\t.\tPASS\t.\tGT\t1/1\t1/1",
        "1\t4203\t.\tA\tG\t.\tPASS\t.\tGT\t1/1\t0/0",
    ]
    vcf = write_vcf(tmp_path / "sample.vcf", records)
    compressed = tmp_path / "no-extension"
    compressed.write_bytes(gzip.compress(vcf.read_bytes()))
    assert vcf_header(compressed)["samples"] == ["ONE", "TWO"]
    with pysam.FastaFile(str(fa)) as fasta:
        calls, counts = scan_variants(compressed, "TWO", windows, fasta)
        assert calls["G1"][0].gt == (1, 2)
        assert len(calls["G1"]) == 1
        assert counts["eligible_variant_records"] == 1
        assert (
            counts["filtered_calls"]
            == counts["missing_calls"]
            == counts["outside_windows"]
            == counts["reference_calls"]
            == 1
        )
        bad = write_vcf(tmp_path / "bad.vcf", ["1\t4200\t.\tT\tG\t.\tPASS\t.\tGT\t1/1\t1/1"])
        with pytest.raises(InputError, match="REF mismatch"):
            scan_variants(bad, "ONE", windows, fasta)
        sv = write_vcf(
            tmp_path / "sv.vcf", ["1\t1000\t.\tA\t<DEL>\t.\tPASS\tEND=4500\tGT\t1/1\t1/1"]
        )
        with pytest.raises(InputError, match="Unsupported"):
            scan_variants(sv, "ONE", windows, fasta)


def test_wrong_build_and_sites_only_rejected(tmp_path):
    vcf = write_vcf(tmp_path / "old.vcf", [], assembly="GRCh37")
    with pytest.raises(InputError, match="GRCh37"):
        vcf_header(vcf)
    vcf.write_text("##fileformat=VCFv4.3\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
    with pytest.raises(InputError, match="sites-only"):
        vcf_header(vcf)


def test_diploid_mean_is_in_tpm_units():
    assert aggregate_expression([math.log1p(1), math.log1p(9)]) == pytest.approx(math.log1p(5))
    assert aggregate_expression([2, 2, 2, 2]) == pytest.approx(2)


@pytest.mark.parametrize(
    "model,expected",
    [("fdr_genes_ridge", 32.52751641619777), ("all_genes_ridge", 46.55558228872374)],
)
def test_frozen_reference_and_matched_delta(model, expected):
    predictor = FrozenPredictor(ROOT)
    empty = compare_predictions(predictor, model, [], {}, 4)
    assert empty["reference_predicted_years"] == pytest.approx(expected)
    assert empty["percent_change"] == 0
    gene = next(iter(predictor.weights[model]))
    # Deliberately shifted native expression: denominator must use matched reference.
    reference = predictor.expression[gene] + 0.5
    plan = [
        {
            "human_gene_id": gene,
            "native_hash": "r",
            "haplotype_hashes": [["r", "a"]] * 4,
            "variant_records": 1,
        }
    ]
    result = compare_predictions(predictor, model, plan, {"r": reference, "a": reference + 0.2}, 4)
    delta_expression = aggregate_expression([reference, reference + 0.2]) - reference
    weight = predictor.weights[model][gene]
    delta_log = weight["coefficient_standardized"] * delta_expression / weight["training_scale"]
    assert result["reference_predicted_years"] != pytest.approx(expected)
    assert result["percent_change"] == pytest.approx(100 * math.expm1(delta_log))
    assert result["genes"][0]["log_lifespan_contribution"] == pytest.approx(
        result["log_lifespan_change"]
    )
    assert result["phase_percent_range"][0] == result["phase_percent_range"][1]
