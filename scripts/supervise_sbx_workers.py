#!/usr/bin/env python3
"""Start and health-check the trusted host worker pools.

launchd owns this supervisor. If a worker exits or fails three consecutive health
checks, the supervisor exits non-zero and launchd restarts the whole worker set.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKERS = (
    ("task", ROOT / "scripts/run_sbx_worker.sh", 8090, "task", 1),
    ("coordinator", ROOT / "scripts/run_sbx_coordinator_worker.sh", 8091, "coordinator", 1),
    (
        "upstream-chat",
        ROOT / "scripts/run_sbx_specialist_worker.sh",
        8092,
        "conversation-upstream",
        1,
    ),
    ("sre", ROOT / "scripts/run_sbx_sre_worker.sh", 8093, "sre", 1),
    (
        "downstream-chat",
        ROOT / "scripts/run_sbx_downstream_specialist_worker.sh",
        8094,
        "conversation-downstream",
        1,
    ),
)
STARTUP_GRACE_SECONDS = 15
CHECK_INTERVAL_SECONDS = 10
MAX_CONSECUTIVE_FAILURES = 3


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}", flush=True)


def healthy(port: int, expected_role: str, expected_capacity: int, token: str) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read())
        return (
            response.status == 200
            and body.get("status") == "ok"
            and body.get("role") == expected_role
            and body.get("capacity") == expected_capacity
            and isinstance(body.get("active"), int)
        )
    except (OSError, ValueError, urllib.error.URLError):
        return False


def stop_all(processes: dict[str, subprocess.Popen[bytes]]) -> None:
    for process in processes.values():
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 15
    for process in processes.values():
        remaining = max(0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
    for process in processes.values():
        process.wait()


def main() -> int:
    token_path = ROOT / "secrets/worker_token"
    token = token_path.read_text().strip()
    if len(token) < 32:
        raise RuntimeError("worker token is missing or too short")

    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGHUP, request_stop)

    processes: dict[str, subprocess.Popen[bytes]] = {}
    failures = {name: 0 for name, *_ in WORKERS}
    try:
        for name, script, _port, _role, _capacity in WORKERS:
            processes[name] = subprocess.Popen(["/bin/sh", str(script)], cwd=ROOT)
            log(f"started {name} worker pid={processes[name].pid}")

        deadline = time.monotonic() + STARTUP_GRACE_SECONDS
        while not stopping and time.monotonic() < deadline:
            for name, process in processes.items():
                if process.poll() is not None:
                    log(f"{name} worker exited during startup code={process.returncode}")
                    return 1
            time.sleep(1)

        while not stopping:
            for name, _script, port, role, capacity in WORKERS:
                process = processes[name]
                if process.poll() is not None:
                    log(f"{name} worker exited code={process.returncode}")
                    return 1
                if healthy(port, role, capacity, token):
                    failures[name] = 0
                else:
                    failures[name] += 1
                    log(f"{name} health check failed count={failures[name]}")
                    if failures[name] >= MAX_CONSECUTIVE_FAILURES:
                        return 1
            time.sleep(CHECK_INTERVAL_SECONDS)
        return 0
    finally:
        stop_all(processes)
        log("all host workers stopped")


if __name__ == "__main__":
    sys.exit(main())
