#!/usr/bin/env bash
# Run on the 96-core workstation inside tmux.
set -euo pipefail
cd /tmp/genes-jpg-longevity-full
root=/mnt/filesystem-s8/genes.jpg
job="$root/runs/longevity-full-cds-budget-job"
mkdir -p "$job"
trap 'printf "%s\n" "$?" > "$job/prepare.exit"' EXIT
export HF_HOME="$root/hf_cache" TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
/home/fishman/genes.jpg/.venv/bin/python -u -m longevity.matched_chunks \
 --dataset "$root/datasets/longevity-full-frozen" \
 --comparison configs/longevity-full-cds-budget-comparison.json \
 --reuse-from "$root/datasets/longevity-full-chunks" \
 --out "$root/datasets/longevity-full-cds-budget-chunks" \
 --workers 96 --resume > "$job/prepare.log" 2>&1
