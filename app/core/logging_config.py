import json
import logging
import sys
import os
from datetime import datetime, timezone
from app.core.middleware import get_correlation_id, get_aws_trace_id


class JSONFormatter(logging.Formatter):
    """
    Formateador estructurado en JSON de una sola línea.
    Permite la indexación automática de campos en AWS CloudWatch Logs Insights.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "correlation_id": getattr(record, "correlation_id", "N/A"),
            "aws_trace_id": getattr(record, "aws_trace_id", "N/A"),
            "logger": record.name,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }

        # Inclusión de información de excepciones si aplican
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        # Inclusión de metadatos o diccionarios extra
        if hasattr(record, "extra_data") and isinstance(record.extra_data, dict):
            log_obj["extra"] = record.extra_data

        return json.dumps(log_obj, ensure_ascii=False)


class TraceContextFilter(logging.Filter):
    """
    Filtro que extrae del contexto asíncrono los IDs de trazabilidad
    ( correlation_id y aws_trace_id ) y los inyecta en cada LogRecord.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        record.aws_trace_id = get_aws_trace_id()
        return True


def setup_logging():
    """Configura el Root Logger con salida JSON estandarizada hacia stdout."""
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    root_logger = logging.getLogger()
    if root_logger.handlers:
        for handler in root_logger.handlers:
            root_logger.removeHandler(handler)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(JSONFormatter())
    stdout_handler.setLevel(log_level)
    stdout_handler.addFilter(TraceContextFilter())

    root_logger.setLevel(log_level)
    root_logger.addHandler(stdout_handler)

    # --- SILENCIAR RUIDO DE LIBRERÍAS DE INFRAESTRUCTURA ---
    for lib_logger in ("httpx", "httpcore", "h11", "botocore", "urllib3", "asyncio"):
        logging.getLogger(lib_logger).setLevel(logging.WARNING)

    logging.info(f"Logging estructurado JSON inicializado en nivel: {log_level_str}")