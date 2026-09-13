"""Trusted publisher: REST git objects, no GitHub credentials in agent workspaces."""

import base64
import copy
import fnmatch
import hashlib
import mimetypes
import time
from urllib.parse import quote

import httpx
from sqlalchemy import select

from ..db import Operation
from ..policy import GuardError, digest, safe_path
from ..redaction import SecretScanner
from .github_auth import HEADERS, InstallationAuth


class GitHub:
    def __init__(self, settings, tokens, reviewer_private_key="", scanner=None):
        self.settings = settings
        self.scanner = scanner or SecretScanner(b"github-adapter-test-salt")
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

    def source(self, repo, sha, *, paths=None):
        tree = self.api(repo, "GET", f"git/trees/{sha}?recursive=1")
        if tree.get("truncated"):
            raise GuardError("Truncated repository tree")
        files, total = {}, 0
        for entry in tree["tree"]:
            if entry["type"] == "tree":
                continue
            if paths is not None and entry["path"] not in paths:
                continue
            safe_path(entry["path"])
            if entry["mode"] not in {"100644", "100755"}:
                raise GuardError("MVP does not support symlinks/submodules")
            total += entry.get("size", 0)
            if total > self.settings.max_bytes or len(files) >= self.settings.max_files:
                raise GuardError("Source exceeds small-repository MVP limit")
            raw = self.api(repo, "GET", f"git/blobs/{entry['sha']}")
            content = base64.b64decode(raw["content"]).decode("utf-8")
            if self.scanner.scan_text(content).blocked:
                raise GuardError("Potential secret blocked at GitHub source boundary")
            files[entry["path"]] = content
        return files

    def source_context(self, repo, sha):
        """Build a model snapshot without removing excluded blobs from the Git tree."""
        tree = self.api(repo, "GET", f"git/trees/{sha}?recursive=1")
        if tree.get("truncated"):
            raise GuardError("Truncated repository tree")
        files, manifest, forwarded = {}, [], 0
        for entry in tree["tree"]:
            if entry["type"] == "tree":
                continue
            path = entry["path"]
            safe_path(path)
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            metadata = {
                "path": path,
                "mode": entry["mode"],
                "size": entry.get("size", 0),
                "git_sha": entry["sha"],
                "mime_type": mime,
            }
            allowed = entry["mode"] in {"100644", "100755"} and any(
                fnmatch.fnmatch(path, pattern) for pattern in repo.allowed_paths
            )
            if not allowed:
                manifest.append({**metadata, "forwarded": False, "reason": "path_or_type"})
                continue
            raw = self.api(repo, "GET", f"git/blobs/{entry['sha']}")
            content = base64.b64decode(raw["content"])
            inspection = self.scanner.inspect_content(content, mime)
            if inspection.blocked:
                reason = "binary" if inspection.metadata.binary else "secret"
                manifest.append(
                    {
                        **metadata,
                        "content_hash": inspection.metadata.content_hash,
                        "forwarded": False,
                        "reason": reason,
                    }
                )
                continue
            forwarded += len(content)
            if forwarded > self.settings.max_bytes or len(files) >= self.settings.max_files:
                manifest.append({**metadata, "forwarded": False, "reason": "limit"})
                continue
            files[path] = inspection.text
            manifest.append(
                {
                    **metadata,
                    "content_hash": inspection.metadata.content_hash,
                    "forwarded": True,
                    "reason": "",
                }
            )
        return {"files": files, "manifest": manifest}

    def issue(self, repo, task, body):
        if self.scanner.scan_text(body).blocked:
            raise GuardError("Potential secret blocked at GitHub Issue boundary")
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

    def issue_reference(self, repo, issue_number):
        """Read the Issue authority together with the validators used by approval fences."""
        response = self.clients["publisher"].get(
            f"/repos/{repo.repository}/issues/{int(issue_number)}"
        )
        response.raise_for_status()
        data = response.json()
        if "pull_request" in data:
            raise GuardError("Requirements authority must be a GitHub Issue")
        etag = response.headers.get("etag", "")
        body = data.get("body") or ""
        if self.scanner.scan_text(body).blocked:
            raise GuardError("Potential secret blocked at GitHub Issue boundary")
        return {
            "number": data["number"],
            "url": data["html_url"],
            "body": body,
            "body_hash": digest(body),
            "updated_at": data["updated_at"],
            "etag": etag,
        }

    def update_issue_body(self, repo, issue_number, body, *, expected_etag, preflight_confirmed):
        """Replace an Issue body only in environments where stale ETags were proven to fail."""
        if not preflight_confirmed or not expected_etag:
            raise GuardError("Conditional Issue update is unavailable; publish a proposal comment")
        if self.scanner.scan_text(body).blocked:
            raise GuardError("Potential secret blocked at GitHub Issue boundary")
        response = self.clients["publisher"].patch(
            f"/repos/{repo.repository}/issues/{int(issue_number)}",
            headers={"If-Match": expected_etag},
            json={"body": body},
        )
        if response.status_code in {409, 412}:
            raise GuardError("GitHub Issue changed concurrently")
        response.raise_for_status()
        return self.issue_reference(repo, issue_number)

    def comment_issue(self, repo, issue_number, body):
        if not body.strip():
            raise GuardError("Issue comment cannot be empty")
        if self.scanner.scan_text(body).blocked:
            raise GuardError("Potential secret blocked at GitHub comment boundary")
        result = self.api(
            repo,
            "POST",
            f"issues/{int(issue_number)}/comments",
            json={"body": body},
        )
        return {"id": result["id"], "url": result["html_url"]}

    def set_issue_state(self, repo, issue_number, state):
        if state not in {"open", "closed"}:
            raise GuardError("Unsupported Issue state")
        result = self.api(
            repo,
            "PATCH",
            f"issues/{int(issue_number)}",
            json={"state": state},
        )
        if result.get("state") != state:
            raise GuardError("GitHub Issue state did not converge")
        return {"number": result["number"], "state": result["state"]}

    def publish(self, repo, task, files, key):
        d, branch = task.data, task.data["branch"]
        legacy_branch = branch.startswith(f"agent/{task.id}/")
        v2_branch = task.workflow_version == 2 and branch.startswith(
            f"agent/issue-{d.get('requirements_issue')}-"
        )
        if not (legacy_branch or v2_branch) or branch == repo.base:
            raise GuardError("Publisher branch denied")
        marker = f"<!-- agent-task:{task.id} -->"
        if any(content and self.scanner.scan_text(content).blocked for content in files.values()):
            raise GuardError("Potential secret blocked at GitHub publish boundary")
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
        authority_hash = d.get("requirements_hash", d.get("spec_hash", ""))
        authority_issue = d.get("requirements_issue", d.get("issue"))
        body = (
            f"{marker}\n要件 {authority_hash}\nCloses #{authority_issue}\n\n"
            f"{d.get('implementation_summary', '')}"
        )
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
                    "draft": task.workflow_version == 2,
                },
            )
        )
        actual = self.confirm_published_pr(repo, pr["number"], branch, head)
        return {"pr": actual["number"], "head_sha": head, "pr_url": actual["html_url"]}

    def confirm_published_pr(self, repo, number, branch, head):
        for attempt in range(3):
            actual = self.api(repo, "GET", f"pulls/{number}")
            if actual["head"]["repo"]["full_name"] != repo.repository:
                raise GuardError("Published PR repository mismatch")
            if actual["head"]["sha"] == head:
                return actual
            ref = self.api(repo, "GET", f"git/ref/heads/{branch}")
            if ref["object"]["sha"] != head:
                raise GuardError("Branch changed while confirming published PR")
            if attempt < 2:
                time.sleep(attempt + 1)
        raise GuardError("Published PR does not match expected commit/repository")

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

    def ready_for_review(self, repo, task):
        pr = self.api(repo, "GET", f"pulls/{task.data['pr']}")
        if not pr.get("draft"):
            return {"ready": True}
        response = self.clients["publisher"].post(
            "/graphql",
            json={
                "query": (
                    "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id})"
                    "{pullRequest{id isDraft}}}"
                ),
                "variables": {"id": pr["node_id"]},
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors") or payload.get("data", {}).get(
            "markPullRequestReadyForReview", {}
        ).get("pullRequest", {}).get("isDraft") is not False:
            raise GuardError("GitHub did not mark the Draft PR ready for review")
        return {"ready": True}

    def snapshot(self, repo, task):
        d = task.data
        pr = self.api(repo, "GET", f"pulls/{d['pr']}")
        head = pr["head"]["sha"]
        changed = self.pages(repo, f"pulls/{d['pr']}/files")
        if len(changed) != pr["changed_files"]:
            raise GuardError("Incomplete PR file listing")
        source = self.source(repo, head, paths={f["filename"] for f in changed})
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
            "spec_hash": (
                task.data.get("requirements_hash", "")
                if task.workflow_version == 2
                else digest(source.get(f"docs/tasks/{task.id}/spec.md", ""))
            ),
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

    def __init__(self, db, settings, scanner=None):
        self.db, self.settings = db, settings
        self.scanner = scanner or SecretScanner(b"mock-github-adapter-salt")

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

    def source_context(self, repo, sha):
        source = self.source(repo, sha)
        files, manifest = {}, []
        for path, content in source.items():
            allowed = any(fnmatch.fnmatch(path, pattern) for pattern in repo.allowed_paths)
            report = self.scanner.scan_text(content)
            forwarded = allowed and not report.blocked
            if forwarded:
                files[path] = content
            manifest.append(
                {
                    "path": path,
                    "mode": "100644",
                    "size": len(content.encode()),
                    "content_hash": digest(content),
                    "mime_type": mimetypes.guess_type(path)[0] or "text/plain",
                    "forwarded": forwarded,
                    "reason": "" if forwarded else ("secret" if report.blocked else "path_or_type"),
                }
            )
        return {"files": files, "manifest": manifest}

    def issue(self, repo, task, body):
        if self.scanner.scan_text(body).blocked:
            raise GuardError("Potential secret blocked at GitHub Issue boundary")
        value = self.read("issue:" + task.id)
        if value is None:
            value = {
                "number": int(hashlib.sha256(task.id.encode()).hexdigest()[:6], 16),
                "body": body,
                "updated_at": "2026-01-01T00:00:00Z",
                "revision": 1,
            }
            self.write("issue:" + task.id, value)
        return value["number"]

    def _issue_key(self, issue_number):
        matches = []
        with self.db.transaction() as s:
            for operation in s.scalars(select(Operation).where(Operation.key.like("mock:issue:%"))):
                if operation.data.get("number") == int(issue_number):
                    matches.append(operation.key.removeprefix("mock:"))
        if len(matches) != 1:
            raise GuardError("Unknown or duplicate mock Issue")
        return matches[0]

    def issue_reference(self, repo, issue_number):
        value = self.read(self._issue_key(issue_number))
        body = value.get("body", "")
        revision = value.get("revision", 1)
        return {
            "number": value["number"],
            "url": f"https://github.com/{repo.repository}/issues/{value['number']}",
            "body": body,
            "body_hash": digest(body),
            "updated_at": value.get("updated_at", "2026-01-01T00:00:00Z"),
            "etag": f'"mock-{revision}"',
        }

    def update_issue_body(self, repo, issue_number, body, *, expected_etag, preflight_confirmed):
        if not preflight_confirmed or not expected_etag:
            raise GuardError("Conditional Issue update is unavailable; publish a proposal comment")
        if self.scanner.scan_text(body).blocked:
            raise GuardError("Potential secret blocked at GitHub Issue boundary")
        key = self._issue_key(issue_number)
        value = self.read(key)
        expected = f'"mock-{value.get("revision", 1)}"'
        if expected_etag != expected:
            raise GuardError("GitHub Issue changed concurrently")
        value.update(
            body=body,
            revision=value.get("revision", 1) + 1,
            updated_at=f"2026-01-01T00:00:{value.get('revision', 1):02d}Z",
        )
        self.write(key, value)
        return self.issue_reference(repo, issue_number)

    def comment_issue(self, repo, issue_number, body):
        if not body.strip():
            raise GuardError("Issue comment cannot be empty")
        if self.scanner.scan_text(body).blocked:
            raise GuardError("Potential secret blocked at GitHub comment boundary")
        self._issue_key(issue_number)
        key = f"issue-comments:{issue_number}"
        comments = self.read(key, [])
        number = len(comments) + 1
        result = {
            "id": number,
            "url": f"https://github.com/{repo.repository}/issues/{issue_number}#issuecomment-{number}",
            "body": body,
        }
        comments.append(result)
        self.write(key, comments)
        return {"id": result["id"], "url": result["url"]}

    def set_issue_state(self, repo, issue_number, state):
        if state not in {"open", "closed"}:
            raise GuardError("Unsupported Issue state")
        key = self._issue_key(issue_number)
        value = self.read(key)
        value["state"] = state
        self.write(key, value)
        return {"number": value["number"], "state": state}

    def publish(self, repo, task, files, key):
        if any(content and self.scanner.scan_text(content).blocked for content in files.values()):
            raise GuardError("Potential secret blocked at GitHub publish boundary")
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
        number = task.data.get("requirements_issue") or self.issue(repo, task, "")
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
                "draft": task.workflow_version == 2,
                "mergeable": True,
                "head_sha": head,
                "base_sha": self.base(repo),
                "merged": False,
                "merge_sha": None,
                "files": files,
                "changed_lines": 20,
                "protection_ok": True,
                "review_ok": False,
                "spec_hash": (
                    task.data.get("requirements_hash", "")
                    if task.workflow_version == 2
                    else digest(source[f"docs/tasks/{task.id}/spec.md"])
                ),
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

    def ready_for_review(self, repo, task):
        snap = self.read(task.id)
        snap["draft"] = False
        self.write(task.id, snap)
        return {"ready": True}

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
