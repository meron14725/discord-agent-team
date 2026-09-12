import copy

import pytest
from test_workflow import ready

from agent_team.contracts import Result
from agent_team.db import Task
from agent_team.policy import GuardError, merge_gate, safe_path, validate_files, validate_review


@pytest.mark.parametrize(
    "path", ["/tmp/x", "../escape", "a/../../x", ".git/config", "a/.git/config", "a\\x", "a//x", "./x"]
)
def test_path_traversal_rejected(path):
    with pytest.raises(GuardError):
        safe_path(path)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "AGENTS.md",
        "docs/AGENTS.md",
        ".env",
        "secrets/key",
        "Dockerfile",
        "prompts/company-policy.md",
        "prompts/roles/downstream.md",
    ],
)
def test_control_and_secret_changes_rejected(team, path):
    with pytest.raises(GuardError):
        validate_files({path: "content"}, team[0], "TASK-1", "sha256:test")


@pytest.mark.parametrize("conclusion", ["pending", "failure", "neutral", "skipped", "unknown", "cancelled"])
def test_non_success_ci_never_merges(team, conclusion):
    settings, _, github, _, _, _ = team
    current = ready(team)
    task = Task(id=current["id"], repo="demo", state=current["state"], data=current["data"])
    snap = github.read(task.id)
    snap["checks"][0]["conclusion"] = conclusion
    assert merge_gate(task, snap, settings.repos["demo"], settings) != "ready"


def test_spoofed_check_issuer_does_not_pass(team):
    settings, _, github, _, _, _ = team
    current = ready(team)
    task = Task(id=current["id"], repo="demo", state=current["state"], data=current["data"])
    snap = github.read(task.id)
    snap["checks"][0]["app_id"] = 999
    assert merge_gate(task, snap, settings.repos["demo"], settings) == "waiting_checks"


def test_auto_low_risk_requires_paths_size_and_checks(team):
    settings, _, github, _, _, _ = team
    current = ready(team)
    settings.merge_mode = "auto_low_risk"
    task = Task(id=current["id"], repo="demo", state="AwaitingChecks", data=current["data"])
    snap = github.read(task.id)
    assert merge_gate(task, snap, settings.repos["demo"], settings) == "ready"
    snap["changed_lines"] = 10000
    assert merge_gate(task, snap, settings.repos["demo"], settings) == "needs_human"


def test_expired_human_approval_requires_renewal(team):
    settings, _, github, _, _, _ = team
    current = ready(team)
    task = Task(id=current["id"], repo="demo", state=current["state"], data=copy.deepcopy(current["data"]))
    task.data["merge_approval"] = {"commits": task.data["review_approval"], "expires": 1}
    assert merge_gate(task, github.read(task.id), settings.repos["demo"], settings, now=2) == "needs_human"


def test_missing_ac_evidence_cannot_approve():
    result = Result(
        schema_version=1,
        task_id="T",
        spec_version=1,
        spec_hash="hash",
        head_sha="a",
        base_sha="b",
        status="completed",
        summary="",
        spec_markdown="",
        questions=[],
        decision="approve",
        findings=[],
        coverage=[],
        plan="",
        risks=[],
        coordination=None,
        specialist=None,
    )
    with pytest.raises(GuardError):
        validate_review(result, "AC-001: must pass")
