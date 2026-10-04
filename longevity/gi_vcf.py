"""Genotype-aware GRCh38 TSS perturbations for the frozen comparative lifespan model.

Coordinates are zero-based, half-open internally. VCF alleles are always applied
on the genomic plus strand, then the window is oriented around the original TSS.
"""

from __future__ import annotations

import bisect
import csv
import gzip
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from longevity.gi_sequence_lifespan import window_qc

FLANK = 40960
PADDING = 2048
MAX_ALLELE = 1000
MAX_RECORDS = 20_000_000
MAX_EXPANDED_BYTES = 4 * 1024**3
PHASE_SEED = 20261004
MODELS = ("fdr_genes_ridge", "all_genes_ridge")
COMPLEMENT = str.maketrans("ACGTN", "TGCAN")


class InputError(ValueError):
    """Actionable problem with an uploaded VCF or a model input."""


def reverse_complement(sequence):
    return sequence.translate(COMPLEMENT)[::-1]


def normalize_contig(contig):
    contig = contig[3:] if contig.lower().startswith("chr") else contig
    return "MT" if contig in {"M", "MT"} else contig


def open_vcf(path):
    with open(path, "rb") as stream:
        compressed = stream.read(2) == b"\x1f\x8b"
    return gzip.open(path, "rt", encoding="utf-8") if compressed else open(path, encoding="utf-8")


def bounded_lines(stream):
    # A malformed gzip member must not allocate an unbounded single text line.
    while line := stream.readline(4 * 1024**2 + 1):
        if len(line) > 4 * 1024**2:
            raise InputError("A VCF line exceeds the 4 MB limit.")
        yield line


def vcf_header(path):
    metadata, size = [], 0
    with open_vcf(path) as stream:
        for line in bounded_lines(stream):
            size += len(line)
            if size > 4 * 1024**2:
                raise InputError("VCF header exceeds 4 MB.")
            if line.startswith("##"):
                metadata.append(line.rstrip())
            elif line.startswith("#CHROM\t"):
                columns = line.rstrip("\r\n").split("\t")
                if len(columns) < 10 or columns[8] != "FORMAT":
                    raise InputError(
                        "Use a sample VCF with FORMAT/GT calls; sites-only files are unsupported."
                    )
                samples = columns[9:]
                if len(set(samples)) != len(samples):
                    raise InputError("VCF sample names must be unique.")
                evidence = "\n".join(
                    x
                    for x in metadata
                    if x.startswith(("##reference=", "##assembly=", "##contig="))
                )
                if re.search(r"grch37|hg19|human_g1k_v37|\bb37\b", evidence, re.IGNORECASE):
                    raise InputError(
                        "This VCF declares GRCh37/hg19. Supply a GRCh38 VCF; no liftover is performed."
                    )
                chr1 = [x for x in metadata if re.search(r"ID=(?:chr)?1[,>]", x)]
                if any("length=249250621" in x.lower() for x in chr1):
                    raise InputError("Chromosome 1 length identifies GRCh37. Supply GRCh38 calls.")
                declared = bool(re.search(r"grch38|hg38", evidence, re.IGNORECASE)) or any(
                    "length=248956422" in x.lower() for x in chr1
                )
                return {
                    "samples": samples,
                    "assembly_evidence": "GRCh38 declared"
                    if declared
                    else "No recognized build declaration; GRCh38 must be confirmed",
                }
            elif line.strip():
                raise InputError("Missing VCF #CHROM header.")
    raise InputError("Missing VCF #CHROM header.")


@dataclass(frozen=True)
class Variant:
    contig: str
    pos: int
    ref: str
    alts: tuple[str, ...]
    gt: tuple[int, ...]
    phased: bool
    phase_set: str

    @property
    def label(self):
        return f"{self.contig}:{self.pos + 1}:{self.ref}:{','.join(self.alts)}"

    def allele(self, draw, haplotype):
        if len(self.gt) == 1 or self.gt[0] == self.gt[-1]:
            index = self.gt[0]
        else:
            block = f"PS:{self.phase_set}" if self.phased else f"unphased:{self.label}"
            key = f"{PHASE_SEED}|{self.contig}|{block}|{draw}"
            flip = hashlib.sha256(key.encode()).digest()[0] & 1
            index = self.gt[haplotype ^ flip]
        return self.ref if index == 0 else self.alts[index - 1]


