"""Publish gene-model shuffle results after successful remote evaluation."""

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path


def command(args, cwd):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worktree", type=Path, required=True)
    ap.add_argument("--job", type=Path, required=True)
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--pr", type=int, required=True)
    args = ap.parse_args()
    status = args.job / "publisher-status.json"
    try:
        while not (args.job / "pipeline.exit").exists():
            status.write_text(json.dumps({"state": "waiting", "checked_at": time.time()}) + "\n")
            time.sleep(30)
        if (args.job / "pipeline.exit").read_text().strip() != "0":
            raise RuntimeError("Evaluation failed; inspect evaluation.log")
        if (args.source / "complete").read_text().strip() != "0":
            raise RuntimeError("Incomplete results")
        summary = json.loads((args.source / "summary.json").read_text())
        for split, n_species in [("val", 335), ("test", 294)]:
            for mode in ("saved", "intact", "shuffled"):
                if summary["results"][split][mode]["n_species"] != n_species:
                    raise RuntimeError("Incomplete species coverage")
        target = args.worktree / "docs/longevity-gene-shuffle"
        target.mkdir(parents=True, exist_ok=True)
        for path in args.source.iterdir():
            if (
                path.suffix in {".json", ".md", ".csv", ".txt", ".png"}
                and path.name != "progress.json"
            ):
                shutil.copy2(path, target / path.name)
        command(["git", "add", "--", str(target)], args.worktree)
        command(["git", "diff", "--cached", "--check"], args.worktree)
        if command(["git", "diff", "--cached", "--name-only"], args.worktree):
            command(
                ["git", "commit", "-m", "Report intact and shuffled gene-model longevity metrics"],
                args.worktree,
            )
        command(["git", "push", "origin", "longevity-gene-shuffle"], args.worktree)
        body = args.job / "completed-pr-body.md"
        body.write_text(
            "Reproduces full validation/test metrics for the gene-genomic longevity run and evaluates "
            "the same best.pt checkpoint (step 76,000) with shuffled DNA tokens. CLS, SEP, PAD positions "
            "and attention masks are preserved; permutation happens after the evaluation crop.\n\n"
            "Report: [docs/longevity-gene-shuffle/README.md](docs/longevity-gene-shuffle/README.md). "
            "Includes MAE, Pearson on log10/year scales, Spearman, per-species predictions, "
            "checkpoint/data checksums, runtime environment, and intact-rerun reproduction errors.\n\n"
            "Validation: tests cover special-token and mask invariance, per-input deterministic "
            "permutations, and cropping before shuffling. Evaluation ran in remote tmux.\n"
        )
        command(["gh", "pr", "edit", str(args.pr), "--body-file", str(body)], args.worktree)
        info = json.loads(
            command(["gh", "pr", "view", str(args.pr), "--json", "url,isDraft"], args.worktree)
        )
        if info["isDraft"]:
            command(["gh", "pr", "ready", str(args.pr)], args.worktree)
        status.write_text(json.dumps({"state": "published", "url": info["url"]}) + "\n")
    except Exception as error:
        status.write_text(json.dumps({"state": "failed", "error": str(error)}) + "\n")
        raise


if __name__ == "__main__":
    main()
