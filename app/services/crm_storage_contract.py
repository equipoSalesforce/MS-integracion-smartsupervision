"""Pure validation for CRM storage. Legacy Salesforce ownership stays unchanged."""
import re
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID

from app.core.config import settings
from app.core.exceptions import SfcIntegrationException


def reject_storage(field: str) -> None:
    raise SfcIntegrationException(
        status_code=403, error_type="S3_KEY_OWNERSHIP_MISMATCH", sfc_field=field,
        raw_message="La referencia de almacenamiento no pertenece al Case CRM o bucket permitido.",
        crm_action="Verifique el UUID del Case, bucket y directorio de todos los adjuntos.",
    )


def validate_crm_case_uuid(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value
    ):
        reject_storage("crm_case_uuid")
    if UUID(value).int == 0:
        reject_storage("crm_case_uuid")
    return value


def normalize_reference(value: str, crm_case_uuid: str, *, bucket: str | None = None,
                        directory: bool = False) -> tuple[str, str]:
    """Resolve a key or stable virtual-host HTTPS URL without network access."""
    case_uuid = validate_crm_case_uuid(crm_case_uuid)
    allowed_bucket = settings.CRM_S3_BUCKET or settings.AWS_S3_BUCKET
    field = "directorio_s3" if directory else "archivos_s3"
    if not isinstance(value, str) or not value or value != value.strip():
        reject_storage(field)
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or any(char in value for char in "\\?#"):
        reject_storage(field)
    resolved_bucket = bucket or allowed_bucket
    key = value
    if "://" in value:
        try:
            url = urlsplit(value)
        except ValueError:
            reject_storage(field)
        host = re.fullmatch(r"([a-z0-9][a-z0-9.-]+)\.s3(?:\.[a-z0-9-]+)?\.amazonaws\.com", url.netloc)
        if url.scheme != "https" or not host or url.query or url.fragment:
            reject_storage(field)
        resolved_bucket = host.group(1)
        if bucket is not None and bucket != resolved_bucket:
            reject_storage(field)
        try:
            key = unquote(url.path[1:], errors="strict")
        except (ValueError, UnicodeError):
            reject_storage(field)
    if resolved_bucket != allowed_bucket:
        reject_storage(field)
    if any(char in key for char in "\\%?#") or any(ord(char) < 32 or ord(char) == 127 for char in key):
        reject_storage(field)
    parts = key.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts) or not key.startswith(f"caso/{case_uuid}/"):
        reject_storage(field)
    if directory:
        key = key.rstrip("/") + "/"
    elif key.endswith("/") or not key.startswith(f"caso/{case_uuid}/adjuntos/"):
        reject_storage(field)
    return resolved_bucket, key


def normalize_crm_storage(crm_case_uuid: str, directory: str | None,
                          files: list[Any]) -> tuple[str | None, list[dict]]:
    """Validate the whole batch before any I/O or checkpoint can skip a file."""
    validate_crm_case_uuid(crm_case_uuid)
    parent = normalize_reference(directory, crm_case_uuid, directory=True)[1] if directory is not None else None
    normalized = []
    for item in files:
        data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        if not data.get("bucket") or data.get("bytes") is not None:
            reject_storage("archivos_s3")
        bucket, key = normalize_reference(data.get("s3_key"), crm_case_uuid, bucket=data.get("bucket"))
        if parent and not key.startswith(parent):
            reject_storage("directorio_s3")
        normalized.append({**data, "bucket": bucket, "s3_key": key})
    return parent, normalized
