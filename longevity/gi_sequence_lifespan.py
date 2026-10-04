"""Extract annotated TSS windows, obtain fixed-context GI expression, predict lifespan."""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from longevity.gi_api import Client, api_key, payload, request_hash, validate_response
from longevity.gi_lifespan_predictor import MODELS, predict_exported
from longevity.gi_prepare import digest, oriented_window, write_json

CONFIG = {
    "model": "g0-expression-8192",
    "description": "Homo sapiens primary hepatocytes, untreated, bulk RNA-seq.",
    "sequence_name": "orthologue_tss_window",
    "flank_bp": 40960,
}


def records(path):
    """Read plain/gzip FASTA, one sequence at a time; IDs end at first whitespace."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as handle:
        name, parts = None, []
        for line in handle:
            if line.startswith(">"):
                if name is not None:
                    yield name, "".join(parts).upper()
                name, parts = line[1:].split()[0], []
            elif line.strip():
                if name is None:
                    raise ValueError("Sequence precedes first FASTA header")
                parts.append(line.strip())
        if name is not None:
            yield name, "".join(parts).upper()


def window_qc(sequence):
    if len(sequence) != 81920:
        return "length_must_be_81920"
    if set(sequence) - set("ACGTN"):
        return "unsupported_dna_alphabet"
    if sequence.count("N") / len(sequence) > 0.01:
        return "more_than_one_percent_N"
    if "N" in sequence[40960 - 4599 : 40960 + 4599]:
        return "N_in_central_9198_bp"
    return None


def wanted_genes(model_dir, model):
    weights = pd.read_csv(Path(model_dir) / "coefficients.csv")
    if model != "both":
        weights = weights[weights.model == model]
    return set(weights.human_gene_id)


def prepare(args):
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    table = pd.read_csv(args.tss_map, dtype={"contig": str})
    required = {"human_gene_id", "contig", "tss0", "strand"}
    if not required.issubset(table):
        raise ValueError(f"TSS map requires columns {sorted(required)}")
    table = table[table.human_gene_id.isin(wanted_genes(args.model_dir, args.model))].copy()
    if table.empty or table.human_gene_id.duplicated().any():
        raise ValueError("TSS map must contain unique fitted gene IDs")
    if (
        not table.strand.isin(["+", "-"]).all()
        or table.tss0.isna().any()
        or (table.tss0 < 0).any()
        or (table.tss0 != table.tss0.astype(int)).any()
    ):
        raise ValueError("TSS coordinates must be nonnegative integers and strand + or -")
    groups = {name: group for name, group in table.groupby("contig")}
    seen, qc = set(), []
    target = out / "windows.fasta.gz"
    temporary = target.with_suffix(".tmp")
    with gzip.open(temporary, "wt") as handle:
        for contig, sequence in records(args.genome):
            if contig not in groups:
                continue
            if contig in seen:
                raise ValueError(f"Duplicate genome contig: {contig}")
            seen.add(contig)
            for row in groups[contig].to_dict("records"):
                window = oriented_window(sequence, int(row["tss0"]), row["strand"], 40960)
                reason = "insufficient_real_flank" if window is None else window_qc(window)
                sha = hashlib.sha256(window.encode()).hexdigest() if window is not None else ""
                expected = row.get("sequence_sha256")
                if pd.notna(expected) and expected and sha != expected:
                    raise ValueError(
                        f"Reference sequence checksum mismatch for {row['human_gene_id']}"
                    )
                qc.append(dict(row, status=reason or "ok", observed_sequence_sha256=sha))
                if reason is None:
                    handle.write(f">{row['human_gene_id']}\n{window}\n")
        for contig in set(groups) - seen:
            qc.extend(
                dict(row, status="contig_missing") for row in groups[contig].to_dict("records")
            )
    pd.DataFrame(qc).to_csv(out / "sequence_qc.csv", index=False)
    if not any(row["status"] == "ok" for row in qc):
        raise ValueError("No valid TSS windows; inspect sequence_qc.csv")
    temporary.replace(target)
    write_json(
        out / "sequence_provenance.json",
        {
            "genome_sha256": digest(args.genome),
            "tss_map_sha256": digest(args.tss_map),
            "windows_sha256": digest(target),
            "model": args.model,
            "valid_windows": sum(row["status"] == "ok" for row in qc),
            "rejected_windows": sum(row["status"] != "ok" for row in qc),
            "config": CONFIG,
            "code_sha256": digest(__file__),
        },
    )
    print(target, flush=True)


def infer_one(row, client, cache, cache_only):
    body = payload(CONFIG, row["sequence"])
    key = request_hash(body)
    path = Path(cache) / f"{key}.json"
    if cache_only:
        if not path.exists():
            raise FileNotFoundError(f"No cached response for {row['gene_id']}: {key}")
        response = json.loads(path.read_text())
        status = "cached"
    else:
        result = client.predict(row)
        if result["status"] not in ("ok", "cached"):
            raise RuntimeError(f"GI prediction failed for {row['gene_id']}; response not used")
        response = json.loads(Path(result["response_path"]).read_text())
        status = result["status"]
    value = validate_response(CONFIG, response)
    return {
        "human_gene_id": row["gene_id"],
        "expression_log_tpm": value,
        "sequence_sha256": row["sequence_sha256"],
        "request_hash": key,
        "status": status,
    }


def predict(args):
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        predict_locked(args, out)


def predict_locked(args, out):
    cache = Path(args.cache or out / "responses")
    client = (
        None if args.cache_only else Client(CONFIG, cache, api_key(args.env_file), args.max_rps)
    )
    wanted = wanted_genes(args.model_dir, args.model)
    seen, rows, batch = set(), [], []

    def flush(pool):
        futures = [pool.submit(infer_one, row, client, cache, args.cache_only) for row in batch]
        rows.extend(f.result() for f in futures)
        batch.clear()
        print(f"Expression complete: {len(rows)} genes", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for gene, sequence in records(args.windows):
            if gene not in wanted:
                continue
            if gene in seen:
                raise ValueError(f"Duplicate window for gene {gene}")
            seen.add(gene)
            reason = window_qc(sequence)
            if reason:
                raise ValueError(f"Invalid window for {gene}: {reason}")
            batch.append(
                {
                    "sequence": sequence,
                    "gene_id": gene,
                    "ensembl_species": args.sample_id,
                    "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                }
            )
            if len(batch) >= args.workers * 2:
                flush(pool)
        if batch:
            flush(pool)
    if not rows:
        raise ValueError("No fitted genes found in the window FASTA")
    predictions = pd.DataFrame(rows).sort_values("human_gene_id")
    predictions.to_csv(out / "gi_predictions.csv", index=False)
    expression = predictions.set_index("human_gene_id")["expression_log_tpm"].to_frame().T
    expression.index = pd.Index([args.sample_id], name="ensembl_species")
    expression.to_csv(out / "expression.csv")
    results = []
    for model in MODELS if args.model == "both" else [args.model]:
        frame = predict_exported(args.model_dir, expression, model).reset_index()
        frame["model"] = model
        results.append(frame)
    result = pd.concat(results, ignore_index=True)
    result.to_csv(out / "lifespan_predictions.csv", index=False)
    write_json(
        out / "prediction_provenance.json",
        {
            "sample_id": args.sample_id,
            "config": CONFIG,
            "windows_sha256": digest(args.windows),
            "coefficients_sha256": digest(Path(args.model_dir) / "coefficients.csv"),
            "frozen_model_sha256": digest(Path(args.model_dir) / "human_prediction_frozen.json"),
            "code_sha256": digest(__file__),
            "cache_only": args.cache_only,
            "request_status_counts": predictions.status.value_counts().to_dict(),
            "missing_fitted_genes_imputed": sorted(wanted - seen),
        },
    )
    print(result.to_string(index=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    stages = parser.add_subparsers(dest="stage", required=True)
    for name in ["prepare", "predict"]:
        command = stages.add_parser(name)
        command.add_argument("--model-dir", default="docs/gi-lifespan-predictor")
        command.add_argument("--model", choices=[*MODELS, "both"], default="fdr_genes_ridge")
        command.add_argument("--output", required=True)
        if name == "prepare":
            command.add_argument("--genome", required=True)
            command.add_argument("--tss-map", required=True)
        else:
            command.add_argument("--windows", required=True)
            command.add_argument("--sample-id", required=True)
            command.add_argument("--cache")
            command.add_argument("--cache-only", action="store_true")
            command.add_argument("--env-file", default=".env")
            command.add_argument("--workers", type=int, default=4)
            command.add_argument("--max-rps", type=float, default=1.5)
    args = parser.parse_args()
    if args.stage == "predict" and (args.workers < 1 or args.max_rps <= 0):
        parser.error("workers and max-rps must be positive")
    (prepare if args.stage == "prepare" else predict)(args)


if __name__ == "__main__":
    main()
