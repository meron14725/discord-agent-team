import re
from pathlib import Path

import pytest
import yaml

from agent_team.prompt_context import load_agent_prompt_context

ROLES = ("coordinator", "cto", "backend_integrator", "security_sre")


def parse_examples(role: str) -> list[dict[str, str]]:
    text = Path(f"prompts/personas/{role}/v1/PERSONA.md").read_text()
    sections = re.split(r"^### (Good|Bad)-([1-5])\n", text, flags=re.MULTILINE)[1:]
    parsed = []
    label_to_key = {
        "場面": "situation",
        "入力": "input",
        "期待": "expected",
        "悪い応答": "bad_response",
        "違反理由": "violation_reason",
        "期待規則": "expected_rule",
    }
    for offset in range(0, len(sections), 3):
        heading, number, body = sections[offset : offset + 3]
        kind = heading.lower()
        row = {
            "id": f"{role}-{heading}-{number}",
            "role": role,
            "example": f"{heading}-{number}",
            "kind": kind,
        }
        for label, value in re.findall(r"^- ([^:]+): (.+)$", body, re.MULTILINE):
            row[label_to_key[label]] = value
        parsed.append(row)
    return parsed


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
    required = {"id", "role", "example", "kind", "situation", "input"}
    body_rows = [row for role in ROLES for row in parse_examples(role)]
    assert rows == body_rows
    for row in rows:
        assert required <= row.keys()
        assert row["role"] in ROLES
        assert row["kind"] in {"good", "bad"}
        if row["kind"] == "good":
            assert row["expected"].strip()
        else:
            assert {"bad_response", "violation_reason", "expected_rule"} <= row.keys()
            assert all(row[field].strip() for field in ("bad_response", "violation_reason", "expected_rule"))


def test_shared_input_exposes_role_specific_decision_criteria():
    rows = yaml.safe_load(Path("tests/fixtures/persona_scenarios.yaml").read_text())["scenarios"]
    shared = [row for row in rows if row["kind"] == "good" and row["situation"] == "変更着手の相談"]
    assert {row["role"] for row in shared} == set(ROLES)
    assert len({row["expected"] for row in shared}) == 4
    assert len({row["input"] for row in shared}) == 1
    criteria = {
        "coordinator": ("優先度", "担当", "次工程"),
        "cto": ("目的", "受入条件", "未決"),
        "backend_integrator": ("差分", "再現", "テスト"),
        "security_sre": ("監視", "影響範囲", "復旧", "承認"),
    }
    for row in shared:
        assert any(word in row["expected"] for word in criteria[row["role"]])


def test_examples_are_concrete_instead_of_repeated_templates():
    rows = yaml.safe_load(Path("tests/fixtures/persona_scenarios.yaml").read_text())["scenarios"]
    for role in ROLES:
        role_rows = [row for row in rows if row["role"] == role]
        assert len({row["situation"] for row in role_rows}) >= 9
        assert len({row["input"] for row in role_rows}) >= 9
        bad = [row for row in role_rows if row["kind"] == "bad"]
        assert len({row["bad_response"] for row in bad}) == 5
        assert len({row["violation_reason"] for row in bad}) == 5
