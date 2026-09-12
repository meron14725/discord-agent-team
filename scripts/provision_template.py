"""Provision only the dedicated private CI template; never overwrite differing remote files."""

import argparse
import base64
import json
from pathlib import Path

import httpx

from agent_team.adapters.github_auth import HEADERS

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "meron14725/agent-team-python-template"
MARKER = "Discord Agent Team: trusted Python CI template"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    files = sorted(
        p for p in (ROOT / "templates/python").rglob("*") if p.is_file() and "__pycache__" not in p.parts
    )
    files.sort(key=lambda p: ".github/workflows" in p.as_posix())
    if not args.apply:
        print(
            json.dumps(
                {
                    "repository": REPOSITORY,
                    "private": True,
                    "files": [str(p.relative_to(ROOT / "templates/python")) for p in files],
                }
            )
        )
        return
    token = (ROOT / "secrets/github_publisher_token").read_text().strip()
    with httpx.Client(
        base_url="https://api.github.com", timeout=30, headers={**HEADERS, "Authorization": "Bearer " + token}
    ) as client:

        def api(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            if response.is_error:
                # Never print response bodies or authentication headers.
                raise RuntimeError(f"{method} {path}: HTTP {response.status_code}")
            return response.json() if response.content else {}

        assert api("GET", "/user")["login"] == "meron14725"
        prefix = "/repos/" + REPOSITORY
        response = client.get(prefix)
        if response.status_code == 404:
            repo = api(
                "POST",
                "/user/repos",
                json={
                    "name": REPOSITORY.split("/")[1],
                    "description": MARKER,
                    "private": True,
                    "is_template": True,
                    "auto_init": True,
                },
            )
        else:
            response.raise_for_status()
            repo = response.json()
        if not repo["private"] or repo.get("description") != MARKER or not repo.get("is_template"):
            raise RuntimeError("Existing repository does not match the dedicated template marker")
        if repo["default_branch"] != "main":
            api("POST", prefix + "/branches/" + repo["default_branch"] + "/rename", json={"new_name": "main"})
        uploaded = []
        for file in files:
            path = file.relative_to(ROOT / "templates/python").as_posix()
            response = client.get(prefix + "/contents/" + path, params={"ref": "main"})
            sha = None
            if response.status_code == 200:
                existing = response.json()
                if base64.b64decode(existing["content"]) == file.read_bytes():
                    uploaded.append(path)
                    continue
                # The API's auto-generated README is the only replaceable initial content.
                if path != "README.md" or base64.b64decode(existing["content"]).decode().strip() != (
                    "# agent-team-python-template\n" + MARKER
                ):
                    raise RuntimeError("Remote file differs; review before replacement: " + path)
                sha = existing["sha"]
            elif response.status_code != 404:
                response.raise_for_status()
            payload = {
                "message": "Initialize trusted project template: " + path,
                "content": base64.b64encode(file.read_bytes()).decode(),
                "branch": "main",
            }
            if sha:
                payload["sha"] = sha
            api("PUT", prefix + "/contents/" + path, json=payload)
            uploaded.append(path)
        result = {"repository": REPOSITORY, "private": True, "default_branch": "main", "files": uploaded}
        (ROOT / "docs/template-validation.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
