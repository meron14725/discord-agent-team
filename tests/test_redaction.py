import hashlib
from dataclasses import asdict

import pytest

from agent_team.redaction import SecretScanner

SALT = b"test-only-scan-salt-value"


def scanner():
    return SecretScanner(SALT)


@pytest.mark.parametrize(
    ("kind", "secret"),
    [
        ("discord_token", ".".join(("A" * 24, "b" * 6, "C" * 30))),
        ("discord_token", "mfa." + "L" * 40),
        ("github_token", "ghp_" + "D" * 36),
        ("github_token", "github_pat_" + "E" * 30),
        ("openai_api_key", "sk-proj-" + "F" * 32),
        ("pem_private_key", "-----BEGIN " + "RSA PRIVATE KEY-----"),
        ("generic_credential_assignment", "password" + "=" + "G" * 20),
        ("authorization_header", "Authorization: Bearer " + "H" * 24),
    ],
)
def test_supported_secret_patterns_return_metadata_only(kind, secret):
    text = "prefix-nearby " + secret + " suffix-nearby"

    report = scanner().scan_text(text)

    assert report.blocked
    assert kind in {finding.kind for finding in report.findings}
    serialized = repr(asdict(report))
    assert secret not in serialized
    assert "prefix-nearby" not in serialized
    assert "suffix-nearby" not in serialized


def test_scan_is_deterministic_for_the_same_salt_and_changes_with_another_salt():
    text = "ordinary clean text"

    first = scanner().scan_text(text)
    second = scanner().scan_text(text)
    other = SecretScanner(b"another-test-salt-value").scan_text(text)

    assert first == second
    assert first.content_hash != other.content_hash


def test_clean_text_is_available_after_content_inspection():
    content = "設計判断だけを保存する".encode()

    inspection = scanner().inspect_content(content, "text/markdown")

    assert not inspection.blocked
    assert inspection.text == content.decode()
    assert inspection.metadata.mime_type == "text/markdown"
    assert inspection.report is not None and not inspection.report.findings


def test_binary_content_returns_metadata_without_retaining_bytes():
    content = b"\x89PNG\r\n\x1a\n\x00binary-payload"

    inspection = scanner().inspect_content(content, "image/png")

    assert inspection.blocked
    assert inspection.text is None
    assert inspection.report is None
    assert inspection.metadata.binary
    assert inspection.metadata.size == len(content)
    assert inspection.metadata.content_hash == "sha256:" + hashlib.sha256(content).hexdigest()
    assert "binary-payload" not in repr(inspection)


def test_untrusted_binary_mime_value_is_not_retained():
    inspection = scanner().inspect_content(b"\x00", "image/png; credential=value")

    assert inspection.metadata.mime_type == "application/octet-stream"
    assert "credential=value" not in repr(inspection)


@pytest.mark.parametrize("secret_stream", ["stdout", "stderr"])
def test_command_output_blocks_both_streams_without_retaining_secret(secret_stream):
    secret = "sk-" + "J" * 32
    values = {"stdout": "safe stdout", "stderr": "safe stderr"}
    values[secret_stream] = "near-start " + secret + " near-end"

    inspection = scanner().inspect_command_output(**values)

    assert inspection.blocked
    assert inspection.stdout is None
    assert inspection.stderr is None
    serialized = repr(asdict(inspection))
    assert secret not in serialized
    assert "near-start" not in serialized
    assert "near-end" not in serialized


def test_clean_command_output_can_be_forwarded():
    inspection = scanner().inspect_command_output("42 tests passed", "")

    assert not inspection.blocked
    assert inspection.stdout == "42 tests passed"
    assert inspection.stderr == ""


def test_additional_patterns_are_classified_without_retaining_the_match():
    scanner_with_custom_pattern = SecretScanner(SALT, {"company_credential": r"corp_[A-Z0-9]{16}"})
    secret = "corp_" + "K" * 16

    report = scanner_with_custom_pattern.scan_text(secret)

    assert [finding.kind for finding in report.findings] == ["company_credential"]
    assert secret not in repr(report)


@pytest.mark.parametrize("salt", [b"short", "not-bytes"])
def test_scan_salt_must_be_nontrivial_bytes(salt):
    with pytest.raises(ValueError):
        SecretScanner(salt)
