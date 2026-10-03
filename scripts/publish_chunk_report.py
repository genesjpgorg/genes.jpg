"""Wait for a successful shared-disk report, then commit/push it and open a GitHub PR.

Run on the already authenticated workstation; no GitHub credential is sent to the cluster.
Failure leaves a status file and never publishes an incomplete result as successful.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path


def run(command, cwd):
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def publish(worktree, job, report_dir):
    worktree, job = Path(worktree), Path(job)
    status = job / "publisher-status.json"
    while not (job / "pipeline.exit").exists():
        status.write_text(json.dumps({"state": "waiting", "checked_at": time.time()}) + "\n")
        time.sleep(30)
    if (job / "pipeline.exit").read_text().strip() != "0":
        raise RuntimeError(
            "Cluster pipeline failed; inspect training.log / report.log before publishing"
        )
    source = job / "report"
    if not (source / "summary.json").exists() or not (source / "README.md").exists():
        raise RuntimeError("Successful pipeline did not produce a complete report")
    target = worktree / report_dir
    if target.exists():
        raise FileExistsError(target)
    shutil.copytree(source, target)
    run(["git", "add", "--", report_dir], worktree)
    run(["git", "diff", "--cached", "--check"], worktree)
    run(
        ["git", "commit", "-m", "Report matched 18-epoch random-genome longevity experiment"],
        worktree,
    )
    branch = run(["git", "branch", "--show-current"], worktree)
    run(["git", "push", "origin", branch], worktree)
    body = job / "pr-body.md"
    body.write_text(
        "Runs the random-genome longevity variant against the frozen longevity-anage100 CDS reference: "
        "same 98 assemblies, 64/16/18 species splits, labels, model and optimizer settings, and 18 epochs. "
        "Each genome contributes 1,000 H5 inputs containing 1,022 DNA BPE tokens plus CLS/SEP.\n\n"
        "The report includes final held-out metrics, learning curves, per-species predictions, "
        "input checksums and the actual environment. It distinguishes epoch matching from compute "
        "matching (36,000 versus 5,040 updates) and marks historical software equivalence unverified.\n\n"
        f"Report: `{report_dir}/README.md`. Preparation and report code are included for reproduction.\n"
    )
    existing = json.loads(
        run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "number,url,isDraft",
            ],
            worktree,
        )
    )
    if existing:
        url = existing[0]["url"]
        run(
            [
                "gh",
                "pr",
                "edit",
                str(existing[0]["number"]),
                "--body-file",
                str(body),
                "--title",
                "Report random-genome chunk longevity training versus CDS",
            ],
            worktree,
        )
        if existing[0].get("isDraft"):
            run(["gh", "pr", "ready", str(existing[0]["number"])], worktree)
    else:
        url = run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                "Report random-genome chunk longevity training versus CDS",
                "--body-file",
                str(body),
            ],
            worktree,
        )
    status.write_text(
        json.dumps(
            {
                "state": "published",
                "url": url,
                "commit": run(["git", "rev-parse", "HEAD"], worktree),
            },
            indent=2,
        )
        + "\n"
    )
    print(url, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worktree", type=Path, required=True)
    ap.add_argument("--job", type=Path, required=True)
    ap.add_argument("--report-dir", default="docs/longevity-chunks-run")
    args = ap.parse_args()
    try:
        publish(args.worktree, args.job, args.report_dir)
    except Exception as error:
        (args.job / "publisher-status.json").write_text(
            json.dumps({"state": "failed", "error": str(error)}, indent=2) + "\n"
        )
        raise


if __name__ == "__main__":
    main()
