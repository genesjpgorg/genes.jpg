#!/usr/bin/env bash
# Run from the checked-out repository after launching H5 preparation separately.
set -euo pipefail
shared_root=${1:?Usage: run_chunk_comparison.sh /path/to/shared/genes.jpg}
training_python=${TRAINING_PYTHON:-.venv-train/bin/python}
job_dir="$shared_root/runs/longevity-anage100-chunks-job"
run_dir="$shared_root/runs/longevity-anage100-chunks"
chunks_dir="$shared_root/datasets/longevity-anage100-chunks"
comparison=configs/longevity-anage100-comparison.json
mkdir -p "$job_dir"
trap 'printf "%s\n" "$?" > "$job_dir/pipeline.exit"' EXIT
export HF_HOME="$shared_root/hf_cache"
export OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false
while [[ ! -f "$job_dir/prepare.exit" ]]; do sleep 30; done
if [[ $(cat "$job_dir/prepare.exit") != 0 ]]; then
    echo "Preparation failed; training will not start." >&2
    exit 1
fi
"$training_python" -u -m longevity.audit_chunks --data "$chunks_dir" \
    --comparison "$comparison" --out "$job_dir/input_audit.json" > "$job_dir/audit.log" 2>&1
"$training_python" -u -m longevity.train --data "$chunks_dir" \
    --comparison "$comparison" --out "$run_dir" > "$job_dir/training.log" 2>&1
"$training_python" -u -m longevity.compare_report --run "$run_dir" \
    --reference "$shared_root/runs/longevity-anage100" --job "$job_dir" \
    --out "$job_dir/report" > "$job_dir/report.log" 2>&1
