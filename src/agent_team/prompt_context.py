import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .roles import RoleId, RoleRegistry, default_role_registry


@dataclass(frozen=True)
class AgentPromptContext:
    company_memory: str
    company_policy: str
    role_policies: dict[RoleId, str]


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


def load_agent_prompt_context(registry: RoleRegistry | None = None) -> AgentPromptContext:
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
    return AgentPromptContext(
        company_memory=read_required_prompt(memory_path, "company memory", 30_000),
        company_policy=read_required_prompt(company_policy_path, "company policy", 30_000),
        role_policies=role_policies,
    )
