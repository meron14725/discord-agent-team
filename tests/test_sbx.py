import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_team.adapters.codex import MockRunner
from agent_team.adapters.sbx import SbxRunner
from agent_team.contracts import RunRequest
from agent_team.policy import GuardError
from agent_team.worker import create_worker

TOKEN = "test-worker-credential-" * 3


def request(**overrides):
    values = dict(
        job_id="job-1",
        role="upstream",
        kind="clarify",
        task_id="task-1",
        spec_version=1,
        spec_hash="hash",
        base_sha="base",
        head_sha="head",
        prompt="sample",
        files={},
        test_commands=[],
        model="",
        timeout=10,
    )
    return RunRequest(**(values | overrides))


def test_host_worker_requires_auth_for_run_cancel_and_health():
    with TestClient(create_worker(MockRunner(), TOKEN, "both")) as client:
        assert client.post("/run", json=request().model_dump()).status_code == 401
        assert client.post("/cancel/job-1").status_code == 401
        assert client.get("/health").status_code == 401
        headers = {"Authorization": "Bearer " + TOKEN}
        assert client.post("/run", json=request().model_dump(), headers=headers).status_code == 200
        assert (
            client.post(
                "/run",
                json=request(role="coordinator", kind="coordinate").model_dump(),
                headers=headers,
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/run",
                json=request(role="sre", kind="respond").model_dump(),
                headers=headers,
            ).status_code
            == 200
        )
        assert (
            client.post("/run", json=request(kind="implement").model_dump(), headers=headers).status_code
            == 403
        )
        assert (
            client.post("/run", json=request(auth_mode="api_key").model_dump(), headers=headers).status_code
            == 403
        )


@pytest.mark.parametrize(
    ("worker_role", "request_role"),
    [
        ("conversation-upstream", "cto"),
        ("conversation-downstream", "backend_integrator"),
        ("sre", "security_sre"),
    ],
)
def test_conversation_workers_accept_canonical_v2_role_ids(worker_role, request_role):
    with TestClient(create_worker(MockRunner(), TOKEN, worker_role)) as client:
        response = client.post(
            "/run",
            json=request(role=request_role, kind="respond").model_dump(),
            headers={"Authorization": "Bearer " + TOKEN},
        )
    assert response.status_code == 200


def test_specialist_pool_runs_two_agents_concurrently_and_separates_sre():
    class ConcurrentRunner(MockRunner):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.peak = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, value):
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 2:
                self.started.set()
            try:
                await self.release.wait()
                return await super().run(value)
            finally:
                self.active -= 1

    async def verify():
        runner = ConcurrentRunner()
        app = create_worker(runner, TOKEN, "specialists", capacity=2)
        headers = {"Authorization": "Bearer " + TOKEN}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
            first = asyncio.create_task(
                client.post(
                    "/run",
                    json=request(job_id="up", role="upstream", kind="respond").model_dump(),
                    headers=headers,
                )
            )
            second = asyncio.create_task(
                client.post(
                    "/run",
                    json=request(job_id="down", role="downstream", kind="respond").model_dump(),
                    headers=headers,
                )
            )
            await asyncio.wait_for(runner.started.wait(), 1)
            busy = await client.post(
                "/run",
                json=request(job_id="third", role="upstream", kind="respond").model_dump(),
                headers=headers,
            )
            separated = await client.post(
                "/run",
                json=request(job_id="sre", role="sre", kind="respond").model_dump(),
                headers=headers,
            )
            assert busy.status_code == 409
            assert separated.status_code == 403
            runner.release.set()
            responses = await asyncio.gather(first, second)
            assert [response.status_code for response in responses] == [200, 200]
            assert runner.peak == 2

    asyncio.run(verify())


@pytest.mark.parametrize(
    "change", [{"auth_mode": "api_key"}, {"kind": "implement"}, {"files": {"../escape": "x"}}, {"timeout": 0}]
)
def test_invalid_request_never_launches_vm(change, tmp_path):
    class NeverLaunch(SbxRunner):
        async def command(self, *args, **kwargs):
            pytest.fail("Invalid input must be rejected before launching sbx")

    with pytest.raises(GuardError):
        asyncio.run(NeverLaunch(tmp_path).run(request(**change)))


