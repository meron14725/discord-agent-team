import argparse
import asyncio
import json
import tempfile

from .adapters.codex import MockRunner
from .adapters.github import MockGitHub
from .config import Check, Settings, load_settings
from .db import Database
from .engine import Engine
from .service import TaskService


async def demo(path, artifacts):
    settings = Settings()
    settings.repos["demo"].checks = [Check(name="tests", app_id=1)]
    db = Database(path if "://" in path else "sqlite:///" + path)
    db.migrate()
    service = TaskService(db, settings)
    github = MockGitHub(db, settings)
    engine = Engine(db, settings, github, MockRunner(request_changes_once=True), artifacts)

    def command(action, **kw):
        from .db import uid

        return service.command(
            action=action,
            event_id=uid(),
            actor="demo-owner",
            guild="demo-guild",
            channel="demo-channel",
            **kw,
        )

    task = command("request", repo="demo", text="挨拶関数のテストを追加")
    for _ in range(20):
        task = service.status(task["id"])
        print(json.dumps({"task": task["id"], "state": task["state"]}, ensure_ascii=False))
        if task["state"] == "AwaitingSpecApproval":
            command(
                "approve_spec", task_id=task["id"], version=task["version"], hash=task["data"]["spec_hash"]
            )
        elif task["state"] == "AwaitingMergeApproval":
            d = task["data"]
            command(
                "approve_merge",
                task_id=task["id"],
                head_sha=d["head_sha"],
                base_sha=d["base_sha"],
                hash=d["spec_hash"],
            )
        elif task["state"] == "Merged":
            print("DEMO ONLY: 外部通信・課金なし。レビュー修正→人間承認→模擬マージ完了。")
            return
        elif task["state"] == "Blocked":
            raise RuntimeError(task["data"]["reason"])
        claimed = engine.claim()
        if claimed:
            await engine.execute(*claimed)
        engine.reconcile()
    raise RuntimeError("Demo did not complete")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["demo", "migrate", "gateway", "renderer", "validate-config", "preflight"],
    )
    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(demo(tmp + "/demo.db", tmp + "/artifacts"))
    elif args.command == "preflight":
        from .adapters.codex import CodexRunner

        asyncio.run(CodexRunner().preflight())
        print("Codex sandbox write boundaries verified; no model invoked")
    elif args.command == "gateway":
        from .adapters.discord import serve

        asyncio.run(serve())
    elif args.command == "renderer":
        import uvicorn

        uvicorn.run("agent_team.renderer:create_renderer", factory=True, host="0.0.0.0", port=8091)
    elif args.command == "validate-config":
        settings = load_settings()
        print(f"Configuration valid: mode={settings.mode}, repos={','.join(settings.repos)}")
    else:
        import os

        Database(os.environ["DATABASE_URL"]).migrate()


if __name__ == "__main__":
    main()
