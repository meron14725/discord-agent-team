import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_workflow import step

from agent_team.adapters.codex import CodexRunner, MockRunner, codex_output_schema
from agent_team.adapters.discord import (
    asks_task_status,
    confirms_issue_body_update,
    format_task_status,
    owner_message_links,
    task_id_from_text,
)
from agent_team.adapters.github import GitHub, MockGitHub
from agent_team.api import create_app
from agent_team.contracts import CommandRequest, PatchProposal, RunRequest, WorkspaceReadRequest
from agent_team.db import Task
from agent_team.policy import GuardError


def test_codex_output_schema_requires_every_object_property():
    schema = codex_output_schema()

    def assert_strict_objects(node):
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                assert node["required"] == list(properties)
                assert node["additionalProperties"] is False
            for value in node.values():
                assert_strict_objects(value)
        elif isinstance(node, list):
            for value in node:
                assert_strict_objects(value)

    assert_strict_objects(schema)


def test_owner_message_links_only_returns_distinct_same_guild_targets():
    text = (
        "https://discord.com/channels/123/456/789 "
        "https://discord.com/channels/999/456/000 "
        "https://discordapp.com/channels/123/456/789 "
        "https://www.discord.com/channels/123/777/888"
    )

    assert owner_message_links(text, "123") == [(456, 789), (777, 888)]


def test_replied_bot_message_resolves_task_status_intent():
    assert task_id_from_text("**TASK-bfa8e370-148**\nBlocked") == "TASK-bfa8e370-148"
    assert task_id_from_text("通常の会話") == ""
    assert asks_task_status("これ、いまどうなってる？")
    assert asks_task_status("何待ちで止まってるの？")
    assert not asks_task_status("要件を一つ追加して")
    assert confirms_issue_body_update("Issue本文へ反映した")
    assert confirms_issue_body_update("Issue本文へ反映しました")
    assert not confirms_issue_body_update("まだ反映していない")


def test_task_status_reply_uses_database_state_and_latest_proposal():
    body = format_task_status(
        {
            "id": "TASK-bfa8e370-148",
            "state": "Blocked",
            "data": {
                "reason": "Issueコメントの要件案を本文へ反映後、再試行してください。",
                "requirements_proposal_url": "https://example.test/latest",
            },
        }
    )

    assert "停止中 (`Blocked`)" in body
    assert "Issueコメントの要件案" in body
    assert "https://example.test/latest" in body
    assert "次:" in body


def test_internal_task_status_endpoint_returns_current_task(team):
    settings, db, _, _, _, command = team
    task = command("request", repo="demo", text="状態参照テスト")
    app = create_app(db, settings, "internal-test-token-that-is-long-enough")
    client = TestClient(app)
    headers = {"Authorization": "Bearer internal-test-token-that-is-long-enough"}

    response = client.get(f"/tasks/{task['id']}", headers=headers)
    missing = client.get("/tasks/TASK-UNKNOWN", headers=headers)

    assert response.status_code == 200
    assert response.json()["state"] == task["state"]
    assert missing.status_code == 404


def test_per_task_repo_created_only_after_spec_approval(team):
    settings, _, github, service, engine, command = team
    settings.repos["demo"].per_task = True
    settings.repos["demo"].repository = "meron14725/project"
    task = command("request", repo="demo", text="new project")
    step(engine)
    assert github.read("repository:" + task["id"]) is None
    task = service.status(task["id"])
    assert task["data"]["repository"].startswith("meron14725/project-task-")
    command("approve_spec", task_id=task["id"], version=task["version"], hash=task["data"]["spec_hash"])
    step(engine)
    assert github.read("repository:" + task["id"])["repository"] == task["data"]["repository"]


