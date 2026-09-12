"""Local, model-free Docker Sandboxes smoke test. No host workspace mounts.

Run after `sbx login`: python3 scripts/probe_sbx.py
Creates and deletes only its own randomly named VM. Does not authenticate to OpenAI.
"""

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    name = "dat-probe-" + uuid.uuid4().hex[:12]
    report = {
        "at": datetime.now(timezone.utc).isoformat(),
        "sandbox": name,
        "model_invoked": False,
        "status": "failed",
        "steps": [],
    }

    def run(args, *, stdin=None, timeout=120):
        print(f"Checking: sbx {args[0]}", flush=True)
        result = subprocess.run(
            ["sbx", *args], input=stdin, capture_output=True, text=True, timeout=timeout,
            env={key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG") if key in os.environ},
        )
        report["steps"].append({
            "args": args,
            "exit_code": result.returncode,
            "stdout": result.stdout[-16000:],
            "stderr": result.stderr[-16000:],
        })
        if result.returncode:
            raise RuntimeError(f"sbx {args[0]} failed: {result.stderr[-1000:]}")
        return result.stdout

    create_attempted = False
    try:
        version = run(["version"])
        if "v0.42.1 " not in version:
            raise RuntimeError("This probe was prepared for sbx v0.42.1; review CLI changes first")
        run(["ls"])  # Fail before creation when Docker login is missing.
        run(["secret", "ls"])  # Metadata only; no secret values are printed.
        run(["mcp", "ls"])
        create_attempted = True
        run([
            "create", "--name", name, "--no-share-skills",
            "--cpus", "2", "--memory", "4g", "--deny-network", "*", "codex",
        ], timeout=600)
        run(["inspect", name])
        run(["exec", name, "codex", "--version"])
        # Only synthetic input crosses the boundary; never copy this repo or credentials.
        payload = json.dumps({"nonce": uuid.uuid4().hex, "host_path": str(ROOT)})
        program = """import json, os, pathlib, re, subprocess, sys
data = json.load(sys.stdin)
assert not pathlib.Path(data['host_path']).exists(), 'host project visible'
for key in ('GITHUB_TOKEN', 'GH_TOKEN'):
    value = os.environ.get(key, '')
    placeholder = len(value) == 40 and re.fullmatch('gho_[A-Za-z0-9]*proxy[A-Za-z0-9]*managed[A-Za-z0-9]*', value)
    assert value in ('', 'proxy-managed') or placeholder, 'non-placeholder credential: ' + key
for key in ('DATABASE_URL', 'DISCORD_TOKEN', 'INTERNAL_TOKEN'):
    assert not os.environ.get(key), 'unexpected control credential: ' + key
ssh = subprocess.run(['ssh-add', '-l'], capture_output=True, timeout=10)
assert ssh.returncode in (1, 2), 'SSH agent exposes identities'
target = pathlib.Path('/home/agent/workspace/probe.json')
target.write_text(json.dumps({'nonce': data['nonce']}))
print(target.read_text())
"""
        output = run(["exec", "-i", name, "python3", "-c", program], stdin=payload)
        assert json.loads(output)["nonce"] == json.loads(payload)["nonce"]
        # HTTP denial is one representative probe, not proof about every protocol/destination.
        network = """import urllib.request, urllib.error
try:
    response = urllib.request.urlopen('https://example.com', timeout=10)
except urllib.error.HTTPError as error:
    assert error.code == 403, str(error)
    print('HTTP 403: example.com denied')
except urllib.error.URLError as error:
    assert '403' in str(error.reason), str(error)
    print('Proxy HTTP 403: example.com denied')
else:
    raise RuntimeError('egress unexpectedly allowed: ' + str(response.status))
"""
        run(["exec", name, "python3", "-c", network])
        run(["stop", name])
        report["status"] = "passed"
    except (OSError, RuntimeError, AssertionError, ValueError, subprocess.TimeoutExpired) as error:
        report["error"] = str(error)
    finally:
        if create_attempted:
            try:
                run(["rm", "--force", name])
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                report["status"] = "failed"
                report["cleanup_error"] = str(error)
        destination = ROOT / "docs" / "sbx-validation.json"
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"{report['status']}: {destination}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
