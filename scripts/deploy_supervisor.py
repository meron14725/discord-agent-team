#!/usr/bin/env python3
"""Trusted, separately installed host release supervisor (Python stdlib only).

Never run this from an agent-controlled checkout. A human installs a pinned
copy with a private config. No network listener, arbitrary command API, SSH,
shell=True, database migrations, or updates to this supervisor are supported.
"""

import argparse
import fcntl
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

SERVICES = ("orchestrator", "discord-gateway", "renderer")
SHA = re.compile(r"[a-f0-9]{40}\Z")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}\Z")
PROTECTED = {
    "src/agent_team/db.py", "src/agent_team/config.py", "src/agent_team/deployments.py",
    "src/agent_team/worker.py", "src/agent_team/adapters/sbx.py",
    "src/agent_team/adapters/codex.py", "src/agent_team/policy.py",
    "prompts/company-policy.md", "prompts/company-memory.md",
}


def validate_changes(paths):
    if not paths:
        raise ValueError("no_changes")
    for path in paths:
        if (path in PROTECTED or not path.startswith(("src/", "tests/", "docs/", "prompts/personas/"))
            or ".." in Path(path).parts or path.endswith((".sql", ".pem"))):
            raise ValueError("protected_change")


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Supervisor:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["project_root"]).resolve()
        self.state = Path(config["state_dir"]).resolve()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.journal = self.state / "deployment.json"
        self.override = self.state / "runtime.override.json"
        self.baseline = self.state / "baseline.json"
        if not SHA.fullmatch(config["baseline_sha"]):
            raise ValueError("invalid_baseline")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config["repository"]):
            raise ValueError("invalid_repository")
        if not config.get("required_checks"):
            raise ValueError("required_checks_missing")
        if not self.baseline.exists():
            save(self.baseline, {"sha": config["baseline_sha"]})
        self.compose = ["docker", "compose", "--project-directory", str(self.root),
                        "-p", config["project_name"], "-f", str(self.root / "compose.yaml"),
                        "-f", str(self.root / "compose.sbx.yaml")]

    def run(self, argv, *, data=None, timeout=60):
        # Bound output in logs: command stdout/stderr are never printed. They
        # can contain credentials or arbitrary text from a candidate image.
        try:
            return subprocess.run(argv, input=data, capture_output=True, check=True,
                                  timeout=timeout, cwd=self.root).stdout
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("command_timeout") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("command_failed") from exc

    def bridge(self, request):
        output = self.run(self.compose + ["exec", "-T", "orchestrator", "python", "-m",
                                         "agent_team.deployments"],
                          data=json.dumps(request).encode())
        return json.loads(output)

    def images(self):
        result = {}
        for service in SERVICES:
            cid = self.run(self.compose + ["ps", "-q", service]).decode().strip()
            if not re.fullmatch(r"[a-f0-9]{12,64}", cid):
                raise ValueError("service_missing")
            image = self.run(["docker", "inspect", "--format", "{{.Image}}", cid]).decode().strip()
            if not IMAGE.fullmatch(image):
                raise ValueError("invalid_image")
            result[service] = image
        return result

    def health(self):
        for service, port in (("orchestrator", 8080), ("renderer", 8091)):
            self.run(self.compose + ["exec", "-T", service, "python", "-c",
                     "import urllib.request; "
                     f"assert urllib.request.urlopen('http://localhost:{port}/health',timeout=5).status==200"],
                     timeout=15)
        self.run(self.compose + ["exec", "-T", "discord-gateway", "python", "-c",
                 "import json,time; from pathlib import Path; "
                 "h=json.loads(Path('/tmp/agent-team-gateway-health.json').read_text()); "
                 "assert h['ready'] and time.time()-h['timestamp']<15"], timeout=15)

    def await_health(self):
        for _ in range(12):
            try:
                self.health()
                return
            except (RuntimeError, ValueError):
                time.sleep(5)
        raise RuntimeError("health_failed")

    def switch(self, images):
        if set(images) != set(SERVICES):
            raise ValueError("invalid_services")
        save(self.override, {"services": {s: {"image": image} for s, image in images.items()}})
        self.run(self.compose + ["-f", str(self.override), "up", "-d", "--no-deps", *SERVICES],
                 timeout=180)
        self.await_health()
        if self.images() != images:
            raise ValueError("image_mismatch")

    def candidate(self, sha):
        if not SHA.fullmatch(sha):
            raise ValueError("invalid_sha")
        checks = json.loads(self.run([
            "gh", "api", f"repos/{self.config['repository']}/commits/{sha}/check-runs?per_page=100"
        ]))
        for required in self.config["required_checks"]:
            matches = [c for c in checks["check_runs"] if c["name"] == required["name"]
                       and c["app"]["id"] == required["app_id"]]
            if not matches or any(c["status"] != "completed" or c["conclusion"] != "success"
                                  for c in matches):
                raise ValueError("required_ci_not_successful")
        baseline = json.loads(self.baseline.read_text())["sha"]
        cache = self.state / "source.git"
        if not cache.exists():
            self.run(["git", "init", "--bare", str(cache)])
        git = ["git", "-c", "core.hooksPath=/dev/null", "--git-dir", str(cache)]
        # Preserve the installed baseline even when its PR was squash-merged.
        # Fetching local objects does not check out or modify the working tree.
        try:
            self.run(git + ["cat-file", "-e", baseline + "^{commit}"])
        except RuntimeError:
            self.run(git + ["fetch", "--no-tags", str(self.root), baseline], timeout=60)
        url = "https://github.com/" + self.config["repository"] + ".git"
        self.run(git + ["fetch", "--no-tags", url, self.config["branch"]], timeout=180)
        if self.run(git + ["rev-parse", "FETCH_HEAD"]).decode().strip() != sha:
            raise ValueError("approved_sha_is_not_current_branch_head")
        paths = self.run(git + ["diff", "--name-only", "--no-renames", baseline, sha]).decode().splitlines()
        validate_changes(paths)
        archive = self.run(git + ["archive", sha], timeout=60)
        if len(archive) > 50_000_000:
            raise ValueError("source_too_large")
        with tempfile.TemporaryDirectory(dir=self.state) as directory:
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                for member in source.getmembers():
                    if not (member.isfile() or member.isdir()):
                        raise ValueError("source_links_forbidden")
                source.extractall(directory, filter="data")
            # Protected files have no changes against the human-installed
            # baseline. The build runs in Docker without production secrets.
            self.run(["docker", "build", "--target", "runtime", "--label",
                      "org.opencontainers.image.revision=" + sha,
                      "-t", "discord-agent-team-release:" + sha, directory], timeout=600)
        image = self.run(["docker", "image", "inspect", "--format", "{{.Id}}",
                          "discord-agent-team-release:" + sha]).decode().strip()
        if not IMAGE.fullmatch(image):
            raise ValueError("invalid_candidate_image")
        return image

    def recover(self, record):
        # After any interruption in the switch window, return to the captured
        # old image set. Never infer deployment success from a missing process.
        if record["phase"] == "switching":
            try:
                self.switch(record["old_images"])
                save(self.baseline, {"sha": record["old_sha"]})
                record.update(phase="complete", status="rolled_back")
            except Exception:
                record.update(phase="complete", status="rollback_failed")
        elif record["phase"] != "complete":
            record.update(phase="complete", status="failed")
        save(self.journal, record)
        return record

    def tick(self):
        if self.journal.exists():
            record = self.recover(json.loads(self.journal.read_text()))
        else:
            last_result = self.state / "last-result.json"
            if last_result.exists() and json.loads(last_result.read_text())["status"] == "rollback_failed":
                raise RuntimeError("manual_recovery_required")
            request = self.bridge({"action": "claim"})
            if request is None:
                return
            record = {"id": request["id"], "phase": "preflight"}
            save(self.journal, record)
            try:
                if (request.get("resumed") or request["repository"] != self.config["repository"]
                    or request["policy"] != "docker-release-v1"
                    or request["services"] != list(SERVICES)
                    or request.get("actor") not in self.config["owner_ids"]
                    or request["expires"] <= time.time()):
                    raise ValueError("invalid_approval")
                record["old_images"] = self.images()
                record["old_sha"] = json.loads(self.baseline.read_text())["sha"]
                self.health()
                image = self.candidate(request["sha"])
                for _ in range(12):
                    if self.bridge({"action": "idle"})["idle"]:
                        break
                    time.sleep(5)
                else:
                    raise ValueError("workers_busy")
                if request["expires"] <= time.time() or self.images() != record["old_images"]:
                    raise ValueError("stale_runtime_or_approval")
                # Backup precedes any replacement. Automatic rollback changes
                # images only; it must never discard post-backup user data.
                backup = self.run(self.compose + ["exec", "-T", "postgres", "pg_dump",
                                                 "-U", "team", "-d", "team", "-Fc"], timeout=120)
                backup_path = self.state / ("backup-" + request["sha"] + ".dump")
                with backup_path.open("wb") as stream:
                    stream.write(backup)
                    stream.flush()
                    os.fsync(stream.fileno())
                record["phase"] = "switching"
                save(self.journal, record)
                self.switch({s: image for s in SERVICES})
                # A stable second health observation before declaring success.
                time.sleep(10)
                self.health()
                save(self.baseline, {"sha": request["sha"]})
                record.update(phase="complete", status="succeeded")
                save(self.journal, record)
            except Exception as error:
                print(json.dumps({"event": "deployment_failure", "id": record["id"],
                                  "phase": record["phase"], "type": type(error).__name__}), flush=True)
                record = self.recover(record)
        # Keep the journal until the controller durably records the outcome.
        # If both versions are down, launchd retries the receipt after recovery.
        self.bridge({"action": "complete", "id": record["id"], "status": record["status"]})
        save(self.state / "last-result.json", record)
        self.journal.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    supervisor = Supervisor(json.loads(Path(args.config).read_text()))
    with (supervisor.state / "supervisor.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                supervisor.tick()
            except Exception as error:
                print(json.dumps({"event": "supervisor_retry", "type": type(error).__name__}), flush=True)
                if args.once:
                    raise
            if args.once:
                break
            time.sleep(10)


if __name__ == "__main__":
    main()
