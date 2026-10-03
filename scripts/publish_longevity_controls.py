"""Publish full-dataset control results after the remote tmux pipeline succeeds."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path


def run(command, cwd):
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def copy_report(source, target):
    source, target = Path(source), Path(target)
    if (source / "complete").read_text().strip() != "0":
        raise ValueError("Incomplete control evaluation")
    summary = json.loads((source / "summary.json").read_text())
    spec = json.loads((source / "comparison.json").read_text())
    if summary["comparison_sha256"] != spec["sha256"]:
        raise ValueError("Report comparison mismatch")
    for split in ("val", "test"):
        for mode in ("intact", "shuffled"):
            if summary["results"][split][mode]["n_species"] != len(spec["split"][split]):
                raise ValueError("Incomplete held-out species coverage")
    target.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.suffix in {".json", ".md", ".csv", ".png", ".txt"}:
            shutil.copy2(path, target / path.name)


def publish(worktree, job, source, pr):
    status = job / "controls-publisher-status.json"
    while not (job / "pipeline.exit").exists():
        status.write_text(json.dumps({"state": "waiting", "checked_at": time.time()}) + "\n")
        time.sleep(30)
    if (job / "pipeline.exit").read_text().strip() != "0":
        raise RuntimeError("Remote pipeline failed; inspect pipeline.log and stage logs")
    target = "docs/longevity-full-controls"
    copy_report(source, worktree / target)
    for filename in ("input-audit.json", "environment.txt", "code-commit.txt"):
        shutil.copy2(job / filename, worktree / target / filename)
    run(["git", "add", "--", target], worktree)
    run(["git", "diff", "--cached", "--check"], worktree)
    if run(["git", "diff", "--cached", "--name-only"], worktree):
        run(
            [
                "git",
                "commit",
                "-m",
                "Report full-dataset longevity intact and shuffled-token metrics",
            ],
            worktree,
        )
    branch = run(["git", "branch", "--show-current"], worktree)
    run(["git", "push", "origin", branch], worktree)
    body = job / "completed-pr-body.md"
    body.write_text((worktree / "docs/longevity-full-pr.md").read_text().replace(
        " (added automatically when the pipeline finishes; PR stays draft until then)", ""))
    run(["gh", "pr", "edit", str(pr), "--body-file", str(body)], worktree)
    info = json.loads(run(["gh", "pr", "view", str(pr), "--json", "url,isDraft"], worktree))
    if info["isDraft"]:
        run(["gh", "pr", "ready", str(pr)], worktree)
    status.write_text(
        json.dumps(
            {
                "state": "published",
                "url": info["url"],
                "commit": run(["git", "rev-parse", "HEAD"], worktree),
            },
            indent=2,
        )
        + "\n"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worktree", type=Path, required=True)
    ap.add_argument("--job", type=Path, required=True)
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--pr", type=int, required=True)
    args = ap.parse_args()
    try:
        publish(args.worktree, args.job, args.source, args.pr)
    except Exception as error:
        (args.job / "controls-publisher-status.json").write_text(
            json.dumps({"state": "failed", "error": str(error)}, indent=2) + "\n"
        )
        raise


if __name__ == "__main__":
    main()
