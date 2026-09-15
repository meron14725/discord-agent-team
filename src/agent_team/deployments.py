"""Owner-approved Docker releases; no Docker or shell authority in the controller.

Uses existing operation/approval records so deploying or rolling back this
feature does not require a database migration.
"""

import json
import os
import re
import sys
import time
from urllib.parse import quote

from sqlalchemy import select, text

from .config import load_settings, secret
from .db import Approval, Database, Event, Job, Operation, Task, uid
from .policy import GuardError, digest

SHA = re.compile(r"[0-9a-f]{40}\Z")
TERMINAL = {"succeeded", "rolled_back", "failed", "rollback_failed"}


def lock_execution(session):
    if session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(72918043)"))


def propose(session, task, settings):
    from .service import notify

    config = settings.deployment
    if not config.enabled or task.repo != config.repository_alias or task.state != "Merged":
        return
    repo = settings.repos[task.repo]
    sha = task.data.get("merge_sha", "")
    if repo.per_task or task.data.get("repository") != repo.repository or not SHA.fullmatch(sha):
        raise GuardError("Invalid deployment repository or merge SHA")
    previous = session.get(Operation, task.data["deployment_id"]) if task.data.get("deployment_id") else None
    if previous and (previous.status in {"queued", "running", "succeeded"}
                     or previous.status == "awaiting_approval" and previous.data["expires"] > time.time()):
        return
    key = f"deployment:{task.id}:{sha}:{uid()}"
    plan = {"repository": repo.repository, "sha": sha,
            "services": ["orchestrator", "discord-gateway", "renderer"],
            "policy": "docker-release-v1"}
    plan_hash = digest(json.dumps(plan, sort_keys=True))
    operation = Operation(task_id=task.id, key=key, status="awaiting_approval",
                          data={**plan, "hash": plan_hash,
                                "expires": time.time() + config.approval_seconds})
    session.add(operation)
    session.flush()
    task.data = {**task.data, "deployment_id": operation.id,
                 "deployment_status": operation.status, "deployment_sha": sha}
    notify(session, task,
           f"Docker反映案: {repo.repository} の `{sha}`\n"
           "統括・Gateway・説明資料サービスを更新します。再起動中は短時間応答が止まります。\n"
           "DB・秘密情報・ホストワーカーは変更しません。起動検証に失敗したら旧イメージへ戻します。\n"
           "保護ファイルの変更や実行中の仕事がある場合は反映を停止します。",
           role="coordinator", approval="deploy", hash=plan_hash, head_sha=sha,
           confirmation_id=operation.id, mention_owner=True,
           next_action=f"オーナーが対象SHAのDocker反映を承認（{config.approval_seconds // 60}分以内）")


def approve(session, task, settings, *, actor, operation_id, plan_hash, sha):
    from .service import notify

    op = session.get(Operation, operation_id, with_for_update=True)
    if (not settings.deployment.enabled or task.repo != settings.deployment.repository_alias
        or task.state != "Merged" or op is None or op.task_id != task.id
        or op.status != "awaiting_approval" or op.data["expires"] <= time.time()
        or actor not in settings.owner_ids
        or op.data["hash"] != plan_hash or op.data["sha"] != sha
        or task.data.get("merge_sha") != sha or task.data.get("deployment_id") != op.id):
        raise GuardError("期限切れ、使用済み、または対象版が異なるDocker反映承認です")
    op.status = "queued"
    op.data = {**op.data, "actor": actor, "approved_at": time.time()}
    session.add(Approval(task_id=task.id, data={"kind": "deploy", "operation_id": op.id,
                                               "actor": actor, "sha": sha, "hash": plan_hash}))
    task.data = {**task.data, "deployment_status": "queued"}
    notify(session, task, "Docker反映を承認しました。更新担当が事前検証を開始します。",
           role="coordinator", next_action="独立した更新担当が実行")


