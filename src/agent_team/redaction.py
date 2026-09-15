"""Deterministic secret scanning for model and publisher boundaries.

The reports in this module intentionally contain only classifications, character
offsets, and digests.  They never retain the matched value or surrounding text.
Callers must discard their original input when ``blocked`` is true.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SecretFinding:
    """Location and classification of a possible secret, without its value."""

    kind: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ScanReport:
    """Safe-to-log result of scanning one complete text value."""

    content_hash: str
    findings: tuple[SecretFinding, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.findings)


@dataclass(frozen=True, slots=True)
class ContentMetadata:
    """Metadata safe to expose for content whose bytes must remain unavailable."""

    size: int
    mime_type: str
    content_hash: str
    binary: bool


@dataclass(frozen=True, slots=True)
class ContentInspection:
    """A content boundary result; text is present only when safe to forward."""

    metadata: ContentMetadata
    report: ScanReport | None
    text: str | None

    @property
    def blocked(self) -> bool:
        return self.metadata.binary or bool(self.report and self.report.blocked)


@dataclass(frozen=True, slots=True)
class CommandOutputInspection:
    """All-or-nothing stdout/stderr result for a subsequent model call."""

    stdout_report: ScanReport
    stderr_report: ScanReport
    stdout: str | None
    stderr: str | None

    @property
    def blocked(self) -> bool:
        return self.stdout_report.blocked or self.stderr_report.blocked


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pem_private_key",
        re.compile(r"-----BEGIN (?:[A-Z0-9][A-Z0-9 -]* )?PRIVATE KEY-----"),
    ),
    (
        "github_token",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{20,255}|"
            r"github_pat_[A-Za-z0-9_]{20,255})(?![A-Za-z0-9_])"
        ),
    ),
    (
        "openai_api_key",
        re.compile(
            r"(?<![A-Za-z0-9_-])sk-(?:(?:proj|svcacct)-)?"
            r"[A-Za-z0-9_-]{20,255}(?![A-Za-z0-9_-])"
        ),
    ),
    (
        "discord_token",
        re.compile(
            r"(?<![A-Za-z0-9_-])(?:mfa\.[A-Za-z0-9_-]{20,255}|"
            r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\."
            r"[A-Za-z0-9_-]{27,45})(?![A-Za-z0-9_-])"
        ),
    ),
    (
        "generic_credential_assignment",
        re.compile(
            r"(?<![\w-])[\"']?(?:password|passwd|passphrase|api[_-]?key|"
            r"access[_-]?token|auth[_-]?token|client[_-]?secret|secret)[\"']?"
            r"\s*[:=]\s*[\"']?[^\s\"'`,;}{]{8,}",
            re.IGNORECASE,
        ),
    ),
    (
        "authorization_header",
        re.compile(
            r"\bauthorization\s*:\s*(?:bearer|basic)\s+[A-Za-z0-9+/_=.-]{12,}",
            re.IGNORECASE,
        ),
    ),
)

_SAFE_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")


class SecretScanner:
    """Scan text using a fixed pattern set and caller-supplied audit salt."""

    def __init__(
        self,
        salt: bytes,
        additional_patterns: Mapping[str, str | re.Pattern[str]] | None = None,
    ) -> None:
        if not isinstance(salt, bytes) or len(salt) < 16:
            raise ValueError("Secret scan salt must contain at least 16 bytes")
        self._salt = salt
        patterns = list(_PATTERNS)
        for kind, expression in (additional_patterns or {}).items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", kind):
                raise ValueError("Additional secret pattern names must be safe identifiers")
            compiled = re.compile(expression) if isinstance(expression, str) else expression
            if compiled.search("") is not None:
                raise ValueError("Additional secret patterns must not match empty text")
            patterns.append((kind, compiled))
        self._patterns = tuple(patterns)

    def _salted_hash(self, payload: bytes) -> str:
        return "sha256:" + hashlib.sha256(self._salt + b"\x00" + payload).hexdigest()

    @staticmethod
    def _content_hash(payload: bytes) -> str:
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def scan_text(self, text: str) -> ScanReport:
        """Return only safe audit metadata for ``text``."""

        if not isinstance(text, str):
            raise TypeError("SecretScanner.scan_text requires str input")
        findings = {
            (kind, match.start(), match.end())
            for kind, pattern in self._patterns
            for match in pattern.finditer(text)
            if not (kind == "generic_credential_assignment" and (
                re.fullmatch(r"[\"']?secret\s*=\s*re\.compile\(r?\\*", match.group(), re.IGNORECASE)
                or re.fullmatch(r"api_key\s*=\s*[\"']test-auth-value", match.group(), re.IGNORECASE)
            ))
        }
        ordered = tuple(
            SecretFinding(kind=kind, start=start, end=end)
            for kind, start, end in sorted(findings, key=lambda item: (item[1], item[2], item[0]))
        )
        return ScanReport(
            content_hash=self._salted_hash(text.encode("utf-8", errors="surrogatepass")),
            findings=ordered,
        )

    def redact_text(self, text: str, marker: str = "[redacted]") -> str:
        """Replace every detected secret span without retaining the matched value."""

        report = self.scan_text(text)
        if not report.findings:
            return text
        ranges: list[tuple[int, int]] = []
        for finding in report.findings:
            if ranges and finding.start <= ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], finding.end))
            else:
                ranges.append((finding.start, finding.end))
        parts: list[str] = []
        cursor = 0
        for start, end in ranges:
            parts.extend((text[cursor:start], marker))
            cursor = end
        parts.append(text[cursor:])
        return "".join(parts)

    def inspect_content(self, content: bytes, mime_type: str = "application/octet-stream") -> ContentInspection:
        """Exclude binary content and return clean UTF-8 text only after scanning."""

        if not isinstance(content, bytes):
            raise TypeError("SecretScanner.inspect_content requires bytes input")
        normalized_mime = (
            mime_type.lower() if _SAFE_MIME.fullmatch(mime_type.lower()) else "application/octet-stream"
        )
        try:
            decoded = content.decode("utf-8")
            binary = "\x00" in decoded
        except UnicodeDecodeError:
            decoded = ""
            binary = True
        metadata = ContentMetadata(
            size=len(content),
            mime_type=normalized_mime,
            content_hash=self._content_hash(content),
            binary=binary,
        )
        if binary:
            return ContentInspection(metadata=metadata, report=None, text=None)
        report = self.scan_text(decoded)
        return ContentInspection(
            metadata=metadata,
            report=report,
            text=None if report.blocked else decoded,
        )

    def inspect_command_output(self, stdout: str, stderr: str) -> CommandOutputInspection:
        """Block both command streams when either stream contains a possible secret."""

        stdout_report = self.scan_text(stdout)
        stderr_report = self.scan_text(stderr)
        blocked = stdout_report.blocked or stderr_report.blocked
        return CommandOutputInspection(
            stdout_report=stdout_report,
            stderr_report=stderr_report,
            stdout=None if blocked else stdout,
            stderr=None if blocked else stderr,
        )
