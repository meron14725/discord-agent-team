from pathlib import Path

import pytest

from agent_team.prompt_context import load_agent_prompt_context


ROLES = ("coordinator", "cto", "backend_integrator", "security_sre")


def test_four_versioned_personas_have_exact_example_counts(monkeypatch):
    context = load_agent_prompt_context(
        persona_enabled=True,
        persona_dir=Path("prompts/personas"),
        active_versions={role: "v1" for role in ROLES},
    )
    assert set(context.personas) == set(ROLES)
    for role, text in context.personas.items():
        assert text.count("### Good-") == 5
        assert text.count("### Bad-") == 5
        assert f"role_id: {role}" in text


def test_missing_persona_fails_without_cross_role_fallback(tmp_path):
    with pytest.raises(ValueError, match="Missing coordinator/v1 persona"):
        load_agent_prompt_context(
            persona_enabled=True,
            persona_dir=tmp_path,
            active_versions={role: "v1" for role in ROLES},
        )
