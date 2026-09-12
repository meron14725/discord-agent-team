import pytest

from agent_team.adapters.codex import MockRunner
from agent_team.adapters.github import MockGitHub
from agent_team.config import Check, Settings
from agent_team.db import Database, uid
from agent_team.engine import Engine
from agent_team.service import TaskService


@pytest.fixture
def team(tmp_path):
    settings = Settings()
    settings.repos["demo"].checks = [Check(name="tests", app_id=1)]
    db = Database("sqlite:///" + str(tmp_path / "team.db"))
    db.migrate()
    github = MockGitHub(db, settings)
    service = TaskService(db, settings)
    engine = Engine(db, settings, github, MockRunner(), tmp_path / "artifacts")

    def command(action, **kw):
        defaults = dict(event_id=uid(), actor="demo-owner", guild="demo-guild", channel="demo-channel")
        defaults.update(kw)
        return service.command(action=action, **defaults)

    return settings, db, github, service, engine, command
