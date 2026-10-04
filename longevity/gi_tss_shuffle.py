"""Paired GI inference after reproducible 8-bp block shuffling around the TSS."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from longevity.gi_api import Client, api_key, payload, request_hash, validate_response
from longevity.gi_prepare import digest, log, write_json


def sequence_hash(sequence):
    return hashlib.sha256(sequence.encode()).hexdigest()


def shuffle_blocks(sequence, tss, flank, block_bp, seed):
    """Permute fixed blocks within each TSS flank; keep N-containing blocks fixed."""
    if flank <= 0 or flank % block_bp or tss < flank or tss + flank > len(sequence):
        raise ValueError("Shuffle region must fit the input and contain whole blocks")
    rng = np.random.default_rng(seed)
    parts = [sequence[: tss - flank]]
    for start, end in [(tss - flank, tss), (tss, tss + flank)]:
        blocks = [sequence[i : i + block_bp] for i in range(start, end, block_bp)]
        movable = [i for i, block in enumerate(blocks) if "N" not in block]
        shuffled = blocks.copy()
        for target, source in zip(movable, rng.permutation(movable), strict=True):
            shuffled[target] = blocks[source]
        assert Counter(shuffled) == Counter(blocks)
        parts.append("".join(shuffled))
    parts.append(sequence[tss + flank :])
    result = "".join(parts)
    if len(result) != len(sequence) or Counter(result) != Counter(sequence):
        raise ValueError("Shuffle altered sequence length or composition")
    return result


def prepare(args):
    root = Path(args.dataset_root).resolve()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    candidates_path = Path(args.candidates).resolve()
    candidates = pd.read_csv(candidates_path)
    baseline = pd.read_csv(args.baseline)
    config = json.loads((root / "config.json").read_text())
    source_path = root / "analyses/broad/gene_windows.parquet"
    source = pd.read_parquet(
        source_path, filters=[("human_gene_id", "in", candidates.human_gene_id.tolist())]
    )
    source = source.set_index("request_id")
    manifest = pd.read_parquet(root / "analyses/broad/prediction_manifest.parquet")
    manifest = manifest[manifest.human_gene_id.isin(candidates.human_gene_id)].copy()
    original = baseline.rename(
        columns={
            "expression_log_tpm": "original_expression_log_tpm",
            "request_hash": "original_request_hash",
        }
    )
    manifest = manifest.merge(
        original[
            [
                "human_gene_id",
                "ensembl_species",
                "original_expression_log_tpm",
                "original_request_hash",
            ]
        ],
        on=["human_gene_id", "ensembl_species"],
        validate="one_to_one",
    )
    if len(manifest) != len(baseline):
        raise ValueError("Baseline and source species differ")
    protocol = {
        "model": config["model"],
        "description": config["description"],
        "sequence_name": config["sequence_name"],
        "input_bp": 2 * config["flank_bp"],
        "tss_index": config["flank_bp"],
        "shuffle_flank_bp": args.flank,
        "block_bp": 8,
        "replicates": args.replicates,
        "seed": args.seed,
        "algorithm": "non-overlapping 8-bp blocks permuted separately upstream/downstream; N-containing blocks fixed; unshuffled exterior retained",
        "overlapping_kmer_counts": "not exactly preserved at newly created block junctions",
        "candidate_source_sha256": digest(candidates_path),
        "baseline_sha256": digest(args.baseline),
        "genes": candidates.gene_name.tolist(),
        "species_gene_pairs": len(manifest),
        "total_requests": len(manifest) * (args.replicates + 1),
        "fresh_native_controls": "one per species–gene in the experiment cache, separate from original inference cache",
    }
    if (out / "protocol.json").exists():
        if json.loads((out / "protocol.json").read_text()) != protocol:
            raise ValueError("Experiment protocol changed; use another output directory")
        for path in [out / "prepared.parquet", out / "sequence_qc.csv"]:
            if not path.exists():
                raise ValueError("Incomplete preparation; inspect output directory")
        return out
    rows, qc = [], []
    for candidate in candidates.itertuples():
        group = manifest[manifest.human_gene_id == candidate.human_gene_id].sort_values(
            "ensembl_species"
        )
        for row in group.to_dict("records"):
            sequence = source.loc[row["request_id"], "sequence"]
            if sequence_hash(sequence) != row["sequence_sha256"]:
                raise ValueError("Original sequence checksum differs from manifest")
            if request_hash(payload(config, sequence)) != row["original_request_hash"]:
                raise ValueError("Native request differs from baseline context or sequence")
            native = dict(
                row,
                sequence=sequence,
                condition="native",
                replicate=-1,
                original_sequence_sha256=row["sequence_sha256"],
                seed=-1,
                request_id=row["request_id"] + ":native",
            )
            rows.append(native)
            seen = {native["sequence_sha256"]}
            for replicate in range(args.replicates):
                identity = f"{args.seed}|{row['human_gene_id']}|{row['ensembl_species']}|{replicate}|{args.flank}"
                seed = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "little")
                shuffled = shuffle_blocks(sequence, config["flank_bp"], args.flank, 8, seed)
                sha = sequence_hash(shuffled)
                if sha in seen:
                    raise ValueError("Unchanged or duplicate shuffled sequence")
                seen.add(sha)
                record = dict(
                    row,
                    sequence=shuffled,
                    sequence_sha256=sha,
                    original_sequence_sha256=row["sequence_sha256"],
                    condition="shuffled",
                    replicate=replicate,
                    seed=str(seed),
                    request_id=row["request_id"] + f":shuffle:{replicate:02}",
                )
                rows.append(record)
                lo, hi = config["flank_bp"] - args.flank, config["flank_bp"] + args.flank
                qc.append(
                    {
                        "human_gene_id": row["human_gene_id"],
                        "ensembl_species": row["ensembl_species"],
                        "replicate": replicate,
                        "sequence_sha256": sha,
                        "changed_bases": sum(
                            a != b for a, b in zip(sequence[lo:hi], shuffled[lo:hi], strict=True)
                        ),
                        "exterior_unchanged": sequence[:lo] == shuffled[:lo]
                        and sequence[hi:] == shuffled[hi:],
                        "composition_preserved": Counter(sequence) == Counter(shuffled),
                    }
                )
    prepared = pd.DataFrame(rows)
    prepared["seed"] = prepared.seed.astype(str)
    if prepared.request_id.duplicated().any():
        raise ValueError("Duplicate experiment request ID")
    prepared.to_parquet(out / "prepared.parquet", index=False, compression="zstd")
    prepared.drop(columns="sequence").to_csv(out / "manifest.csv", index=False)
    pd.DataFrame(qc).to_csv(out / "sequence_qc.csv", index=False)
    candidates.to_csv(out / "candidates.csv", index=False)
    write_json(out / "model_config.json", config)
    write_json(out / "protocol.json", protocol)
    write_json(
        out / "provenance.json",
        {
            "code_sha256": digest(__file__),
            "api_code_sha256": digest(Path(__file__).with_name("gi_api.py")),
            "prepared_sha256": digest(out / "prepared.parquet"),
            "manifest_sha256": digest(out / "manifest.csv"),
            "model_config_sha256": digest(out / "model_config.json"),
            "source_manifest_sha256": digest(root / "analyses/broad/prediction_manifest.parquet"),
            "prepared_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    log(f"Prepared {len(prepared):,} requests for {len(candidates)} genes in {out}")
    return out


def run(args):
    out = Path(args.output).resolve()
    with (out / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another shuffle runner is active") from exc
        run_locked(args, out)


def run_locked(args, out):
    provenance = json.loads((out / "provenance.json").read_text())
    if digest(out / "prepared.parquet") != provenance["prepared_sha256"]:
        raise ValueError("Prepared sequence file changed")
    config = json.loads((out / "model_config.json").read_text())
    if digest(out / "model_config.json") != provenance["model_config_sha256"]:
        raise ValueError("Frozen GI model context changed")
    data = pd.read_parquet(out / "prepared.parquet")
    candidates = pd.read_csv(out / "candidates.csv")
    client = Client(config, out / "responses", api_key(args.env_file), max_rps=args.max_rps)
    (out / "genes").mkdir(exist_ok=True)
    started = time.monotonic()
    progress = {
        "status": "running",
        "total_requests": len(data),
        "requests_done": 0,
        "genes_done": 0,
        "genes_total": len(candidates),
        "api_new": 0,
        "api_cached": 0,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    def save():
        progress["elapsed_seconds"] = time.monotonic() - started
        progress["updated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_json(out / "progress.json", progress)

    save()
    try:
        with (
            (out / "requests.jsonl").open("a") as handle,
            ThreadPoolExecutor(max_workers=args.workers) as pool,
        ):
            for candidate in candidates.itertuples():
                gene = candidate.human_gene_id
                group = data[data.human_gene_id == gene]
                records = []
                for condition in ["native", "shuffled"]:
                    futures = {
                        pool.submit(client.predict, row): row
                        for row in group[group.condition == condition].to_dict("records")
                    }
                    for future in as_completed(futures):
                        row = futures[future]
                        result = future.result()
                        handle.write(json.dumps(result) + "\n")
                        handle.flush()
                        if result["status"] not in ["ok", "cached"]:
                            raise RuntimeError(
                                f"GI request failed: {row['request_id']}; inspect requests.jsonl"
                            )
                        response = json.loads(Path(result["response_path"]).read_text())
                        value = validate_response(config, response)
                        record = {k: v for k, v in row.items() if k != "sequence"}
                        record.update(
                            expression_log_tpm=value,
                            request_hash=result["request_hash"],
                            response_path=result["response_path"],
                            scored_window_start=response["meta"]["task_specific_counts"][
                                "scored_window"
                            ][0],
                            scored_window_end=response["meta"]["task_specific_counts"][
                                "scored_window"
                            ][1],
                        )
                        records.append(record)
                        progress["requests_done"] += 1
                        progress["api_cached" if result["status"] == "cached" else "api_new"] += 1
                        if progress["requests_done"] % 50 == 0:
                            save()
                            log(
                                f"Shuffle inference {progress['requests_done']}/{len(data)}; gene {candidate.gene_name}"
                            )
                result = pd.DataFrame(records)
                if set(result.request_id) != set(group.request_id) or len(result) != len(group):
                    raise ValueError("Incomplete gene result")
                target = out / "genes" / f"{gene}.parquet"
                temporary = target.with_suffix(".parquet.tmp")
                result.sort_values(["condition", "replicate", "ensembl_species"]).to_parquet(
                    temporary, index=False
                )
                temporary.replace(target)
                progress["genes_done"] += 1
                progress["last_gene"] = candidate.gene_name
                save()
                log(f"Completed {candidate.gene_name}: {len(group)} predictions")
        progress["status"] = "completed"
        save()
    except BaseException as exc:
        progress["status"] = "failed"
        progress["error"] = f"{type(exc).__name__}: {exc}"
        save()
        raise
    log(json.dumps(progress))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "run"])
    parser.add_argument("--dataset-root", default="data/datasets/gi-hepatocytes-ensembl116")
    parser.add_argument("--output", required=True)
    parser.add_argument("--candidates", default="docs/gi-longevity-candidates/candidates.csv")
    parser.add_argument("--baseline", default="docs/gi-longevity-candidates/species_expression.csv")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--flank", type=int, default=4096)
    parser.add_argument("--replicates", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261004)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-rps", type=float, default=1.5)
    args = parser.parse_args()
    if args.replicates < 1 or args.workers < 1 or args.max_rps <= 0:
        parser.error("replicates, workers and max-rps must be positive")
    if args.stage == "prepare":
        prepare(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
