# app/services/momento_1_sync.py
import asyncio
import httpx
import logging
from typing import Dict, Any, List, Optional

from app.integrations.sfc_client import SfcClient
from app.core.mapping import SfcSalesforceMapper
from app.core.config import settings

logger = logging.getLogger(__name__)

class SincronizacionService:
    def __init__(self, sfc_client: SfcClient, s3_client=None):
        self.sfc_client = sfc_client
        self.s3_client = s3_client

    async def ejecutar_flujo_completo_momento_1(self) -> List[Dict[str, Any]]:
        """
        Orquesta de forma secuencial y síncrona en memoria la descarga y subida a S3.
        Retorna la lista final de quejas mapeadas SIN enviar el ACK a la SFC.
        """
        logger.info("[Momento 1] Iniciando descarga, almacenamiento en S3 y mapeo en memoria (ACK diferido).")
        
        quejas_finales_crm = []
        url_actual = None

        # Consumo de páginas de quejas de la SFC
        while True:
            respuesta = await self.sfc_client.fetch_quejas_pagina(url=url_actual)
            response_data = respuesta.get("Response") if "Response" in respuesta else respuesta
            lista_quejas = response_data.get("results", [])

            if not lista_quejas:
                break

            # Procesamos la página actual de quejas (Descarga de archivos, S3 y traducción)
            quejas_procesadas_pagina = await self._procesar_pagina_quejas(lista_quejas)
            quejas_finales_crm.extend(quejas_procesadas_pagina)

            # Control de paginación de la SFC
            url_actual = response_data.get("next")
            if not url_actual:
                break

        logger.info(f"[Momento 1] Sincronización completada. Total quejas procesadas para el CRM: {len(quejas_finales_crm)}")
        return quejas_finales_crm

    async def _procesar_pagina_quejas(self, raw_quejas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Procesa de forma concurrente un lote de quejas de la SFC.
        Ya NO envía el ACK automáticamente.
        """
        tareas = []
        for queja_sfc in raw_quejas:
            tareas.append(self._procesar_queja_individual(queja_sfc))

        # Disparamos el procesamiento de todas las quejas del lote en paralelo
        resultados = await asyncio.gather(*tareas)

        # Filtramos únicamente las quejas que se procesaron con éxito (no retornaron None)
        return [q for q in resultados if q is not None]

    async def confirmar_recepcion_ack(self, ids_quejas: List[str]) -> Dict[str, Any]:
        """
        Recibe la lista de IDs de quejas confirmadas por el CRM local
        y transmite la confirmación (ACK Batch) de manera manual a la SFC.
        """
        if not ids_quejas:
            return {
                "status": "warning",
                "message": "No se proporcionaron IDs para confirmar ACK.",
                "confirmados": 0
            }

        logger.info(f"[Momento 1 ACK] Enviando confirmación ACK Batch para {len(ids_quejas)} quejas a la SFC.")
        await self.sfc_client.send_ack_batch(ids_quejas)

        return {
            "status": "success",
            "message": f"ACK confirmado exitosamente ante la SFC para {len(ids_quejas)} quejas.",
            "confirmados": len(ids_quejas),
            "ids_procesados": ids_quejas
        }

    async def _procesar_queja_individual(self, queja_sfc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Descarga los adjuntos de la queja de SFC, los sube a S3 y 
        traduce la queja a la nomenclatura esperada por el CRM local.
        """
        codigo_queja = queja_sfc.get("codigo_queja")
        tiene_anexos = queja_sfc.get("anexo_queja", False)
        
        if tiene_anexos:
            logger.info(f"La queja {codigo_queja} tiene anexos")
        
        adjuntos_procesados = []

        if tiene_anexos:
            try:
                # 1. Obtenemos los enlaces de descarga temporales de la SFC
                archivos_sfc = await self.sfc_client.get_adjuntos_list(codigo_queja)
                response_data = archivos_sfc.get("Response") if "Response" in archivos_sfc else archivos_sfc
                lista_adjuntos = response_data.get("results", [])

                # 2. Descargamos y subimos a S3 de forma asíncrona concurrente
                tareas_descarga = []
                for adjunto in lista_adjuntos:
                    file_url = adjunto.get("file")
                    file_id = adjunto.get("id")
                    file_type = adjunto.get("type")
                    
                    s3_key = f"quejas/{codigo_queja}/{file_id}_{file_type}"
                    tareas_descarga.append(self._descargar_y_subir_a_s3(file_url, s3_key, file_id, file_type))

                if tareas_descarga:
                    resultados_s3 = await asyncio.gather(*tareas_descarga)
                    # Filtramos únicamente las cargas exitosas
                    adjuntos_procesados = [a for a in resultados_s3 if a is not None]

            except Exception as e:
                if settings.ENVIRONMENT != "local":
                    logger.error(f"Fallo al procesar adjuntos para la queja {codigo_queja}. Se omitirá este ciclo: {str(e)}")
                    return None

        # 3. Traducimos el payload completo usando el Mapper Universal
        queja_traducida = SfcSalesforceMapper.sfc_payload_to_db_dict(queja_sfc)

        # Enriquecemos la queja mapeada agregando el listado de archivos que quedaron guardados en S3
        queja_traducida["archivos_s3"] = adjuntos_procesados
            
        if settings.ENVIRONMENT == "local" and not adjuntos_procesados and tiene_anexos:
            bucket_local = getattr(settings, "AWS_S3_BUCKET", None) or "global66-sfc-bucket-local"
        
            queja_traducida["archivos_s3"] = [
                {
                    "nombre_archivo": f"ANEXO_MOCK_{codigo_queja}.pdf",
                    "s3_key": f"local/quejas/{codigo_queja}/ANEXO_MOCK_{codigo_queja}.pdf",
                    "bucket": bucket_local
                }
            ] if tiene_anexos else []
            
        return queja_traducida

    async def _descargar_y_subir_a_s3(self, url: str, s3_key: str, file_id: Any, file_type: str) -> Optional[Dict[str, Any]]:
        """
        Descarga un archivo temporal de la SFC y lo almacena de forma remota en S3.
        Retorna la metadata de ubicación de S3.
        """
        logger.info(f"El environment actual es: {settings.ENVIRONMENT}")
        
        if settings.ENVIRONMENT == "local":
            
            return {
                "nombre_archivo": "archivo_ejemplo.pdf",
                "s3_key": "local/quejas/mock/archivo_ejemplo.pdf",
                "bucket": getattr(settings, "AWS_S3_BUCKET", "global66-sfc-bucket-local") or "global66-sfc-bucket-local"
            }
        
        try:
            async with httpx.AsyncClient() as clean_client:
                response = await clean_client.get(url, timeout=15.0)
                response.raise_for_status()
                file_bytes = response.content

            if self.s3_client:
                logger.info(f"Subiendo a S3 -> Bucket: {settings.AWS_S3_BUCKET} | Key: {s3_key}")
                await asyncio.to_thread(
                    self.s3_client.put_object,
                    Bucket=settings.AWS_S3_BUCKET,
                    Key=s3_key,
                    Body=file_bytes
                )
            else:
                logger.warning(f"[LOCAL DEV] Subida a S3 simulada para: {s3_key}")

            filename = f"{file_id}.{file_type}"
            return {
                "nombre_archivo": filename,
                "s3_key": s3_key,
                "bucket": settings.AWS_S3_BUCKET
            }
        except Exception as e:
            logger.error(f"Error al descargar/subir archivo {s3_key}: {str(e)}")
            return None