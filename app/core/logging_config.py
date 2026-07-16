# app/core/logging_config.py
import logging
import sys
import os

def setup_logging():
    # Permitimos configurar el nivel de logs desde el .env (por defecto INFO)
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    # Limpiamos handlers existentes para evitar logs duplicados
    root_logger = logging.getLogger()
    if root_logger.handlers:
        for handler in root_logger.handlers:
            root_logger.removeHandler(handler)

    # Formato limpio y profesional (ideal para analizar en CloudWatch con filtros)
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Configurar salida a Standard Output (stdout)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.setLevel(log_level)

    # Configuración del Root Logger
    root_logger.setLevel(log_level)
    root_logger.addHandler(stdout_handler)

    # --- CONTROL DE RUIDO ---
    # Silenciamos librerías externas que inundan el log con peticiones internas de red
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("h11").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    logging.info(f"Logging inicializado con éxito en nivel: {log_level_str}")