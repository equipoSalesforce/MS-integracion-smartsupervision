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