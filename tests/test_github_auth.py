import json
import time
from datetime import datetime, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_team.adapters.github_auth import REVIEW_PERMISSIONS, InstallationAuth


def test_installation_auth_signs_caches_and_refreshes(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = time.time()
    monkeypatch.setattr("agent_team.adapters.github_auth.time.time", lambda: now)
    auth = InstallationAuth(123, 456, key)
    minted = []

    def handle(request):
        if request.url.path == "/app/installations/456/access_tokens":
            assert request.headers["User-Agent"] == "discord-agent-team/0.1.0"
            claims = jwt.decode(
                request.headers["Authorization"].removeprefix("Bearer "),
                key.public_key(), algorithms=["RS256"], options={"verify_iat": False},
            )
            assert claims["iss"] == "123"
            assert claims["iat"] == int(now) - 60
            assert claims["exp"] == int(now) + 540
            assert json.loads(request.content) == {"permissions": REVIEW_PERMISSIONS}
            minted.append(f"installation-{len(minted)}")
            return httpx.Response(201, json={
                "token": minted[-1],
                "expires_at": datetime.fromtimestamp(now + 3600, timezone.utc).isoformat(),
            })
        assert request.headers["Authorization"] == "Bearer " + minted[-1]
        return httpx.Response(200, json={"total_count": 0})

    with httpx.Client(auth=auth, transport=httpx.MockTransport(handle)) as client:
        for _ in range(2):
            client.get("https://api.github.com/installation/repositories").raise_for_status()
        assert len(minted) == 1
        now += 3550
        client.get("https://api.github.com/installation/repositories").raise_for_status()
        assert len(minted) == 2


def test_installation_failure_does_not_send_review_or_fallback():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    seen = []

    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(403, json={"message": "denied"})

    with httpx.Client(auth=InstallationAuth(123, 456, key), transport=httpx.MockTransport(handle)) as c:
        with pytest.raises(httpx.HTTPStatusError):
            c.post("https://api.github.com/repos/example/repo/pulls/1/reviews", json={})
    assert seen == ["/app/installations/456/access_tokens"]


def test_installation_credentials_reject_other_origins():
    with httpx.Client(auth=InstallationAuth(123, 456, "unused")) as c:
        with pytest.raises(ValueError, match="origin"):
            c.get("https://example.com")