def test_repository_provision_response_loss_reuses_marker(team):
    settings = team[0]
    settings.repos["demo"].per_task = True
    settings.repos["demo"].repository = "meron14725/project"
    task = Task(id="TASK-123", repo="demo", data={"repository": "meron14725/project-task-123"})
    repo = settings.repo_for(task)
    created = []

    def request(r):
        if r.url.path == "/user":
            return httpx.Response(200, json={"login": "meron14725"})
        if r.method == "GET":
            if created:
                return httpx.Response(
                    200,
                    json={
                        "full_name": repo.repository,
                        "private": True,
                        "description": "agent-task:TASK-123",
                    },
                )
            return httpx.Response(404)
        assert r.url.path == "/user/repos"
        created.append(json.loads(r.content))
        return httpx.Response(201, json={"full_name": repo.repository})

    github = GitHub(settings, {})
    github.clients["publisher"] = httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(request)
    )
    github.ensure_repo(repo, task)
    github.ensure_repo(repo, task)
    assert len(created) == 1 and created[0]["private"] is True


def test_new_template_branch_read_retries_only_expected_transient_errors(team, monkeypatch):
    settings = team[0]
    repo = settings.repos["demo"]
    responses = [httpx.Response(404), httpx.Response(409), httpx.Response(200, json={
        "commit": {"sha": "a" * 40}
    })]

    def request(request):
        return responses.pop(0)

    github = GitHub(settings, {})
    github.clients["publisher"] = httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(request)
    )
    monkeypatch.setattr("agent_team.adapters.github.time.sleep", lambda _: None)
    assert github.base(repo, attempts=3) == "a" * 40
    assert not responses


def test_issue_authority_conditional_update_and_comment_fallback(team):
    settings, db, _, _, _, _ = team
    github = MockGitHub(db, settings)
    task = Task(id="TASK-ISSUE", repo="demo", data={"summary": "requirements"})
    repo = settings.repos["demo"]
    issue_number = github.issue(repo, task, "first")
    initial = github.issue_reference(repo, issue_number)

    updated = github.update_issue_body(
        repo,
        issue_number,
        "second",
        expected_etag=initial["etag"],
        preflight_confirmed=True,
    )
    assert updated["body"] == "second" and updated["body_hash"] != initial["body_hash"]
    with pytest.raises(GuardError, match="concurrently"):
        github.update_issue_body(
            repo,
            issue_number,
            "lost update",
            expected_etag=initial["etag"],
            preflight_confirmed=True,
        )
    assert github.issue_reference(repo, issue_number)["body"] == "second"

    with pytest.raises(GuardError, match="proposal comment"):
        github.update_issue_body(
            repo,
            issue_number,
            "third",
            expected_etag=updated["etag"],
            preflight_confirmed=False,
        )
    comment = github.comment_issue(repo, issue_number, "本文変更案: third")
    assert "issuecomment" in comment["url"]


def test_live_issue_update_sends_if_match_and_rejects_stale(team):
    settings = team[0]
    seen = []

    def request(request):
        seen.append(request)
        if request.method == "PATCH":
            assert request.headers["if-match"] == '"etag-1"'
            return httpx.Response(412, request=request)
        return httpx.Response(500, request=request)

    github = GitHub(settings, {})
    github.clients["publisher"] = httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(request)
    )
    with pytest.raises(GuardError, match="concurrently"):
        github.update_issue_body(
            settings.repos["demo"],
            2,
            "new body",
            expected_etag='"etag-1"',
            preflight_confirmed=True,
        )
    assert len(seen) == 1


