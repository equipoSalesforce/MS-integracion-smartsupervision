# app/services/momento_1_sync.py
import asyncio
import httpx
import logging
from typing import Dict, Any, List, Optional, Tuple

from app.integrations.sfc_client import SfcClient
from app.core.mapping import SfcSalesforceMapper
from app.core.config import settings

logger = logging.getLogger(__name__)

class SincronizacionService:
    def __init__(self, sfc_client: SfcClient, s3_client=None):
        self.sfc_client = sfc_client
        self.s3_client = s3_client

    @staticmethod
    def _normalizar_tipo_archivo(raw_type: str, file_url: str = "") -> Tuple[str, str]:
        """
        Normaliza la cadena 'type' de la SFC (ej: 'application/pdf', 'pdf', 'image/png')
        retornando una tupla con (extensión_limpia, ContentType_oficial).
        """
        val = str(raw_type or "").lower().strip()
        
        mime_map = {
            "application/pdf": ("pdf", "application/pdf"),
            "pdf": ("pdf", "application/pdf"),
            "image/png": ("png", "image/png"),
            "png": ("png", "image/png"),
            "image/jpeg": ("jpg", "image/jpeg"),
            "image/jpg": ("jpg", "image/jpeg"),
            "jpg": ("jpg", "image/jpeg"),
            "jpeg": ("jpg", "image/jpeg"),
            "text/plain": ("txt", "text/plain"),
            "txt": ("txt", "text/plain"),
            "application/msword": ("doc", "application/msword"),
            "doc": ("doc", "application/msword"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "application/zip": ("zip", "application/zip"),
            "zip": ("zip", "application/zip")
        }

        if val in mime_map:
            return mime_map[val]

        if "/" in val:
            ext = val.split("/")[-1].replace("vnd.", "").replace("x-", "")
            return ext, val

        if file_url and "." in file_url.split("/")[-1]:
            possible_ext = file_url.split("/")[-1].split(".")[-1].lower()
            if len(possible_ext) <= 4:
                return possible_ext, f"application/{possible_ext}"

        if val:
            return val, f"application/{val}"

        return "pdf", "application/pdf"

    async def ejecutar_flujo_completo_momento_1(self) -> List[Dict[str, Any]]:
        """
        Orquesta de forma secuencial y síncrona en memoria la descarga y subida a S3.
        Retorna la lista final de quejas mapeadas SIN enviar el ACK a la SFC.
        """
        logger.info("[Momento 1] Iniciando descarga, almacenamiento en S3 y mapeo en memoria (ACK diferido).")
        
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

        logger.info(f"[Momento 1] Sincronización completada. Total quejas procesadas para el CRM: {len(quejas_finales_crm)}")
        return quejas_finales_crm

    async def _procesar_pagina_quejas(self, raw_quejas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Procesa de forma concurrente un lote de quejas de la SFC.
        """
        resultados = []
        TAMANO_CHUNK = 10
        chunks = [raw_quejas[i:i + TAMANO_CHUNK] for i in range(0, len(raw_quejas), TAMANO_CHUNK)]
        
        for chunk in chunks:
            tareas = [self._procesar_queja_individual(queja) for queja in chunk]
            resultados_chunk = await asyncio.gather(*tareas)
            resultados.extend(resultados_chunk)

        return [q for q in resultados if q is not None]

    async def confirmar_recepcion_ack(self, ids_quejas: List[str]) -> Dict[str, Any]:
        """
        Recibe la lista de IDs de quejas confirmadas por el CRM local
        y transmite la confirmación (ACK Batch) a la SFC dividiendo en lotes <= 100.
        Discrimina los IDs exitosos de los que retornaron 'pqrs_error'.
        """
        if not ids_quejas:
            return {
                "status": "warning",
                "message": "No se proporcionaron IDs para confirmar ACK.",
                "confirmados": 0,
                "ids_procesados": [],
                "ids_error": []
            }

        logger.info(f"[Momento 1 ACK] Iniciando confirmación ACK para un total de {len(ids_quejas)} quejas.")
        
        TAMANO_LOTE = 100
        lotes = [ids_quejas[i:i + TAMANO_LOTE] for i in range(0, len(ids_quejas), TAMANO_LOTE)]
        
        ids_exitosos: List[str] = []
        ids_con_error: List[str] = []
        
        for index, lote in enumerate(lotes):
            logger.info(f"[Momento 1 ACK] Enviando lote {index + 1}/{len(lotes)} con {len(lote)} quejas a la SFC...")
            try:
                respuesta_sfc = await self.sfc_client.send_ack_batch(lote)
                data_sfc = respuesta_sfc.get("Response") if isinstance(respuesta_sfc, dict) and "Response" in respuesta_sfc else respuesta_sfc
                
                raw_errors = data_sfc.get("pqrs_error", []) if isinstance(data_sfc, dict) else []
                set_errores = {str(err_id).strip() for err_id in raw_errors}

                for pqrs_id in lote:
                    str_id = str(pqrs_id).strip()
                    if str_id in set_errores:
                        ids_con_error.append(str_id)
                        logger.warning(f"⚠️ [Momento 1 ACK] La SFC reportó error para la queja: {str_id}")
                    else:
                        ids_exitosos.append(str_id)

            except Exception as e:
                logger.error(f"❌ Error HTTP al confirmar el lote {index + 1}: {str(e)}")
                ids_con_error.extend([str(x).strip() for x in lote])

        total_solicitados = len(ids_quejas)
        total_exitosos = len(ids_exitosos)
        total_errores = len(ids_con_error)

        if total_exitosos == total_solicitados:
            status = "success"
            message = f"ACK confirmado exitosamente ante la SFC para {total_exitosos} de {total_solicitados} quejas."
        elif total_exitosos > 0:
            status = "partial"
            message = f"ACK procesado parcialmente ante la SFC. Confirmadas: {total_exitosos}, Con error: {total_errores} de {total_solicitados} quejas."
        else:
            status = "error"
            message = f"Fallo en la confirmación de ACK ante la SFC para las {total_solicitados} quejas."

        return {
            "status": status,
            "message": message,
            "confirmados": total_exitosos,
            "ids_procesados": ids_exitosos,
            "ids_error": ids_con_error
        }

    async def _procesar_queja_individual(self, queja_sfc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Descarga los adjuntos de la queja de SFC, los sube a S3 y 
        traduce la queja a la nomenclatura esperada por el CRM local.
        """
        codigo_queja = queja_sfc.get("codigo_queja")
        tiene_anexos = queja_sfc.get("anexo_queja", False)
        
        if settings.ENVIRONMENT == "local":
            codigo_entidad = queja_sfc.get("entidad_cod")
            tipo_entidad = queja_sfc.get("tipo_entidad")
            logger.info(f"[LOCAL] El codigo de entidad es {codigo_entidad} y el tipo entidad es {tipo_entidad}")
        
        if tiene_anexos:
            logger.info(f"La queja {codigo_queja} tiene anexos. Iniciando descarga...")
        
        adjuntos_procesados = []

        if tiene_anexos:
            try:
                archivos_sfc = await self.sfc_client.get_adjuntos_list(codigo_queja)
                response_data = archivos_sfc.get("Response") if "Response" in archivos_sfc else archivos_sfc
                lista_adjuntos = response_data.get("results", [])

                tareas_descarga = []
                for adjunto in lista_adjuntos:
                    file_url = adjunto.get("file")
                    file_id = adjunto.get("id")
                    raw_type = adjunto.get("type")
                    
                    # 🎯 1. Limpieza de Extensión y Mapeo a ContentType
                    ext, content_type = self._normalizar_tipo_archivo(raw_type, file_url)
                    s3_key = f"quejas/{codigo_queja}/{file_id}.{ext}"
                    
                    tareas_descarga.append(
                        self._descargar_y_subir_a_s3(
                            url=file_url,
                            s3_key=s3_key,
                            file_id=file_id,
                            ext=ext,
                            content_type=content_type
                        )
                    )

                if tareas_descarga:
                    resultados_s3 = await asyncio.gather(*tareas_descarga)
                    
                    if None in resultados_s3:
                        if settings.ENVIRONMENT not in ("local", "qa"):
                            logger.error(f"❌ Abortando queja {codigo_queja}: Al menos un anexo falló. No se enviará ACK.")
                            return None
                        else:
                            logger.warning(f"⚠️ [{settings.ENVIRONMENT.upper()}] Falló un anexo en {codigo_queja}, pero se continuará por pruebas.")

                    adjuntos_procesados = [a for a in resultados_s3 if a is not None]

            except Exception as e:
                if settings.ENVIRONMENT not in ("local", "qa"):
                    logger.error(f"❌ Fallo general al procesar adjuntos para la queja {codigo_queja}: {str(e)}")
                    return None
                else:
                    logger.warning(f"⚠️ [{settings.ENVIRONMENT.upper()}] Fallo general en anexos de {codigo_queja}: {str(e)}")

        queja_traducida = SfcSalesforceMapper.sfc_payload_to_db_dict(queja_sfc)
        queja_traducida["archivos_s3"] = adjuntos_procesados
            
        if settings.ENVIRONMENT == "local" and not adjuntos_procesados and tiene_anexos:
            bucket_local = getattr(settings, "AWS_S3_BUCKET", None) or "global66-sfc-bucket-local"
            queja_traducida["archivos_s3"] = [
                {
                    "nombre_archivo": f"ANEXO_MOCK_{codigo_queja}.pdf",
                    "s3_key": f"local/quejas/{codigo_queja}/ANEXO_MOCK_{codigo_queja}.pdf",
                    "bucket": bucket_local
                }
            ]
            
        return queja_traducida

    async def _descargar_y_subir_a_s3(
        self, 
        url: str, 
        s3_key: str, 
        file_id: Any, 
        ext: str, 
        content_type: str
    ) -> Optional[Dict[str, Any]]:
        """
        Descarga un archivo de la SFC y lo almacena en S3 conservando su tipo y extensión correctos.
        """
        try:
            async with httpx.AsyncClient() as clean_client:
                response = await clean_client.get(url, timeout=15.0)
                response.raise_for_status()
                file_bytes = response.content

            bucket_name = settings.AWS_S3_BUCKET

            if self.s3_client:
                logger.info(f"Subiendo a S3 -> Bucket: {bucket_name} | Key: {s3_key} | ContentType: {content_type}")
                # 🎯 2. Inyección explícita de ContentType al subir el archivo a S3/MinIO
                await asyncio.to_thread(
                    self.s3_client.put_object,
                    Bucket=bucket_name,
                    Key=s3_key,
                    Body=file_bytes,
                    ContentType=content_type
                )
            else:
                logger.warning(f"[DEV] Subida a S3 simulada para: {s3_key}")

            filename = f"{file_id}.{ext}"
            return {
                "nombre_archivo": filename,
                "s3_key": s3_key,
                "bucket": bucket_name
            }
        except Exception as e:
            logger.error(f"Error al descargar/subir archivo {s3_key}: {str(e)}")
            return None