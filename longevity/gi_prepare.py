"""Prepare frozen, human-anchored mammalian orthologues and GI expression windows.

Stages: discover -> orthologues -> prepare -> finalize. No expression inference here.
All source downloads and outputs live outside Git under the configured dataset root.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

import numpy as np
import pandas as pd
import requests


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def download(url, dest):
    """Atomically cache downloads; interrupted transfers never become source files."""
    dest = Path(dest)
    if dest.exists() and dest.with_suffix(dest.suffix + ".source.json").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(5):
        try:
            with requests.get(url, stream=True, timeout=(30, 120)) as r:
                r.raise_for_status()
                h = hashlib.sha256()
                with tmp.open("wb") as f:
                    for block in r.iter_content(4 << 20):
                        f.write(block)
                        h.update(block)
                expected = r.headers.get("Content-Length")
                if expected and not r.headers.get("Content-Encoding"):
                    assert tmp.stat().st_size == int(expected), "incomplete download"
                tmp.replace(dest)
                write_json(
                    dest.with_suffix(dest.suffix + ".source.json"),
                    {
                        "url": url,
                        "sha256": h.hexdigest(),
                        "bytes": dest.stat().st_size,
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "etag": r.headers.get("ETag"),
                        "last_modified": r.headers.get("Last-Modified"),
                    },
                )
                return dest
        except (requests.RequestException, AssertionError) as exc:
            if (
                isinstance(exc, requests.HTTPError)
                and exc.response is not None
                and 400 <= exc.response.status_code < 500
                and exc.response.status_code != 429
            ):
                raise
            if attempt == 4:
                raise
            time.sleep(2**attempt)


def listing(url):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return [urljoin(url, x) for x in re.findall(r'href="([^"]+)"', r.text)]


def load_config(path):
    c = json.loads(Path(path).read_text())
    out = Path(c["output"])
    out.mkdir(parents=True, exist_ok=True)
    frozen = out / "config.json"
    if frozen.exists() and json.loads(frozen.read_text()) != c:
        raise ValueError("Configuration changed: use a new output directory.")
    write_json(frozen, c)
    return c, out


def select_cohort(candidates, limit, order_limit):
    """Round-robin orders; span mass-adjusted lifespan within orders, without GI scores."""
    d = candidates.copy()
    x = np.log10(d.adult_weight_g.to_numpy(float))
    y = np.log10(d.max_longevity_yrs.to_numpy(float))
    d["selection_longevity_residual"] = y - np.polyval(np.polyfit(x, y, 1), x)
    queues = {}
    for order, group in d.groupby("anage_order"):
        remaining = group.sort_values(["selection_longevity_residual", "ensembl_species"])
        chosen = []
        # Include the human anchor, then choose low/high extremes and largest residual gaps.
        if "homo_sapiens" in set(remaining.ensembl_species):
            i = remaining.index[remaining.ensembl_species.eq("homo_sapiens")][0]
            chosen.append(i)
            remaining = remaining.drop(i)
        if len(remaining):
            i = remaining.index[0]
            chosen.append(i)
            remaining = remaining.drop(i)
        if len(remaining):
            i = remaining.index[-1]
            chosen.append(i)
            remaining = remaining.drop(i)
        while len(remaining) and len(chosen) < order_limit:
            distances = np.abs(
                remaining.selection_longevity_residual.to_numpy()[:, None]
                - d.loc[chosen, "selection_longevity_residual"].to_numpy()
            ).min(1)
            i = remaining.index[int(np.argmax(distances))]
            chosen.append(i)
            remaining = remaining.drop(i)
        queues[order] = chosen[:order_limit]
    picked = []
    while len(picked) < limit and any(queues.values()):
        for order in sorted(queues):
            if queues[order] and len(picked) < limit:
                picked.append(queues[order].pop(0))
    return d.loc[picked].sort_values(["anage_order", "ensembl_species"]).reset_index(drop=True)


def discover(c, out):
    raw = out / "sources"
    cat = download(
        "https://rest.ensembl.org/info/species?content-type=application/json",
        raw / "ensembl_species.json",
    )
    catalog = json.loads(cat.read_text())["species"]
    a = pd.read_parquet(c["anage_table"])
    a = a[
        a.anage_class.eq("Mammalia")
        & a.data_quality.isin(["high", "acceptable"])
        & a.adult_weight_g.gt(0)
        & a.max_longevity_yrs.gt(0)
    ].copy()
    # No automatic resolution of divergent labels sharing one species taxid.
    dup = a[a.duplicated("ncbi_taxid", keep=False)]
    dup.to_csv(out / "excluded_ambiguous_labels.csv", index=False)
    a = a[~a.ncbi_taxid.isin(dup.ncbi_taxid)].set_index("ncbi_taxid")
    rows = []
    for s in catalog:
        tid = int(s["taxon_id"])
        if tid not in a.index or s["release"] != c["ensembl_release"]:
            continue
        # Reference/default assemblies only; never count strains as species replicates.
        if s.get("strain") and not s["strain"].startswith("reference"):
            continue
        if "_gca" in s["name"] or s["name"] == "heterocephalus_glaber_male":
            continue
        if "core" not in s["groups"]:
            continue
        r = a.loc[tid].to_dict()
        r.update(
            ncbi_taxid=tid,
            ensembl_species=s["name"],
            ensembl_assembly=s["assembly"],
            ensembl_accession=s.get("accession"),
            ensembl_release=s["release"],
        )
        rows.append(r)
    d = pd.DataFrame(rows).sort_values("ensembl_species").drop_duplicates("ncbi_taxid")
    d.to_csv(out / "candidates.csv", index=False)
    # Assess all candidates before selecting the final <=50-species analysis cohort.
    d.to_csv(out / "cohort.csv", index=False)
    d.to_parquet(out / "cohort.parquet", index=False)
    write_json(
        out / "cohort_provenance.json",
        {
            "anage_table": c["anage_table"],
            "anage_sha256": digest(c["anage_table"]),
            "selection": "round-robin orders, greedy coverage of OLS mass-adjusted log lifespan",
            "selection_uses_expression": False,
            "candidate_species": len(d),
            "preparation_species": len(d),
            "target_analysis_species": c["max_species"],
            "orders": d.anage_order.value_counts().to_dict(),
            "ambiguous_label_taxids_excluded": sorted(set(dup.ncbi_taxid.astype(int))),
            "annotation": "Ensembl canonical protein-coding transcript, exclude incomplete start/end",
            "orthology": "human-anchored high-confidence ortholog_one2one; no paralogue pooling",
        },
    )
    log(
        f"Preparing {len(d)} mammal candidates / {d.anage_order.nunique()} orders before final annotation QC"
    )


def normalize_homologies(chunk, wanted):
    f = chunk[
        (chunk.homology_type == "ortholog_one2one")
        & (pd.to_numeric(chunk.is_high_confidence, errors="coerce") == 1)
    ]
    frames = []
    for human_first in [True, False]:
        source = "species" if human_first else "homology_species"
        target = "homology_species" if human_first else "species"
        human_id = "gene_stable_id" if human_first else "homology_gene_stable_id"
        gene_id = "homology_gene_stable_id" if human_first else "gene_stable_id"
        z = f[f[source].eq("homo_sapiens") & f[target].isin(wanted)]
        frames.append(
            pd.DataFrame(
                {"human_gene_id": z[human_id], "gene_id": z[gene_id], "ensembl_species": z[target]}
            )
        )
    return pd.concat(frames, ignore_index=True)


def collect_homology_export(c, out, species, wanted):
    release = c["ensembl_release"]
    name = f"Compara.{release}.protein_default.homologies.tsv.gz"
    url = f"https://ftp.ensembl.org/pub/release-{release}/tsv/ensembl-compara/homologies/{species}/{name}"
    path = out / "sources" / name if species == "homo_sapiens" else out / "sources" / species / name
    saved = out / "homology_exports" / f"{species}.parquet"
    saved.parent.mkdir(exist_ok=True)
    if saved.exists():
        return pd.read_parquet(saved)
    try:
        download(url, path)
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise
        write_json(
            saved.with_suffix(".unavailable.json"),
            {
                "url": url,
                "http_status": 404,
                "meaning": "No partner orthology export in the pinned Ensembl release",
            },
        )
        return pd.DataFrame(columns=["human_gene_id", "gene_id", "ensembl_species"])
    frames = []
    columns = [
        "species",
        "homology_species",
        "gene_stable_id",
        "homology_gene_stable_id",
        "homology_type",
        "is_high_confidence",
    ]
    for chunk in pd.read_csv(path, sep="\t", chunksize=250000, usecols=columns, low_memory=False):
        frames.append(normalize_homologies(chunk, wanted))
    d = pd.concat(frames, ignore_index=True).drop_duplicates()
    d.to_parquet(saved.with_suffix(".parquet.tmp"), index=False)
    saved.with_suffix(".parquet.tmp").replace(saved)
    log(f"Collected human orthologies from {species} export: {len(d):,}")
    return d


def mappings_fingerprint(mappings):
    data = (
        mappings[["human_gene_id", "gene_id", "ensembl_species"]]
        .sort_values(["ensembl_species", "human_gene_id", "gene_id"])
        .to_csv(index=False)
    )
    return hashlib.sha256(data.encode()).hexdigest()


def orthologues(c, out, workers=12):
    cohort = pd.read_parquet(out / "cohort.parquet")
    wanted = set(cohort.ensembl_species)
    url = f"https://ftp.ensembl.org/pub/release-{c['ensembl_release']}/tsv/ensembl-compara/homologies/README.gene_trees.tsv_dumps.txt"
    download(url, out / "sources" / "README.homology_exports.txt")
    frames = []
    # Ensembl partitions orthologies arbitrarily between the two species exports.
    # BOTH files are required, irrespective of source/target direction within a row.
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(collect_homology_export, c, out, s, wanted) for s in sorted(wanted)
        ]
        for future in as_completed(futures):
            frames.append(future.result())
    d = pd.concat(frames, ignore_index=True).drop_duplicates()
    # Reject any inconsistency between otherwise one-to-one records.
    ambiguous = d.duplicated(["ensembl_species", "human_gene_id"], keep=False)
    ambiguous |= d.duplicated(["ensembl_species", "gene_id"], keep=False)
    d[ambiguous].to_csv(out / "excluded_ambiguous_orthologues.csv", index=False)
    d = d[~ambiguous]
    if "homo_sapiens" in wanted:
        ids = sorted(set(d.human_gene_id))
        d = pd.concat(
            [
                d,
                pd.DataFrame(
                    {"human_gene_id": ids, "gene_id": ids, "ensembl_species": "homo_sapiens"}
                ),
            ],
            ignore_index=True,
        )
    d.to_parquet(out / "orthologues.parquet.tmp", index=False)
    (out / "orthologues.parquet.tmp").replace(out / "orthologues.parquet")
    d.groupby("ensembl_species").size().to_csv(
        out / "orthologue_counts.csv", header=["orthologues"]
    )
    log(f"Mapped {len(d):,} species-gene pairs / {d.human_gene_id.nunique():,} human anchor genes")


def attributes(text):
    result = {}
    for key, value in re.findall(r'(\S+) "([^"]*)"', text):
        result.setdefault(key, []).append(value)
    return result


def transcript_table(gtf):
    rows, excluded = [], []
    with gzip.open(gtf, "rt") as f:
        for line in f:
            if line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if fields[2] != "transcript":
                continue
            a = attributes(fields[8])
            if a.get("gene_biotype") != ["protein_coding"]:
                continue
            if a.get("transcript_biotype") != ["protein_coding"]:
                continue
            if "Ensembl_canonical" not in a.get("tag", []):
                continue
            row = {
                "gene_id": a["gene_id"][0],
                "transcript_id": a["transcript_id"][0],
                "gene_name": a.get("gene_name", [""])[0],
                "contig": fields[0],
                "strand": fields[6],
                "transcript_start0": int(fields[3]) - 1,
                "transcript_end0": int(fields[4]),
                "transcript_support_level": a.get("transcript_support_level", [""])[0],
            }
            row["tss0"] = (
                row["transcript_start0"] if row["strand"] == "+" else row["transcript_end0"] - 1
            )
            if set(a.get("tag", [])) & {
                "mRNA_start_NF",
                "mRNA_end_NF",
                "cds_start_NF",
                "cds_end_NF",
            }:
                excluded.append(dict(row, reason="incomplete_canonical_transcript"))
            else:
                rows.append(row)
    d = pd.DataFrame(rows)
    if d.empty:
        raise ValueError(f"No eligible Ensembl canonical transcripts: {gtf}")
    duplicates = d[d.duplicated("gene_id", keep=False)]
    excluded.extend(
        dict(r, reason="ambiguous_canonical_transcript") for r in duplicates.to_dict("records")
    )
    return d[~d.gene_id.isin(duplicates.gene_id)], pd.DataFrame(excluded)


def window_bounds(tss0, strand, flank):
    if strand not in ("+", "-"):
        raise ValueError(f"Invalid strand: {strand}")
    shift = int(strand == "-")
    return tss0 - flank + shift, tss0 + flank + shift


def oriented_window(seq, tss0, strand, flank):
    start, end = window_bounds(tss0, strand, flank)
    if start < 0 or end > len(seq):
        return None
    window = seq[start:end].upper()
    if strand == "-":
        window = window.translate(str.maketrans("ACGTN", "TGCAN"))[::-1]
    return window


def fasta_records(path):
    """One contig at a time, never hold an entire mammalian genome in memory."""
    with gzip.open(path, "rt") as f:
        name, parts = None, []
        for line in f:
            if line.startswith(">"):
                if name is not None:
                    yield name, "".join(parts)
                name, parts = line[1:].split()[0], []
            else:
                parts.append(line.strip())
        if name is not None:
            yield name, "".join(parts)


def prepare_species(c, out, species):
    import pyarrow as pa
    import pyarrow.parquet as pq

    final = out / "windows" / f"{species}.parquet"
    marker = out / "qc" / f"{species}.json"
    mappings = pd.read_parquet(
        out / "orthologues.parquet", filters=[("ensembl_species", "=", species)]
    )
    mapping_hash = mappings_fingerprint(mappings)
    if final.exists() and marker.exists():
        previous = json.loads(marker.read_text())
        if previous.get("orthologue_mapping_sha256") == mapping_hash:
            return previous
    release = c["ensembl_release"]
    root = f"https://ftp.ensembl.org/pub/release-{release}"
    gtf_urls = [u for u in listing(f"{root}/gtf/{species}/") if u.endswith(f".{release}.gtf.gz")]
    assert len(gtf_urls) == 1, (species, gtf_urls)
    dna_urls = [
        u
        for u in listing(f"{root}/fasta/{species}/dna/")
        if u.endswith(".dna.primary_assembly.fa.gz")
    ]
    if not dna_urls:
        dna_urls = [
            u for u in listing(f"{root}/fasta/{species}/dna/") if u.endswith(".dna.toplevel.fa.gz")
        ]
    assert len(dna_urls) == 1, (species, dna_urls)
    cohort = pd.read_parquet(out / "cohort.parquet")
    row = cohort[cohort.ensembl_species.eq(species)].iloc[0]
    # Both filenames must explicitly name the same assembly frozen in the catalog.
    assert f".{row.ensembl_assembly}." in gtf_urls[0], gtf_urls[0]
    assert f".{row.ensembl_assembly}." in dna_urls[0], dna_urls[0]
    source = out / "sources" / species
    gtf = download(gtf_urls[0], source / gtf_urls[0].rsplit("/", 1)[1])
    transcripts, rejected_tx = transcript_table(gtf)
    mappings = pd.read_parquet(
        out / "orthologues.parquet", filters=[("ensembl_species", "=", species)]
    )
    genes = mappings.merge(transcripts, on="gene_id", how="inner", validate="one_to_one")
    (out / "annotations").mkdir(exist_ok=True)
    genes.to_parquet(out / "annotations" / f"{species}.parquet", index=False)
    (out / "qc").mkdir(exist_ok=True)
    rejected_tx.to_csv(out / "qc" / f"{species}.transcript_exclusions.csv", index=False)
    dna = download(dna_urls[0], source / dna_urls[0].rsplit("/", 1)[1])
    final.parent.mkdir(exist_ok=True)
    temp = final.with_suffix(".parquet.part")
    by_contig = {str(k): g for k, g in genes.groupby("contig")}
    seen, rejected, buffer, metadata = set(), [], [], []
    flank, writer = c["flank_bp"], None

    def flush():
        nonlocal writer, buffer
        if not buffer:
            return
        table = pa.Table.from_pylist(buffer)
        if writer is None:
            writer = pq.ParquetWriter(temp, table.schema, compression="zstd", compression_level=3)
        writer.write_table(table, row_group_size=64)
        buffer = []

    try:
        for contig, seq in fasta_records(dna):
            if contig not in by_contig:
                continue
            seen.add(contig)
            for g in by_contig[contig].to_dict("records"):
                window = oriented_window(seq, g["tss0"], g["strand"], flank)
                reason = None
                if window is None:
                    reason = "insufficient_real_flank_at_contig_edge"
                elif set(window) - set("ACGTN"):
                    reason = "unsupported_dna_alphabet"
                elif window.count("N") / len(window) > c["max_window_n_fraction"]:
                    reason = "window_n_fraction"
                elif (
                    window[flank - 4599 : flank + 4599].count("N") / 9198 > c["max_core_n_fraction"]
                ):
                    reason = "core_n_fraction"
                if reason:
                    rejected.append(dict(g, reason=reason))
                    continue
                start, end = window_bounds(g["tss0"], g["strand"], flank)
                rec = dict(
                    g,
                    ncbi_taxid=int(row.ncbi_taxid),
                    ensembl_assembly=row.ensembl_assembly,
                    window_start0=start,
                    window_end0=end,
                    tss_index=flank,
                    sequence_sha256=hashlib.sha256(window.encode()).hexdigest(),
                    gc_fraction=(window.count("G") + window.count("C")) / len(window),
                    n_fraction=window.count("N") / len(window),
                )
                metadata.append(rec)
                buffer.append(dict(rec, sequence=window))
                if len(buffer) >= 64:
                    flush()
        flush()
    finally:
        if writer:
            writer.close()
    for contig in set(by_contig) - seen:
        rejected.extend(
            dict(r, reason="contig_not_in_primary_fasta")
            for r in by_contig[contig].to_dict("records")
        )
    if metadata:
        temp.replace(final)
    else:
        # Empty species are documented exclusions, not failed downloads or zero expression.
        pd.DataFrame(columns=["gene_id", "sequence"]).to_parquet(final, index=False)
    (out / "window_metadata").mkdir(exist_ok=True)
    pd.DataFrame(metadata).to_parquet(out / "window_metadata" / f"{species}.parquet", index=False)
    pd.DataFrame(rejected).to_csv(out / "qc" / f"{species}.window_exclusions.csv", index=False)
    result = {
        "species": species,
        "orthologue_mapping_sha256": mapping_hash,
        "orthologues": len(mappings),
        "canonical_transcripts": len(transcripts),
        "mapped_canonical_transcripts": len(genes),
        "valid_windows": len(metadata),
        "window_exclusions": pd.Series([r["reason"] for r in rejected], dtype=str)
        .value_counts()
        .to_dict(),
        "window_sha256": digest(final),
        "annotation_sha256": digest(gtf),
        "fasta_sha256": json.loads(dna.with_suffix(dna.suffix + ".source.json").read_text())[
            "sha256"
        ],
    }
    write_json(marker, result)
    log(f"{species}: {len(metadata):,} valid windows / {len(mappings):,} orthologues")
    return result


def finalize(c, out, analysis):
    view = out / "analyses" / analysis
    view.mkdir(parents=True, exist_ok=True)
    min_windows = 5000 if analysis == "core" else c["min_species_valid_windows"]
    cohort = pd.read_parquet(out / "cohort.parquet")
    missing = [s for s in cohort.ensembl_species if not (out / "qc" / f"{s}.json").exists()]
    if missing:
        raise ValueError(f"Preparation incomplete for {len(missing)} species: {missing}")
    qc = {s: json.loads((out / "qc" / f"{s}.json").read_text()) for s in cohort.ensembl_species}
    tree_map = pd.read_csv(out / "phylogeny" / "taxon_mapping.csv")
    tree_species = set(tree_map.loc[tree_map.tree_tip.notna(), "ensembl_species"])
    excluded = []
    for row in cohort.to_dict("records"):
        reasons = []
        if qc[row["ensembl_species"]]["valid_windows"] < min_windows:
            reasons.append(f"fewer_than_{min_windows}_valid_windows")
        if row["ensembl_species"] not in tree_species:
            reasons.append("no_exact_species_in_dated_tree")
        if reasons:
            excluded.append(dict(row, reasons=";".join(reasons)))
    pd.DataFrame(excluded).to_csv(view / "excluded_species_qc.csv", index=False)
    excluded_species = {r["ensembl_species"] for r in excluded}
    cohort = cohort[~cohort.ensembl_species.isin(excluded_species)]
    if len(cohort) < 20:
        raise ValueError(f"Insufficient species for {analysis}: {len(cohort)}")
    if len(cohort) > c["max_species"]:
        cohort = select_cohort(cohort, c["max_species"], c["max_species_per_order"])
    cohort.to_parquet(view / "analysis_cohort.parquet", index=False)
    cohort.to_csv(view / "analysis_cohort.csv", index=False)
    d = pd.concat(
        [pd.read_parquet(out / "window_metadata" / f"{s}.parquet") for s in cohort.ensembl_species],
        ignore_index=True,
    )
    d = d.merge(
        cohort[
            [
                "ensembl_species",
                "anage_order",
                "anage_family",
                "max_longevity_yrs",
                "adult_weight_g",
                "data_quality",
                "sample_size",
                "specimen_origin",
            ]
        ],
        on="ensembl_species",
        validate="many_to_one",
    )
    coverage = d.groupby("human_gene_id").agg(
        species=("ensembl_species", "nunique"), orders=("anage_order", "nunique")
    )
    required_species = (
        math.ceil(c["min_gene_species_fraction"] * len(cohort)) if analysis == "core" else 30
    )
    coverage["eligible"] = (coverage.species >= required_species) & (
        coverage.orders >= c["min_gene_orders"]
    )
    coverage.to_csv(view / "gene_coverage.csv")
    eligible = set(coverage.index[coverage.eligible])
    manifest = d[d.human_gene_id.isin(eligible)].copy()
    manifest["request_id"] = manifest.ensembl_species + ":" + manifest.gene_id
    manifest.to_parquet(view / "prediction_manifest.parquet", index=False)
    manifest[
        [
            "request_id",
            "human_gene_id",
            "ensembl_species",
            "gene_id",
            "transcript_id",
            "sequence_sha256",
        ]
    ].to_csv(view / "prediction_manifest.csv", index=False)
    summary = {
        "species": len(cohort),
        "orders": cohort.anage_order.nunique(),
        "valid_windows_before_gene_coverage_filter": len(d),
        "eligible_genes": len(eligible),
        "prediction_requests": len(manifest),
        "model": c["model"],
        "description": c["description"],
        "analysis": analysis,
        "min_species_per_gene": required_species,
        "min_valid_windows_per_species": min_windows,
        "excluded_species": sorted(excluded_species),
        "manifest_sha256": digest(view / "prediction_manifest.parquet"),
        "config_sha256": digest(out / "config.json"),
        "status": "prepared_not_predicted",
    }
    cov = pd.read_csv(out / "phylogeny" / "brownian_covariance_myr.csv", index_col=0)
    names = list(cohort.ensembl_species)
    cov.loc[names, names].to_csv(view / "brownian_covariance_myr.csv")
    write_json(view / "summary.json", summary)
    log(json.dumps(summary))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage", choices=["discover", "orthologues", "prepare", "finalize"])
    p.add_argument("--config", default="configs/gi-hepatocytes.json")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--species", nargs="+")
    p.add_argument("--analysis", choices=["core", "broad", "both"], default="both")
    args = p.parse_args()
    c, out = load_config(args.config)
    if args.stage == "discover":
        discover(c, out)
    elif args.stage == "orthologues":
        orthologues(c, out, args.workers)
    elif args.stage == "finalize":
        for analysis in ["core", "broad"] if args.analysis == "both" else [args.analysis]:
            finalize(c, out, analysis)
    else:
        species = args.species or list(pd.read_parquet(out / "cohort.parquet").ensembl_species)
        priority = [
            "homo_sapiens",
            "rattus_norvegicus",
            "heterocephalus_glaber_female",
            "rhinolophus_ferrumequinum",
            "pan_troglodytes",
            "sus_scrofa",
        ]
        species = sorted(
            species, key=lambda s: priority.index(s) if s in priority else len(priority)
        )
        errors = []
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(prepare_species, c, out, s): s for s in species}
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001 -- record each failed species before exiting nonzero
                    errors.append({"species": futures[f], "error": str(e)})
                    log(f"FAILED {futures[f]}: {e}")
        write_json(out / "preparation_errors.json", errors)
        if errors:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
