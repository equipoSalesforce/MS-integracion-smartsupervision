# app/core/middleware.py
import uuid
import logging
from contextvars import ContextVar
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

# Variable de contexto asíncrono para inyectar el Correlation ID en cualquier punto de la app
correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="N/A")

logger = logging.getLogger(__name__)


def get_correlation_id() -> str:
    """Devuelve el Correlation ID activo en la corrutina actual."""
    return correlation_id_ctx.get()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """
    Middleware que extrae el encabezado X-Correlation-ID de la petición entrante
    o genera un nuevo UUIDv4 si no viene informado. Lo propaga en los logs y la respuesta.
    """

    async def dispatch(self, request: Request, call_next):
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        
        # Establece la variable de contexto para esta petición
        token = correlation_id_ctx.set(correlation_id)
        request.state.correlation_id = correlation_id

        try:
            response = await call_next(request)
            # Retorna el mismo ID en las cabeceras de respuesta para Salesforce
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        finally:
            correlation_id_ctx.reset(token)