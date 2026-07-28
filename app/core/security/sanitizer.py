# app/core/security/sanitizer.py
import json
from typing import Any, Dict, Union

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