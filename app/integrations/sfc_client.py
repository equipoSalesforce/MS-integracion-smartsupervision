# app/integrations/sfc_client.py
import asyncio
import functools
import os
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

ssl_context = ssl.create_default_context()
ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
ssl_context.maximum_version = ssl.TLSVersion.TLSv1_2


async def log_request(request: httpx.Request):
    """Hook para registrar peticiones HTTP salientes hacia la SFC en formato JSON estructurado."""
    cid = get_correlation_id()
    aws_trace = get_aws_trace_id()

    if cid and cid != "N/A":
        request.headers["X-Correlation-ID"] = cid
    if aws_trace and aws_trace != "N/A":
        request.headers["X-Amzn-Trace-Id"] = aws_trace

    # 🟢 FIX: Si la petición involucra archivos/multipart, retornar INMEDIATAMENTE
    # para evitar tocar request.content sobre peticiones en streaming
    is_file_request = (
        SfcEndpoints.STORAGE.value in str(request.url) 
        or "multipart/form-data" in request.headers.get("content-type", "")
    )
    if is_file_request:
        return

    headers_clean = sanitizar_headers(dict(request.headers))

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
        self._owns_client = False
        
        if http_client is not None:
            self.client = http_client
        else:
            self._owns_client = True
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
        url = f"{self.base_url}{SfcEndpoints.QUEJA.value}"
        logger.info(f"[SfcClient] Enviando metadatos de queja a: {url}")
        
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
        endpoint = SfcEndpoints.STORAGE.value
        url = f"{self.base_url}{endpoint}"
        
        if not file_name:
            file_name = f"soporte_{sfc_codigo_queja}.{file_type}"
            
        if len(file_name) > 150:
            stem, ext = os.path.splitext(file_name)
            file_name = f"{stem[:150 - len(ext)]}{ext}"

        data = {
            "codigo_queja": sfc_codigo_queja,
            "type": file_type
        }

        # 1. Obtener token válido y calcular la firma específica de transferencia de archivos
        token = await self.interceptor.get_valid_token()
        signature = self.interceptor.signature_context.get_signature(
            method="POST",
            url=endpoint,
            payload=data,
            is_file_upload=True  # 🟢 FIX: Garantiza el uso de FileTransferSignatureStrategy
        )
        
        # 2. Construir cabeceras HTTP con la firma calculada
        headers = {
            "Authorization": f"Bearer {token}",
            "Cache-Control": "no-cache",
            "Accept": "*/*",
            "Accept-Language": "es",
            "X-SFC-Signature": signature
        }

        if hasattr(file_data, "seek") and callable(file_data.seek):
            file_data.seek(0)

        files = {
            "file": (file_name, file_data, f"application/{file_type}")
        }

        logger.info(f"Transmitiendo archivo adjunto ({file_name}) para la queja SFC: {sfc_codigo_queja}")
        
        try:
            # 🟢 FIX: Pasar headers=headers explícitamente en la solicitud POST
            response = await self.client.post(url, data=data, files=files, headers=headers)
            
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
        """Cierra el cliente HTTPX solo si fue creado localmente por esta instancia."""
        if self._owns_client and self.client and not self.client.is_closed:
            await self.client.aclose()