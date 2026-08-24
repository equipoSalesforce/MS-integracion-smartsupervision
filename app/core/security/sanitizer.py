# app/core/security/sanitizer.py
import re
import logging
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

# Encabezados de seguridad que contienen credenciales o firmas
SENSITIVE_HEADERS = {
    "authorization", "x-sfc-signature", "x-api-key", "cookie", "set-cookie"
}

IGNORED_LOG_HEADERS = {
    "host", "accept", "accept-encoding", "connection", 
    "user-agent", "content-length", "via", "server", 
    "alt-svc", "referrer-policy", "x-content-type-options", 
    "x-frame-options", "permissions-policy", "content-security-policy"
}

# 🟡 FIX P1-16: se reemplazó el enfoque de blocklist (SENSITIVE_FIELDS) por una
# allowlist (CAMPOS_LOGGEABLES). Con blocklist, un campo NUEVO que alguien olvide
# agregar aquí se loggea sin enmascarar por defecto — el default es inseguro. Con
# allowlist, cualquier campo desconocido se enmascara por defecto y sólo lo
# explícitamente aprobado (identificadores, códigos de clasificación, timestamps,
# metadatos de archivo) se loggea en claro; el default ahora es seguro.
CAMPOS_LOGGEABLES = {
    # Identificadores / trazabilidad (no PII: códigos internos, no datos personales)
    "case_id", "smart_code__c", "smart_code", "codigo_queja", "correlation_id",
    "tipo_operacion", "operation", "registro_id", "queue_item_id", "worker_id",
    "event_id", "id", "source", "status_code", "error_type", "sfc_field",

    # Clasificación / estado del caso (catálogos, no datos personales)
    "status", "estado", "estado_cod", "canal__c", "punto_recepcion",
    "instancia_de_recepcion__c", "product__c", "smart_producto_nombre__c",
    "categorias_col__c", "tutela__c", "ente_de_control__c", "admision_col__c",
    "sc_id_type__c", "sc_genero__c", "tipo_de_persona__c", "sc_lgbtiq__c",
    "sc_condicion_especial__c", "smart_escalamiento_dcf__c", "smart_anexo_queja__c",
    "quejas_express__c", "producto_digital__c", "departamento__c", "sc_municipio__c",
    "codigo_pais__c", "tipo_fraude__c", "modalidad_fraude__c", "favorabilidad__c",
    "aceptacion__c", "rectificacion__c", "prorroga__c", "a_favor_de__c",
    "desistimiento_queja__c", "marcacion__c", "card_amount__c",
    "total_devuelto_por_desconocimiento__c",

    # Timestamps
    "createddate", "closeddate", "lastmodifieddate", "fecha_creacion",
    "fecha_cierre", "fecha_actualizacion", "created_at", "updated_at", "completed_at",

    # Metadatos de archivo (ubicación, no contenido)
    "archivos_s3", "s3_key", "nombre_archivo", "bucket", "nombre_archivo_fraude",
    "directorio_s3",
}

REDACTED_PLACEHOLDER = "[REDACTED]"

DANGEROUS_TAGS_RE = re.compile(
    r"<(script|iframe|embed|object|link|meta|base)[^>]*?>", 
    re.IGNORECASE | re.DOTALL
)
DANGEROUS_SCHEMES_RE = re.compile(
    r'(src|href)\s*=\s*["\']?\s*(file://|http://169\.254\.|http://127\.|http://localhost|http://10\.|http://172\.(1[6-9]|2[0-9]|3[01])\.|http://192\.168\.)',
    re.IGNORECASE
)


def mask_value(val: str, visible_chars: int = 2) -> str:
    """Enmascara cadenas de texto conservando pocos caracteres para trazabilidad de auditoría."""
    if not val or not isinstance(val, str):
        return "***"
    val_clean = val.strip()
    if len(val_clean) <= visible_chars * 2:
        return "*" * len(val_clean)
    return f"{val_clean[:visible_chars]}***{val_clean[-visible_chars:]}"


