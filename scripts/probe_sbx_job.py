"""Use a real Codex runner with the existing DB engine, but mock GitHub/Discord.

Start the authenticated host worker first. Consumes up to two subscription runs.
Simulates owner approval in its disposable DB; never writes to real GitHub or Discord.
"""

import asyncio
import json
import tempfile
from pathlib import Path

from agent_team.adapters.codex import RemoteRunner
from agent_team.adapters.github import MockGitHub
from agent_team.config import Settings
from agent_team.db import Database
from agent_team.engine import Engine
from agent_team.service import TaskService


async def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="dat-job-probe-") as directory:
        settings = Settings(
            upstream_url="http://127.0.0.1:8090", downstream_url="http://127.0.0.1:8090", run_timeout=300
        )
        settings.repos["demo"].test_commands = [["python3", "-m", "unittest", "discover", "-s", "tests"]]
        db = Database("sqlite:///" + directory + "/probe.db")
        db.migrate()
        service = TaskService(db, settings)
        runner = RemoteRunner(settings, (root / "secrets/worker_token").read_text().strip())
        engine = Engine(db, settings, MockGitHub(db, settings), runner, directory + "/artifacts")
        task = service.command(
            action="request",
            event_id="probe",
            actor="demo-owner",
            guild="demo-guild",
            channel="demo-channel",
            repo="demo",
            text="Python 3.12以降。src/example.pyのgreeting()関数が引数なしで文字列helloを返す機能。戻り値は改行なし。既存関数も同じ挙動。tests/test_example.pyでunittestを使って戻り値と型を確認するテスト追加が今回の実装対象。実行コマンドpython3 -m unittest discover -s tests。外部依存・外部通信なし。関数配置先や対応版を含め以上を確定要件として仕様を作成してください。",
        )
        await engine.execute(*engine.claim())
        state = service.status(task["id"])
        states = [state["state"]]
        if state["state"] == "AwaitingSpecApproval":
            service.command(
                action="approve_spec",
                event_id="probe-approval",
                actor="demo-owner",
                guild="demo-guild",
                channel="demo-channel",
                task_id=task["id"],
                version=state["version"],
                hash=state["data"]["spec_hash"],
            )
            await engine.execute(*engine.claim())
            state = service.status(task["id"])
            states.append(state["state"])
        report = {
            "state": state["state"],
            "states": states,
            "approval": "simulated owner in disposable DB only",
            "data": state["data"],
            "github": "mock",
            "discord_sent": False,
            "responses": [json.loads(p.read_text()) for p in Path(directory + "/artifacts").glob("*.json")],
        }
        (root / "docs/sbx-job-validation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        print("Job state:", state["state"])
        assert states[0] == "AwaitingSpecApproval", state["data"].get("reason")
        assert len(report["responses"]) == 2, state["data"].get("reason")
        assert state["state"] == "Reviewing", state["data"].get("reason")
        assert any(r["tests"] for r in report["responses"])
        assert all(t["exit_code"] == 0 for r in report["responses"] for t in r["tests"])


if __name__ == "__main__":
    asyncio.run(main())
