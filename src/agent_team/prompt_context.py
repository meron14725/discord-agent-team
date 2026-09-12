import os
from dataclasses import dataclass
from pathlib import Path

from .contracts import SpecialistRole

ROLE_POLICY_FILES: dict[SpecialistRole, str] = {
    "upstream": "upstream.md",
    "downstream": "downstream.md",
    "sre": "sre.md",
}


@dataclass(frozen=True)
class AgentPromptContext:
    company_memory: str
    company_policy: str
    role_policies: dict[SpecialistRole, str]


def read_required_prompt(path: Path, label: str, max_characters: int) -> str:
    if not path.is_file():
        raise ValueError(f"Missing {label}: {path}")
    value = path.read_text()
    if not value.strip():
        raise ValueError(f"Empty {label}: {path}")
    if len(value) > max_characters:
        raise ValueError(f"{label} exceeds {max_characters} characters")
    return value


def load_agent_prompt_context() -> AgentPromptContext:
    memory_path = Path(os.environ.get("COMPANY_MEMORY", "prompts/company-memory.md"))
    company_policy_path = Path(
        os.environ.get("COMPANY_POLICY", "prompts/company-policy.md")
    )
    role_policy_dir = Path(os.environ.get("ROLE_POLICY_DIR", "prompts/roles"))
    return AgentPromptContext(
        company_memory=read_required_prompt(memory_path, "company memory", 30_000),
        company_policy=read_required_prompt(company_policy_path, "company policy", 30_000),
        role_policies={
            role: read_required_prompt(role_policy_dir / filename, f"{role} policy", 10_000)
            for role, filename in ROLE_POLICY_FILES.items()
        },
    )
