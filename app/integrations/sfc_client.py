import httpx
from typing import Dict, Any, Optional
from app.core.config import settings
from app.integrations.sfc_interceptor import SfcRequestInterceptor

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