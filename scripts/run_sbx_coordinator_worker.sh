#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export WORKER_TOKEN_FILE="$PWD/secrets/worker_token"
export SBX_STATE_DIR="$PWD/artifacts/sbx-coordinator-state"
export PYTHONPATH="$PWD/src"
export AUTH_MODE=chatgpt
export WORKER_ROLE=coordinator
exec .venv/bin/python -m uvicorn agent_team.worker:create_sbx_worker \
  --factory --host 127.0.0.1 --port 8091