def test_timeout_removes_vm_even_when_exec_is_stuck(tmp_path):
    class StuckRunner(SbxRunner):
        removed = []

        async def execute(self, request, snapshot, started):
            self.names[request.job_id] = "dat-job-owned"
            await asyncio.sleep(30)

        async def command(self, job_id, args, *other, **kwargs):
            self.removed.append(args)

    runner = StuckRunner(tmp_path)
    with pytest.raises(TimeoutError):
        asyncio.run(runner.run(request(timeout=1)))
    assert runner.removed == [["rm", "--force", "dat-job-owned"]]
    assert not runner.names
    assert not list(tmp_path.iterdir())


def test_restart_recovers_only_journaled_vms_and_excludes_second_launcher(tmp_path):
    name = "dat-job-" + "a" * 16
    (tmp_path / "vms.json").write_text(json.dumps({"interrupted-job": name}))

    class RecoveryRunner(SbxRunner):
        removed = []

        async def command(self, job_id, args, *other, **kwargs):
            self.removed.append(args)

    async def verify():
        runner = RecoveryRunner(state_dir=tmp_path)
        await runner.startup()
        assert runner.removed == [["rm", "--force", name]]
        assert json.loads((tmp_path / "vms.json").read_text()) == {}
        other = RecoveryRunner(state_dir=tmp_path)
        try:
            with pytest.raises(BlockingIOError):
                await other.startup()
        finally:
            if other.lock_file:
                other.lock_file.close()
        await runner.shutdown()

    asyncio.run(verify())


@pytest.mark.parametrize("brokered,maintenance", [(False, False), (True, False), (True, True)])
def test_runner_denies_template_network_and_rejects_stale_identity(tmp_path, brokered, maintenance):
    class ScriptedRunner(SbxRunner):
        calls = []
        model_prompt = ""

        async def command(self, job_id, args, *other, **kwargs):
            self.calls.append(args)
            if "--output-last-message" in args:
                self.model_prompt = other[-1]
            if args[-2:] == ["cat", "/tmp/team-result.json"]:
                response = await MockRunner().run(request(task_id="stale-task"))
                return response.result.model_dump_json()
            if args == ["version"]:
                return "sbx version: v0.42.1 validated"
            if args[:2] == ["settings", "get"]:
                return "false"
            if args == ["mcp", "ls"]:
                return "No MCP servers registered"
            if args[0] == "inspect":
                return "Auth mode: oauth"
            if args[:2] == ["policy", "ls"]:
                return json.dumps(
                    {"rules": [{"decision": "allow", "resources": ["chatgpt.com:443", "api.github.com:443"]}]}
                )
            if args[:2] == ["policy", "check"]:
                return json.dumps({"allowed": args[-1] == "chatgpt.com:443"})
            if args[-2:] == ["codex", "--version"]:
                return "codex-cli 0.149.1"
            if args[0] == "exec" and "-c" in args and "Non-regular artifact" in args[-2]:
                response = await MockRunner().run(request(task_id="stale-task"))
                return json.dumps({"files": {}, "result": response.result.model_dump()})
            return ""

    runner = ScriptedRunner(tmp_path)
    with pytest.raises(GuardError, match="identity"):
        asyncio.run(runner.run(request(
            role="backend_integrator" if brokered else "upstream",
            kind="implement" if brokered else "clarify",
            maintenance_paths=["src/app.py"] if maintenance else [],
            files={"app.py": "value = 42\n"},
            test_commands=[["python", "-m", "pytest"]] if brokered else [],
        )))
    if brokered:
        supplied = json.loads(runner.model_prompt.rsplit("\n", 1)[-1])
        if maintenance:
            assert supplied["source_paths"] == ["app.py"]
            assert "shell read commands" in runner.model_prompt
        else:
            assert supplied["files"] == {"app.py": "value = 42\n"}
        assert supplied["test_commands"] == [["python", "-m", "pytest"]]
    create = next(args for args in runner.calls if args[0] == "create")
    assert create[-1].endswith("/source:ro") and create[-2].endswith("/scratch")
    assert any(
        args[:3] == ["policy", "deny", "network"] and args[-1] == "api.github.com:443"
        for args in runner.calls
    )
    assert runner.calls[-1][0:2] == ["rm", "--force"]
    invocation = next(args for args in runner.calls if "--output-last-message" in args)
    assert "--ignore-user-config" in invocation
    assert 'model_providers.sandboxd.base_url="https://chatgpt.com/backend-api/codex"' in invocation


