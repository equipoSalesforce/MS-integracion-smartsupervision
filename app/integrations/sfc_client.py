# app/integrations/sfc_client.py
import asyncio
import functools
import ssl
import httpx
import json
import logging
from typing import Dict, Any, Optional, Union
from app.core.config import settings
from app.core.exceptions import SfcErrorTranslator, SfcIntegrationException
from app.core.auth import SfcAuthManager 
from app.core.constants import SfcEndpoints, SmartStatus
from app.core.security.sanitizer import sanitizar_headers, sanitizar_payload
from app.core.middleware import get_aws_trace_id, get_correlation_id
from app.core.security.signatures import ssl_context

logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN DE SEGURIDAD SSL / TLS 1.2 ---
ssl_context = ssl.create_default_context()
ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
ssl_context.maximum_version = ssl.TLSVersion.TLSv1_2


# 🛠️ Hook Sanitizado para Registrar Peticiones Salientes (Request)
async def log_request(request: httpx.Request):
    """Hook para registrar peticiones HTTP salientes hacia la SFC en formato JSON estructurado."""
    cid = get_correlation_id()
    aws_trace = get_aws_trace_id()

    if cid and cid != "N/A":
        request.headers["X-Correlation-ID"] = cid
    if aws_trace and aws_trace != "N/A":
        request.headers["X-Amzn-Trace-Id"] = aws_trace

    is_file_request = (
        SfcEndpoints.STORAGE.value in str(request.url) 
        or "multipart/form-data" in request.headers.get("content-type", "")
    )
    if is_file_request and not getattr(settings, "ENABLE_FILE_LOGS", False):
        return

    headers_clean = sanitizar_headers(dict(request.headers))

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type or "octet-stream" in content_type:
        body_clean = "<Contenido Binario / Multipart - Omitido>"
    else:
        try:
            if request.content:
                raw_json = json.loads(request.content.decode("utf-8"))
                body_clean = sanitizar_payload(raw_json)
            else:
                body_clean = None
        except Exception:
            body_clean = "<Contenido No-JSON / Raw>"

    logger.info("AUDIT_HTTP_OUTGOING_REQUEST", extra={
        "extra_data": {
            "direction": "OUTGOING_REQUEST",
            "method": request.method,
            "url": str(request.url),
            "headers": headers_clean,
            "body": body_clean
        }
    })


# 🛠️ Hook Sanitizado para Registrar Respuestas Entrantes (Response)
async def log_response(response: httpx.Response):
    """Hook para registrar respuestas HTTP entrantes desde la SFC en formato JSON estructurado."""
    is_file_response = (
        SfcEndpoints.STORAGE.value in str(response.url)
        or (response.request and "multipart/form-data" in response.request.headers.get("content-type", ""))
    )
    if is_file_response and not getattr(settings, "ENABLE_FILE_LOGS", False):
        return

    await response.aread()
    headers_clean = sanitizar_headers(dict(response.headers))

    try:
        if response.text:
            raw_json = response.json()
            body_clean = sanitizar_payload(raw_json)
        else:
            body_clean = None
    except Exception:
        body_clean = response.text or None

    logger.info("AUDIT_HTTP_INCOMING_RESPONSE", extra={
        "extra_data": {
            "direction": "INCOMING_RESPONSE",
            "status_code": response.status_code,
            "reason_phrase": response.reason_phrase,
            "url": str(response.url),
            "headers": headers_clean,
            "body": body_clean
        }
    })


