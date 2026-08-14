# app/integrations/sfc_client.py
import asyncio
import functools
import os
import ssl
from urllib.parse import urlparse
import httpx
import json
import logging
from typing import Dict, Any, Optional, Union
from app.core.config import settings
from app.core.exceptions import SfcErrorTranslator, SfcIntegrationException
from app.core.auth import SfcAuthManager 
from app.core.constants import SfcEndpoints, SmartStatus
from app.core.security.sanitizer import sanitizar_headers, sanitizar_payload, sanitizar_texto_plano
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
    content_type = response.headers.get("content-type", "").lower()
    url_str = str(response.url).lower()
    
    is_file_response = (
        SfcEndpoints.STORAGE.value in url_str
        or "storage.googleapis.com" in url_str
        or "application/pdf" in content_type
        or "application/octet-stream" in content_type
        or (response.request and "multipart/form-data" in response.request.headers.get("content-type", ""))
    )
    
    if is_file_response and not settings.ENABLE_FILE_LOGS:
        logger.info("AUDIT_HTTP_INCOMING_RESPONSE", extra={
            "extra_data": {
                "direction": "INCOMING_RESPONSE",
                "status_code": response.status_code,
                "reason_phrase": response.reason_phrase,
                "url": str(response.url),
                "headers": sanitizar_headers(dict(response.headers)),
                "body": f"[BINARY_FILE_CONTENT: {content_type}]"
            }
        })
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
        # 🟡 FIX P1-16: un body no-JSON (HTML de error, texto plano) no tiene campos
        # que enmascarar selectivamente y puede reflejar datos del request original —
        # se registra tamaño + vista previa acotada en vez del contenido completo.
        body_clean = sanitizar_texto_plano(response.text)

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
    🟢 FIX HALLAZGO 39: Clasificación estricta de Throttling / Rate Limiting (429)
    separada de fallas de infraestructura (5xx, Timeouts, DNS o errores de aplicación).
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        attempts = 0
        max_retries = settings.SFC_MINI_RETRY_ATTEMPTS
        delay = settings.SFC_MINI_RETRY_DELAY_SECONDS

        while True:
            try:
                return await func(*args, **kwargs)
            except SfcIntegrationException as exc:
                raw_msg_lower = str(getattr(exc, "raw_message", "") or "").lower()
                error_type_str = str(getattr(exc, "error_type", "") or "").upper()
                
                # 🟢 Solo respuestas de regulación de cuota o Rate Limit 429
                is_throttled = (
                    exc.status_code == 429 or 
                    error_type_str in ("THROTTLED_ERROR", "RATE_LIMIT_ERROR") or
                    "throttled" in raw_msg_lower or
                    "quota" in raw_msg_lower or
                    "resource_exhausted" in raw_msg_lower
                )

                if is_throttled and attempts < max_retries:
                    attempts += 1
                    logger.warning(
                        f"⏳ [SfcClient] Solicitud regulada / cuota superada por la SFC (HTTP 429 Throttled/Quota). "
                        f"Ejecutando mini-delay de {delay}s antes del reintento {attempts}/{max_retries}..."
                    )
                    await asyncio.sleep(delay)
                    continue
                
                # Errores de infraestructura (500, 502, 503, timeouts, DNS) o de negocio se elevan directamente
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

    def _sanitizar_y_validar_next_url(self, raw_url: Optional[str]) -> Optional[str]:
        if not raw_url or not str(raw_url).strip():
            return None

        url_str = str(raw_url).strip()
        parsed = urlparse(url_str)
        base_parsed = urlparse(self.base_url)

        if parsed.netloc:
            if parsed.netloc.lower() != base_parsed.netloc.lower():
                logger.error(
                    f"🚨 [SSRF Protection] Se detectó una URL 'next' con un host no autorizado: '{parsed.netloc}'. "
                    f"Host esperado: '{base_parsed.netloc}'."
                )
                raise SfcIntegrationException(
                    status_code=400,
                    error_type="SSRF_PROTECTION_ERROR",
                    sfc_field="url_next",
                    raw_message=f"La URL de paginación devuelta por el servidor ('{parsed.netloc}') no coincide con la URL base de la SFC.",
                    crm_action="Contacte al equipo de soporte de la SFC para reportar la inconsistencia en los enlaces de paginación."
                )

        path_and_query = parsed.path
        if parsed.query:
            path_and_query += f"?{parsed.query}"

        return path_and_query

    @handle_sfc_throttling
    async def fetch_quejas_pagina(self, url: Optional[str] = None) -> Dict[str, Any]:
        endpoint_relativo = self._sanitizar_y_validar_next_url(url) or SfcEndpoints.QUEJA.value
        target_url = f"{self.base_url}{endpoint_relativo}" if endpoint_relativo.startswith("/") else f"{self.base_url}/{endpoint_relativo}"

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
                logger.error(f"SFC rechazó la queja. Código: {response.status_code} (detalle sanitizado disponible en AUDIT_HTTP_INCOMING_RESPONSE)")
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

        if hasattr(file_data, "seek") and callable(file_data.seek):
            file_data.seek(0)

        files = {
            "file": (file_name, file_data, f"application/{file_type}")
        }

        logger.info(f"Transmitiendo archivo adjunto ({file_name}) para la queja SFC: {sfc_codigo_queja}")

        try:
            # 🟢 FIX HALLAZGO 19: Se usa el mismo `auth=self.interceptor` que el resto de
            # llamadas SFC en vez de fabricar token/firma manualmente, para heredar el
            # refresh-and-retry automático ante 401. Los campos a firmar (codigo_queja/type)
            # se pasan vía `extensions`, ya que SfcAuthManager no puede releer un body
            # multipart ya construido.
            response = await self.client.post(
                url,
                data=data,
                files=files,
                auth=self.interceptor,
                extensions={"sfc_signature_fields": data}
            )

            if response.status_code not in (200, 201):
                logger.error(f"SFC rechazó la carga del archivo. Código: {response.status_code} (detalle sanitizado disponible en AUDIT_HTTP_INCOMING_RESPONSE)")
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
                logger.error(f"SFC rechazó la actualización del caso. Código: {response.status_code} (detalle sanitizado disponible en AUDIT_HTTP_INCOMING_RESPONSE)")
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")
    
    @handle_sfc_throttling
    async def fetch_usuarios_pagina(self, url: Optional[str] = None) -> Dict[str, Any]:
        endpoint_relativo = self._sanitizar_y_validar_next_url(url) or SfcEndpoints.USUARIOS.value
        target_url = f"{self.base_url}{endpoint_relativo}" if endpoint_relativo.startswith("/") else f"{self.base_url}/{endpoint_relativo}"

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