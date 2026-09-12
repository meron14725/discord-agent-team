"""Initialize local, ignored configuration without overwriting existing secrets."""

import os
import secrets
from pathlib import Path

os.umask(0o077)
root = Path(__file__).resolve().parents[1]
config = root / "config.yaml"
if not config.exists():
    config.write_bytes((root / "config.example.yaml").read_bytes())
folder = root / "secrets"
folder.mkdir(exist_ok=True, mode=0o700)
for name in [
    "db_password",
    "internal_token",
    "worker_token",
    "github_publisher_token",
    "github_reviewer_token",
    "github_merger_token",
    "discord_upstream_token",
    "discord_downstream_token",
    "discord_coordinator_token",
    "discord_sre_token",
    "openai_api_key",
]:
    path = folder / name
    if not path.exists():
        path.write_text(
            secrets.token_urlsafe(48) if name in {"db_password", "internal_token", "worker_token"} else ""
        )
        path.chmod(0o600)
print("config.yaml and secrets initialized; existing files preserved. No external connections made.")