def handle_sfc_throttling(func):
    """
    Decorador asíncrono para métodos de SfcClient.
    Captura respuestas 429 / Throttling / Quota Exceeded y ejecuta un mini-delay
    transparente antes de reintentar la operación hasta N veces.
    Si se agotan los reintentos, la excepción se relanza para que el orquestador
    o router la capture y la guarde en la cola de contingencia (Redis).
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        attempts = 0
        max_retries = getattr(settings, "SFC_MINI_RETRY_ATTEMPTS", settings.SFC_MINI_RETRY_ATTEMPTS)
        delay = getattr(settings, "SFC_MINI_RETRY_DELAY_SECONDS", settings.SFC_MINI_RETRY_DELAY_SECONDS)

        while True:
            try:
                return await func(*args, **kwargs)
            except SfcIntegrationException as exc:
                raw_msg_lower = str(getattr(exc, "raw_message", "") or "").lower()
                error_type_str = str(getattr(exc, "error_type", "") or "").upper()
                
                is_throttled = (
                    exc.status_code == 429 or 
                    error_type_str in ("THROTTLED_ERROR", "RATE_LIMIT_ERROR", "INFRASTRUCTURE_ERROR") or
                    "throttled" in raw_msg_lower or
                    "quota" in raw_msg_lower or
                    "resource_exhausted" in raw_msg_lower
                )

                if is_throttled and attempts < max_retries:
                    attempts += 1
                    logger.warning(
                        f"⏳ [SfcClient] Solicitud regulada / cuota superada por la SFC (429 Throttled/Quota). "
                        f"Ejecutando mini-delay de {delay}s antes del reintento {attempts}/{max_retries}..."
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

    return wrapper


class SfcClient:
    def __init__(self, interceptor: SfcAuthManager, http_client: Optional[httpx.AsyncClient] = None):
        self.base_url = settings.SFC_URL_BASE.rstrip('/')
        self.interceptor = interceptor
        
        if http_client is not None:
            self.client = http_client
        else:
            timeout_sfc = httpx.Timeout(connect=3.0, read=15.0, write=10.0, pool=10.0)
            self.client = httpx.AsyncClient(
                timeout=timeout_sfc,
                auth=interceptor,
                verify=ssl_context,
                event_hooks={
                    'request': [log_request],
                    'response': [log_response]
                }
            )

    @handle_sfc_throttling
    async def fetch_quejas_pagina(self, url: Optional[str] = None) -> Dict[str, Any]:
        """Obtiene una página de quejas."""
        target_url = url if url else f"{self.base_url}{SfcEndpoints.QUEJA.value}"
        try:
            response = await self.client.get(target_url, auth=self.interceptor)
            if response.status_code not in (200, 201):
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")

    @handle_sfc_throttling
    async def get_adjuntos_list(self, codigo_queja: str) -> Dict[str, Any]:
        """Obtiene el listado de archivos adjuntos asociados a una queja."""
        target_url = f"{self.base_url}{SfcEndpoints.STORAGE.value}?codigo_queja__codigo_queja={codigo_queja}"
        try:
            response = await self.client.get(target_url, auth=self.interceptor)
            if response.status_code not in (200, 201):
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")

    @handle_sfc_throttling
    async def send_ack_batch(self, pqrs_ids: list) -> Dict[str, Any]:
        """Envía el lote de confirmación de recibidos (ACK)."""
        target_url = f"{self.base_url}{SfcEndpoints.ACK_COMPLAINT.value}"
        payload = {"pqrs": pqrs_ids}
        try:
            response = await self.client.post(target_url, json=payload, auth=self.interceptor)
            if response.status_code not in (200, 201):
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")
    
    @handle_sfc_throttling
    async def post_nueva_queja(self, payload_mapeado: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envía la información estructurada de una queja nueva a la SFC.
        """
        url = f"{self.base_url}{SfcEndpoints.QUEJA.value}"
        logger.info(f"[SfcClient] Enviando metadatos de queja a: {url}")
        logger.info(f"Enviando POST de datos de queja regulatoria: {payload_mapeado.get('codigo_queja')}")
        
        try:
            response = await self.client.post(url, json=payload_mapeado, auth=self.interceptor)
            
            if response.status_code not in (200, 201):
                logger.error(f"SFC rechazó la queja. Código: {response.status_code}. Respuesta: {response.text}")
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
                
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")
    
    @handle_sfc_throttling
    async def post_adjunto_queja(
        self, 
        sfc_codigo_queja: str, 
        file_data: Any, 
        file_type: str, 
        file_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Envía un archivo asociado a una queja hacia la SFC utilizando multipart/form-data.
        Soporta transmisión vía streaming directo desde objetos file-like (SpooledTemporaryFile)
        o bytes en memoria, evitando lecturas globales que causen desbordamientos de RAM (OOM).
        """
        endpoint = SfcEndpoints.STORAGE.value
        url = f"{self.base_url}{endpoint}"
        logger.info(f"[SfcClient] Enviando metadatos de adjuntos a: {url}")
        
        if not file_name:
            file_name = f"soporte_{sfc_codigo_queja}.{file_type}"
            
        if len(file_name) > 150:
            file_name = file_name[-150:]
        
        token = await self.interceptor.get_valid_token()
        signature = self.interceptor.signature_context.get_signature(
            method="POST",
            url=endpoint,
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

        # 🟢 1. REBOBINADO AUTOMÁTICO: Si file_data es un stream, posicionar el puntero al inicio
        if hasattr(file_data, "seek") and callable(file_data.seek):
            file_data.seek(0)

        # 🟢 2. TRANSMISIÓN EN STREAMING SINO ES BYTES DIRECTOS:
        # Se pasa file_data directamente a httpx sin llamar a .read(), permitiendo
        # que httpx lea en fragmentos pequeños (chunked streaming) desde el SpooledTemporaryFile.
        files = {
            "file": (file_name, file_data, f"application/{file_type}")
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

    @handle_sfc_throttling
    async def put_actualizar_queja(self, sfc_codigo_queja: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envía la actualización completa de estado, fraudes o cierre (Momento 3)
        hacia la SFC utilizando el verbo PATCH/PUT de forma síncrona.
        """
        url = f"{self.base_url}{SfcEndpoints.QUEJA.value}{sfc_codigo_queja}/"
        logger.info(f"[SfcClient] Enviando actualización de estado M3 a: {url}")
        
        try:
            response = await self.client.patch(url, json=payload, auth=self.interceptor)
            
            if response.status_code not in (200, 201):
                logger.error(f"SFC rechazó la actualización del caso. Código: {response.status_code}. Respuesta: {response.text}")
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
                
            return response.json()
            
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")
    
    @handle_sfc_throttling
    async def fetch_usuarios_pagina(self, url: Optional[str] = None) -> Dict[str, Any]:
        """Obtiene una página de usuarios actualizados (Momento 4)."""
        target_url = url if url else f"{self.base_url}{SfcEndpoints.USUARIOS.value}"
        try:
            response = await self.client.get(target_url, auth=self.interceptor)
            if response.status_code not in (200, 201):
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")

    @handle_sfc_throttling
    async def send_user_ack_batch(self, numeros_id_cf: list) -> Dict[str, Any]:
        """Envía el lote de confirmación de recibido para usuarios (Momento 4 ACK)."""
        target_url = f"{self.base_url}{SfcEndpoints.USUARIOS_ACK.value}"
        payload = {"numero_id_CF": numeros_id_cf}
        try:
            response = await self.client.post(target_url, json=payload, auth=self.interceptor)
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