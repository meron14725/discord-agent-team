#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export WORKER_TOKEN_FILE="$PWD/secrets/worker_token"
export SBX_STATE_DIR="${TEAM_WORKER_STATE_ROOT:-$PWD/artifacts}/sbx-downstream-chat-state"
export PYTHONPATH="$PWD/src"
export AUTH_MODE=chatgpt
export WORKER_ROLE=conversation-downstream
export WORKER_CONCURRENCY=1
exec "${TEAM_WORKER_PYTHON:-.venv/bin/python}" -m uvicorn agent_team.worker:create_sbx_worker \
  --factory --host 127.0.0.1 --port 8094