class WindowIndex:
    def __init__(self, windows, padding=PADDING):
        groups = defaultdict(list)
        for row in windows:
            groups[row["contig"]].append(
                (row["window_start0"] - padding, row["window_end0"] + padding, row["human_gene_id"])
            )
        self.groups = {}
        for contig, intervals in groups.items():
            intervals.sort()
            maximum, ends = -1, []
            for _, end, _ in intervals:
                maximum = max(maximum, end)
                ends.append(maximum)
            self.groups[contig] = ([v[0] for v in intervals], ends, intervals)

    def overlap(self, contig, start, end):
        if contig not in self.groups:
            return []
        starts, ends, intervals = self.groups[contig]
        lo, hi = bisect.bisect_right(ends, start), bisect.bisect_left(starts, end)
        return [gene for a, b, gene in intervals[lo:hi] if b > start and a < end]


def scan_variants(path, sample, windows, fasta, progress=lambda **kw: None):
    header = vcf_header(path)
    if sample not in header["samples"]:
        raise InputError("Selected sample is absent from the VCF.")
    sample_column = 9 + header["samples"].index(sample)
    index, per_gene, counts = WindowIndex(windows), defaultdict(list), Counter()
    seen = set()
    size = 0
    with open_vcf(path) as stream:
        for line_number, line in enumerate(bounded_lines(stream), 1):
            size += len(line)
            if size > MAX_EXPANDED_BYTES:
                raise InputError("Expanded VCF exceeds the 4 GB limit.")
            if line.startswith("#") or not line.strip():
                continue
            counts["records_scanned"] += 1
            if counts["records_scanned"] > MAX_RECORDS:
                raise InputError("VCF exceeds 20 million records.")
            if counts["records_scanned"] % 5000 == 0:
                progress(stage="scanning", counts=dict(counts))
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) != 9 + len(header["samples"]):
                raise InputError(f"Malformed VCF columns at line {line_number}.")
            contig = normalize_contig(fields[0])
            try:
                pos = int(fields[1]) - 1
            except ValueError as exc:
                raise InputError(f"Invalid position at line {line_number}.") from exc
            if pos < 0:
                raise InputError(f"VCF positions must be positive (line {line_number}).")
            ref = fields[3].upper()
            end = pos + len(ref)
            match = re.search(r"(?:^|;)END=(\d+)(?:;|$)", fields[7])
            if match:
                end = max(end, int(match[1]))
            genes = index.overlap(contig, pos, end)
            if not genes:
                counts["outside_windows"] += 1
                continue
            counts["records_in_padded_windows"] += 1
            if fields[6] not in {"PASS", "."}:
                counts["filtered_calls"] += 1
                continue
            fmt = dict(zip(fields[8].split(":"), fields[sample_column].split(":")))
            if fmt.get("FT", "PASS") not in {"PASS", "."}:
                counts["filtered_calls"] += 1
                continue
            gt_text = fmt.get("GT", ".")
            if "." in gt_text:
                counts["missing_calls"] += 1
                continue
            try:
                gt = tuple(int(x) for x in re.split(r"[/|]", gt_text))
            except ValueError as exc:
                raise InputError(f"Invalid GT at {contig}:{pos + 1}.") from exc
            if len(gt) not in {1, 2} or ("/" in gt_text and "|" in gt_text):
                raise InputError(f"Only haploid or diploid GT is supported ({contig}:{pos + 1}).")
            alts = tuple(x.upper() for x in fields[4].split(","))
            if any(x < 0 or x > len(alts) for x in gt):
                raise InputError(f"GT allele index is invalid at {contig}:{pos + 1}.")
            if not any(gt):
                counts["reference_calls"] += 1
                continue
            used_alleles = [ref] + [alts[x - 1] for x in gt if x]
            if any(not a or set(a) - set("ACGT") or len(a) > MAX_ALLELE for a in used_alleles):
                raise InputError(
                    f"Unsupported called allele at {contig}:{pos + 1}. Use sequence-resolved SNVs, MNVs or indels of at most {MAX_ALLELE} bp; symbolic/SV alleles are unsupported in predictor windows."
                )
            observed = fasta.fetch(contig, pos, pos + len(ref)).upper()
            if observed != ref:
                raise InputError(
                    f"REF mismatch at {contig}:{pos + 1}. Check that the VCF uses GRCh38 and normalized plus-strand alleles."
                )
            phase_set = fmt.get("PS", "implicit")
            if phase_set == ".":
                phase_set = "implicit"
            variant = Variant(contig, pos, ref, alts, gt, "|" in gt_text, phase_set)
            if variant in seen:
                counts["duplicate_calls"] += 1
                continue
            seen.add(variant)
            for gene in genes:
                per_gene[gene].append(variant)
            counts["eligible_variant_records"] += 1
            if len(gt) == 1:
                counts["haploid_calls"] += 1
            elif gt[0] != gt[1]:
                counts["phased_heterozygotes" if variant.phased else "unphased_heterozygotes"] += 1
    progress(stage="scanning", counts=dict(counts))
    return per_gene, dict(counts)


