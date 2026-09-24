"""Bounded, request-local evidence for the CRM UUID dispatch contract."""
import hashlib
import json
from contextvars import ContextVar

from app.core.security.sanitizer import sanitizar_payload, sanitizar_texto_plano

_evidence: ContextVar[dict | None] = ContextVar("crm_dispatch_evidence", default=None)


def enable_crm_evidence() -> None:
    value = _evidence.get()
    if value is not None:
        value["enabled"] = True
        value["storage_ownership"] = "PASS"


def capture_prepared(payload: dict, stage: str) -> None:
    value = _evidence.get()
    if value is not None and value.get("enabled") and len(value["prepared"]) < 60:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        value["prepared"].append({"stage": stage, "payload": sanitizar_payload(payload),
                                  "sha256": hashlib.sha256(serialized.encode()).hexdigest()})


async def capture_sfc_response(response) -> None:
    from app.core.reopen_diagnostic import capture_reopen_http_status
    capture_reopen_http_status(response)
    value = _evidence.get()
    if value is None or not value.get("enabled") or len(value["responses"]) >= 60:
        return
    content_type = response.headers.get("content-type", "").lower()
    if "json" not in content_type and "text/" not in content_type:
        return
    await response.aread()
    try:
        body = sanitizar_payload(response.json())
    except (ValueError, UnicodeError):
        body = sanitizar_texto_plano(response.text)
    raw = json.dumps(body, ensure_ascii=False, default=str)
    value["responses"].append({
        "http_status": response.status_code, "method": response.request.method,
        "raw_response": raw[:8192], "truncated": len(raw) > 8192,
        "correlation_id": response.headers.get("x-correlation-id") or response.request.headers.get("x-correlation-id"),
    })


class CrmDispatchEvidenceMiddleware:
    """Add evidence to new CRM responses; leave legacy bodies untouched."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").endswith("/sync/despacho"):
            return await self.app(scope, receive, send)
        value = {"enabled": False, "prepared": [], "responses": []}
        token = _evidence.set(value)
        start = None
        chunks = []

        async def with_evidence(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] != "http.response.body":
                return await send(message)
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            if value["enabled"]:
                try:
                    data = json.loads(body)
                    if isinstance(data, dict):
                        data["ssv_observability"] = {key: item for key, item in value.items() if key != "enabled"}
                        body = json.dumps(data, ensure_ascii=False, default=str).encode()
                except (ValueError, UnicodeError):
                    pass
            if start is not None:
                start = {**start, "headers": [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
                         + [(b"content-length", str(len(body)).encode())]}
                await send(start)
            await send({"type": "http.response.body", "body": body})

        try:
            await self.app(scope, receive, with_evidence)
        finally:
            _evidence.reset(token)
