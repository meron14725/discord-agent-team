import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

from .adapters.codex import CodexRunner
from .config import secret
from .contracts import RunRequest

log = logging.getLogger(__name__)


def create_worker(runner=None, token=None, role=None, capacity=None):
    auth_mode = os.environ.get("AUTH_MODE", "chatgpt")
    runner = runner or (
        CodexRunner(auth_home="/codex-auth")
        if auth_mode == "chatgpt"
        else CodexRunner(secret("OPENAI_API_KEY"))
    )
    active = {}
    role = role or os.environ["WORKER_ROLE"]
    capacity = capacity or int(os.environ.get("WORKER_CONCURRENCY", "1"))
    if not 1 <= capacity <= 3:
        raise ValueError("WORKER_CONCURRENCY must be between 1 and 3")
    token = token or secret("WORKER_TOKEN")
    if len(token) < 32:
        raise ValueError("WORKER_TOKEN must contain at least 32 characters")

    @asynccontextmanager
    async def lifespan(app):
        if hasattr(runner, "startup"):
            await runner.startup()
        try:
            yield
        finally:
            for job_id, future in list(active.items()):
                future.cancel()
                await runner.cancel(job_id)
            if hasattr(runner, "shutdown"):
                await runner.shutdown()

    def authenticate(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "Invalid worker credential")

    app = FastAPI(lifespan=lifespan, dependencies=[Depends(authenticate)])

    @app.get("/health")
    def health():
        return {"status": "ok", "role": role, "capacity": capacity, "active": len(active)}

    @app.post("/run")
    async def run(request: RunRequest):
        if request.auth_mode != auth_mode:
            raise HTTPException(403, "Authentication mode mismatch; no fallback allowed")
        expected_roles = {
            "coordinate": {"coordinator"},
            "respond": {"upstream", "downstream", "sre"},
            "clarify": {"upstream"},
            "implement": {"downstream"},
            "fix": {"downstream"},
            "review": {"upstream"},
        }[request.kind]
        allowed_pairs = {
            "task": {
                ("upstream", "clarify"),
                ("upstream", "review"),
                ("downstream", "implement"),
                ("downstream", "fix"),
            },
            "coordinator": {("coordinator", "coordinate")},
            "specialists": {("upstream", "respond"), ("downstream", "respond")},
            "conversation-upstream": {("upstream", "respond")},
            "conversation-downstream": {("downstream", "respond")},
            "sre": {("sre", "respond")},
        }
        if (
            request.role not in expected_roles
            or (role != "both" and (request.role, request.kind) not in allowed_pairs.get(role, set()))
        ):
            raise HTTPException(403, "Wrong worker role")
        if len(active) >= capacity:
            raise HTTPException(409, "Worker is busy")
        future = asyncio.create_task(runner.run(request))
        active[request.job_id] = future
        try:
            try:
                return await asyncio.wait_for(future, request.timeout + 30)
            except Exception as error:
                log.warning(
                    "Worker run failed job=%s role=%s kind=%s error=%s: %s",
                    request.job_id,
                    request.role,
                    request.kind,
                    type(error).__name__,
                    str(error)[:800],
                )
                detail = f"{type(error).__name__}: {str(error)[:1000]}"
                raise HTTPException(500, detail) from error
        finally:
            try:
                await runner.cancel(request.job_id)
            finally:
                active.pop(request.job_id, None)

    @app.post("/cancel/{job_id}")
    async def cancel(job_id: str):
        if job_id in active:
            active[job_id].cancel()
            await runner.cancel(job_id)
        return {"cancelled": True}

    return app


def create_sbx_worker():
    from .adapters.sbx import SbxRunner

    return create_worker(
        runner=SbxRunner(state_dir=os.environ.get("SBX_STATE_DIR", "artifacts/sbx-state")),
        role=os.environ.get("WORKER_ROLE", "both"),
        capacity=int(os.environ.get("WORKER_CONCURRENCY", "1")),
    )
