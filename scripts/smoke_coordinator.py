import json
import sys
import time
from pathlib import Path

import httpx

token = Path("secrets/worker_token").read_text().strip()
owner_message = sys.argv[1] if len(sys.argv) > 1 else "みんなこんにちは"
request = {
    "auth_mode": "chatgpt",
    "job_id": f"coord-smoke-{time.time_ns()}",
    "role": "coordinator",
    "kind": "coordinate",
    "task_id": "COORD-SMOKE",
    "spec_version": 0,
    "spec_hash": "",
    "base_sha": "",
    "head_sha": "",
    "prompt": json.dumps(
        {
            "current_owner_message": owner_message,
            "recent_discord_context_oldest_first": [
                "まか: 各メンバー、こんにちは",
                "統括Bot: こんにちは。統括です。",
            ],
            "available_roles": {
                "upstream": "要件整理・設計・レビュー",
                "downstream": "実装・修正",
                "sre": "Discord・実行基盤・運用・障害対応",
            },
        },
        ensure_ascii=False,
    ),
    "files": {},
    "test_commands": [],
    "model": "",
    "timeout": 300,
}
response = httpx.post(
    "http://127.0.0.1:8091/run",
    json=request,
    headers={"Authorization": "Bearer " + token},
    timeout=360,
)
print(response.status_code)
if response.is_success:
    print(json.dumps(response.json()["result"]["coordination"], ensure_ascii=False))
else:
    print(response.text[:2000])
sys.exit(0 if response.is_success else 1)
