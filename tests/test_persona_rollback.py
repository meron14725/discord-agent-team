from agent_team.persona import PersonaDefinition, PersonaRegistry


def test_role_version_snapshot_can_roll_back_without_code_change():
    registry = PersonaRegistry({
        ("coordinator", "v1"): PersonaDefinition("coordinator", "v1", "old"),
        ("coordinator", "v2"): PersonaDefinition("coordinator", "v2", "new"),
    })
    assert registry.select({"coordinator": "v2"})["coordinator"].content == "new"
    assert registry.select({"coordinator": "v1"})["coordinator"].content == "old"
