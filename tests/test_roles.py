import copy

import pytest
from pydantic import ValidationError

from agent_team.config import Settings
from agent_team.contracts import CoordinationMessage, RunRequest, SpecialistHandoff
from agent_team.roles import RoleDefinition, RoleRegistry, default_role_registry


def extra_role(role_id: str) -> RoleDefinition:
    return RoleDefinition(
        id=role_id,
        display_name=role_id,
        responsibilities=["focused work"],
        capabilities=["analysis"],
        tools=["artifact_read"],
        forbidden_actions=["publish_directly"],
        input_schema="SpecialistRequest",
        output_schema="SpecialistDecision",
        consultable_roles=["cto"],
        parallel_class="read_only",
        discord_bot_key=f"discord_{role_id}_token",
        worker_endpoint=f"http://{role_id}-worker:8090",
        enabled=False,
    )


def test_default_registry_has_initial_eight_roles_and_v1_aliases():
    registry = default_role_registry()

    assert len(registry.entries) == 8
    assert registry.resolve("upstream") == "cto"
    assert registry.resolve("downstream") == "backend_integrator"
    assert registry.resolve("sre") == "security_sre"
    assert registry.enabled_role_ids == (
        "coordinator",
        "cto",
        "backend_integrator",
        "security_sre",
        "frontend_ux",
        "qa",
        "evaluation_manager",
        "analyst",
    )
    assert registry.disabled_role_ids == ()
    assert registry.discord_enabled_role_ids == (
        "coordinator",
        "cto",
        "backend_integrator",
        "security_sre",
    )
    assert registry.role("frontend_ux").fallback_role == "backend_integrator"
    assert registry.role("qa").fallback_role == "cto"
    assert registry.role("evaluation_manager").fallback_role == "cto"
    assert registry.role("analyst").fallback_role == "cto"
    assert registry.role("security_sre").fallback_role is None


def test_registry_accepts_more_than_ten_roles_without_code_changes():
    registry = default_role_registry()
    expanded = RoleRegistry(entries=[*registry.entries, extra_role("writer"), extra_role("designer"), extra_role("legal")])

    assert len(expanded.entries) == 11
    assert expanded.role("designer").discord_bot_key == "discord_designer_token"


def test_registry_rejects_unknown_consultation_and_duplicate_alias():
    registry = default_role_registry()
    values = registry.model_dump()
    values["entries"][0]["consultable_roles"].append("missing_role")
    with pytest.raises(ValidationError, match="unknown roles"):
        RoleRegistry.model_validate(values)

    values = registry.model_dump()
    values["entries"][2]["aliases"] = ["upstream"]
    with pytest.raises(ValidationError, match="alias is not unique"):
        RoleRegistry.model_validate(values)


def test_registry_rejects_more_privileged_or_incomplete_fallback():
    registry = default_role_registry()
    values = registry.model_dump()
    qa = next(role for role in values["entries"] if role["id"] == "qa")
    qa["fallback_role"] = "security_sre"
    with pytest.raises(ValidationError, match="same parallel class"):
        RoleRegistry.model_validate(values)

    values = registry.model_dump()
    qa = next(role for role in values["entries"] if role["id"] == "qa")
    qa["fallback_role"] = "cto"
    qa["capabilities"].append("unavailable_capability")
    with pytest.raises(ValidationError, match="every source capability"):
        RoleRegistry.model_validate(values)


def test_settings_route_canonical_and_v1_roles_and_expose_bot_keys():
    settings = Settings(
        specialist_urls={"cto": "http://cto:8090", "downstream": "http://legacy:8090"}
    )

    assert settings.specialist_endpoint("cto") == "http://cto:8090"
    assert settings.specialist_endpoint("upstream") == "http://cto:8090"
    assert settings.specialist_endpoint("downstream") == "http://legacy:8090"
    assert settings.discord_bot_key("sre") == "discord_sre_token"
    assert settings.role_enabled("frontend_ux")
    assert not settings.discord_role_enabled("frontend_ux")
    with pytest.raises(ValueError, match="Unknown role"):
        Settings(specialist_urls={"unknown": "http://unknown:8090"})


def test_contracts_accept_registry_ids_and_v1_aliases_but_reject_invalid_ids():
    assert CoordinationMessage(role="cto", instruction="review").role == "cto"
    assert SpecialistHandoff(role="upstream", reason="requirements", instruction="clarify").role == "upstream"
    with pytest.raises(ValidationError):
        CoordinationMessage(role="../cto", instruction="review")

    request = {
        "auth_mode": "chatgpt",
        "job_id": "job-1",
        "role": "evaluation_manager",
        "kind": "respond",
        "task_id": "task-1",
        "spec_version": 0,
        "spec_hash": "",
        "base_sha": "",
        "head_sha": "",
        "prompt": "{}",
        "files": {},
        "test_commands": [],
        "model": "",
        "timeout": 30,
    }
    assert RunRequest.model_validate(request).role == "evaluation_manager"
    invalid = copy.deepcopy(request)
    invalid["role"] = "Evaluation Manager"
    with pytest.raises(ValidationError):
        RunRequest.model_validate(invalid)
