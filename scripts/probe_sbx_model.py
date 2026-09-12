"""One subscription-backed Codex smoke run in a disposable, mountless microVM.

Requires the explicitly approved `sbx secret set openai --oauth` login.
Consumes subscription usage. No API key fallback, Discord, or GitHub operations.
"""

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "docker/sandbox-templates@sha256:8b4cd0a46c8b600bc6b6a64af23c03d4c2807fbfc61f47568092a93fb9dc88b0"


def main():
    name = "dat-model-" + uuid.uuid4().hex[:12]
    report = {
        "at": datetime.now(timezone.utc).isoformat(),
        "sandbox": name,
        "status": "failed",
        "model_attempted": False,
        "steps": [],
    }
    env = {k: os.environ[k] for k in ("HOME", "PATH", "TMPDIR", "LANG") if k in os.environ}

    def run(args, *, stdin=None, timeout=120):
        print("Checking: sbx " + args[0], flush=True)
        result = subprocess.run(
            ["sbx", *args], input=stdin, capture_output=True, text=True, timeout=timeout, env=env
        )
        report["steps"].append({
            "args": args, "exit_code": result.returncode,
            "stdout": result.stdout[-24000:], "stderr": result.stderr[-12000:],
        })
        if result.returncode:
            raise RuntimeError(f"sbx {args[0]} failed: {result.stderr[-1000:]}")
        return result.stdout

    created = False
    try:
        assert "v0.42.1 " in run(["version"]), "Unexpected sbx version"
        metadata = run(["secret", "ls"])
        assert "openai" in metadata.lower() and "oauth" in metadata.lower(), "OpenAI OAuth required"
        created = True  # Also clean up a partial creation if possible.
        run([
            "create", "--name", name, "--template", IMAGE, "--no-share-skills",
            "--cpus", "2", "--memory", "4g", "codex",
        ], timeout=600)
        inspection = run(["inspect", name])
        assert "oauth" in inspection.lower(), "Sandbox must use OAuth"
        run(["policy", "allow", "network", "--sandbox", name, "chatgpt.com:443"])
        version = run(["exec", name, "codex", "--version"])
        assert version.strip() == "codex-cli 0.149.1", "Unexpected Codex version"
        run(["exec", name, "codex", "exec", "--help"])
        schema = {
            "type": "object", "additionalProperties": False,
            "properties": {"ok": {"type": "boolean"}, "total": {"type": "integer"}},
            "required": ["ok", "total"],
        }
        bootstrap = """import json, pathlib, sys
root = pathlib.Path('/home/agent/workspace')
(root / 'input.json').write_text('[20, 22]')
(root / 'schema.json').write_text(sys.stdin.read())
print('Synthetic input and schema ready')
"""
        run(["exec", "-i", name, "python3", "-c", bootstrap], stdin=json.dumps(schema))
        prompt = (
            "Read input.json in the current directory. Sum its integers and write the integer "
            "followed by a newline to result.txt. Return JSON with ok=true and total equal to "
            "that sum. Do not access the network, inspect credentials, or spawn agents."
        )
        report["model_attempted"] = True
        run([
            "exec", "-i", "-w", "/home/agent/workspace", name,
            "codex", "exec", "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check", "--ephemeral", "--json",
            "--output-schema", "/home/agent/workspace/schema.json",
            "--output-last-message", "/home/agent/workspace/response.json", "-",
        ], stdin=prompt, timeout=180)
        verify = """import json, pathlib
root = pathlib.Path('/home/agent/workspace')
response = json.loads((root / 'response.json').read_text())
assert response == {'ok': True, 'total': 42}, 'Invalid structured result'
assert (root / 'result.txt').read_text() == '42\\n', 'Invalid file output'
print(json.dumps({'response': response, 'artifact': '42\\n'}))
"""
        report["result"] = json.loads(run(["exec", name, "python3", "-c", verify]))
        report["status"] = "passed"
    except (OSError, RuntimeError, AssertionError, ValueError, subprocess.TimeoutExpired) as error:
        report["error"] = str(error)
    finally:
        if created:
            try:
                run(["rm", "--force", name])
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                report["status"] = "failed"
                report["cleanup_error"] = str(error)
        path = ROOT / "docs" / "sbx-model-validation.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"{report['status']}: {path}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
