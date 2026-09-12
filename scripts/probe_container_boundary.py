"""No model/login/network calls. Report outer container controls and nested userns setup."""

import ctypes
import errno
import json
import os
from pathlib import Path

status = dict(
    line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line
)
assert os.getuid() == 10001
assert status["NoNewPrivs"].strip() == "1"
assert int(status["CapBnd"].strip(), 16) == 0
assert status["Seccomp"].strip() == "2"
for name in (
    "db_password",
    "internal_token",
    "github_publisher_token",
    "github_reviewer_token",
    "github_merger_token",
    "discord_upstream_token",
    "discord_downstream_token",
):
    assert not (Path("/run/secrets") / name).exists(), f"Unexpected credential mount: {name}"
assert not Path("/var/run/docker.sock").exists()
assert not Path("/app/config.yaml").exists()
print(
    json.dumps(
        {
            "uid": os.getuid(),
            "no_new_privileges": True,
            "capability_bounding_set": 0,
            "seccomp_filter": True,
            "control_credentials_absent": True,
            "docker_socket_absent": True,
        }
    ),
    flush=True,
)
# Child changes only its own namespace membership. The parent keeps the original identity.
pid = os.fork()
if pid == 0:
    libc = ctypes.CDLL(None, use_errno=True)
    outcome = libc.unshare(ctypes.c_int(0x10000000 | 0x00020000))  # CLONE_NEWUSER | CLONE_NEWNS
    error = ctypes.get_errno() if outcome else 0
    print(
        json.dumps(
            {
                "nested_user_and_mount_namespace": outcome == 0,
                "errno": error,
                "error": errno.errorcode.get(error, ""),
            }
        ),
        flush=True,
    )
    os._exit(0 if outcome == 0 else 1)
_, exit_status = os.waitpid(pid, 0)
raise SystemExit(os.waitstatus_to_exitcode(exit_status))