def test_codex_runner_blocks_secret_before_model_process(tmp_path):
    runner = CodexRunner(
        api_key="test-auth-value",
        workspace=tmp_path,
        scan_salt=b"test-only-worker-scan-salt",
    )
    called = False

    async def process(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("model process must not start")

    runner.sandbox_verified = True
    runner.process = process
    request = RunRequest(
        job_id="secret-boundary",
        role="cto",
        kind="clarify",
        task_id="TASK-1",
        spec_version=0,
        spec_hash="",
        base_sha="",
        head_sha="",
        prompt="Authorization: Bearer " + "X" * 30,
        files={},
        test_commands=[],
        model="",
        timeout=30,
    )
    with pytest.raises(GuardError, match="prompt boundary"):
        asyncio.run(runner.run(request))
    assert called is False


def test_internal_api_blocks_secret_before_task_creation(team):
    settings, db, _, _, _, _ = team
    app = create_app(db, settings, "test-token-that-is-at-least-thirty-two-bytes")
    client = TestClient(app)
    response = client.post(
        "/commands",
        json={
            "action": "request",
            "event_id": "secret-event",
            "actor": "demo-owner",
            "guild": "demo-guild",
            "channel": "demo-channel",
            "repo": "demo",
            "text": "sk-" + "Z" * 32,
        },
        headers={"Authorization": "Bearer test-token-that-is-at-least-thirty-two-bytes"},
    )
    assert response.status_code == 409
    assert "Discord input boundary" in response.json()["detail"]


def test_v2_patch_broker_applies_only_typed_patch_and_allowlisted_command(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("value = 1\n")
    runner = CodexRunner(
        api_key="test-auth-value",
        workspace=tmp_path,
        scan_salt=b"test-only-worker-scan-salt",
    )
    request = RunRequest(
        job_id="broker",
        role="backend_integrator",
        kind="implement",
        task_id="TASK-2",
        spec_version=1,
        spec_hash="sha256:req",
        base_sha="a" * 40,
        head_sha="b" * 40,
        prompt="safe",
        files={"app.py": "value = 1\n"},
        test_commands=[["python", "-m", "pytest"]],
        model="",
        timeout=30,
    )
    result = SimpleNamespace(
        workspace_reads=[WorkspaceReadRequest(paths=["app.py"])],
        commands=[CommandRequest(argv=["python", "-m", "pytest"])],
        patches=[
            PatchProposal(
                patch="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n",
                rationale="要件を満たす",
            )
        ],
    )
    asyncio.run(
        runner.apply_proposals(
            request,
            result,
            source,
            tmp_path,
            {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
            time.monotonic(),
        )
    )
    assert (source / "app.py").read_text() == "value = 2\n"


def test_model_repository_snapshot_excludes_secret_and_disallowed_content(team):
    settings, _, github, *_ = team
    sha = "d" * 40
    secret_value = "ghp_" + "S" * 36
    github.write(
        "source:" + sha,
        {
            "src/safe.py": "answer = 42\n",
            "src/secret.py": f"token = '{secret_value}'\n",
            ".github/workflows/ci.yml": "untrusted control data",
        },
    )
    snapshot = github.source_context(settings.repos["demo"], sha)
    assert snapshot["files"] == {"src/safe.py": "answer = 42\n"}
    assert secret_value not in repr(snapshot["manifest"])
    reasons = {item["path"]: item["reason"] for item in snapshot["manifest"]}
    assert reasons["src/secret.py"] == "secret"
    assert reasons[".github/workflows/ci.yml"] == "path_or_type"


def test_internal_api_auth_and_bot_rejection(team):
    settings, db, _, _, _, _ = team
    app = create_app(db, settings, "test-token")
    client = TestClient(app)
    payload = dict(
        action="request",
        event_id="event",
        actor="demo-owner",
        guild="demo-guild",
        channel="demo-channel",
        repo="demo",
        text="hello",
    )
    assert client.post("/commands", json=payload).status_code == 401
    payload["bot"] = True
    assert (
        client.post("/commands", json=payload, headers={"Authorization": "Bearer test-token"}).status_code
        == 409
    )
    coordination = client.post(
        "/coordinate",
        json={
            "event_id": "message-1",
            "actor": "demo-owner",
            "guild": "demo-guild",
            "channel": "demo-channel",
            "text": "みんなに挨拶して",
            "history": ["owner: こんにちは"],
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert coordination.status_code == 200
    assert coordination.json()["action"] == "delegate"
    assert {item["role"] for item in coordination.json()["delegations"]} == {
        "cto",
        "backend_integrator",
        "security_sre",
    }
    specialist = client.post(
        "/specialist-turn",
        json={
            "event_id": "message-1",
            "actor": "demo-owner",
            "guild": "demo-guild",
            "channel": "demo-channel",
            "text": "構成を確認して",
            "history": [],
            "role": "sre",
            "instruction": "稼働状況を見て回答する",
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert specialist.status_code == 200
    assert specialist.json()["action"] == "reply"


def test_discord_snapshot_is_guild_bound_and_sre_only(team):
    settings, db, _, _, _, _ = team
    app = create_app(db, settings, "test-token")
    client = TestClient(app)
    snapshot = {
        "guild_id": "other-guild",
        "bot_id": "sre-bot",
        "permissions": {
            "administrator": False,
            "view_audit_log": True,
            "manage_guild": False,
            "manage_channels": False,
            "manage_roles": False,
            "manage_messages": False,
            "manage_threads": False,
            "create_public_threads": True,
            "create_private_threads": True,
        },
        "roles": [],
        "channels": [],
        "recent_audit": [],
    }
    payload = {
        "event_id": "snapshot-1",
        "actor": "demo-owner",
        "guild": "demo-guild",
        "channel": "demo-channel",
        "text": "Discordを診断して",
        "history": [],
        "role": "sre",
        "instruction": "現在状態を確認する",
        "discord_snapshot": snapshot,
    }
    headers = {"Authorization": "Bearer test-token"}
    assert client.post("/specialist-turn", json=payload, headers=headers).status_code == 409
    payload["discord_snapshot"]["guild_id"] = "demo-guild"
    payload["role"] = "upstream"
    assert client.post("/specialist-turn", json=payload, headers=headers).status_code == 409


def test_subscription_runner_uses_shared_auth_without_api_fallback(tmp_path, monkeypatch):
    auth = tmp_path / "auth"
    auth.mkdir()
    (auth / "auth.json").write_text("{}")
    request = RunRequest(
        job_id="job",
        role="upstream",
        kind="clarify",
        task_id="T",
        spec_version=0,
        spec_hash="",
        base_sha="",
        head_sha="",
        prompt="{}",
        files={},
        test_commands=[],
        model="",
        timeout=10,
    )
    response = asyncio.run(MockRunner().run(request))
    seen = []

    class Fake(CodexRunner):
        async def preflight(self):
            pass

        async def process(self, job_id, args, cwd, env, timeout, stdin=None):
            seen.append((args, env))
            if "--version" in args:
                return 0, "codex-cli 0.154.0"
            Path(args[args.index("--output-last-message") + 1]).write_text(response.result.model_dump_json())
            return 0, '{"type":"turn.completed","usage":{"input_tokens":42}}'

    monkeypatch.setenv("OPENAI_API_KEY", "never-inherit")
    monkeypatch.setenv("GITHUB_TOKEN", "never-inherit")
    runner = Fake(auth_home=auth, workspace=tmp_path / "work")
    result = asyncio.run(runner.run(request))
    assert result.usage["input_tokens"] == 42
    args, env = seen[-1]
    assert "--model" not in args and args[args.index("--sandbox") + 1] == "read-only"
    assert env["CODEX_HOME"] == str(auth)
    assert not {"OPENAI_API_KEY", "CODEX_API_KEY", "GITHUB_TOKEN"} & env.keys()


def test_process_timeout_terminates_child_group(tmp_path):
    runner = CodexRunner(workspace=tmp_path)

    async def run():
        with pytest.raises(TimeoutError):
            await runner.process(
                "job",
                [sys.executable, "-c", "import time; time.sleep(60)"],
                tmp_path,
                {"PATH": "/usr/bin:/bin"},
                0.1,
            )
        assert not runner.processes

    asyncio.run(run())


def test_symlink_artifact_rejected(tmp_path):
    (tmp_path / "escape").symlink_to("/etc/passwd")
    with pytest.raises(GuardError):
        CodexRunner().collect(tmp_path)


def test_subscription_serializes_roles_and_enforces_run_cap(team):
    settings, _, _, service, engine, command = team
    settings.mode = "live"
    settings.auth_mode = "chatgpt"
    settings.daily_run_limit = 1
    first = command("request", repo="demo", text="one")
    second = command("request", repo="demo", text="two")
    claim = engine.claim()
    assert claim and engine.claim() is None
    asyncio.run(engine.execute(*claim))
    assert engine.claim() is None
    assert service.status(second["id"])["state"] == "Blocked"
    assert service.status(first["id"])["state"] == "AwaitingSpecApproval"
