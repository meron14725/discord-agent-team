import asyncio
import shutil

from agent_team.contracts import CoordinationDecision
from agent_team.persona import PersonaDefinition, persona_formatter, render_persona_reply
from agent_team.prompt_context import load_agent_prompt_context


def test_process_restart_loader_applies_rollback_version(tmp_path):
    root = tmp_path / "personas"
    shutil.copytree("prompts/personas", root)
    shutil.copytree(
        "tests/fixtures/personas/coordinator/v1",
        root / "coordinator" / "v1",
        dirs_exist_ok=True,
    )
    shutil.copytree(
        "tests/fixtures/personas/coordinator/v2",
        root / "coordinator" / "v2",
    )
    decision = CoordinationDecision(repository_alias="", action="reply", reply="確認しました。", task_summary="", delegations=[])
    initial_versions = {
        "coordinator": "v2",
        "cto": "v1",
        "backend_integrator": "v1",
        "security_sre": "v1",
    }
    initial_boot = load_agent_prompt_context(
        persona_enabled=True,
        persona_dir=root,
        active_versions=initial_versions,
    )
    current = PersonaDefinition(
        "coordinator",
        initial_boot.persona_versions["coordinator"],
        initial_boot.personas["coordinator"],
    )
    first = asyncio.run(render_persona_reply(
        decision=decision, role_id="coordinator", persona=current, formatter=persona_formatter
    ))
    assert first.audit.version == "v2" and first.text.startswith("新版 ")

    # A simulated process restart invokes the production loader with a complete
    # configuration snapshot whose coordinator version was rolled back.
    rolled_back_versions = {**initial_versions, "coordinator": "v1"}
    restarted = load_agent_prompt_context(
        persona_enabled=True,
        persona_dir=root,
        active_versions=rolled_back_versions,
    )
    current = PersonaDefinition(
        "coordinator",
        restarted.persona_versions["coordinator"],
        restarted.personas["coordinator"],
    )
    rolled_back = asyncio.run(render_persona_reply(
        decision=decision, role_id="coordinator", persona=current, formatter=persona_formatter
    ))
    assert rolled_back.audit.version == "v1" and rolled_back.text.startswith("旧版 ")
