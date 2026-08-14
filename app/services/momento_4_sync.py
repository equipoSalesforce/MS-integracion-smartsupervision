# app/services/momento_4_sync.py
import asyncio
import logging
import time
from typing import Dict, Any, List, Optional

from app.integrations.sfc_client import SfcClient
from app.core.mapping import SfcSalesforceMapper
from app.core.config import settings
from app.services.email_service import EmailAlertService

logger = logging.getLogger(__name__)


class UserSync:
    def __init__(self, sfc_client: SfcClient):
        self.sfc_client = sfc_client

    async def sincronizar_usuarios(self) -> Dict[str, Any]:
        """
        Orquesta de forma síncrona en memoria la descarga y traducción de la 
        información actualizada de consumidores financieros (Momento 4).
        Deduplica usuarios por numero_id_CF y reporta errores parciales en failed_items.
        """
        logger.info("[Momento 4] Iniciando sincronización de datos de usuarios desde la SFC.")

        usuarios_finales_crm: List[Dict[str, Any]] = []
        failed_items: List[Dict[str, Any]] = []
        vistos_ids = set()
        total_procesados = 0
        url_actual = None
        # 🟢 FIX P1-12: cota de páginas/tiempo, mismo riesgo que en Momento 1 (enlace
        # 'next' de la SFC sin fin).
        pagina_actual = 0
        inicio = time.monotonic()
        paginacion_incompleta = False

        while True:
            pagina_actual += 1
            if pagina_actual > settings.SFC_SYNC_MAX_PAGINAS or (time.monotonic() - inicio) > settings.SFC_SYNC_MAX_SEGUNDOS:
                logger.critical(
                    f"🔥 [Momento 4] Ciclo de paginación cortado tras {pagina_actual - 1} página(s) "
                    f"({len(usuarios_finales_crm)} usuarios acumulados): se alcanzó el límite de "
                    f"páginas/tiempo configurado. Posible enlace 'next' inválido o backlog anómalo en la SFC."
                )
                await EmailAlertService.notificar_falla_infraestructura(
                    smart_code="SYNC_M4_PAGINACION",
                    error_msg=(
                        f"Ciclo de paginación M4 cortado tras {pagina_actual - 1} página(s) "
                        f"({len(usuarios_finales_crm)} usuarios acumulados) por exceder el límite de "
                        f"páginas/tiempo configurado."
                    )
                )
                paginacion_incompleta = True
                break

            respuesta = await self.sfc_client.fetch_usuarios_pagina(url=url_actual)
            response_data = respuesta.get("Response") if "Response" in respuesta else respuesta

            if isinstance(response_data, dict):
                lista_usuarios = response_data.get("results", [])
                if not lista_usuarios and "numero_id_CF" in response_data:
                    lista_usuarios = [response_data]
            elif isinstance(response_data, list):
                lista_usuarios = response_data
            else:
                lista_usuarios = []

            if not lista_usuarios:
                break

            for usuario_sfc in lista_usuarios:
                total_procesados += 1
                num_id = "DESCONOCIDO"
                if isinstance(usuario_sfc, dict):
                    num_id = str(usuario_sfc.get("numero_id_CF") or usuario_sfc.get("numero_id") or "DESCONOCIDO").strip()

                try:
                    if not isinstance(usuario_sfc, dict):
                        raise ValueError("El registro de usuario recibido de la SFC no es un diccionario válido.")

                    # 🟢 FIX HALLAZGO 50: Deduplicación explícita de usuarios en lote por numero_id_CF
                    if num_id != "DESCONOCIDO" and num_id in vistos_ids:
                        logger.info(f"ℹ️ [Momento 4] Registro duplicado del usuario '{num_id}' omitido.")
                        continue

                    usuario_traducido = SfcSalesforceMapper.sfc_user_payload_to_db_dict(usuario_sfc)
                    if not usuario_traducido or "id_number__c" not in usuario_traducido:
                        raise ValueError(f"Fallo en el mapeo de campos o 'id_number__c' ausente para el usuario {num_id}.")

                    if num_id != "DESCONOCIDO":
                        vistos_ids.add(num_id)

                    usuarios_finales_crm.append(usuario_traducido)

                except Exception as err_map:
                    err_msg = str(err_map)
                    logger.error(f"❌ [Momento 4] Error mapeando usuario '{num_id}': {err_msg}")
                    failed_items.append({
                        "numero_id_CF": num_id,
                        "raw_payload": usuario_sfc,
                        "error": err_msg
                    })

            if isinstance(response_data, dict):
                url_actual = response_data.get("next")
            else:
                url_actual = None

            if not url_actual:
                break

        status = "success" if not failed_items else ("partial" if usuarios_finales_crm else "error")
        if paginacion_incompleta:
            status = "partial"

        logger.info(
            f"📊 [Momento 4] Sincronización finalizada. Status: {status} | "
            f"Procesados: {total_procesados} | Únicos Exitosos: {len(usuarios_finales_crm)} | Fallidos: {len(failed_items)}"
        )

        return {
            "status": status,
            "total_procesados": total_procesados,
            "total_exitosos": len(usuarios_finales_crm),
            "total_fallidos": len(failed_items),
            "usuarios": usuarios_finales_crm,
            "failed_items": failed_items,
            # 🟢 FIX P1-12: indica que se cortó el ciclo antes de agotar la paginación
            # de la SFC (límite de páginas/tiempo alcanzado), para que el CRM sepa que
            # la lista de usuarios puede no representar el backlog completo.
            "paginacion_incompleta": paginacion_incompleta
        }

    async def confirmar_recepcion_ack_usuarios(self, numeros_id_cf: List[str]) -> Dict[str, Any]:
        """
        Recibe la lista de números de identificación (numero_id_CF) procesados por el CRM,
        los deduplica preservando el orden original y transmite la confirmación ACK en lotes
        de máximo 100 registros hacia la SFC.
        """
        if not numeros_id_cf:
            return {
                "status": "warning",
                "message": "No se proporcionaron números de identificación (numero_id_CF) para confirmar ACK.",
                "confirmados": 0,
                "ids_procesados": [],
                "ids_error": []
            }

        # 🟢 FIX HALLAZGO 50: Deduplicación limpia preservando orden e ignorando cadenas vacías
        ids_unicos = list(dict.fromkeys(str(x).strip() for x in numeros_id_cf if str(x).strip()))

        if len(ids_unicos) < len(numeros_id_cf):
            logger.info(
                f"🧹 [Momento 4 ACK] Se deduplicaron {len(numeros_id_cf) - len(ids_unicos)} IDs repetidos/vacíos "
                f"en la solicitud. Procesando {len(ids_unicos)} elementos únicos."
            )

        if not ids_unicos:
            return {
                "status": "warning",
                "message": "Todos los números de identificación proporcionados estaban vacíos tras la limpieza.",
                "confirmados": 0,
                "ids_procesados": [],
                "ids_error": []
            }

        logger.info(f"[Momento 4 ACK] Iniciando confirmación ACK para {len(ids_unicos)} usuarios únicos.")

        TAMANO_LOTE = 100
        lotes = [ids_unicos[i:i + TAMANO_LOTE] for i in range(0, len(ids_unicos), TAMANO_LOTE)]
        
        ids_exitosos: List[str] = []
        ids_con_error: List[str] = []

        for index, lote in enumerate(lotes):
            logger.info(f"[Momento 4 ACK] Enviando lote {index + 1}/{len(lotes)} ({len(lote)} usuarios) a la SFC...")
            try:
                respuesta_sfc = await self.sfc_client.send_user_ack_batch(lote)
                data_sfc = respuesta_sfc.get("Response") if isinstance(respuesta_sfc, dict) and "Response" in respuesta_sfc else respuesta_sfc
                
                raw_errors = (
                    data_sfc.get("numero_id_CF_error", data_sfc.get("pqrs_error", [])) 
                    if isinstance(data_sfc, dict) else []
                )
                set_errores = {str(err_id).strip() for err_id in raw_errors}

                for user_id in lote:
                    str_id = str(user_id).strip()
                    if str_id in set_errores:
                        ids_con_error.append(str_id)
                    else:
                        ids_exitosos.append(str_id)

            except Exception as e:
                logger.error(f"❌ [Momento 4 ACK] Error de comunicación en lote {index + 1}: {e}")
                ids_con_error.extend([str(x).strip() for x in lote])

        status = "success" if not ids_con_error else ("partial" if ids_exitosos else "error")

        return {
            "status": status,
            "message": f"ACK de usuarios procesado ante la SFC: {len(ids_exitosos)} exitosos, {len(ids_con_error)} con error.",
            "confirmados": len(ids_exitosos),
            "ids_procesados": ids_exitosos,
            "ids_error": ids_con_error
        }