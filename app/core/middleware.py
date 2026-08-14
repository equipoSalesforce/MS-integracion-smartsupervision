import json
import uuid
import logging
from contextvars import ContextVar
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

# ContextVars asíncronas para almacenar la doble trazabilidad por corrutina
correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="N/A")
aws_trace_id_ctx: ContextVar[str] = ContextVar("aws_trace_id", default="N/A")

logger = logging.getLogger(__name__)


def get_correlation_id() -> str:
    """Devuelve el Correlation ID activo de la corrutina actual (Origen Salesforce/CRM)."""
    return correlation_id_ctx.get()


def get_aws_trace_id() -> str:
    """Devuelve el AWS Trace ID activo de la corrutina actual (Origen ALB/X-Ray)."""
    return aws_trace_id_ctx.get()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """
    Middleware de trazabilidad distribuida.
    Captura/genera el X-Correlation-ID (negocio) y el X-Amzn-Trace-Id (infraestructura AWS),
    establece las variables de contexto asíncronas y los propaga en las cabeceras de respuesta.
    """

    async def dispatch(self, request: Request, call_next):
        # 1. Trazabilidad de Negocio (Salesforce/CRM)
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        
        # 2. Trazabilidad de Infraestructura (AWS ALB / X-Ray)
        aws_trace_id = request.headers.get("X-Amzn-Trace-Id") or "N/A"

        token_cid = correlation_id_ctx.set(correlation_id)
        token_trace = aws_trace_id_ctx.set(aws_trace_id)

        request.state.correlation_id = correlation_id
        request.state.aws_trace_id = aws_trace_id

        try:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = correlation_id
            if aws_trace_id != "N/A":
                response.headers["X-Amzn-Trace-Id"] = aws_trace_id
            return response
        finally:
            correlation_id_ctx.reset(token_cid)
            aws_trace_id_ctx.reset(token_trace)


class MaxBodySizeExceededError(Exception):
    """Señal interna: el body de la request superó el límite configurado."""


# 🟢 FIX HALLAZGO 41: límite global de tamaño de request body.
class MaxBodySizeMiddleware:
    """
    Middleware ASGI puro (no BaseHTTPMiddleware, para poder cortar el stream de bytes
    a medida que llegan) que rechaza con 413 cualquier request cuyo body exceda
    `max_body_size`. Cubre dos casos:
      1. Content-Length declarado: rechazo inmediato, sin leer nada del body.
      2. Sin Content-Length confiable (chunked, o header incorrecto): se cuenta cada
         chunk recibido y se aborta apenas se supera el límite, sin acumular todo en RAM.
    """

    def __init__(self, app, max_body_size: int):
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_size:
                    await self._enviar_413(send)
                    return
            except ValueError:
                pass

        total_size = 0

        async def receive_wrapper():
            nonlocal total_size
            message = await receive()
            if message["type"] == "http.request":
                total_size += len(message.get("body") or b"")
                if total_size > self.max_body_size:
                    raise MaxBodySizeExceededError()
            return message

        try:
            await self.app(scope, receive_wrapper, send)
        except MaxBodySizeExceededError:
            await self._enviar_413(send)

    @staticmethod
    async def _enviar_413(send):
        body = json.dumps({
            "status_code": 413,
            "error_type": "REQUEST_BODY_TOO_LARGE",
            "sfc_field": None,
            "raw_message": "El tamaño del body de la solicitud excede el límite permitido.",
            "crm_action_friendly": (
                "Reduzca el tamaño del payload (ej. cantidad de archivos o longitud de texto) y reintente."
            )
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })