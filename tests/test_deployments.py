import importlib.util
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_team.api import create_app
from agent_team.db import Approval, Job, Operation, Outbox, Task
from agent_team.deployments import bridge, propose
from agent_team.policy import GuardError

spec = importlib.util.spec_from_file_location("deploy_supervisor", "scripts/deploy_supervisor.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
SHA = "a" * 40
OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64


def proposal(team):
    settings, db, *_ = team
    settings.deployment.enabled = True
    settings.deployment.repository_alias = "demo"
    with db.transaction() as session:
        task = Task(id="deploy-task", repo="demo", state="Merged", thread_id="123",
                    data={"repository": "example/demo", "merge_sha": SHA})
        session.add(task)
        session.flush()
        propose(session, task, settings)
        propose(session, task, settings)
        return task.id, task.data["deployment_id"]


def authorize(team, task_id, op_id, **overrides):
    _, db, _, service, *_ = team
    with db.transaction() as session:
        op = session.get(Operation, op_id)
        plan_hash = op.data["hash"]
    return service.command(**{
        "action": "approve_deploy", "event_id": str(time.time_ns()), "actor": "demo-owner",
        "guild": "demo-guild", "channel": "demo-channel", "task_id": task_id,
        "head_sha": SHA, "hash": plan_hash, "confirmation_id": op_id, **overrides,
    })


def test_owner_button_to_durable_deployment_and_idempotent_result(team):
    settings, db, *_ = team
    task_id, op_id = proposal(team)
    with db.transaction() as session:
        notices = list(session.scalars(select(Outbox).where(Outbox.task_id == task_id)))
        assert len(notices) == 1
        out_id = notices[0].id
    client = TestClient(create_app(db=db, settings=settings, token="internal-test"))
    identity = {"action": "button", "event_id": "owner-click", "actor": "demo-owner",
                "guild": "demo-guild", "channel": "123"}
    result = client.post(f"/buttons/{out_id}", headers={"Authorization": "Bearer internal-test"},
                         json=identity)
    assert result.status_code == 200, result.text
    assert result.json()["data"]["deployment_status"] == "queued"
    request = bridge(db, settings, {"action": "claim"})
    assert request["id"] == op_id and request["sha"] == SHA
    assert request["actor"] == "demo-owner"
    assert bridge(db, settings, {"action": "claim"})["resumed"]
    for _ in range(2):
        assert bridge(db, settings, {"action": "complete", "id": op_id, "status": "succeeded"})["ok"]
    with db.transaction() as session:
        assert session.get(Task, task_id).state == "Merged"
        assert session.get(Task, task_id).data["deployment_status"] == "succeeded"
        approvals = list(session.scalars(select(Approval)))
        assert len(approvals) == 1 and approvals[0].data["sha"] == SHA
        notices = list(session.scalars(select(Outbox).where(Outbox.task_id == task_id)))
        assert len(notices) == 3 and notices[-1].data["mention_owner"]
    with pytest.raises(GuardError):
        bridge(db, settings, {"action": "complete", "id": op_id, "status": "rolled_back"})


@pytest.mark.parametrize("overrides", [
    {"actor": "someone-else"}, {"guild": "another-server"}, {"bot": True},
    {"head_sha": "b" * 40}, {"hash": "stale"}, {"confirmation_id": "missing"},
])
def test_wrong_owner_or_stale_approval_cannot_queue(team, overrides):
    task_id, op_id = proposal(team)
    with pytest.raises(GuardError):
        authorize(team, task_id, op_id, **overrides)
    with team[1].transaction() as session:
        assert session.get(Operation, op_id).status == "awaiting_approval"


def test_expired_and_reused_approvals_fail_and_retry_requires_new_button(team):
    task_id, op_id = proposal(team)
    with team[1].transaction() as session:
        op = session.get(Operation, op_id)
        op.data = {**op.data, "expires": 0}
    with pytest.raises(GuardError):
        authorize(team, task_id, op_id)
    with team[1].transaction() as session:
        task = session.get(Task, task_id)
        propose(session, task, team[0])
        new_id = task.data["deployment_id"]
    assert new_id != op_id
    authorize(team, task_id, new_id)
    with pytest.raises(GuardError):
        authorize(team, task_id, new_id)


def test_no_deployment_without_opt_in_or_for_other_repositories(team):
    settings, db, *_ = team
    with db.transaction() as session:
        task = Task(id="ordinary", repo="demo", state="Merged", data={"merge_sha": SHA})
        session.add(task)
        propose(session, task, settings)
        settings.deployment.enabled = True
        settings.deployment.repository_alias = "platform"
        propose(session, task, settings)
        assert not list(session.scalars(select(Operation)))


def test_in_flight_task_blocks_claim_and_deployment_blocks_new_jobs(team):
    settings, db, _, _, engine, _ = team
    task_id, op_id = proposal(team)
    authorize(team, task_id, op_id)
    with db.transaction() as session:
        job = Job(task_id=task_id, role="cto", kind="review", status="running")
        session.add(job)
        session.flush()
        job_id = job.id
    assert bridge(db, settings, {"action": "claim"}) is None
    with db.transaction() as session:
        session.get(Job, job_id).status = "done"
    assert bridge(db, settings, {"action": "claim"})
    assert engine.claim() is None


@pytest.mark.parametrize("path", [
    "Dockerfile", "compose.yaml", "config.yaml", "secrets/token", "pyproject.toml", "uv.lock",
    "scripts/deploy_supervisor.py", "src/agent_team/db.py", "src/agent_team/deployments.py",
    "src/agent_team/adapters/sbx.py", "prompts/company-policy.md", "src/../config.yaml",
])
def test_protected_changes_never_reach_docker(path):
    with pytest.raises(ValueError):
        host.validate_changes([path])


class FakeSupervisor(host.Supervisor):
    def __init__(self, tmp_path, *, switch_failure=False, rollback_failure=False):
        super().__init__({"project_root": str(tmp_path), "state_dir": str(tmp_path / "state"),
                          "project_name": "test", "baseline_sha": "b" * 40,
                          "repository": "example/demo", "branch": "main", "owner_ids": ["owner"],
                          "required_checks": [{"name": "tests", "app_id": 1}]})
        self.requests = []
        self.switches = []
        self.switch_failure, self.rollback_failure = switch_failure, rollback_failure

    def bridge(self, request):
        self.requests.append(request)
        if request["action"] == "idle":
            return {"idle": True}
        if request["action"] == "claim":
            return {"id": "deploy", "sha": SHA, "repository": "example/demo", "actor": "owner",
                    "services": list(host.SERVICES), "policy": "docker-release-v1", "expires": time.time()+900}
        return {"ok": True}

    def images(self):
        return {s: OLD for s in host.SERVICES}

    def health(self):
        pass

    def candidate(self, sha):
        return NEW

    def run(self, argv, **kwargs):
        assert "pg_dump" in argv
        return b"backup"

    def switch(self, images):
        self.switches.append(images)
        # Proof that restart recovery information exists BEFORE replacement.
        assert json.loads(self.journal.read_text())["phase"] == "switching"
        if (images["orchestrator"] == NEW and self.switch_failure) or self.rollback_failure:
            raise RuntimeError("health_failed")


@pytest.mark.parametrize("fail,rollback_fail,expected", [
    (False, False, "succeeded"), (True, False, "rolled_back"), (True, True, "rollback_failed"),
])
def test_host_deploy_and_health_failure_rollback(tmp_path, monkeypatch, fail, rollback_fail, expected):
    monkeypatch.setattr(host.time, "sleep", lambda _: None)
    supervisor = FakeSupervisor(tmp_path, switch_failure=fail, rollback_failure=rollback_fail)
    supervisor.tick()
    assert supervisor.requests[-1] == {"action": "complete", "id": "deploy", "status": expected}
    assert len(supervisor.switches) == (2 if fail else 1)
    assert not supervisor.journal.exists()
    if expected == "rollback_failed":
        with pytest.raises(RuntimeError, match="manual_recovery_required"):
            supervisor.tick()


def test_crash_during_replacement_rolls_back_before_new_work(tmp_path):
    supervisor = FakeSupervisor(tmp_path)
    host.save(supervisor.journal, {"id": "deploy", "phase": "switching",
                                  "old_images": supervisor.images(), "old_sha": "b" * 40})
    supervisor.tick()
    assert supervisor.switches == [{s: OLD for s in host.SERVICES}]
    assert supervisor.requests == [{"action": "complete", "id": "deploy", "status": "rolled_back"}]


def test_failed_receipt_is_retried_without_redeploying(tmp_path, monkeypatch):
    supervisor = FakeSupervisor(tmp_path)
    host.save(supervisor.journal, {"id": "deploy", "phase": "complete", "status": "succeeded"})
    def unavailable(request):
        raise RuntimeError("controller_down")
    monkeypatch.setattr(supervisor, "bridge", unavailable)
    with pytest.raises(RuntimeError):
        supervisor.tick()
    assert supervisor.journal.exists() and supervisor.switches == []


@pytest.mark.parametrize("case", ["ci_failure", "wrong_ci_issuer", "stale_head", "protected_diff"])
def test_candidate_checks_fail_before_any_docker_build(tmp_path, case):
    class CandidateSupervisor(FakeSupervisor):
        candidate = host.Supervisor.candidate

        def run(self, argv, **kwargs):
            assert argv[0] != "docker", "Rejected candidate reached Docker"
            if argv[0] == "gh":
                return json.dumps({"check_runs": [{
                    "name": "tests", "app": {"id": 2 if case == "wrong_ci_issuer" else 1},
                    "status": "completed", "conclusion": "failure" if case == "ci_failure" else "success",
                }]}).encode()
            if "rev-parse" in argv:
                return ("c" * 40 if case == "stale_head" else SHA).encode()
            if "diff" in argv:
                return b"src/agent_team/db.py\n"
            return b""

    with pytest.raises(ValueError):
        CandidateSupervisor(tmp_path).candidate(SHA)
