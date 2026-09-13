"""GitHub Issue を正本にする v2 要件文書の検証。"""
import re
from dataclasses import dataclass

from .policy import GuardError, digest

REQUIRED_SECTIONS = (
    "背景",
    "目的",
    "対象ユーザー",
    "スコープ",
    "対象外",
    "機能要件",
    "非機能要件",
    "受入条件",
    "テスト",
    "未解決事項",
)
AC_PATTERN = re.compile(
    r"(?m)^\s*(?:[-*]\s*)?(?:\*\*(AC-\d+)\*\*|(AC-\d+))\s*[:：-]\s*(\S.*)$"
)
HEADING_PATTERN = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")


@dataclass(frozen=True)
class RequirementsIdentity:
    repository: str
    issue_number: int
    issue_url: str
    updated_at: str
    body_hash: str
    acceptance_ids: tuple[str, ...]


def extract_acceptance_criteria(body: str) -> dict[str, str]:
    """受入条件を順序を保って抽出し、曖昧な重複を拒否する。"""
    criteria: dict[str, str] = {}
    for emphasized_id, plain_id, statement in AC_PATTERN.findall(body):
        acceptance_id = emphasized_id or plain_id
        if acceptance_id in criteria:
            raise GuardError(f"Duplicate acceptance criterion: {acceptance_id}")
        if len(statement.strip()) < 8:
            raise GuardError(f"Acceptance criterion is not testable: {acceptance_id}")
        criteria[acceptance_id] = statement.strip()
    if not criteria:
        raise GuardError("Requirements must contain acceptance criteria")
    return criteria


def validate_requirements_body(body: str) -> dict[str, str]:
    if not body.strip():
        raise GuardError("Requirements body is empty")
    headings = [heading.casefold() for heading in HEADING_PATTERN.findall(body)]
    missing = [section for section in REQUIRED_SECTIONS if not any(section in h for h in headings)]
    if missing:
        raise GuardError("Missing requirements sections: " + ", ".join(missing))
    return extract_acceptance_criteria(body)


def bind_requirements(
    *, repository: str, issue_number: int, issue_url: str, updated_at: str, body: str
) -> RequirementsIdentity:
    if not repository or issue_number <= 0 or not issue_url.startswith("https://github.com/"):
        raise GuardError("Invalid GitHub Issue identity")
    if not updated_at:
        raise GuardError("GitHub Issue updated_at is required")
    criteria = validate_requirements_body(body)
    return RequirementsIdentity(
        repository=repository,
        issue_number=issue_number,
        issue_url=issue_url,
        updated_at=updated_at,
        body_hash=digest(body),
        acceptance_ids=tuple(criteria),
    )


def assert_requirements_current(expected: RequirementsIdentity, *, body: str, updated_at: str) -> None:
    if updated_at != expected.updated_at or digest(body) != expected.body_hash:
        raise GuardError("GitHub Issue changed after requirements approval")
