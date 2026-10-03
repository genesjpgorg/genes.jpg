"""Print a text progress report for a longevity.train run (loss curve + val metrics).

Usage: python -m longevity.progress /mnt/filesystem-c8/genes.jpg/runs/longevity-anage100
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BARS = " ▁▂▃▄▅▆▇█"


def spark(xs: list[float], width: int = 60) -> str:
    """Unicode sparkline of xs, averaged into at most `width` buckets."""
    if not xs:
        return ""
    n = max(1, -(-len(xs) // width))
    ys = [sum(xs[i : i + n]) / len(xs[i : i + n]) for i in range(0, len(xs), n)]
    lo, hi = min(ys), max(ys)
    return "".join(BARS[int((y - lo) / (hi - lo + 1e-12) * (len(BARS) - 1))] for y in ys)


def main() -> None:
    run = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    cfg = json.loads((run / "config.json").read_text())
    recs = [json.loads(line) for line in (run / "metrics.jsonl").read_text().splitlines() if line]
    train = [r for r in recs if r["kind"] == "train"]
    val = [r for r in recs if r["kind"] == "val"]
    done = [r for r in recs if r["kind"] == "done"]

    total = cfg["total_steps"]
    if train:
        t = train[-1]
        pct = t["step"] / total
        print(f"{run.name}: step {t['step']}/{total} ({pct:.0%})  epoch {t['epoch']:.2f}  "
              f"{t['elapsed_min']:.1f} min  {t['tok_per_s'] / 1e3:.0f}k tok/s  "
              f"lr {t['lr_encoder']:.1e}" + ("  DONE" if done else ""))
        bar = int(40 * pct)
        print("[" + "#" * bar + "." * (40 - bar) + "]")
        losses = [r["loss"] for r in train]
        print(f"\ntrain loss (MSE, z-units)  first {losses[0]:.3f}  last {losses[-1]:.3f}  "
              f"min {min(losses):.3f}")
        print("  " + spark(losses))
    if val:
        print(f"\nval (species-level, log10 years; baseline = predict train mean "
              f"{val[-1]['baseline_species_mae_log10']:.3f})")
        print(f"  {'step':>6} {'epoch':>6} {'MAE':>7} {'Spearman':>9} {'Pearson':>8}")
        for r in val:
            print(f"  {r['step']:>6} {r['epoch']:>6.2f} {r['species_mae_log10']:>7.3f} "
                  f"{r.get('species_spearman', float('nan')):>9.3f} "
                  f"{r.get('species_pearson', float('nan')):>8.3f}")
        print("  MAE      " + spark([r["species_mae_log10"] for r in val], 30))
        print("  Spearman " + spark([r.get("species_spearman", 0) for r in val], 30))
    for r in recs:
        if r["kind"] == "test":
            print(f"\ntest: MAE {r['species_mae_log10']:.3f} (baseline "
                  f"{r['baseline_species_mae_log10']:.3f})  Spearman "
                  f"{r.get('species_spearman', float('nan')):.3f}")


if __name__ == "__main__":
    main()
