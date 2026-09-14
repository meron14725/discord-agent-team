from pathlib import Path
import re

import pytest
import yaml

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


def test_all_forty_examples_have_machine_readable_review_evidence():
    fixture = yaml.safe_load(Path("tests/fixtures/persona_scenarios.yaml").read_text())
    rows = fixture["scenarios"]
    assert len(rows) == 40
    assert len({row["id"] for row in rows}) == 40
    required = {"id", "role", "example", "kind", "situation", "input", "expected"}
    body_ids = set()
    for role in ROLES:
        text = Path(f"prompts/personas/{role}/v1/PERSONA.md").read_text()
        body_ids.update(
            f"{role}-{kind}-{number}"
            for kind, number in re.findall(r"^### (Good|Bad)-([1-5])$", text, re.MULTILINE)
        )
    assert {row["id"] for row in rows} == body_ids
    for row in rows:
        assert required <= row.keys()
        assert row["role"] in ROLES
        assert row["kind"] in {"good", "bad"}
        if row["kind"] == "bad":
            assert row["violation_reason"].strip()


def test_shared_input_exposes_role_specific_decision_criteria():
    rows = yaml.safe_load(Path("tests/fixtures/persona_scenarios.yaml").read_text())["scenarios"]
    shared = [row for row in rows if row["kind"] == "good" and row["situation"] == "共通シナリオ1"]
    assert {row["role"] for row in shared} == set(ROLES)
    assert len({row["expected"] for row in shared}) == 4
    assert len({row["input"] for row in shared}) == 1
