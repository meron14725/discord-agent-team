import fnmatch
import hashlib
import re
import time
from pathlib import PurePosixPath

from .contracts import DiscordSREPlan, DiscordTargetSnapshot


class GuardError(ValueError):
    pass


def digest(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def safe_path(path: str):
    p = PurePosixPath(path)
    if not path or p.is_absolute() or any(x in ("..", ".git") for x in p.parts) or "\\" in path:
        raise GuardError("Unsafe artifact path")
    if str(p) != path or any(ord(c) < 32 for c in path):
        raise GuardError("Noncanonical artifact path")


DISCORD_CHANNEL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,88}[a-z0-9])?$")


def discord_sre_change_digest(plan: DiscordSREPlan, before: DiscordTargetSnapshot | None) -> str:
    import json

    body = json.dumps(
        {
            "plan": plan.model_dump(mode="json"),
            "before": before.model_dump(mode="json") if before else None,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return digest(body)


def validate_discord_sre_plan(
    plan: DiscordSREPlan,
    before: DiscordTargetSnapshot | None,
    settings,
) -> str:
    sre = settings.discord_sre
    if not sre.enabled:
        raise GuardError("Discord SRE changes are disabled")
    if plan.guild_id != settings.guild_id:
        raise GuardError("Discord SRE guild mismatch")

    protected = set(sre.protected_channel_ids)
    if sre.audit_channel_id:
        protected.add(sre.audit_channel_id)
    if plan.target_id and plan.target_id in protected:
        raise GuardError("Protected Discord target")

    managed = set(sre.managed_category_ids)
    if plan.operation == "create_text_channel":
        if before is not None:
            raise GuardError("Create plans cannot bind an existing target")
        if plan.parent_category_id not in managed:
            raise GuardError("Discord category is outside the managed area")
        if not DISCORD_CHANNEL_NAME.fullmatch(plan.name) or "--" in plan.name:
            raise GuardError("Unsafe Discord channel name")
    else:
        if before is None or before.target_id != plan.target_id:
            raise GuardError("Discord target snapshot mismatch")
        if before.guild_id != plan.guild_id or before.category_id not in managed:
            raise GuardError("Discord target is outside the managed area")
        if plan.operation == "update_channel_topic" and before.kind != "text":
            raise GuardError("Only text channel topics may be updated")
        if plan.operation == "archive_thread" and before.kind != "thread":
            raise GuardError("Only threads may be archived")

    if len(plan.topic) > 1024 or any(ord(char) < 32 and char not in "\t" for char in plan.topic):
        raise GuardError("Unsafe Discord channel topic")
    return discord_sre_change_digest(plan, before)


FORBIDDEN = (
    ".github/*",
    "**/AGENTS.md",
    "AGENTS.md",
    ".codex/*",
    ".agents/*",
    "prompts/*",
    "config*.yaml",
    "compose*",
    "Dockerfile*",
    "docker/*",
    "*.pem",
    ".env*",
    "secrets/*",
)
HIGH_RISK = ("*lock*", "*auth*", "*billing*", "*migration*", "*infra*", "*requirements*", "*pyproject*")
SECRET = re.compile(r"-----BEGIN .*PRIVATE KEY-----|(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})")


def validate_maintenance_paths(paths):
    for path in paths:
        safe_path(path)
        if any(char in path for char in "*?[]"):
            raise GuardError("Maintenance paths must be exact")
        if not (path == "config.example.yaml" or path.startswith("src/") or path.startswith("tests/")
                or re.fullmatch(r"prompts/personas/[a-z_]+/v[0-9]+/PERSONA\.md", path)):
            raise GuardError("Maintenance path is outside persona implementation scope")
    return set(paths)


def maintenance_paths(settings, task):
    grant = settings.maintenance_authorizations.get(task.id)
    if grant is None:
        return []
    if (task.workflow_version != 2 or grant.owner_id not in settings.owner_ids
        or grant.repository != settings.repo_for(task).repository
        or grant.requirements_hash != task.data.get("requirements_hash")
        or grant.plan_hash != task.data.get("plan_hash")):
        raise GuardError("Stale maintenance authorization")
    return sorted(validate_maintenance_paths(grant.paths))


def validate_files(files, settings, task_id, spec_hash, maintenance=()):
    exceptions = validate_maintenance_paths(maintenance)
    if not files or len(files) > settings.max_files:
        raise GuardError("Empty or oversized change")
    if sum(len((v or "").encode()) for v in files.values()) > settings.max_bytes:
        raise GuardError("Artifact too large")
    for path, content in files.items():
        safe_path(path)
        if path not in exceptions and any(fnmatch.fnmatch(path, pattern) for pattern in FORBIDDEN):
            raise GuardError(f"Protected path: {path}")
        if content and SECRET.search(content):
            raise GuardError("Potential secret in artifact")
    spec_path = f"docs/tasks/{task_id}/spec.md"
    if spec_path in files and (files[spec_path] is None or digest(files[spec_path]) != spec_hash):
        raise GuardError("Approved specification was modified")


def validate_review(result, spec_body):
    if result.decision != "approve":
        return
    if any(f.severity != "low" for f in result.findings):
        raise GuardError("Blocking findings cannot be approved")
    required = set(re.findall(r"\bAC-\d+\b", spec_body))
    covered = {c.acceptance_id for c in result.coverage if c.status == "met" and c.evidence.strip()}
    if not required or not required <= covered or any(c.status != "met" for c in result.coverage):
        raise GuardError("Missing acceptance evidence")


def merge_gate(task, snapshot, repo, settings, now=None):
    now = now or time.time()
    d = task.data
    if task.state not in {"AwaitingChecks", "AwaitingMergeApproval", "ReadyToMerge", "Merging"}:
        raise GuardError("Task is not mergeable")
    if settings.merge_mode == "disabled":
        return "disabled"
    expected = (repo.repository, repo.base, d["branch"], repo.publisher_login)
    actual = tuple(snapshot.get(k) for k in ("repository", "base", "branch", "author"))
    if actual != expected or snapshot.get("marker") != task.id:
        raise GuardError("PR identity mismatch")
    if snapshot.get("state") != "open" or snapshot.get("draft") or not snapshot.get("mergeable"):
        raise GuardError("PR is closed, draft, conflicting, or mergeability unknown")
    if snapshot.get("head_sha") != d.get("head_sha") or snapshot.get("base_sha") != d.get("base_sha"):
        raise GuardError("Commit changed; approvals must be invalidated")
    if task.workflow_version == 2:
        if not d.get("requirements_approval_id") or not d.get("plan_approval_id"):
            raise GuardError("Requirements or implementation plan is unapproved")
        expected_review = [d["head_sha"], d["base_sha"], d["requirements_hash"], d["plan_hash"]]
        authority_hash = d["requirements_hash"]
    else:
        if d.get("spec_approval") != d.get("spec_hash"):
            raise GuardError("Specification is unapproved")
        expected_review = [d["head_sha"], d["base_sha"], d["spec_hash"]]
        authority_hash = d["spec_hash"]
    if d.get("review_approval") != expected_review:
        raise GuardError("Current commits have no review approval")
    if not snapshot.get("protection_ok") or not snapshot.get("review_ok"):
        raise GuardError("Strict base protection / independent review unverified")
    checks = snapshot.get("checks", [])
    if not repo.checks:
        raise GuardError("No trusted CI configured")
    for required in repo.checks:
        matching = [c for c in checks if c["name"] == required.name and c["app_id"] == required.app_id]
        if matching and any(
            c["head_sha"] == d["head_sha"]
            and c["conclusion"] in {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
            for c in matching
        ):
            return "failed_checks"
        if not matching or any(
            c["head_sha"] != d["head_sha"] or c["conclusion"] != "success" for c in matching
        ):
            return "waiting_checks"
    files = snapshot.get("files", {})
    validate_files(files, settings, task.id, authority_hash)
    if snapshot.get("spec_hash") != authority_hash:
        raise GuardError("PR specification hash mismatch")
    low_risk = all(
        any(fnmatch.fnmatch(p, a) for a in repo.allowed_paths)
        and not any(fnmatch.fnmatch(p.lower(), a) for a in HIGH_RISK)
        for p in files
    )
    low_risk = (
        low_risk and snapshot.get("changed_lines", settings.auto_max_lines + 1) <= settings.auto_max_lines
    )
    if settings.merge_mode == "auto_low_risk" and low_risk:
        return "ready"
    human = d.get("merge_approval") or {}
    if human.get("commits") != expected_review or human.get("expires", 0) <= now:
        return "needs_human"
    return "ready"