def trimmed_edit(pos, ref, alt):
    """Keep shared anchors outside the edit so indels preserve the correct TSS."""
    while ref and alt and ref[0] == alt[0]:
        pos, ref, alt = pos + 1, ref[1:], alt[1:]
    while ref and alt and ref[-1] == alt[-1]:
        ref, alt = ref[:-1], alt[:-1]
    return pos, ref, alt


def apply_haplotype(sequence, start, tss, strand, variants, draw, haplotype, flank=FLANK):
    edits = set()
    for variant in variants:
        allele = variant.allele(draw, haplotype)
        if allele != variant.ref:
            edits.add(trimmed_edit(variant.pos, variant.ref, allele))
    parts, cursor, shift = [], start, 0
    previous = None
    for pos, ref, alt in sorted(edits):
        if pos < start or pos + len(ref) > start + len(sequence):
            raise InputError(
                "An allele crosses the available reference flank; cannot safely construct this window."
            )
        if pos < cursor or (previous is not None and pos == previous):
            raise InputError(
                f"Overlapping alleles in one haplotype at genomic position {pos + 1}; normalize or reconcile the VCF."
            )
        if sequence[pos - start : pos - start + len(ref)] != ref:
            raise InputError("Allele/reference mismatch while assembling a haplotype.")
        if len(ref) != len(alt) and pos <= tss < pos + len(ref):
            raise InputError(
                "A called indel removes or replaces the annotated TSS. This model requires a surviving reference TSS."
            )
        if pos + len(ref) <= tss:
            shift += len(alt) - len(ref)
        parts.extend((sequence[cursor - start : pos - start], alt))
        cursor, previous = pos + len(ref), pos
    parts.append(sequence[cursor - start :])
    edited = "".join(parts)
    center = tss - start + shift
    left = center - flank + (1 if strand == "-" else 0)
    right = left + 2 * flank
    if left < 0 or right > len(edited):
        raise InputError(
            "Cumulative indels exhaust the 2,048-bp safety flank; this window cannot be inferred."
        )
    window = edited[left:right]
    return reverse_complement(window) if strand == "-" else window


def gene_sequences(fasta, row, variants, draws):
    contig, start, end = row["contig"], row["window_start0"], row["window_end0"]
    native = fasta.fetch(contig, start, end).upper()
    if row["strand"] == "-":
        native = reverse_complement(native)
    if hashlib.sha256(native.encode()).hexdigest() != row["sequence_sha256"]:
        raise InputError(
            f"Reference checksum mismatch for {row['gene_name']}; use the pinned Ensembl 116 GRCh38 FASTA."
        )
    expanded_start = max(0, start - PADDING)
    expanded_end = min(fasta.get_reference_length(contig), end + PADDING)
    expanded = fasta.fetch(contig, expanded_start, expanded_end).upper()
    windows = []
    for draw in range(draws):
        pair = [
            apply_haplotype(expanded, expanded_start, row["tss0"], row["strand"], variants, draw, h)
            for h in (0, 1)
        ]
        for sequence in pair:
            reason = window_qc(sequence)
            if reason:
                raise InputError(f"Sequence QC failed for {row['gene_name']}: {reason}.")
        windows.append(pair)
    return native, windows


