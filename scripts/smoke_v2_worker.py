import json
import sys
import time
from pathlib import Path

import httpx

token = Path("secrets/worker_token").read_text().strip()
request = {
    "auth_mode": "chatgpt",
    "job_id": f"v2-smoke-{time.time_ns()}",
    "role": "analyst",
    "kind": "consult",
    "task_id": "V2-SMOKE",
    "spec_version": 1,
    "spec_hash": "smoke",
    "base_sha": "",
    "head_sha": "",
    "prompt": json.dumps(
        {
            "topic": "v2 workerの疎通確認",
            "question": "応答可能なら、判断基準と推奨を一文ずつ返してください。",
            "constraints": ["読み取り専用", "外部操作なし"],
        },
        ensure_ascii=False,
    ),
    "files": {},
    "test_commands": [],
    "model": "",
    "timeout": 300,
}
response = httpx.post(
    "http://127.0.0.1:8095/run",
    json=request,
    headers={"Authorization": "Bearer " + token},
    timeout=360,
)
print(response.status_code)
if response.is_success:
    result = response.json()["result"]
    print(json.dumps({"status": result["status"], "summary": result["summary"]}, ensure_ascii=False))
else:
    print(response.text[:2000])
sys.exit(0 if response.is_success else 1)
