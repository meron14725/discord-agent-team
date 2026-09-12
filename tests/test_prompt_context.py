import pytest

from agent_team.prompt_context import load_agent_prompt_context, load_vendor_skill_context
from agent_team.roles import default_role_registry


def test_default_prompt_context_separates_company_and_role_rules():
    context = load_agent_prompt_context()

    assert "最大2往復" in context.company_policy
    assert "会社共通規則" in context.company_policy
    registry = default_role_registry()
    assert set(registry.role_ids).issubset(context.role_policies)
    assert {"upstream", "downstream", "sre"}.issubset(context.role_policies)
    assert context.role_policies["upstream"] == context.role_policies["cto"]
    assert context.role_policies["downstream"] == context.role_policies["backend_integrator"]
    assert context.role_policies["sre"] == context.role_policies["security_sre"]
    assert "要件整理" in context.role_policies["upstream"]
    assert "実装" in context.role_policies["downstream"]
    assert "Discord管理" in context.role_policies["sre"]
    assert "最大2往復" not in "\n".join(context.role_policies.values())


def test_missing_company_policy_stops_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_POLICY", str(tmp_path / "missing.md"))
    with pytest.raises(ValueError, match="Missing company policy"):
        load_agent_prompt_context()


def test_role_policy_size_is_bounded(tmp_path, monkeypatch):
    policy_dir = tmp_path / "roles"
    policy_dir.mkdir()
    for role in default_role_registry().entries:
        (policy_dir / f"{role.id}.md").write_text("rule")
    (policy_dir / "security_sre.md").write_text("x" * 10_001)
    monkeypatch.setenv("ROLE_POLICY_DIR", str(policy_dir))
    with pytest.raises(ValueError, match="security_sre policy exceeds"):
        load_agent_prompt_context()


def test_vendored_skill_context_exposes_only_hash_verified_skill_instructions():
    context = load_vendor_skill_context()

    assert context
    assert all(path.endswith("/SKILL.md") for path in context)
    assert any("grill-with-docs" in path for path in context)
    assert any("domain-modeling" in path for path in context)
    assert any("explain-visually" in path for path in context)
