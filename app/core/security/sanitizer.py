# app/core/security/sanitizer.py
import json
import re
import logging
from typing import Any, Dict, Union

logger = logging.getLogger(__name__)

# Encabezados de seguridad que contienen credenciales o firmas
SENSITIVE_HEADERS = {
    "authorization", "x-sfc-signature", "x-api-key", "cookie", "set-cookie"
}

# Campos de PII y credenciales en payloads (SFC y CRM)
SENSITIVE_FIELDS = {
    "nombres", "suppliedname", "numero_id_cf", "id_number__c", 
    "correo", "suppliedemail", "telefono", "suppliedphone", 
    "direccion", "direccion__c", "first_name", "last_name", 
    "email", "phone", "address", "sfc_password", "password", 
    "secret_key", "sfc_secret_key"
}

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
    """Oculta tokens y firmas de los encabezados HTTP."""
    sanitized = {}
    for k, v in headers.items():
        if k.lower() in SENSITIVE_HEADERS:
            sanitized[k] = mask_value(v, visible_chars=4)
        else:
            sanitized[k] = v
    return sanitized


def sanitizar_payload(data: Union[Dict, list, str, Any]) -> Any:
    """Recorre recursivamente un JSON y enmascara los campos declarados como PII."""
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            if k.lower() in SENSITIVE_FIELDS and isinstance(v, str):
                cleaned[k] = mask_value(v)
            else:
                cleaned[k] = sanitizar_payload(v)
        return cleaned
    elif isinstance(data, list):
        return [sanitizar_payload(item) for item in data]
    return data

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