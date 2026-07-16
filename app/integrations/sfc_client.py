# app/integrations/sfc_client.py
import httpx
import logging
from typing import Dict, Any, Optional
from app.core.config import settings
from app.core.exceptions import SfcErrorTranslator
# 💡 Importamos tu clase real para el tipado correcto
from app.core.auth import SfcAuthManager 

logger = logging.getLogger(__name__)


class SfcClient:
    def __init__(self, interceptor: SfcAuthManager):
        self.base_url = settings.SFC_URL_BASE.rstrip('/')
        self.client = httpx.AsyncClient(auth=interceptor, verify=True)
        self.interceptor = interceptor  # 👈 Apuesta directa a tu SfcAuthManager

    async def fetch_quejas_pagina(self, url: Optional[str] = None) -> Dict[str, Any]:
        """Obtiene una página de quejas."""
        target_url = url if url else f"{settings.SFC_URL_BASE.rstrip('/')}/api/queja/"
        try:
            response = await self.client.get(target_url)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")

    async def get_adjuntos_list(self, codigo_queja: str) -> Dict[str, Any]:
        """Obtiene el listado de archivos adjuntos asociados a una queja."""
        target_url = f"{settings.SFC_URL_BASE.rstrip('/')}/api/storage/?codigo_queja__codigo_queja={codigo_queja}"
        try:
            response = await self.client.get(target_url)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")

    async def send_ack_batch(self, pqrs_ids: list) -> Dict[str, Any]:
        """Envía el lote de confirmación de recibidos (ACK)."""
        target_url = f"{settings.SFC_URL_BASE.rstrip('/')}/api/complaint/ack"
        payload = {"pqrs": pqrs_ids}
        try:
            response = await self.client.post(target_url, json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")
    
    async def post_nueva_queja(self, payload_mapeado: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envía la información estructurada de una queja nueva a la SFC.
        """
        url = f"{self.base_url}/api/queja/"
        logger.info(f"[SfcClient] Enviando metadatos de queja a: {url}")
                
        wrapped_payload = {
            "Body": payload_mapeado
        }
        
        logger.info(f"Enviando POST de datos de queja regulatoria: {payload_mapeado.get('codigo_queja')}")
        
        try:
            response = await self.client.post(url, json=wrapped_payload)
            
            if response.status_code != 201:
                logger.error(f"SFC rechazó la queja. Código: {response.status_code}. Respuesta: {response.text}")
                
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")
    
    async def post_adjunto_queja(self, sfc_codigo_queja: str, file_bytes: bytes, file_type: str, file_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Envía un archivo binario asociado a una queja hacia la SFC utilizando multipart/form-data.
        Bypassea el interceptor automático usando auth=None para mitigar errores de streaming.
        """
        url = f"{self.base_url}/api/storage/"
        logger.info(f"[SfcClient] Enviando metadatos de adjuntos a: {url}")
        
        #TODO: confirmar más tarde nomenclatura para archivos (fuera de reglas para fraude y finalizacion)
        if not file_name:
            file_name = f"soporte_{sfc_codigo_queja}.{file_type}"
            
        if len(file_name) > 150:
            file_name = file_name[-150:]
        
        # 🛠️ CORRECCIÓN: Llamamos a los métodos directamente sobre self.interceptor[cite: 2]
        token = await self.interceptor.get_valid_token()
        signature = self.interceptor.signature_context.get_signature(
            method="POST",
            url="/api/storage/",  # Buscamos firmar el path canónico relativo exigido por la SFC[cite: 2]
            payload={
                "codigo_queja": sfc_codigo_queja,
                "type": file_type
            }
        )
        
        # Inyectamos cabeceras requeridas según la proforma canónica para adjuntos[cite: 2]
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
            # Despachamos omitiendo el interceptor con auth=None para permitir el stream nativo de HTTPX
            response = await self.client.post(url, data=data, files=files, headers=headers, auth=None)
            
            if response.status_code not in (200, 201):
                logger.error(f"SFC rechazó la carga del archivo. Código: {response.status_code}. Respuesta: {response.text}")
                
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")

    async def put_actualizar_queja(self, sfc_codigo_queja: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envía la actualización completa de estado, fraudes o cierre (Momento 3)
        hacia la SFC utilizando el verbo PUT de forma síncrona.
        """
        url = f"{self.base_url}/api/queja/{sfc_codigo_queja}/"
        logger.info(f"[SfcClient] Enviando actualización de estado M3 a: {url}")
        
        # Envolvemos el payload mapeado en la raíz canónica exigida por la proforma
        wrapped_payload = {
            "Body": payload
        }
        
        try:
            # Al ser un JSON estándar, permitimos que el interceptor (SfcAuthManager)
            # calcule y estampe el header X-SFC-Signature de forma transparente[cite: 2].
            response = await self.client.put(url, json=wrapped_payload)
            
            if response.status_code != 200:
                logger.error(
                    f"SFC rechazó la actualización del caso. "
                    f"Código: {response.status_code}. Respuesta: {response.text}[cite: 2]"
                )
                
            response.raise_for_status()
            return response.json()
            
        except httpx.HTTPStatusError as exc:
            SfcErrorTranslator.procesar_y_lanzar(exc.response.status_code, exc.response.text)
        except httpx.RequestError:
            SfcErrorTranslator.procesar_y_lanzar(503, "upstream request timeout")
    
    
    async def close(self):
        """Cierra de forma segura el pool de conexiones del cliente HTTPX."""
        await self.client.aclose()