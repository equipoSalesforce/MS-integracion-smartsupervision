# app/integrations/sfc_client.py
import httpx
import logging
from typing import Dict, Any, Optional
from app.core.config import settings
from app.core.exceptions import SfcErrorTranslator
from app.core.auth import SfcAuthManager 

logger = logging.getLogger(__name__)


# 🛠️ Hook para registrar la Petición Saliente (Request)
async def log_request(request: httpx.Request):
    headers_formatted = "\n".join([f"  {k}: {v}" for k, v in request.headers.items()])

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type or "octet-stream" in content_type:
        body_str = "<[Contenido Binario / Multipart - Omitido por tamaño]>"
    else:
        try:
            body_str = request.content.decode("utf-8") if request.content else "<Vacio>"
        except Exception:
            body_str = f"<[Contenido No-UTF8: {len(request.content)} bytes]>"

    logger.info(
        f"\n==================== [HTTP OUTGOING REQUEST] ====================\n"
        f"Method  : {request.method}\n"
        f"URL     : {request.url}\n"
        f"Headers :\n{headers_formatted}\n"
        f"Body    :\n{body_str}\n"
        f"=================================================================="
    )


# 🛠️ Hook para registrar la Respuesta Entrante (Response)
async def log_response(response: httpx.Response):
    await response.aread()

    headers_formatted = "\n".join([f"  {k}: {v}" for k, v in response.headers.items()])

    logger.info(
        f"\n==================== [HTTP INCOMING RESPONSE] ====================\n"
        f"Status  : {response.status_code} {response.reason_phrase}\n"
        f"URL     : {response.url}\n"
        f"Headers :\n{headers_formatted}\n"
        f"Body    :\n{response.text}\n"
        f"=================================================================="
    )


class SfcClient:
    def __init__(self, interceptor: SfcAuthManager):
        self.base_url = settings.SFC_URL_BASE.rstrip('/')
        self.interceptor = interceptor
        self.client = httpx.AsyncClient(
            auth=interceptor,
            verify=True,
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
            
            # 🎯 FIX: Interrumpir inmediatamente e invocar al traductor si la SFC rechazó la petición
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
            
            # 🎯 FIX: Interrupción inmediata con await ante rechazo
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
            
            # 🎯 FIX: Interrupción inmediata con await ante rechazo
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