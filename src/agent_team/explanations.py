"""要件・計画の説明成果物に対する、生成器外側の検査。"""

import base64
import hashlib
import html as html_module
import re
import struct
import zlib
from dataclasses import dataclass

import httpx

from .policy import GuardError, digest

REQUIRED_LABELS = (
    "利用価値",
    "対象範囲",
    "対象外",
    "通常フロー",
    "権限",
    "データ境界",
    "復元困難",
    "失敗",
    "受入条件",
    "ロールバック",
    "未解決事項",
)
UNSAFE_HTML = re.compile(
    r"<(?:script|iframe|object|embed|link|base)\b|\bon\w+\s*=|\b(?:src|href)\s*=\s*['\"](?:https?:|//|data:text/html)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExplanationIdentity:
    source_kind: str
    source_hash: str
    html_hash: str
    png_hash: str


def validate_explanation_contract(text: str) -> None:
    missing = [label for label in REQUIRED_LABELS if label not in text]
    if missing:
        raise GuardError("Missing explanation sections: " + ", ".join(missing))


def validate_html(html: str) -> None:
    if UNSAFE_HTML.search(html):
        raise GuardError("Explanation HTML contains active or external content")
    compact = html.casefold().replace(" ", "")
    if "content-security-policy" not in compact:
        raise GuardError("Explanation HTML requires a CSP")


def bind_explanation(
    *, source_kind: str, source_hash: str, html: str, png: bytes
) -> ExplanationIdentity:
    if source_kind not in {"requirements", "plan"} or not source_hash.startswith("sha256:"):
        raise GuardError("Invalid explanation source identity")
    validate_explanation_contract(html)
    validate_html(html)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise GuardError("Explanation image must be PNG")
    return ExplanationIdentity(
        source_kind=source_kind,
        source_hash=source_hash,
        html_hash=digest(html),
        png_hash="sha256:" + hashlib.sha256(png).hexdigest(),
    )



def render_explanation(source_kind: str, source_hash: str, markdown: str) -> tuple[str, bytes]:
    """資格情報や外部資材を使わない固定templateで説明成果物を作る。"""
    if source_kind not in {"requirements", "plan"} or not source_hash.startswith("sha256:"):
        raise GuardError("Invalid explanation source identity")
    escaped = html_module.escape(markdown)
    labels = "".join(f"<li>{label}</li>" for label in REQUIRED_LABELS)
    title = "要件説明" if source_kind == "requirements" else "実装計画説明"
    html = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{font-family:system-ui,sans-serif;max-width:1000px;margin:auto;padding:32px;color:#172033}}
h1{{color:#3156d3}} ul{{columns:2}} pre{{white-space:pre-wrap;background:#f4f6fb;padding:24px}}
</style></head><body><h1>{title}</h1><p>source: <code>{source_hash}</code></p>
<h2>説明項目</h2><ul>{labels}</ul><h2>承認対象の内容</h2><pre>{escaped}</pre></body></html>"""
    width, height = 1200, 630
    row = b"\x00" + b"\xf4\xf6\xfb" * width
    raw = row * height

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    validate_explanation_contract(html)
    validate_html(html)
    return html, png


class LocalExplanationRenderer:
    def render(self, source_kind: str, source_hash: str, markdown: str) -> tuple[str, bytes]:
        return render_explanation(source_kind, source_hash, markdown)


class RemoteExplanationRenderer:
    """Calls the credential-free renderer process with a bounded retry."""

    def __init__(self, url: str, *, attempts: int = 2, timeout: float = 30):
        self.url = url.rstrip("/")
        self.attempts = attempts
        self.timeout = timeout

    def render(self, source_kind: str, source_hash: str, markdown: str) -> tuple[str, bytes]:
        last_error = None
        for _ in range(self.attempts):
            try:
                response = httpx.post(
                    self.url + "/render",
                    json={
                        "source_kind": source_kind,
                        "source_hash": source_hash,
                        "markdown": markdown,
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
                html = payload["html"]
                png = base64.b64decode(payload["png_base64"], validate=True)
                bind_explanation(
                    source_kind=source_kind,
                    source_hash=source_hash,
                    html=html,
                    png=png,
                )
                return html, png
            except (httpx.HTTPError, KeyError, ValueError) as error:
                last_error = error
        raise GuardError("Explanation renderer failed after two attempts") from last_error
