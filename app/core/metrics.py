# app/core/metrics.py
import json
import logging
import sys
import time
from typing import Dict, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)


def emit_emf_metric(
    namespace: str,
    metrics: Dict[str, Tuple[float, str]],
    dimensions: Optional[Dict[str, str]] = None
) -> None:
    """
    Emite una línea CloudWatch Embedded Metric Format (EMF) directamente a stdout.

    Se escribe DIRECTO a stdout, sin pasar por el logger JSON estándar de
    app.core.logging_config: CloudWatch exige que el bloque "_aws" esté en el nivel
    superior de la línea de log. Si se emitiera vía logger.info(...), el
    JSONFormatter existente anidaría este objeto dentro de un campo "message" como
    string escapado, y CloudWatch dejaría de reconocerlo como EMF.

    No requiere infraestructura nueva: el driver awslogs de ECS ya envía todo el
    stdout del contenedor a CloudWatch Logs, y CloudWatch extrae automáticamente
    las métricas de cualquier línea con esta forma — sin necesitar un metric filter.

    `metrics` es un dict {nombre_metrica: (valor, unidad_cloudwatch)}, p.ej.
    {"queue_depth": (12, "Count")}. `dimensions` son las etiquetas de baja
    cardinalidad bajo las que se agrupa la métrica (por defecto sólo Environment);
    deliberadamente NO se debe pasar aquí nada de alta cardinalidad (smart_code,
    correlation_id, etc.) — cada combinación de dimensiones genera un stream de
    métrica facturado aparte.
    """
    if not metrics:
        return

    dims = dimensions or {"Environment": settings.ENVIRONMENT}
    dimension_names = list(dims.keys())

    emf_obj = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [dimension_names],
                    "Metrics": [
                        {"Name": name, "Unit": unit} for name, (_, unit) in metrics.items()
                    ]
                }
            ]
        },
        **dims,
        **{name: value for name, (value, _) in metrics.items()}
    }

    try:
        sys.stdout.write(json.dumps(emf_obj, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    except Exception as e:
        logger.warning(f"⚠️ [EMF Metrics] No se pudo escribir la métrica '{namespace}': {e}")
