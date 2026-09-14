import asyncio
from pathlib import Path

from agent_team.contracts import CoordinationDecision
from agent_team.persona import PersonaDefinition, PersonaRegistry, persona_formatter, render_persona_reply
from agent_team.prompt_context import _validate_persona


def test_role_version_snapshot_can_roll_back_without_code_change():
    root = Path("tests/fixtures/personas/coordinator")
    definitions = {}
    for version in ("v1", "v2"):
        content = (root / version / "PERSONA.md").read_text()
        _validate_persona(content, "coordinator", version)
        definitions[("coordinator", version)] = PersonaDefinition("coordinator", version, content)
    registry = PersonaRegistry({
        **definitions,
    })
    decision = CoordinationDecision(repository_alias="", action="reply", reply="確認しました。", task_summary="", delegations=[])
    active_versions = {"coordinator": "v2"}
    current = registry.select(active_versions)["coordinator"]
    first = asyncio.run(render_persona_reply(
        decision=decision, role_id="coordinator", persona=current, formatter=persona_formatter
    ))
    assert first.audit.version == "v2" and "new" in current.content

    # Atomic reload/restart boundary: construct a complete new snapshot, then swap it.
    active_versions = {"coordinator": "v1"}
    current = registry.select(active_versions)["coordinator"]
    rolled_back = asyncio.run(render_persona_reply(
        decision=decision, role_id="coordinator", persona=current, formatter=persona_formatter
    ))
    assert rolled_back.audit.version == "v1" and "old" in current.content
