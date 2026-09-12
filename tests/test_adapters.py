import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_workflow import step

from agent_team.adapters.codex import CodexRunner, MockRunner
from agent_team.adapters.github import GitHub
from agent_team.api import create_app
from agent_team.contracts import RunRequest
from agent_team.db import Task
from agent_team.policy import GuardError


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
        "upstream",
        "downstream",
        "sre",
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
