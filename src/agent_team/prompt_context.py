import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .roles import RoleId, RoleRegistry, default_role_registry


@dataclass(frozen=True)
class AgentPromptContext:
    company_memory: str
    company_policy: str
    role_policies: dict[RoleId, str]
    personas: dict[RoleId, str]
    persona_versions: dict[RoleId, str]


def load_vendor_skill_context(root: Path | None = None) -> dict[str, str]:
    root = root or Path(os.environ.get("VENDOR_SKILLS", "vendor/agent-skills"))
    manifest = json.loads((root / "manifest.json").read_text())
    selected = {}
    for snapshot in manifest["snapshots"]:
        for entry in snapshot["files"]:
            path = root / entry["path"]
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError(f"Vendored skill hash mismatch: {entry['path']}")
            if entry["path"].endswith("/SKILL.md"):
                selected[entry["path"]] = content.decode("utf-8")
    return selected


def read_required_prompt(path: Path, label: str, max_characters: int) -> str:
    if not path.is_file():
        raise ValueError(f"Missing {label}: {path}")
    value = path.read_text()
    if not value.strip():
        raise ValueError(f"Empty {label}: {path}")
    if len(value) > max_characters:
        raise ValueError(f"{label} exceeds {max_characters} characters")
    return value


def load_agent_prompt_context(
    registry: RoleRegistry | None = None,
    *,
    persona_enabled: bool = False,
    persona_dir: Path | None = None,
    active_versions: dict[str, str] | None = None,
    persona_max_characters: int = 30_000,
) -> AgentPromptContext:
    registry = registry or default_role_registry()
    memory_path = Path(os.environ.get("COMPANY_MEMORY", "prompts/company-memory.md"))
    company_policy_path = Path(
        os.environ.get("COMPANY_POLICY", "prompts/company-policy.md")
    )
    role_policy_dir = Path(os.environ.get("ROLE_POLICY_DIR", "prompts/roles"))
    canonical_policies = {
        role.id: read_required_prompt(
            role_policy_dir / f"{role.id}.md", f"{role.id} policy", 10_000
        )
        for role in registry.entries
    }
    role_policies = dict(canonical_policies)
    role_policies.update(
        {
            alias: canonical_policies[canonical]
            for alias, canonical in registry.aliases.items()
        }
    )
    personas: dict[str, str] = {}
    versions = dict(active_versions or {})
    if persona_enabled:
        root = persona_dir or Path(os.environ.get("PERSONA_DIR", "prompts/personas"))
        required = {"coordinator", "cto", "backend_integrator", "security_sre"}
        if set(versions) != required:
            raise ValueError("Persona role/version mapping must exactly match initial roles")
        loaded = {}
        for role, version in versions.items():
            value = read_required_prompt(
                root / role / version / "PERSONA.md", f"{role}/{version} persona", persona_max_characters
            )
            _validate_persona(value, role, version)
            loaded[role] = value
        personas = loaded  # atomic assignment only after the whole registry validates
    return AgentPromptContext(
        company_memory=read_required_prompt(memory_path, "company memory", 30_000),
        company_policy=read_required_prompt(company_policy_path, "company policy", 30_000),
        role_policies=role_policies,
        personas=personas,
        persona_versions=versions if persona_enabled else {},
    )


def _validate_persona(value: str, role: str, version: str) -> None:
    for heading in ("Identity", "Character", "Conversation", "Voice", "Examples"):
        if f"## {heading}" not in value:
            raise ValueError(f"{role}/{version} persona missing {heading}")
    if f"role_id: {role}" not in value or f"version: {version}" not in value:
        raise ValueError(f"Persona identity mismatch: {role}/{version}")
    marker = re.search(r"^presentation_marker:\s*([^\n]+)$", value, re.MULTILINE)
    if marker and (
        len(marker.group(1)) > 32
        or any(token in marker.group(1) for token in ("?", "？", "承認", "完了", "実行", "失敗", "引き継ぎ", "委任"))
    ):
        raise ValueError(f"Unsafe persona presentation marker: {role}/{version}")
    if len(re.findall(r"^### Good-[1-5]$", value, re.MULTILINE)) != 5:
        raise ValueError(f"{role}/{version} persona must have exactly five good examples")
    if len(re.findall(r"^### Bad-[1-5]$", value, re.MULTILINE)) != 5:
        raise ValueError(f"{role}/{version} persona must have exactly five bad examples")
