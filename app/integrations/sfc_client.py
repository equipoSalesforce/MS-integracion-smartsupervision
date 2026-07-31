# app/integrations/sfc_client.py
import ssl
import httpx
import json
import logging
from typing import Dict, Any, Optional, Union
from app.core.config import settings
from app.core.exceptions import SfcErrorTranslator
from app.core.auth import SfcAuthManager 
from app.core.constants import SfcEndpoints, SmartStatus
from app.core.security.sanitizer import sanitizar_headers, sanitizar_payload 

logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN DE SEGURIDAD SSL / TLS 1.2 ---
ssl_context = ssl.create_default_context()
ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
ssl_context.maximum_version = ssl.TLSVersion.TLSv1_2


# 🛠️ Hook Sanitizado para Registrar Peticiones Salientes (Request)
async def log_request(request: httpx.Request):
    # --- FILTRO DE LOGS DE ARCHIVOS ---
    is_file_request = (
        SfcEndpoints.STORAGE.value in str(request.url) 
        or "multipart/form-data" in request.headers.get("content-type", "")
    )
    enable_file_logs = getattr(settings, "ENABLE_FILE_LOGS", False)

    if is_file_request and not enable_file_logs:
        return  # Omitir log para binarios pesados
    # -----------------------------------

    headers_clean = sanitizar_headers(request.headers)
    headers_formatted = "\n".join([f"  {k}: {v}" for k, v in headers_clean.items()])

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type or "octet-stream" in content_type:
        body_str = "<[Contenido Binario / Multipart - Omitido por tamaño]>"
    else:
        try:
            if request.content:
                raw_json = json.loads(request.content.decode("utf-8"))
                clean_json = sanitizar_payload(raw_json)
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
        SfcEndpoints.STORAGE.value in str(response.url)
        or (response.request and "multipart/form-data" in response.request.headers.get("content-type", ""))
    )
    enable_file_logs = getattr(settings, "ENABLE_FILE_LOGS", False)

    if is_file_response and not enable_file_logs:
        return  # Omitir log para binarios pesados
    # -----------------------------------

    await response.aread()

    headers_clean = sanitizar_headers(response.headers)
    headers_formatted = "\n".join([f"  {k}: {v}" for k, v in headers_clean.items()])

    try:
        if response.text:
            raw_json = response.json()
            clean_json = sanitizar_payload(raw_json)
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
    def __init__(self, interceptor: SfcAuthManager, http_client: Optional[httpx.AsyncClient] = None):
        self.base_url = settings.SFC_URL_BASE.rstrip('/')
        self.interceptor = interceptor
        
        # 🎯 Reutiliza el cliente HTTP global si se provee; de lo contrario crea uno propio
        if http_client is not None:
            self.client = http_client
        else:
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
    
    async def post_nueva_queja(self, payload_mapeado: Dict[str, Any]) -> Dict[str, Any]:
        """Envía la información estructurada de una queja nueva a la SFC."""
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
    
    async def post_adjunto_queja(self, sfc_codigo_queja: str, file_bytes: bytes, file_type: str, file_name: Optional[str] = None) -> Dict[str, Any]:
        """Envía un archivo binario asociado a una queja hacia la SFC."""
        endpoint = SfcEndpoints.STORAGE.value
        url = f"{self.base_url}{endpoint}"
        
        if not file_name:
            file_name = f"soporte_{sfc_codigo_queja}.{file_type}"
            
        if len(file_name) > 150:
            file_name = file_name[-150:]
        
        token = await self.interceptor.get_valid_token()
        signature = self.interceptor.signature_context.get_signature(
            method="POST",
            url=endpoint,
            payload={"codigo_queja": sfc_codigo_queja, "type": file_type}
        )
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Cache-Control": "no-cache",
            "Accept": "*/*",
            "Accept-Language": "es",
            "X-SFC-Signature": signature
        }
        
        data = {"codigo_queja": sfc_codigo_queja, "type": file_type}
        files = {"file": (file_name, file_bytes, f"application/{file_type}")}

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
        """Envía la actualización completa de estado, fraudes o cierre (Momento 3)."""
        url = f"{self.base_url}{SfcEndpoints.QUEJA.value}{sfc_codigo_queja}/"
        try:
            response = await self.client.patch(url, json=payload, auth=self.interceptor)
            if response.status_code not in (200, 201):
                logger.error(f"SFC rechazó la actualización. Código: {response.status_code}. Respuesta: {response.text}")
                await SfcErrorTranslator.procesar_y_lanzar(response.status_code, response.text)
            return response.json()
        except httpx.HTTPStatusError as exc:
            await SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            await SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")

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
        """Cierra de forma segura el pool de conexiones."""
        await self.client.aclose()