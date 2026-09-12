import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

token = Path("secrets/worker_token").read_text().strip()
instructions = {
    "cto": "要件整理や作業化はせず、あなた自身の役割を含む短い日本語の自己紹介を返してください。",
    "backend_integrator": "実装タスクにはせず、あなた自身の役割を含む短い日本語の自己紹介を返してください。",
    "security_sre": "運用作業にはせず、あなた自身の役割を含む短い日本語の自己紹介を返してください。",
}
urls = {
    "cto": "http://127.0.0.1:8092/run",
    "backend_integrator": "http://127.0.0.1:8094/run",
    "security_sre": "http://127.0.0.1:8093/run",
}


async def run(role, instruction):
    request = {
        "auth_mode": "chatgpt",
        "job_id": f"respond-smoke-{role}-{time.time_ns()}",
        "role": role,
        "kind": "respond",
        "task_id": f"CHAT-SMOKE-{role}",
        "spec_version": 0,
        "spec_hash": "",
        "base_sha": "",
        "head_sha": "",
        "prompt": json.dumps(
            {
                "current_owner_message": "みんな自己紹介して　どんな役割なのか",
                "recent_discord_context_oldest_first": [],
                "delegated_goal": instruction,
                "trusted_platform_snapshot": {},
            },
            ensure_ascii=False,
        ),
        "files": {},
        "test_commands": [],
        "model": "",
        "timeout": 300,
    }
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=360) as client:
        response = await client.post(
            urls[role],
            json=request,
            headers={"Authorization": "Bearer " + token},
        )
    elapsed = time.monotonic() - started
    if response.is_success:
        print(
            role,
            f"{elapsed:.1f}s",
            json.dumps(response.json()["result"]["specialist"], ensure_ascii=False),
            flush=True,
        )
    else:
        print(role, response.status_code, response.text[:1000], flush=True)
    return response.is_success


async def main():
    started = time.monotonic()
    results = await asyncio.gather(
        *(run(role, instruction) for role, instruction in instructions.items())
    )
    print(f"total {time.monotonic() - started:.1f}s", flush=True)
    return all(results)


sys.exit(0 if asyncio.run(main()) else 1)
