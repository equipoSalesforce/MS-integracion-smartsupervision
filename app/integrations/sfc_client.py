import ssl
import httpx
import json
import logging
from typing import Dict, Any, Optional, Union
from app.core.config import settings
from app.core.exceptions import SfcErrorTranslator
from app.core.auth import SfcAuthManager 

logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN DE SEGURIDAD SSL / TLS 1.2 ---
ssl_context = ssl.create_default_context()
ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
ssl_context.maximum_version = ssl.TLSVersion.TLSv1_2

# --- CONSTANTES DE SEGURIDAD Y PRIVACIDAD ---
SENSITIVE_HEADERS = {
    "authorization", "x-sfc-signature", "x-api-key", "cookie", "set-cookie"
}

SENSITIVE_FIELDS = {
    "nombres", "suppliedname", "numero_id_cf", "id_number__c", 
    "correo", "suppliedemail", "telefono", "suppliedphone", 
    "direccion", "direccion__c", "first_name", "last_name", 
    "email", "phone", "address", "password", "secret_key"
}


def _mask_val(val: Any) -> Any:
    """Enmascara cadenas conservando los 2 primeros y 2 últimos caracteres."""
    if not isinstance(val, str):
        return val
    clean = val.strip()
    if len(clean) <= 4:
        return "*" * len(clean)
    return f"{clean[:2]}***{clean[-2:]}"


def _sanitizar_headers(headers: httpx.Headers) -> Dict[str, str]:
    """Enmascara encabezados sensibles como Tokens de Autorización y Firmas."""
    sanitized = {}
    for k, v in headers.items():
        if k.lower() in SENSITIVE_HEADERS:
            sanitized[k] = _mask_val(v)
        else:
            sanitized[k] = v
    return sanitized


def _sanitizar_payload(data: Any) -> Any:
    """Recorre recursivamente estructuras JSON y enmascara campos de PII."""
    if isinstance(data, dict):
        return {
            k: _mask_val(v) if k.lower() in SENSITIVE_FIELDS and isinstance(v, str) else _sanitizar_payload(v)
            for k, v in data.items()
        }
    elif isinstance(data, list):
        return [_sanitizar_payload(item) for item in data]
    return data


# 🛠️ Hook Sanitizado para Registrar Peticiones Salientes (Request)
async def log_request(request: httpx.Request):
    # --- FILTRO DE LOGS DE ARCHIVOS ---
    is_file_request = (
        "/api/storage/" in str(request.url) 
        or "multipart/form-data" in request.headers.get("content-type", "")
    )
    enable_file_logs = getattr(settings, "ENABLE_FILE_LOGS", False)

    if is_file_request and not enable_file_logs:
        return  # Omitir el log de archivos
    # -----------------------------------

    headers_clean = _sanitizar_headers(request.headers)
    headers_formatted = "\n".join([f"  {k}: {v}" for k, v in headers_clean.items()])

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type or "octet-stream" in content_type:
        body_str = "<[Contenido Binario / Multipart - Omitido por tamaño]>"
    else:
        try:
            if request.content:
                raw_json = json.loads(request.content.decode("utf-8"))
                clean_json = _sanitizar_payload(raw_json)
                body_str = json.dumps(clean_json, ensure_ascii=False)
            else:
                body_str = "<Vacio>"
        except Exception:
            body_str = f"<[Contenido No-JSON / Raw: {len(request.content)} bytes]>" if request.content else "<Vacio>"

    logger.info(
        f"\n==================== [AUDIT HTTP OUTGOING REQUEST] ====================\n"
        f"Method  : {request.method}\n"
        f"URL     : {request.url}\n"
        f"Headers :\n{headers_formatted}\n"
        f"Body    :\n{body_str}\n"
        f"=========================================================================="
    )


# 🛠️ Hook Sanitizado para Registrar Respuestas Entrantes (Response)
async def log_response(response: httpx.Response):
    # --- FILTRO DE LOGS DE ARCHIVOS ---
    is_file_response = (
        "/api/storage/" in str(response.url)
        or (response.request and "multipart/form-data" in response.request.headers.get("content-type", ""))
    )
    enable_file_logs = getattr(settings, "ENABLE_FILE_LOGS", False)

    if is_file_response and not enable_file_logs:
        return  # Omitir el log de archivos
    # -----------------------------------

    await response.aread()

    headers_clean = _sanitizar_headers(response.headers)
    headers_formatted = "\n".join([f"  {k}: {v}" for k, v in headers_clean.items()])

    try:
        if response.text:
            raw_json = response.json()
            clean_json = _sanitizar_payload(raw_json)
            body_str = json.dumps(clean_json, ensure_ascii=False)
        else:
            body_str = "<Vacio>"
    except Exception:
        body_str = response.text or "<Vacio>"

    logger.info(
        f"\n==================== [AUDIT HTTP INCOMING RESPONSE] ====================\n"
        f"Status  : {response.status_code} {response.reason_phrase}\n"
        f"URL     : {response.url}\n"
        f"Headers :\n{headers_formatted}\n"
        f"Body    :\n{body_str}\n"
        f"=========================================================================="
    )