class FrozenPredictor:
    def __init__(self, root):
        root = Path(root)
        self.root = root
        self.specs = json.loads(
            (root / "docs/gi-lifespan-predictor/human_prediction_frozen.json").read_text()
        )
        with (root / "docs/gi-lifespan-predictor/coefficients.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.weights = {model: {} for model in MODELS}
        for row in rows:
            if row["model"] in self.weights:
                self.weights[row["model"]][row["human_gene_id"]] = {
                    k: v if k in {"human_gene_id", "gene_name", "model"} else float(v)
                    for k, v in row.items()
                }
        with (root / "docs/gi-longevity-final/reference_human_expression.csv").open() as stream:
            expression = next(csv.DictReader(stream))
        self.expression = {
            gene: float(value)
            for gene, value in expression.items()
            if gene.startswith("ENSG") and value and math.isfinite(float(value))
        }
        with (root / "docs/gi-longevity-final/reference_human_tss.csv").open() as stream:
            self.windows = {
                row["human_gene_id"]: dict(
                    row, **{k: int(row[k]) for k in ("tss0", "window_start0", "window_end0")}
                )
                for row in csv.DictReader(stream)
            }
        for model in MODELS:
            if not math.isclose(
                math.exp(self.log_prediction(model)),
                self.specs[model]["predicted_years"],
                rel_tol=1e-12,
            ):
                raise RuntimeError("Frozen model/reference artifacts disagree.")

    def model_windows(self, model):
        return [self.windows[g] for g in self.weights[model] if g in self.windows]

    def log_prediction(self, model, replacements=None):
        replacements = replacements or {}
        terms = []
        for gene, weight in self.weights[model].items():
            value = replacements.get(gene, self.expression.get(gene, weight["training_median"]))
            if not math.isfinite(value):
                raise InputError("Non-finite expression estimate.")
            terms.append(
                weight["coefficient_standardized"]
                * (value - weight["training_mean"])
                / weight["training_scale"]
            )
        return self.specs[model]["intercept_log_years"] + math.fsum(terms)


def aggregate_expression(values):
    """Mean TPM across haplotypes/draws, returned in the GI log1p(TPM) units."""
    # log(mean(exp(x))) is exactly log1p(mean(expm1(x))); stable at large x.
    maximum = max(values)
    return maximum + math.log(math.fsum(math.exp(x - maximum) for x in values) / len(values))


def compare_predictions(predictor, model, plan, expressions, draws):
    native, modified, draw_vectors, rows = {}, {}, [{} for _ in range(draws)], []
    for item in plan:
        gene = item["human_gene_id"]
        reference = expressions[item["native_hash"]]
        pairs = [[expressions[h] for h in pair] for pair in item["haplotype_hashes"]]
        changed = aggregate_expression([x for pair in pairs for x in pair])
        native[gene], modified[gene] = reference, changed
        for draw, pair in enumerate(pairs):
            draw_vectors[draw][gene] = aggregate_expression(pair)
        weight = predictor.weights[model][gene]
        contribution = (
            weight["coefficient_standardized"] * (changed - reference) / weight["training_scale"]
        )
        rows.append(
            {
                "human_gene_id": gene,
                "gene_name": weight["gene_name"],
                "variant_records": item["variant_records"],
                "reference_expression_log1p_tpm": reference,
                "modified_expression_log1p_tpm": changed,
                "expression_delta": changed - reference,
                "log_lifespan_contribution": contribution,
                "coefficient_standardized": weight["coefficient_standardized"],
                "training_scale": weight["training_scale"],
            }
        )
    ref_log, alt_log = (
        predictor.log_prediction(model, native),
        predictor.log_prediction(model, modified),
    )
    phase_percent = [
        100 * math.expm1(predictor.log_prediction(model, vector) - ref_log)
        for vector in draw_vectors
    ]
    return {
        "reference_predicted_years": math.exp(ref_log),
        "modified_predicted_years": math.exp(alt_log),
        "percent_change": 100 * math.expm1(alt_log - ref_log),
        "frozen_reference_predicted_years": predictor.specs[model]["predicted_years"],
        "log_lifespan_change": alt_log - ref_log,
        "phase_percent_range": [min(phase_percent), max(phase_percent)],
        "genes": sorted(rows, key=lambda x: abs(x["log_lifespan_contribution"]), reverse=True),
        "features_total": len(predictor.weights[model]),
        "features_with_reference_windows": len(predictor.model_windows(model)),
        "median_imputed_gene_ids": sorted(
            set(predictor.weights[model]) - set(predictor.expression)
        ),
    }
