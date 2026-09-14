import asyncio
import time

import pytest
from sqlalchemy import func, select

from agent_team.adapters.codex import MockRunner
from agent_team.db import Approval, Event, Job, Operation, Outbox, Run, Task
from agent_team.engine import Engine
from agent_team.policy import GuardError


def step(engine):
    claimed = engine.claim()
    if claimed:
        asyncio.run(engine.execute(*claimed))
    return claimed


def test_slow_preparation_keeps_job_lease_alive(team, monkeypatch):
    settings, db, _, service, engine, command = team
    settings.lease_seconds = 1
    task = command("request", repo="demo", text="slow source fetch")
    prepare = engine.prepare

    def slow_prepare(*args):
        time.sleep(1.2)
        return prepare(*args)

    monkeypatch.setattr(engine, "prepare", slow_prepare)
    claim = step(engine)
    with db.transaction() as session:
        job = session.get(Job, claim[0])
        assert job.status == "done"
        assert job.attempt == 1
    assert service.status(task["id"])["state"] == "AwaitingSpecApproval"


def ready(team):
    _, _, _, service, engine, command = team
    task = command("request", repo="demo", text="テスト追加")
    step(engine)
    task = service.status(task["id"])
    command("approve_spec", task_id=task["id"], version=task["version"], hash=task["data"]["spec_hash"])
    step(engine)
    step(engine)
    engine.reconcile()
    return service.status(task["id"])


def test_end_to_end_requires_human_and_confirms_merge(team):
    _, db, _, service, engine, command = team
    task = ready(team)
    assert task["state"] == "AwaitingMergeApproval"
    engine.reconcile()
    assert service.status(task["id"])["state"] == "AwaitingMergeApproval"
    d = task["data"]
    command(
        "approve_merge",
        task_id=task["id"],
        hash=d["spec_hash"],
        head_sha=d["head_sha"],
        base_sha=d["base_sha"],
    )
    engine.reconcile()
    merged = service.status(task["id"])
    assert merged["state"] == "Merged"
    assert merged["data"]["merge_sha"]
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Approval)) == 2
        assert s.scalar(select(func.count()).select_from(Outbox)) > 0


def test_request_replay_ten_times_is_idempotent(team):
    _, db, _, _, _, command = team
    tasks = [command("request", event_id="same-message", repo="demo", text="hello") for _ in range(10)]
    assert len({t["id"] for t in tasks}) == 1
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Task)) == 1
        assert s.scalar(select(func.count()).select_from(Job)) == 1


@pytest.mark.parametrize(
    "override", [{"actor": "intruder"}, {"guild": "other"}, {"channel": "other"}, {"bot": True}]
)
def test_unauthorized_has_no_side_effects(team, override):
    _, db, _, _, _, command = team
    with pytest.raises(GuardError):
        command("request", repo="demo", text="hello", **override)
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Task)) == 0
        assert s.scalar(select(func.count()).select_from(Event)) == 0


def test_stale_spec_approval_and_revision_fences_work(team):
    _, _, _, service, engine, command = team
    task = command("request", repo="demo", text="hello")
    claim = engine.claim()
    request = engine.prepare(*claim)
    response = asyncio.run(engine.runner.run(request))
    command("answer", task_id=task["id"], text="changed")
    with pytest.raises(GuardError):
        engine.finish(*claim, request, response)
    step(engine)
    current = service.status(task["id"])
    with pytest.raises(GuardError):
        command(
            "approve_spec",
            task_id=task["id"],
            version=current["version"] - 1,
            hash=current["data"]["spec_hash"],
        )


def test_lease_expiry_rejects_old_completion(team):
    _, db, _, _, engine, command = team
    command("request", repo="demo", text="hello")
    claim = engine.claim()
    request = engine.prepare(*claim)
    response = asyncio.run(engine.runner.run(request))
    with db.transaction() as s:
        s.get(Job, claim[0]).lease = time.time() - 1
    newer = engine.claim()
    assert newer[0] == claim[0] and newer[1] > claim[1]
    with pytest.raises(GuardError):
        engine.finish(*claim, request, response)


@pytest.mark.parametrize("action", ["pause", "cancel", "revise"])
def test_stop_prevents_running_job_publication(team, action):
    _, db, _, _, engine, command = team
    task = command("request", repo="demo", text="hello")
    claim = engine.claim()
    request = engine.prepare(*claim)
    response = asyncio.run(engine.runner.run(request))
    command(action, task_id=task["id"], text="stop")
    with pytest.raises(GuardError):
        engine.finish(*claim, request, response)
    with db.transaction() as s:
        assert not list(s.scalars(select(Operation).where(Operation.status == "done")))


def test_head_change_invalidates_both_approvals(team):
    _, _, github, service, engine, command = team
    task = ready(team)
    d = task["data"]
    command(
        "approve_merge",
        task_id=task["id"],
        hash=d["spec_hash"],
        head_sha=d["head_sha"],
        base_sha=d["base_sha"],
    )
    snap = github.read(task["id"])
    snap["head_sha"] = "b" * 40
    github.write(task["id"], snap)
    engine.reconcile()
    current = service.status(task["id"])
    assert current["state"] == "Reviewing"
    assert not current["data"].get("review_approval") and not current["data"].get("merge_approval")