class SfcClient:
    def __init__(self, interceptor: SfcAuthManager):
        self.base_url = settings.SFC_URL_BASE.rstrip('/')
        self.interceptor = interceptor
        self.client = httpx.AsyncClient(
            auth=interceptor,
            verify=ssl_context,
            event_hooks={
                'request': [log_request],
                'response': [log_response]
            }
        )

    async def fetch_quejas_pagina(self, url: Optional[str] = None) -> Dict[str, Any]:
        """Obtiene una página de quejas."""
        target_url = url if url else f"{self.base_url}/api/queja/"
        try:
            response = await self.client.get(target_url)
            if response.status_code not in (200, 201):
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")

    async def get_adjuntos_list(self, codigo_queja: str) -> Dict[str, Any]:
        """Obtiene el listado de archivos adjuntos asociados a una queja."""
        target_url = f"{self.base_url}/api/storage/?codigo_queja__codigo_queja={codigo_queja}"
        try:
            response = await self.client.get(target_url)
            if response.status_code not in (200, 201):
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")

    async def send_ack_batch(self, pqrs_ids: list) -> Dict[str, Any]:
        """Envía el lote de confirmación de recibidos (ACK)."""
        target_url = f"{self.base_url}/api/complaint/ack"
        payload = {"pqrs": pqrs_ids}
        try:
            response = await self.client.post(target_url, json=payload)
            if response.status_code not in (200, 201):
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")
    
    async def post_nueva_queja(self, payload_mapeado: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envía la información estructurada de una queja nueva a la SFC.
        """
        url = f"{self.base_url}/api/queja/"
        logger.info(f"[SfcClient] Enviando metadatos de queja a: {url}")
        logger.info(f"Enviando POST de datos de queja regulatoria: {payload_mapeado.get('codigo_queja')}")
        
        try:
            response = await self.client.post(url, json=payload_mapeado)
            
            if response.status_code not in (200, 201):
                logger.error(f"SFC rechazó la queja. Código: {response.status_code}. Respuesta: {response.text}")
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
                
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")
    
    async def post_adjunto_queja(self, sfc_codigo_queja: str, file_bytes: bytes, file_type: str, file_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Envía un archivo binario asociado a una queja hacia la SFC utilizando multipart/form-data.
        Bypassea el interceptor automático usando auth=None para mitigar errores de streaming.
        """
        url = f"{self.base_url}/api/storage/"
        logger.info(f"[SfcClient] Enviando metadatos de adjuntos a: {url}")
        
        if not file_name:
            file_name = f"soporte_{sfc_codigo_queja}.{file_type}"
            
        if len(file_name) > 150:
            file_name = file_name[-150:]
        
        token = await self.interceptor.get_valid_token()
        signature = self.interceptor.signature_context.get_signature(
            method="POST",
            url="/api/storage/",
            payload={
                "codigo_queja": sfc_codigo_queja,
                "type": file_type
            }
        )
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Cache-Control": "no-cache",
            "Accept": "*/*",
            "Accept-Language": "es",
            "X-SFC-Signature": signature
        }
        
        data = {
            "codigo_queja": sfc_codigo_queja,
            "type": file_type
        }
        
        files = {
            "file": (file_name, file_bytes, f"application/{file_type}")
        }

        logger.info(f"Transmitiendo archivo adjunto ({file_type}) para la queja SFC: {sfc_codigo_queja}")
        
        try:
            response = await self.client.post(url, data=data, files=files, headers=headers, auth=None)
            
            if response.status_code not in (200, 201):
                logger.error(f"SFC rechazó la carga del archivo. Código: {response.status_code}. Respuesta: {response.text}")
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
                
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")

    async def put_actualizar_queja(self, sfc_codigo_queja: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envía la actualización completa de estado, fraudes o cierre (Momento 3)
        hacia la SFC utilizando el verbo PATCH/PUT de forma síncrona.
        """
        url = f"{self.base_url}/api/queja/{sfc_codigo_queja}/"
        logger.info(f"[SfcClient] Enviando actualización de estado M3 a: {url}")
        
        try:
            response = await self.client.patch(url, json=payload)
            
            if response.status_code not in (200, 201):
                logger.error(f"SFC rechazó la actualización del caso. Código: {response.status_code}. Respuesta: {response.text}")
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
                
            return response.json()
            
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")
    
    async def fetch_usuarios_pagina(self, url: Optional[str] = None) -> Dict[str, Any]:
        """Obtiene una página de usuarios actualizados (Momento 4)."""
        target_url = url if url else f"{self.base_url}/api/usuarios/info/"
        try:
            response = await self.client.get(target_url)
            if response.status_code not in (200, 201):
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")

    async def send_user_ack_batch(self, numeros_id_cf: list) -> Dict[str, Any]:
        """Envía el lote de confirmación de recibido para usuarios (Momento 4 ACK)."""
        target_url = f"{self.base_url}/api/usuarios/ack/"
        payload = {"numero_id_CF": numeros_id_cf}
        try:
            response = await self.client.post(target_url, json=payload)
            if response.status_code not in (200, 201):
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")
    
    async def close(self):
        """Cierra de forma segura el pool de conexiones del cliente HTTPX."""
        await self.client.aclose()