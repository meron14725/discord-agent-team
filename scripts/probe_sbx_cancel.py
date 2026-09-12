"""Model-free cancellation test against a real microVM and process tree."""

import asyncio
import contextlib
import json
import tempfile
import uuid
from pathlib import Path

from agent_team.adapters.sbx import IMAGE, SbxRunner


async def main():
    name = "dat-job-" + uuid.uuid4().hex[:16]
    job = "cancel-probe"
    report = {"sandbox": name, "model_invoked": False, "status": "failed"}
    with tempfile.TemporaryDirectory() as directory:
        runner = SbxRunner(state_dir=directory)
        await runner.startup()
        runner.names[job] = name
        runner.save_names()
        execution = None
        try:
            await runner.command(
                job,
                [
                    "create",
                    "--name",
                    name,
                    "--template",
                    IMAGE,
                    "--no-share-skills",
                    "--cpus",
                    "2",
                    "--memory",
                    "4g",
                    "--deny-network",
                    "*",
                    "codex",
                ],
                600,
            )
            execution = asyncio.create_task(
                runner.command(
                    job,
                    [
                        "exec",
                        name,
                        "python3",
                        "-c",
                        "import subprocess,time,pathlib; subprocess.Popen(['sleep','120']); pathlib.Path('/tmp/process-started').touch(); time.sleep(120)",
                    ],
                    180,
                )
            )
            for _ in range(20):
                await asyncio.sleep(0.5)
                output = await runner.command(
                    "check",
                    [
                        "exec",
                        name,
                        "python3",
                        "-c",
                        "import pathlib; print(pathlib.Path('/tmp/process-started').exists())",
                    ],
                )
                if output.strip() == "True":
                    break
            else:
                raise RuntimeError("Process tree never started")
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            await runner.cancel(job)
            assert not runner.names
            assert json.loads((Path(directory) / "vms.json").read_text()) == {}
            output = await runner.command("check", ["inspect", name], allowed_codes=(0, 1))
            assert f"sandbox '{name}' not found" in output
            report["status"] = "passed"
            report["evidence"] = (
                "Process tree started; cancellation deleted VM; journal empty; inspect not found"
            )
        finally:
            if execution and not execution.done():
                execution.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await execution
            await runner.shutdown()
            root = Path(__file__).resolve().parents[1]
            (root / "docs/sbx-cancel-validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(report["status"])


if __name__ == "__main__":
    asyncio.run(main())
