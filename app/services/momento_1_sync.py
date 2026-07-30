import asyncio
import logging
from typing import Dict, Any, List, Optional

from app.integrations.sfc_client import SfcClient
from app.services.s3_service import S3StorageService
from app.core.mapping import SfcSalesforceMapper
from app.core.config import settings

logger = logging.getLogger(__name__)

class SincronizacionService:
    def __init__(self, sfc_client: SfcClient, s3_client=None):
        self.sfc_client = sfc_client
        self.s3_service = S3StorageService(s3_client=s3_client)

    async def ejecutar_flujo_completo_momento_1(self) -> List[Dict[str, Any]]:
        logger.info("[Momento 1] Descargando y procesando lote de quejas M1 de SFC...")
        quejas_finales_crm = []
        url_actual = None

        while True:
            respuesta = await self.sfc_client.fetch_quejas_pagina(url=url_actual)
            response_data = respuesta.get("Response") if "Response" in respuesta else respuesta
            lista_quejas = response_data.get("results", [])

            if not lista_quejas:
                break

            quejas_procesadas_pagina = await self._procesar_pagina_quejas(lista_quejas)
            quejas_finales_crm.extend(quejas_procesadas_pagina)

            url_actual = response_data.get("next")
            if not url_actual:
                break

        return quejas_finales_crm

    async def _procesar_pagina_quejas(self, raw_quejas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        TAMANO_CHUNK = 10
        chunks = [raw_quejas[i:i + TAMANO_CHUNK] for i in range(0, len(raw_quejas), TAMANO_CHUNK)]
        resultados = []
        for chunk in chunks:
            tareas = [self._procesar_queja_individual(queja) for queja in chunk]
            resultados_chunk = await asyncio.gather(*tareas)
            resultados.extend(resultados_chunk)
        return [q for q in resultados if q is not None]

    async def _procesar_queja_individual(self, queja_sfc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        codigo_queja = queja_sfc.get("codigo_queja")
        tiene_anexos = queja_sfc.get("anexo_queja", False)
        adjuntos_procesados = []

        if tiene_anexos:
            try:
                archivos_sfc = await self.sfc_client.get_adjuntos_list(codigo_queja)
                response_data = archivos_sfc.get("Response") if "Response" in archivos_sfc else archivos_sfc
                lista_adjuntos = response_data.get("results", [])

                # 🎯 DELEGACIÓN AL S3 STORAGE SERVICE
                adjuntos_procesados = await self.s3_service.transferir_lote_sfc_a_s3(
                    codigo_queja=codigo_queja,
                    adjuntos_sfc=lista_adjuntos
                )
            except Exception as e:
                logger.error(f"Error procesando anexos M1 para {codigo_queja}: {e}")

        queja_traducida = SfcSalesforceMapper.sfc_payload_to_db_dict(queja_sfc)
        queja_traducida["archivos_s3"] = adjuntos_procesados
        return queja_traducida

    async def confirmar_recepcion_ack(self, ids_quejas: List[str]) -> Dict[str, Any]:
        if not ids_quejas:
            return {"status": "warning", "confirmados": 0, "ids_procesados": [], "ids_error": []}

        TAMANO_LOTE = 100
        lotes = [ids_quejas[i:i + TAMANO_LOTE] for i in range(0, len(ids_quejas), TAMANO_LOTE)]
        ids_exitosos, ids_con_error = [], []

        for lote in lotes:
            try:
                respuesta_sfc = await self.sfc_client.send_ack_batch(lote)
                data_sfc = respuesta_sfc.get("Response") if isinstance(respuesta_sfc, dict) and "Response" in respuesta_sfc else respuesta_sfc
                raw_errors = data_sfc.get("pqrs_error", []) if isinstance(data_sfc, dict) else []
                set_errores = {str(err_id).strip() for err_id in raw_errors}

                for pqrs_id in lote:
                    str_id = str(pqrs_id).strip()
                    if str_id in set_errores:
                        ids_con_error.append(str_id)
                    else:
                        ids_exitosos.append(str_id)
            except Exception as e:
                logger.error(f"Error enviando lote ACK: {e}")
                ids_con_error.extend([str(x).strip() for x in lote])

        return {
            "status": "success" if not ids_con_error else "partial",
            "confirmados": len(ids_exitosos),
            "ids_procesados": ids_exitosos,
            "ids_error": ids_con_error
        }