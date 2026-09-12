import pytest

from agent_team.prompt_context import load_agent_prompt_context


def test_default_prompt_context_separates_company_and_role_rules():
    context = load_agent_prompt_context()

    assert "最大2往復" in context.company_policy
    assert "会社共通規則" in context.company_policy
    assert set(context.role_policies) == {"upstream", "downstream", "sre"}
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
    for name in ("upstream.md", "downstream.md", "sre.md"):
        (policy_dir / name).write_text("rule")
    (policy_dir / "sre.md").write_text("x" * 10_001)
    monkeypatch.setenv("ROLE_POLICY_DIR", str(policy_dir))
    with pytest.raises(ValueError, match="sre policy exceeds"):
        load_agent_prompt_context()