def sanitizar_headers(headers: Any) -> Dict[str, str]:
    """Oculta tokens y firmas de los encabezados HTTP y filtra cabeceras ruidosas."""
    sanitized = {}
    for k, v in headers.items():
        k_lower = str(k).lower()
        if k_lower in IGNORED_LOG_HEADERS:
            continue
        if k_lower in SENSITIVE_HEADERS:
            sanitized[k] = mask_value(str(v), visible_chars=4)
        else:
            sanitized[k] = v
    return sanitized


def sanitizar_payload(data: Union[Dict, list, str, Any]) -> Any:
    """
    Recorre recursivamente un JSON y sólo deja en claro los campos declarados en
    CAMPOS_LOGGEABLES (allowlist); cualquier campo NO reconocido se redacta por
    defecto, sea PII conocida o un campo nuevo que aún no se haya clasificado.
    """
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                # Se recorre siempre, sea o no la llave contenedora parte de la
                # allowlist: los campos PII anidados igual se enmascaran por su
                # propio nombre: no perder de vista sub-campos legítimamente
                # loggeables sólo porque el contenedor no fue clasificado.
                cleaned[k] = sanitizar_payload(v)
            elif str(k).lower() in CAMPOS_LOGGEABLES:
                cleaned[k] = v
            elif isinstance(v, str):
                cleaned[k] = mask_value(v)
            elif v is None:
                cleaned[k] = None
            else:
                cleaned[k] = REDACTED_PLACEHOLDER
        return cleaned
    elif isinstance(data, list):
        return [sanitizar_payload(item) for item in data]
    return data


def sanitizar_texto_plano(raw_text: Optional[str], max_chars: int = 120) -> str:
    """
    🟡 FIX P1-16: para cuerpos de respuesta que NO son JSON (HTML, texto plano de un
    error upstream), no hay estructura de campos que enmascarar selectivamente — y ese
    texto puede reflejar de vuelta datos del payload original enviado (ej. un mensaje
    de error de la SFC/CRM que cita el valor rechazado). En vez de loggear el texto
    completo sin control, se registra sólo tamaño y una vista previa acotada, igual de
    útil para depurar sin arriesgar un volcado completo de datos externos a CloudWatch.
    """
    if not raw_text:
        return "<vacío>"
    largo = len(raw_text)
    preview = raw_text[:max_chars].replace("\n", " ").replace("\r", " ")
    sufijo = "..." if largo > max_chars else ""
    return f"<contenido no-JSON, {largo} caracteres> preview='{preview}{sufijo}'"


def sanitizar_html_para_pdf(html_raw: str) -> str:
    """
    Sanitiza el contenido HTML entrante antes de procesarlo para generación de PDF.
    Remueve etiquetas ejecutables/incrustadas (script, iframe, embed, link) y bloquea
    esquemas de archivos locales (file://) o direcciones IP privadas (Metadata AWS / SSRF).
    """
    if not html_raw:
        return ""

    html_clean = html_raw

    # 1. Eliminar etiquetas de riesgo alto (scripts, iframes, objetos)
    if DANGEROUS_TAGS_RE.search(html_clean):
        logger.warning("🛡️ [Sanitizer PDF] Etiquetas peligrosas detectadas y neutralizadas en HTML.")
        html_clean = DANGEROUS_TAGS_RE.sub("", html_clean)

    # 2. Bloquear URLs apuntando a IPs privadas (AWS IMDS / localhost) o archivos locales (file://)
    if DANGEROUS_SCHEMES_RE.search(html_clean):
        logger.warning("🛡️ [Sanitizer PDF] Enlace/Esquema sospechoso (SSRF/File) neutralizado en HTML.")
        html_clean = DANGEROUS_SCHEMES_RE.sub(r'\1="#"', html_clean)

    return html_clean