def bridge(db, settings, request):
    """Only the trusted host supervisor invokes this via local docker exec.

    It is intentionally not an HTTP endpoint reachable with an agent token.
    Terminal receipts are idempotent so restart/rollback does not lose notices.
    """
    from .service import notify

    if request["action"] == "idle":
        import httpx

        urls = {settings.coordinator_url, settings.workflow_v2.worker_url}
        urls.update(settings.specialist_endpoint(role.id) for role in settings.role_registry.entries
                    if role.enabled)
        for url in urls:
            response = httpx.get(url + "/health", headers={"Authorization": "Bearer " + secret("WORKER_TOKEN")},
                                 timeout=5)
            response.raise_for_status()
            status = response.json()
            if status.get("status") != "ok" or status.get("active") != 0:
                return {"idle": False}
        return {"idle": True}

    with db.transaction() as session:
        action = request["action"]
        if action == "claim":
            lock_execution(session)
            if not settings.deployment.enabled:
                return None
            # Don't stop the controller while it owns a task job.
            if session.scalar(select(Job.id).where(Job.status == "running").limit(1)):
                return None
            running = session.scalar(select(Operation).where(
                Operation.key.like("deployment:%"), Operation.status == "running").limit(1))
            if running:
                return {"id": running.id, "task_id": running.task_id,
                        **running.data, "resumed": True}
            op = session.scalar(select(Operation).where(
                Operation.key.like("deployment:%"), Operation.status == "queued"
            ).order_by(Operation.created).with_for_update(skip_locked=True).limit(1))
            if op is None:
                return None
            task = session.get(Task, op.task_id)
            if (op.data["expires"] <= time.time() or op.data.get("actor") not in settings.owner_ids
                or task.state != "Merged" or task.data.get("merge_sha") != op.data["sha"]):
                op.status = "failed"
                task.data = {**task.data, "deployment_status": "failed"}
                notify(session, task, "Docker反映承認が失効しました。変更は実行していません。",
                       role="coordinator", mention_owner=True, next_action="反映案を再作成")
                return None
            op.status = "running"
            task.data = {**task.data, "deployment_status": "running"}
            return {"id": op.id, "task_id": op.task_id, **op.data}
        if action == "complete":
            op = session.get(Operation, request["id"], with_for_update=True)
            if not op or not op.key.startswith("deployment:"):
                raise GuardError("Unknown deployment")
            status = request["status"]
            if status not in TERMINAL:
                raise GuardError("Invalid deployment outcome")
            if op.status in TERMINAL:
                if op.status != status:
                    raise GuardError("Conflicting deployment outcome")
                return {"ok": True}
            if op.status != "running":
                raise GuardError("Deployment was not claimed")
            op.status = status
            # Only fixed outcome codes and image IDs; never command output/secrets.
            op.data = {**op.data, "completed_at": time.time()}
            task = session.get(Task, op.task_id)
            task.data = {**task.data, "deployment_status": status}
            labels = {"succeeded": "Docker反映と稼働検証が完了しました。",
                      "rolled_back": "新しい版の検証に失敗したため、旧イメージへ戻しました。",
                      "failed": "事前検証に失敗しました。Docker反映は実行していません。",
                      "rollback_failed": "旧イメージへの復旧にも失敗しました。ホスト側の更新ログを確認してください。"}
            session.add(Event(task_id=task.id, source="deployment", external_id=uid(),
                              data={"operation_id": op.id, "status": status, "sha": op.data["sha"]}))
            notify(session, task, labels[status] + f"\n対象: `{op.data['sha']}`",
                   role="coordinator", mention_owner=True,
                   next_action="なし" if status == "succeeded" else "SREが更新ログを確認")
            return {"ok": True}
        raise GuardError("Unknown deployment bridge action")


def main():
    settings = load_settings()
    url = os.environ.get("DATABASE_URL") or (
        f"postgresql+psycopg://team:{quote(secret('DB_PASSWORD'), safe='')}@postgres/team"
    )
    request = json.loads(sys.stdin.read(8192))
    print(json.dumps(bridge(Database(url), settings, request)))


if __name__ == "__main__":
    main()
