import asyncio
import logging
from typing import Dict, Any, List, Optional

from app.integrations.sfc_client import SfcClient
from app.core.mapping import SfcSalesforceMapper
from app.core.config import settings

logger = logging.getLogger(__name__)


class UserSync:
    def __init__(self, sfc_client: SfcClient):
        self.sfc_client = sfc_client

    async def sincronizar_usuarios(self) -> List[Dict[str, Any]]:
        """
        Orquesta de forma síncrona en memoria la descarga y traducción de la 
        información actualizada de consumidores financieros (Momento 4).
        Retorna la lista final de usuarios mapeados SIN enviar el ACK a la SFC.
        """
        logger.info("[Momento 4] Iniciando sincronización de datos de usuarios desde la SFC.")

        usuarios_finales_crm = []
        url_actual = None

        # Consumo paginado de usuarios en la SFC
        while True:
            respuesta = await self.sfc_client.fetch_usuarios_pagina(url=url_actual)
            response_data = respuesta.get("Response") if "Response" in respuesta else respuesta

            # Manejo flexible de la respuesta (Lista vs Diccionario Paginado)
            if isinstance(response_data, dict):
                lista_usuarios = response_data.get("results", [])
                
                # 🎯 CORRECCIÓN: Se usa 'numero_id_CF' en lugar de 'usuario_id'
                if not lista_usuarios and "numero_id_CF" in response_data:
                    lista_usuarios = [response_data]
            elif isinstance(response_data, list):
                lista_usuarios = response_data
            else:
                lista_usuarios = []

            if not lista_usuarios:
                break

            # Mapeo en memoria de la página actual
            for usuario_sfc in lista_usuarios:
                try:
                    usuario_traducido = SfcSalesforceMapper.sfc_user_payload_to_db_dict(usuario_sfc)
                    if usuario_traducido:
                        usuarios_finales_crm.append(usuario_traducido)
                except Exception as err_map:
                    num_id = usuario_sfc.get("numero_id_CF", "DESCONOCIDO")
                    logger.error(f"❌ Error al mapear usuario {num_id}: {str(err_map)}")

            # Control de paginación (URL 'next')
            if isinstance(response_data, dict):
                url_actual = response_data.get("next")
            else:
                url_actual = None

            if not url_actual:
                break

        logger.info(
            f"[Momento 4] Sincronización completada. Total usuarios procesados para el CRM: {len(usuarios_finales_crm)}"
        )
        return usuarios_finales_crm

    async def confirmar_recepcion_ack_usuarios(self, numeros_id_cf: List[str]) -> Dict[str, Any]:
        """
        Recibe la lista de números de identificación (numero_id_CF) procesados exitosamente por el CRM
        y transmite la confirmación ACK en lotes de máximo 100 registros hacia la SFC.
        """
        if not numeros_id_cf:
            return {
                "status": "warning",
                "message": "No se proporcionaron números de identificación (numero_id_CF) para confirmar ACK.",
                "confirmados": 0
            }

        logger.info(f"[Momento 4 ACK] Iniciando confirmación ACK para {len(numeros_id_cf)} usuarios.")

        # Lotes de máximo 100 elementos
        TAMANO_LOTE = 100
        lotes = [numeros_id_cf[i:i + TAMANO_LOTE] for i in range(0, len(numeros_id_cf), TAMANO_LOTE)]

        total_confirmados = 0

        for index, lote in enumerate(lotes):
            logger.info(f"[Momento 4 ACK] Enviando lote {index + 1}/{len(lotes)} ({len(lote)} usuarios) a la SFC...")
            try:
                await self.sfc_client.send_user_ack_batch(lote)
                total_confirmados += len(lote)
            except Exception as e:
                logger.error(
                    f"❌ Error crítico al confirmar ACK de usuarios en el lote {index + 1}. "
                    f"Confirmados previamente: {total_confirmados}. Detalle: {str(e)}"
                )
                raise

        return {
            "status": "success",
            "message": f"ACK de usuarios confirmado exitosamente ante la SFC para {total_confirmados} de {len(numeros_id_cf)} registros.",
            "confirmados": total_confirmados,
            "ids_procesados": numeros_id_cf
        }