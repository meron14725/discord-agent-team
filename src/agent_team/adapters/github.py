"""Trusted publisher: REST git objects, no GitHub credentials in agent workspaces."""

import base64
import copy
import hashlib
import time
from urllib.parse import quote

import httpx
from sqlalchemy import select

from ..db import Operation
from ..policy import GuardError, digest, safe_path
from .github_auth import HEADERS, InstallationAuth


class GitHub:
    def __init__(self, settings, tokens, reviewer_private_key=""):
        self.settings = settings
        self.clients = {
            role: httpx.Client(
                base_url="https://api.github.com",
                timeout=30,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            for role, token in tokens.items()
        }
        if app := settings.github_reviewer_app:
            if not reviewer_private_key:
                raise ValueError("Reviewer App private key is required")
            if old := self.clients.get("reviewer"):
                old.close()
            self.clients["reviewer"] = httpx.Client(
                base_url="https://api.github.com", timeout=30, headers=HEADERS,
                auth=InstallationAuth(app.app_id, app.installation_id, reviewer_private_key),
            )

    def api(self, repo, method, path, role="publisher", **kwargs):
        response = self.clients[role].request(method, f"/repos/{repo.repository}/{path}", **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def pages(self, repo, path, role="publisher"):
        values = []
        for page in range(1, 101):
            sep = "&" if "?" in path else "?"
            data = self.api(repo, "GET", f"{path}{sep}per_page=100&page={page}", role)
            batch = data.get("check_runs", []) if isinstance(data, dict) else data
            values.extend(batch)
            if len(batch) < 100:
                return values
        raise GuardError("GitHub pagination limit exceeded")

    def ensure_repo(self, repo, task):
        owner, name = repo.repository.split("/")
        configured = self.settings.repos[task.repo]
        if (
            not configured.per_task
            or owner != configured.repository.split("/")[0]
            or not name.endswith(task.id.lower())
        ):
            raise GuardError("Repository provisioning target denied")
        marker = f"agent-task:{task.id}"
        try:
            existing = self.api(repo, "GET", "")
            if existing.get("description") != marker or existing.get("private") != repo.private:
                raise GuardError("Existing repository does not match provisioning marker")
            return {"repository": existing["full_name"]}
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
        client = self.clients["publisher"]
        user = client.get("/user")
        user.raise_for_status()
        if user.json()["login"].lower() != owner.lower():
            raise GuardError("Per-task creation requires the configured owner's user token")
        if repo.template_repository:
            path = f"/repos/{repo.template_repository}/generate"
            payload = {"owner": owner, "name": name, "private": repo.private, "description": marker}
        else:
            path = "/user/repos"
            payload = {"name": name, "private": repo.private, "description": marker, "auto_init": True}
        response = client.post(path, json=payload)
        response.raise_for_status()
        return {"repository": response.json()["full_name"]}

    def base(self, repo, attempts=1):
        for attempt in range(attempts):
            try:
                return self.api(repo, "GET", f"branches/{quote(repo.base, safe='')}")["commit"]["sha"]
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in {404, 409} or attempt + 1 == attempts:
                    raise
                # Template generation can return before the default branch is readable.
                time.sleep(min(2**attempt, 4))
        raise AssertionError("unreachable")

    def source(self, repo, sha):
        tree = self.api(repo, "GET", f"git/trees/{sha}?recursive=1")
        if tree.get("truncated"):
            raise GuardError("Truncated repository tree")
        files, total = {}, 0
        for entry in tree["tree"]:
            if entry["type"] == "tree":
                continue
            safe_path(entry["path"])
            if entry["mode"] not in {"100644", "100755"}:
                raise GuardError("MVP does not support symlinks/submodules")
            total += entry.get("size", 0)
            if total > self.settings.max_bytes or len(files) >= self.settings.max_files:
                raise GuardError("Source exceeds small-repository MVP limit")
            raw = self.api(repo, "GET", f"git/blobs/{entry['sha']}")
            files[entry["path"]] = base64.b64decode(raw["content"]).decode("utf-8")
        return files

    def issue(self, repo, task, body):
        marker = f"<!-- agent-task:{task.id} -->"
        existing = [
            i
            for i in self.pages(repo, "issues?state=all")
            if "pull_request" not in i and marker in (i.get("body") or "")
        ]
        if len(existing) > 1:
            raise GuardError("Duplicate issue marker; reconcile manually")
        return (
            existing[0]
            if existing
            else self.api(
                repo,
                "POST",
                "issues",
                json={"title": f"[{task.id}] {task.data['summary'][:180]}", "body": marker + "\n" + body},
            )
        )["number"]

    def publish(self, repo, task, files, key):
        d, branch = task.data, task.data["branch"]
        if not branch.startswith(f"agent/{task.id}/") or branch == repo.base:
            raise GuardError("Publisher branch denied")
        marker = f"<!-- agent-task:{task.id} -->"
        prs = self.pages(repo, f"pulls?state=all&head={repo.repository.split('/')[0]}:{branch}")
        if len(prs) > 1:
            raise GuardError("Multiple PRs for task branch")
        if prs and prs[0]["state"] != "open":
            raise GuardError("Existing PR is closed; refusing to reopen")
        parent = d.get("head_sha") or d["base_sha"]
        ref = None
        try:
            ref = self.api(repo, "GET", f"git/ref/heads/{branch}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
        if ref:
            commit = self.api(repo, "GET", f"git/commits/{ref['object']['sha']}")
            if commit["message"] == key:
                head = commit["sha"]  # Reconcile response lost after ref update.
            else:
                if ref["object"]["sha"] != parent:
                    raise GuardError("Branch changed outside current run")
                head = None
        else:
            head = None
        if head is None:
            parent_commit = self.api(repo, "GET", f"git/commits/{parent}")
            entries = []
            for path, content in files.items():
                safe_path(path)
                entry = {"path": path, "mode": "100644", "type": "blob"}
                entry.update({"sha": None} if content is None else {"content": content})
                entries.append(entry)
            tree = self.api(
                repo, "POST", "git/trees", json={"base_tree": parent_commit["tree"]["sha"], "tree": entries}
            )
            head = self.api(
                repo, "POST", "git/commits", json={"message": key, "tree": tree["sha"], "parents": [parent]}
            )["sha"]
            if ref:
                self.api(repo, "PATCH", f"git/refs/heads/{branch}", json={"sha": head, "force": False})
            else:
                self.api(repo, "POST", "git/refs", json={"ref": f"refs/heads/{branch}", "sha": head})
        body = f"{marker}\n仕様 v{task.spec_version} {d['spec_hash']}\nCloses #{d['issue']}\n\n{d.get('implementation_summary', '')}"
        pr = (
            prs[0]
            if prs
            else self.api(
                repo,
                "POST",
                "pulls",
                json={
                    "title": f"[{task.id}] {d['summary'][:160]}",
                    "head": branch,
                    "base": repo.base,
                    "body": body,
                },
            )
        )
        actual = self.api(repo, "GET", f"pulls/{pr['number']}")
        if actual["head"]["sha"] != head or actual["head"]["repo"]["full_name"] != repo.repository:
            raise GuardError("Published PR does not match expected commit/repository")
        return {"pr": actual["number"], "head_sha": head, "pr_url": actual["html_url"]}

    def review(self, repo, task, result, key):
        reviews = self.pages(repo, f"pulls/{task.data['pr']}/reviews", "reviewer")
        existing = [r for r in reviews if key in (r.get("body") or "") and r["commit_id"] == result.head_sha]
        if existing:
            return existing[0]["id"]
        body = key + "\n" + result.summary
        for f in result.findings:
            body += (
                f"\n- {f.id} [{f.severity}] {f.file}:{f.line} (参考位置): {f.reason}\n  {f.requested_change}"
            )
        event = {"approve": "APPROVE", "request_changes": "REQUEST_CHANGES", "needs_human": "COMMENT"}[
            result.decision
        ]
        return self.api(
            repo,
            "POST",
            f"pulls/{task.data['pr']}/reviews",
            "reviewer",
            json={"commit_id": result.head_sha, "body": body, "event": event},
        )["id"]

    def snapshot(self, repo, task):
        d = task.data
        pr = self.api(repo, "GET", f"pulls/{d['pr']}")
        head = pr["head"]["sha"]
        changed = self.pages(repo, f"pulls/{d['pr']}/files")
        if len(changed) != pr["changed_files"]:
            raise GuardError("Incomplete PR file listing")
        source = self.source(repo, head)
        files = {f["filename"]: source.get(f["filename"]) for f in changed}
        checks = self.pages(repo, f"commits/{head}/check-runs?filter=latest", role="reviewer")
        reviews = self.pages(repo, f"pulls/{d['pr']}/reviews")
        latest = {}
        for r in reviews:
            if r["state"] != "COMMENTED":
                latest[r["user"]["login"]] = r
        review = latest.get(repo.reviewer_login, {})
        protection_ok = False
        try:
            protection = self.api(repo, "GET", f"branches/{quote(repo.base, safe='')}/protection")
            required = protection.get("required_status_checks") or {}
            approval = protection.get("required_pull_request_reviews") or {}
            pinned = {(c["context"], c.get("app_id")) for c in required.get("checks", [])}
            protection_ok = bool(
                required.get("strict")
                and approval.get("dismiss_stale_reviews")
                and approval.get("required_approving_review_count", 0) >= 1
                and protection.get("enforce_admins", {}).get("enabled")
                and protection.get("required_conversation_resolution", {}).get("enabled")
                and all((c.name, c.app_id) in pinned for c in repo.checks)
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in {403, 404}:
                raise
        return {
            "repository": pr["head"]["repo"]["full_name"],
            "base": pr["base"]["ref"],
            "branch": pr["head"]["ref"],
            "author": pr["user"]["login"],
            "marker": task.id if f"<!-- agent-task:{task.id} -->" in (pr.get("body") or "") else "",
            "state": pr["state"],
            "draft": pr["draft"],
            "mergeable": pr["mergeable"] is True,
            "head_sha": head,
            "base_sha": self.base(repo),
            "merged": pr["merged"],
            "merge_sha": pr.get("merge_commit_sha"),
            "files": files,
            "changed_lines": pr["additions"] + pr["deletions"],
            "protection_ok": protection_ok,
            "review_ok": review.get("state") == "APPROVED"
            and review.get("commit_id") == head
            and not any(r["state"] == "CHANGES_REQUESTED" for r in latest.values()),
            "spec_hash": digest(source.get(f"docs/tasks/{task.id}/spec.md", "")),
            "checks": [
                {
                    "name": c["name"],
                    "app_id": c["app"]["id"],
                    "head_sha": c["head_sha"],
                    "conclusion": c["conclusion"] if c["status"] == "completed" else "pending",
                }
                for c in checks
            ],
        }

    def merge(self, repo, task):
        return self.api(
            repo,
            "PUT",
            f"pulls/{task.data['pr']}/merge",
            "merger",
            json={"sha": task.data["head_sha"], "merge_method": "squash"},
        )


class MockGitHub:
    """Persistent deterministic simulator. Never calls GitHub."""

    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def read(self, key, default=None):
        with self.db.transaction() as s:
            op = s.scalar(select(Operation).where(Operation.key == "mock:" + key))
            return copy.deepcopy(op.data) if op else copy.deepcopy(default)

    def write(self, key, data):
        with self.db.transaction() as s:
            op = s.scalar(select(Operation).where(Operation.key == "mock:" + key))
            if op:
                op.data = data
            else:
                s.add(Operation(task_id="mock", key="mock:" + key, data=data, status="done"))

    def ensure_repo(self, repo, task):
        value = {"repository": repo.repository}
        self.write("repository:" + task.id, value)
        return value

    def base(self, repo, attempts=1):
        return self.read("base", {"sha": "a" * 40})["sha"]

    def source(self, repo, sha):
        return self.read("source:" + sha, {"src/example.py": "def greeting():\n    return 'hello'\n"})

    def issue(self, repo, task, body):
        value = self.read("issue:" + task.id)
        if value is None:
            value = {"number": int(hashlib.sha256(task.id.encode()).hexdigest()[:6], 16), "body": body}
            self.write("issue:" + task.id, value)
        return value["number"]

    def publish(self, repo, task, files, key):
        existing = self.read(key)
        if existing:
            return existing
        source = self.source(repo, task.data.get("head_sha") or task.data["base_sha"])
        for path, content in files.items():
            if content is None:
                source.pop(path, None)
            else:
                source[path] = content
        head = hashlib.sha1(key.encode()).hexdigest()
        self.write("source:" + head, source)
        number = self.issue(repo, task, "")
        result = {"pr": number, "head_sha": head, "pr_url": f"https://example.invalid/pull/{number}"}
        self.write(
            task.id,
            {
                "repository": repo.repository,
                "base": repo.base,
                "branch": task.data["branch"],
                "author": repo.publisher_login,
                "marker": task.id,
                "state": "open",
                "draft": False,
                "mergeable": True,
                "head_sha": head,
                "base_sha": self.base(repo),
                "merged": False,
                "merge_sha": None,
                "files": files,
                "changed_lines": 20,
                "protection_ok": True,
                "review_ok": False,
                "spec_hash": digest(source[f"docs/tasks/{task.id}/spec.md"]),
                "checks": [
                    {"name": c.name, "app_id": c.app_id, "head_sha": head, "conclusion": "success"}
                    for c in repo.checks
                ],
            },
        )
        self.write(key, result)
        return result

    def review(self, repo, task, result, key):
        snap = self.read(task.id)
        snap["review_ok"] = result.decision == "approve"
        self.write(task.id, snap)
        return 1

    def snapshot(self, repo, task):
        snap = self.read(task.id)
        snap["base_sha"] = self.base(repo)
        return snap

    def merge(self, repo, task):
        snap = self.snapshot(repo, task)
        if snap["head_sha"] != task.data["head_sha"]:
            raise GuardError("SHA mismatch")
        snap.update(merged=True, state="closed", merge_sha="f" * 40)
        self.write(task.id, snap)
        return {"merged": True, "sha": snap["merge_sha"]}
