import httpx
import logging
from typing import Dict, Any, Optional
from app.core.config import settings
from app.integrations.sfc_interceptor import SfcRequestInterceptor
from app.core.auth import SfcAuthManager

logger = logging.getLogger(__name__)


class SfcClient:
    def __init__(self, interceptor: SfcRequestInterceptor):
        self.client = httpx.AsyncClient(auth=interceptor, verify=True)

    async def fetch_quejas_pagina(self, url: Optional[str] = None) -> Dict[str, Any]:
        """Obtiene una página de quejas."""
        target_url = url if url else f"{settings.SFC_API_BASE_URL.rstrip('/')}/api/queja/"
        response = await self.client.get(target_url)
        response.raise_for_status()
        return response.json()

    async def get_adjuntos_list(self, codigo_queja: str) -> Dict[str, Any]:
        """Obtiene el listado de archivos adjuntos asociados a una queja."""
        target_url = f"{settings.SFC_API_BASE_URL.rstrip('/')}/api/storage/?codigo_queja__codigo_queja={codigo_queja}"
        response = await self.client.get(target_url)
        response.raise_for_status()
        return response.json()

    async def send_ack_batch(self, pqrs_ids: list) -> Dict[str, Any]:
        """Envía el lote de confirmación de recibidos (ACK)."""
        target_url = f"{settings.SFC_API_BASE_URL.rstrip('/')}/api/complaint/ack"
        payload = {"pqrs": pqrs_ids}
        response = await self.client.post(target_url, json=payload)
        response.raise_for_status()
        return response.json()
    
    async def post_nueva_queja(self, payload_mapeado: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envía la información estructurada de una queja nueva a la SFC.
        Espera un código de retorno '201 Created' de éxito.
        """
        endpoint = "/api/queja/"
        
        # Cumplimos el estándar de envoltura estipulado por la SFC
        wrapped_payload = {
            "Body": payload_mapeado
        }

        logger.info(f"Enviando POST de datos de queja regulatoria: {payload_mapeado.get('codigo_queja')}")
        
        response = await self.client.post(endpoint, json=wrapped_payload)
        
        # Si la SFC devuelve un error de validación, capturamos el cuerpo para diagnóstico
        if response.status_code != 201:
            logger.error(f"SFC rechazó la queja. Código: {response.status_code}. Respuesta: {response.text}")
            
        response.raise_for_status()
        return response.json()
    
    async def post_adjunto_queja(self, sfc_codigo_queja: str, file_bytes: bytes, file_type: str) -> Dict[str, Any]:
        """
        Envía un archivo binario asociado a una queja hacia la SFC utilizando multipart/form-data[cite: 4].
        El SfcAuthManager interceptará esta petición para firmar omitiendo los bytes del archivo[cite: 4].
        """
        endpoint = "/api/storage/"
        
        # Nomenclatura del archivo requerida (Máximo 150 caracteres)[cite: 4]
        file_name = f"soporte_{sfc_codigo_queja}.{file_type}"
        
        # Estructuramos los datos para la transmisión multipart
        # Nota: SfcAuthManager leerá 'codigo_queja' y 'type' de aquí para calcular la firma criptográfica[cite: 4]
        data = {
            "codigo_queja": sfc_codigo_queja,
            "type": file_type
        }
        
        files = {
            "file": (file_name, file_bytes, f"application/{file_type}")
        }

        logger.info(f"Transmitiendo archivo adjunto ({file_type}) para la queja SFC: {sfc_codigo_queja}")
        
        response = await self.client.post(endpoint, data=data, files=files)
        
        if response.status_code not in (200, 201):
            logger.error(f"SFC rechazó la carga del archivo. Código: {response.status_code}. Respuesta: {response.text}")
            
        response.raise_for_status()
        return response.json()

    async def close(self):
        """Cierra de forma segura el pool de conexiones del cliente HTTPX."""
        await self.client.aclose()