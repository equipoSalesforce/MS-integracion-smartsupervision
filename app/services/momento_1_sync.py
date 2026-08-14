import asyncio
import logging
import time
from typing import Dict, Any, List, Optional

from app.integrations.sfc_client import SfcClient
from app.services.s3_service import S3StorageService
from app.core.mapping import SfcSalesforceMapper
from app.core.config import settings
from app.services.email_service import EmailAlertService

logger = logging.getLogger(__name__)


class SincronizacionService:
    def __init__(self, sfc_client: SfcClient, s3_client=None):
        self.sfc_client = sfc_client
        self.s3_service = S3StorageService(
            s3_client=s3_client,
            http_client=getattr(sfc_client, "client", None)
        )

    async def ejecutar_flujo_completo_momento_1(self) -> List[Dict[str, Any]]:
        logger.info("[Momento 1] Descargando y procesando lote de quejas M1 de SFC...")
        quejas_finales_crm = []
        url_actual = None
        # 🟢 FIX P1-12: cota de páginas/tiempo para no seguir un enlace 'next' de la
        # SFC indefinidamente (ciclo de paginación, backlog anómalo, etc.).
        pagina_actual = 0
        inicio = time.monotonic()

        while True:
            pagina_actual += 1
            if pagina_actual > settings.SFC_SYNC_MAX_PAGINAS or (time.monotonic() - inicio) > settings.SFC_SYNC_MAX_SEGUNDOS:
                logger.critical(
                    f"🔥 [Momento 1] Ciclo de paginación cortado tras {pagina_actual - 1} página(s) "
                    f"({len(quejas_finales_crm)} quejas acumuladas): se alcanzó el límite de páginas/tiempo "
                    f"configurado. Posible enlace 'next' inválido o backlog anómalamente grande en la SFC."
                )
                await EmailAlertService.notificar_falla_infraestructura(
                    smart_code="SYNC_M1_PAGINACION",
                    error_msg=(
                        f"Ciclo de paginación M1 cortado tras {pagina_actual - 1} página(s) "
                        f"({len(quejas_finales_crm)} quejas acumuladas) por exceder el límite de "
                        f"páginas/tiempo configurado."
                    )
                )
                break

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
            resultados_chunk = await asyncio.gather(*tareas, return_exceptions=True)
            
            for res in resultados_chunk:
                if isinstance(res, Exception):
                    logger.error(
                        f"❌ [Momento 1] Error no controlado procesando queja individual dentro del lote: {res}",
                        exc_info=res
                    )
                elif res is not None:
                    resultados.append(res)

        return resultados

    async def _procesar_queja_individual(self, queja_sfc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        codigo_queja = queja_sfc.get("codigo_queja")
        tiene_anexos = queja_sfc.get("anexo_queja", False)
        adjuntos_procesados = []

        if tiene_anexos:
            try:
                archivos_sfc = await self.sfc_client.get_adjuntos_list(codigo_queja)
                response_data = archivos_sfc.get("Response") if "Response" in archivos_sfc else archivos_sfc
                lista_adjuntos = response_data.get("results", [])

                if lista_adjuntos:
                    adjuntos_procesados = await self.s3_service.transferir_lote_sfc_a_s3(
                        codigo_queja=codigo_queja,
                        adjuntos_sfc=lista_adjuntos
                    )

                    # 🟢 FIX HALLAZGO 16: Regla All-or-Nothing
                    # Si la SFC reportaba N adjuntos y no se pudieron transferir TODOS a S3,
                    # descartamos la queja del lote entregado al CRM.
                    if len(adjuntos_procesados) < len(lista_adjuntos):
                        logger.error(
                            f"❌ [Momento 1 All-or-Nothing] Se esperaban {len(lista_adjuntos)} adjuntos "
                            f"para la queja {codigo_queja}, pero solo se procesaron {len(adjuntos_procesados)} en S3. "
                            f"Omite la queja para forzar reintento completo en el siguiente ciclo."
                        )
                        return None
            except Exception as e:
                logger.error(
                    f"❌ [Momento 1 All-or-Nothing] Fallo procesando anexos M1 para {codigo_queja}: {e}. "
                    f"Omite la queja para evitar entregar información incompleta al CRM."
                )
                return None

        adjuntos_limpios = [
            {
                "nombre_archivo": adj.get("nombre_archivo"),
                "s3_key": adj.get("s3_key"),
                "bucket": adj.get("bucket")
            }
            for adj in adjuntos_procesados
            if isinstance(adj, dict)
        ]

        queja_traducida = SfcSalesforceMapper.sfc_payload_to_db_dict(queja_sfc)
        queja_traducida["archivos_s3"] = adjuntos_limpios
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