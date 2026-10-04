#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
gi_shuffle_out="${1:?Pass the prepared experiment output directory}"
gi_shuffle_env="${2:-.env}"
gi_shuffle_python="${GI_PYTHON:-python}"
exec > >(tee -a "$gi_shuffle_out/run.log") 2>&1
trap 'gi_shuffle_code=$?; printf "%s\n" "$gi_shuffle_code" > "$gi_shuffle_out/exit-code"' EXIT
"$gi_shuffle_python" -u -m longevity.gi_tss_shuffle run \
  --output "$gi_shuffle_out" --env-file "$gi_shuffle_env" --workers 4 --max-rps 1.5
"$gi_shuffle_python" -u -m longevity.gi_tss_shuffle_report \
  --output "$gi_shuffle_out" --publish-dir docs/gi-longevity-candidates/shuffle
