"""Credential-free explanation rendering service."""

import base64

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .explanations import render_explanation
from .policy import GuardError
from .redaction import SecretScanner


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: str
    source_hash: str
    markdown: str = Field(min_length=1, max_length=2_000_000)


def create_renderer():
    app = FastAPI(title="Explanation Renderer")
    scanner = SecretScanner(b"credential-free-renderer-process")

    @app.get("/health")
    def health():
        return {"status": "ok", "credentials": False, "network_assets": False}

    @app.post("/render")
    def render(request: RenderRequest):
        try:
            if scanner.scan_text(request.markdown).blocked:
                raise GuardError("Potential secret blocked at renderer input boundary")
            html, png = render_explanation(
                request.source_kind,
                request.source_hash,
                request.markdown,
            )
            if scanner.scan_text(html).blocked:
                raise GuardError("Potential secret blocked at renderer output boundary")
            return {
                "html": html,
                "png_base64": base64.b64encode(png).decode("ascii"),
            }
        except GuardError as error:
            raise HTTPException(422, str(error)) from error

    return app