def test_brokered_source_context_delivers_exact_contents_and_command_allowlist():
    from agent_team.adapters.sbx import brokered_source_context

    req = request(role='backend_integrator', kind='implement',
                  files={'src/app.py': 'value = "日本語"\n', 'tests/test_app.py': 'assert True\n'},
                  test_commands=[['python', '-m', 'pytest']])
    context = brokered_source_context(req)
    supplied = json.loads(context.split('\n', 2)[-1])
    assert supplied == {'files': req.files, 'test_commands': req.test_commands}
    assert 'untrusted data' in context
    assert 'without invoking shell' in context
    assert 'Do not claim tests were run' in context


def test_runtime_support_is_retained_but_not_duplicated_into_maintenance_prompt():
    from agent_team.adapters.sbx import brokered_source_context

    req = request(role='backend_integrator', kind='implement', maintenance_paths=['src/app.py'],
                  files={'src/app.py': 'value = 42', 'tests/test_app.py': 'assert True',
                         'vendor/bundle.js': 'x' * 1_000_000, 'docs/old-plan.md': 'historical'})
    payload = json.loads(brokered_source_context(req).rsplit('\n', 1)[-1])
    assert payload['source_paths'] == sorted(req.files)
    assert 'read-only source' in brokered_source_context(req)
    assert 'files' not in payload
    assert len(req.files['vendor/bundle.js']) == 1_000_000
    assert len(brokered_source_context(req)) < 2000


def test_maintenance_test_runtime_installs_offline_and_only_when_authorized(tmp_path):
    (tmp_path / 'requirements.txt').write_text('pytest==9.1.1\n')
    (tmp_path / 'wheels').mkdir()

    class RuntimeRunner(SbxRunner):
        calls = []

        async def command(self, job_id, args, *other, **kwargs):
            self.calls.append(args)
            return ''

    runner = RuntimeRunner(test_runtime_dir=tmp_path)
    req = request(role='backend_integrator', kind='implement', test_commands=[['python','-m','pytest']])
    asyncio.run(runner.prepare_test_runtime('job', 'sandbox', req))
    assert runner.calls == []
    asyncio.run(runner.prepare_test_runtime('job', 'sandbox', req.model_copy(update={'maintenance_paths':['src/app.py']})))
    assert len(runner.calls) == 3
    install = runner.calls[1]
    assert '--no-index' in install and '--require-hashes' in install
    assert install[:4] == ['exec','--user','root','sandbox']
    assert runner.calls[2][-2:] == ['/usr/bin/python3','/usr/local/bin/python']
    (tmp_path / 'requirements.txt').unlink()
    with pytest.raises(GuardError, match='unavailable'):
        asyncio.run(runner.prepare_test_runtime('job', 'sandbox', req.model_copy(update={'maintenance_paths':['src/app.py']})))


def test_collector_allows_baseline_plus_new_files_but_stays_bounded(tmp_path):
    import subprocess
    import sys

    from agent_team.adapters.sbx import COLLECT

    source = tmp_path / 'source'
    source.mkdir()
    result_path = tmp_path / 'result.json'
    result_path.write_text('{}')
    for number in range(101):
        (source / f'{number}.txt').write_text('content')
    script = COLLECT.replace('/tmp/team-result.json', str(result_path))
    completed = subprocess.run([sys.executable, '-c', script, str(source)], capture_output=True, text=True)
    assert completed.returncode == 0
    assert len(json.loads(completed.stdout)['files']) == 101
    for number in range(101, 201):
        (source / f'{number}.txt').write_text('content')
    exceeded = subprocess.run([sys.executable, '-c', script, str(source)], capture_output=True, text=True)
    assert exceeded.returncode != 0 and 'Artifact limit exceeded' in exceeded.stderr
