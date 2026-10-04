#!/usr/bin/env bash
set -euo pipefail
umask 077
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
export GI_WEB_STATE="${GI_WEB_STATE:-$HOME/.local/share/gi-lifespan-web}"
export GI_REFERENCE_FASTA="${GI_REFERENCE_FASTA:-$GI_WEB_STATE/GRCh38.fa}"
export GI_ENV_FILE="${GI_ENV_FILE:-$repo_root/.env}"
exec "$repo_root/.venv/bin/python" -m uvicorn longevity.gi_web:app --host 127.0.0.1 --port "${GI_WEB_PORT:-8787}" --workers 1