def test_base_change_blocks_stale_integration(team):
    _, _, github, service, engine, _ = team
    task = ready(team)
    github.write("base", {"sha": "c" * 40})
    engine.reconcile()
    assert service.status(task["id"])["state"] == "Blocked"


@pytest.mark.parametrize(
    "change,expected", [({"merged": True, "merge_sha": "f" * 40}, "Merged"), ({"state": "closed"}, "Blocked")]
)
def test_manual_github_actions_reconciled(team, change, expected):
    _, _, github, service, engine, _ = team
    task = ready(team)
    snap = github.read(task["id"])
    snap.update(change)
    github.write(task["id"], snap)
    engine.reconcile()
    current = service.status(task["id"])
    assert current["state"] == expected
    if expected == "Merged":
        assert current["data"]["merged_externally"] is True


def test_review_fix_loop_and_limits(team):
    settings, _, _, service, engine, command = team
    engine.runner = MockRunner(request_changes_once=True)
    settings.max_rounds = 0
    task = ready(team)
    assert service.status(task["id"])["state"] == "Blocked"


def test_restart_recovers_expired_job_without_duplicate_request(team):
    settings, db, github, service, engine, command = team
    task = command("request", repo="demo", text="hello")
    claimed = engine.claim()
    with db.transaction() as s:
        s.get(Job, claimed[0]).lease = 0
    restarted = Engine(db, settings, github, service_runner := MockRunner(), engine.artifacts)
    assert service_runner
    step(restarted)
    assert service.status(task["id"])["state"] == "AwaitingSpecApproval"


def test_budget_exhaustion_prevents_model_execution(team):
    settings, db, _, service, engine, command = team
    settings.mode = "live"
    settings.auth_mode = "api_key"
    settings.daily_budget_usd = settings.task_budget_usd = 1
    settings.run_reservation_usd = 2
    task = command("request", repo="demo", text="hello")
    assert engine.claim() is None
    assert service.status(task["id"])["state"] == "Blocked"
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Run)) == 0


def test_coordinator_requeues_missing_executable_after_trusted_config_change(team):
    settings, db, _, service, engine, command = team
    task = command("request", repo="demo", text="hello")
    step(engine)
    task = service.status(task["id"])
    command("approve_spec", task_id=task["id"], version=task["version"], hash=task["data"]["spec_hash"])
    claimed = engine.claim()
    with db.transaction() as s:
        job = s.get(Job, claimed[0])
        job.status = "failed"
        job.data = {
            **job.data,
            "request": {"test_commands": [["python", "-m", "unittest"]]},
            "response": {"tests": [{"command": ["python"], "exit_code": 127, "output": "missing"}]},
        }
        db_task = s.get(Task, task["id"])
        db_task.state = "Blocked"
        db_task.data = {**db_task.data, "reason": "Configured tests did not pass"}
    settings.repos["demo"].test_commands = [["python3", "-m", "unittest"]]

    engine.recover_after_runtime_change()

    assert service.status(task["id"])["state"] == "Queued"
    with db.transaction() as s:
        jobs = list(s.scalars(select(Job).where(Job.task_id == task["id"])))
        assert [job.status for job in jobs].count("queued") == 1
        assert any(job.status == "cancelled" for job in jobs)


def test_coordinator_discards_invalid_finding_against_valid_managed_spec(team):
    _, db, github, service, engine, command = team
    task = ready(team)
    spec_path = f"docs/tasks/{task['id']}/spec.md"
    snapshot = github.read(task["id"])
    assert snapshot["spec_hash"] == task["data"]["spec_hash"]
    with db.transaction() as s:
        db_task = s.get(Task, task["id"])
        db_task.state = "Blocked"
        db_task.data = {
            **db_task.data,
            "reason": "Approved specification was modified",
            "findings": [{"file": spec_path, "id": "F-001"}],
        }

    engine.recover_invalid_spec_finding()

    current = service.status(task["id"])
    assert current["state"] == "Reviewing"
    assert current["data"]["findings"] == []
    with db.transaction() as s:
        assert s.scalar(
            select(func.count()).select_from(Job).where(
                Job.task_id == task["id"], Job.kind == "review", Job.status == "queued"
            )
        ) == 1


def test_api_success_response_loss_reconciles_one_pr(team):
    _, db, github, service, engine, command = team
    task = command("request", repo="demo", text="hello")
    step(engine)
    task = service.status(task["id"])
    command("approve_spec", task_id=task["id"], version=task["version"], hash=task["data"]["spec_hash"])
    claim = engine.claim()
    request = engine.prepare(*claim)
    response = asyncio.run(engine.runner.run(request))
    original = github.publish

    def lost(*args):
        original(*args)
        raise TimeoutError("response lost")

    github.publish = lost
    with pytest.raises(TimeoutError):
        engine.finish(*claim, request, response)
    github.publish = original
    engine.finish(*claim, request, response)
    assert service.status(task["id"])["state"] == "Reviewing"
    with db.transaction() as s:
        published = [
            o for o in s.scalars(select(Operation)) if o.key.startswith("mock:") and ":publish:" in o.key
        ]
        assert len(published) == 1
