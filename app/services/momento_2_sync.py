# app/services/momento_2_sync.py
import asyncio
import logging
from typing import Dict, Any, List

from app.integrations.sfc_client import SfcClient
from app.schemas.sfc_payloads import SfcNuevaQuejaPayload
from app.core.config import settings
from app.core.mapping import SfcSalesforceMapper

logger = logging.getLogger(__name__)

class Momento2SincronizacionService:
    def __init__(self, sfc_client: SfcClient, s3_client=None):     
        self.sfc_client = sfc_client
        self.s3_client = s3_client

    async def ejecutar_envio_momento_2(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Orquesta el flujo del Momento 2 de forma Stateless (sin base de datos).
        Recibe el payload completo del CRM, lo mapea y transmite a la SFC.
        """
        smart_code = payload.get("Smart_Code__c")
        if not smart_code:
            return {"status": "error", "message": "Falta el campo obligatorio 'Smart_Code__c' en el payload."}

        logger.info(f"[Momento 2] Iniciando pipeline de despacho síncrono para el caso: {smart_code}")

        try:
            # 1. Transformación con el Mapper Universal (Textos CRM -> Códigos SFC)
            sfc_raw_payload = SfcSalesforceMapper.db_entity_to_sfc_payload(payload)

            # 2. Regla de Negocio: ID Compuesto regulatorio
            tipo_entidad = payload.get("tipo_entidad", 1) or 1
            entidad_cod = payload.get("entidad_cod", "423") or "423"
            sfc_id_largo = f"{tipo_entidad}{entidad_cod}{smart_code}"
            sfc_raw_payload["codigo_queja"] = sfc_id_largo

            # 3. Validación de salida utilizando el esquema SFC
            payload_validado = SfcNuevaQuejaPayload(**sfc_raw_payload)

            # 4. Envío de metadatos (La SFC exige envoltura "Body")
            payload_final = {"Body": payload_validado.model_dump()}
            
            logger.info(f"[Momento 2] Enviando queja a la SFC con código regulatorio: {sfc_id_largo}")
            await self.sfc_client.post_nueva_queja(payload_final)
            
            # 5. Pipeline de archivos (S3 -> SFC)
            # El CRM envía el listado de adjuntos de S3 en el cuerpo del JSON bajo "archivos_s3"
            archivos_s3 = payload.get("archivos_s3", [])
            
            if payload_validado.anexo_queja and not archivos_s3:
                logger.warning(
                    f"[Momento 2] La queja {smart_code} marca 'anexo_queja' como Verdadero, "
                    f"pero la lista 'archivos_s3' llegó vacía desde el CRM."
                )

            if archivos_s3:
                await self._procesar_y_enviar_adjuntos_s3(archivos=archivos_s3, sfc_code=sfc_id_largo)

            return {
                "status": "success",
                "message": "Queja y documentos transmitidos correctamente a la SFC de forma síncrona",
                "codigo_queja_sfc": sfc_id_largo
            }

        except Exception as e:
            logger.error(f"Fallo en pipeline del Momento 2 para caso {smart_code}: {str(e)}")
            return {"status": "error", "message": f"Pipeline interrumpido: {str(e)}"}

    async def _procesar_y_enviar_adjuntos_s3(self, archivos: List[Dict[str, Any]], sfc_code: str):
        """Descarga del listado exacto de archivos en S3 y los sube de manera concurrente a la SFC."""
        if not self.s3_client:
            logger.warning(f"[LOCAL DEV] Carga de adjuntos simulada para SFC: {sfc_code}")
            return

        tareas_envio = []

        for archivo in archivos:
            s3_key = archivo.get("s3_key")
            bucket = archivo.get("bucket", settings.AWS_S3_BUCKET)

            if not s3_key:
                logger.warning("[Momento 2] Se recibió un adjunto sin clave 's3_key', se omitirá.")
                continue

            # 1. Obtener metadatos para validar límite de tamaño (Máximo 30MB por la SFC)
            metadata = await asyncio.to_thread(
                self.s3_client.head_object,
                Bucket=bucket,
                Key=s3_key
            )
            file_size = metadata.get("ContentLength", 0)

            if file_size > 30 * 1024 * 1024:
                raise ValueError(f"El archivo {s3_key} supera el límite de 30MB permitido por la SFC.")

            # 2. Descargar el archivo de S3 directo a memoria
            s3_file = await asyncio.to_thread(
                self.s3_client.get_object,
                Bucket=bucket,
                Key=s3_key
            )
            file_bytes = s3_file["Body"].read()
            file_type = s3_key.split(".")[-1] if "." in s3_key else "pdf"

            # 3. Disparar concurrencia hacia la SFC
            tareas_envio.append(self.sfc_client.post_adjunto_queja(
                sfc_codigo_queja=sfc_code,
                file_bytes=file_bytes,
                file_type=file_type
            ))

        if tareas_envio:
            logger.info(f"[Momento 2] Transmitiendo concurrentemente {len(tareas_envio)} anexos de S3 a la SFC.")
            await asyncio.gather(*tareas_envio)