# app/core/logging_config.py
import logging
import sys
import os
from app.core.middleware import get_correlation_id


class CorrelationIdFilter(logging.Filter):
    """
    Filtro de Logging que inyecta el correlation_id activo en cada registro de log
    obteniéndolo del contexto asíncrono (ContextVar) del Middleware.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        return True


def setup_logging():
    # Permitimos configurar el nivel de logs desde el .env (por defecto INFO)
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    # Limpiamos handlers existentes para evitar logs duplicados
    root_logger = logging.getLogger()
    if root_logger.handlers:
        for handler in root_logger.handlers:
            root_logger.removeHandler(handler)

    # Formato profesional con inyección de Correlation ID [CID: ...]
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s [CID: %(correlation_id)s] [%(name)s.%(funcName)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Configurar salida a Standard Output (stdout)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.setLevel(log_level)
    stdout_handler.addFilter(CorrelationIdFilter())

    # Configuración del Root Logger
    root_logger.setLevel(log_level)
    root_logger.addHandler(stdout_handler)

    # --- CONTROL DE RUIDO DE LIBRERÍAS EXTERNAS ---
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("h11").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    logging.info(f"Logging inicializado con éxito en nivel: {log_level_str}")