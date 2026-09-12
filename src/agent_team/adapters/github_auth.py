"""Installation credentials stay in the trusted controller, never in job payloads."""

import threading
import time
from datetime import datetime

import httpx
import jwt

API_URL = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "discord-agent-team/0.1.0",
}
REVIEW_PERMISSIONS = {"contents": "read", "pull_requests": "write", "checks": "read"}


class InstallationAuth(httpx.Auth):
    requires_response_body = True

    def __init__(self, app_id, installation_id, private_key):
        self.app_id = app_id
        self.installation_id = installation_id
        self._private_key = private_key
        self._token = ""
        self._expires = 0
        self._lock = threading.Lock()

    def app_jwt(self):
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)},
            self._private_key,
            algorithm="RS256",
        )

    def auth_flow(self, request):
        if (
            request.url.scheme != "https"
            or request.url.host != "api.github.com"
            or request.url.port not in (None, 443)
        ):
            raise ValueError("GitHub credentials require the GitHub API origin")
        with self._lock:
            if time.time() >= self._expires - 60:
                self._token = ""
                response = yield httpx.Request(
                    "POST",
                    f"{API_URL}/app/installations/{self.installation_id}/access_tokens",
                    headers={**HEADERS, "Authorization": "Bearer " + self.app_jwt()},
                    json={"permissions": REVIEW_PERMISSIONS},
                )
                response.raise_for_status()
                data = response.json()
                expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
                if expires <= time.time() + 60 or not data.get("token"):
                    raise ValueError("GitHub returned an invalid installation credential")
                self._expires = expires
                self._token = data["token"]
            request.headers["Authorization"] = "Bearer " + self._token
        # Never replay a write automatically on 401 or fall back to the publisher identity.
        yield request
