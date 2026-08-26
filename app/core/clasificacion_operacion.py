# app/core/clasificacion_operacion.py
"""
Clasificación de "es cierre" para una queja -- fuente única de verdad.

🔴 FIX (hallazgo N9, revisión externa v5, 2026-08-25): esta misma expresión booleana
vivía duplicada de forma independiente en tres archivos (crm_payloads.py, despacho_
queja_orchestrator.py, idempotency_service.py). Hoy no diverge en la práctica -- se
confirmó con el equipo que `Aceptacion__c` sólo se popula junto con `Favorabilidad__c`
(ambos exigidos juntos por QuejaUnificadaCrmInput._validar_reglas_cierre para
cualquier cierre real), así que la condición de `Aceptacion__c` nunca dispara sola.
Pero al vivir triplicada sin ningún punto de control compartido, un cambio futuro en
cualquiera de los tres lugares (o en esa regla de negocio) podía hacerlas divergir en
silencio -- exactamente el riesgo que este módulo elimina.
"""
from typing import Optional


def es_estado_cierre(
    status: Optional[str],
    closed_date,
    favorabilidad: Optional[str],
    aceptacion: Optional[str],
) -> bool:
    """
    True si el estado/los campos presentes indican un CIERRE definitivo de la queja.
    Los cuatro parámetros son deliberadamente valores sueltos (no un payload/dict
    completo) para que cualquier caller -- ya sea que tenga un modelo Pydantic
    (atributos) o un dict crudo (`.get(...)`) -- pueda extraer sus propios cuatro
    campos sin que esta función necesite conocer la forma de su origen.
    """
    status_clean = (status or "").strip().lower()
    return (
        status_clean in ("closed", "cerrado")
        or closed_date is not None
        or favorabilidad is not None
        or aceptacion is not None
    )
