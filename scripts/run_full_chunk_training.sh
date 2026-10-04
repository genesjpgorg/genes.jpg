#!/usr/bin/env bash
# Launch with: tmux new-session -d -s longevity-full-train 'bash scripts/run_full_chunk_training.sh'
set -euo pipefail
cd /home/fishman/genesjpg-full
root=/mnt/filesystem-n0/genes.jpg
job="$root/runs/longevity-full-cds-budget-job"
mkdir -p "$job"
trap 'printf "%s\n" "$?" > "$job/pipeline.exit"' EXIT
exec >> "$job/pipeline.log" 2>&1
ulimit -n 8192
export HF_HOME="$root/hf_cache" TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1
printf 'Waiting for complete, verified input preparation\n'
while [[ ! -f "$job/prepare.exit" ]]; do sleep 30; done
[[ "$(cat "$job/prepare.exit")" == 0 ]] || { echo 'Preparation failed'; exit 1; }
.venv-train/bin/python -u -m longevity.audit_chunks \
 --data "$root/datasets/longevity-full-cds-budget-chunks" --comparison configs/longevity-full-cds-budget-comparison.json \
 --out "$job/input-audit.json" > "$job/audit.log" 2>&1
exec 9> /home/fishman/.cache/genesjpg-gpu.lock
flock -x 9
.venv-train/bin/python - <<'ENV' > "$job/environment.txt"
from importlib.metadata import distributions
print("\n".join(sorted(f"{d.metadata['Name']}=={d.version}" for d in distributions())))
ENV
git rev-parse HEAD > "$job/code-commit.txt"
git diff --exit-code -- longevity scripts configs
printf 'Starting full training\n'
.venv-train/bin/python -u -m longevity.train \
 --data "$root/datasets/longevity-full-cds-budget-chunks" --out "$root/runs/longevity-full-cds-budget-chunks" \
 --comparison configs/longevity-full-cds-budget-comparison.json > "$job/training.log" 2>&1
printf 'Evaluating intact and shuffled tokens\n'
.venv-train/bin/python -u -m longevity.shuffle_eval \
 --run "$root/runs/longevity-full-cds-budget-chunks" --data "$root/datasets/longevity-full-cds-budget-chunks" \
 --out "$root/runs/longevity-full-cds-budget-chunks-controls" > "$job/full_shuffle.log" 2>&1
printf 'Training and evaluations completed\n'
