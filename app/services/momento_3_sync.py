# app/services/momento_3_sync.py
import asyncio
import logging
from typing import Dict, Any, List, Optional, Union
from datetime import datetime

from app.integrations.sfc_client import SfcClient
from app.core.config import settings
from app.core.exceptions import SfcIntegrationException
from app.core.mapping import SfcSalesforceMapper 
from app.schemas.sfc_payloads import SfcActualizarQuejaPayload 
from app.schemas.crm_payloads import QuejaUnificadaCrmInput, ArchivoS3Schema

logger = logging.getLogger(__name__)


class Momento3SincronizacionService:
    def __init__(self, sfc_client: SfcClient, s3_client=None):
        self.sfc_client = sfc_client
        self.s3_client = s3_client
        self.tipo_entidad = settings.SFC_TIPO_ENTIDAD
        self.entidad_cod = settings.SFC_ENTIDAD_COD

    async def ejecutar_actualizacion_tramite(
        self, payload: Union[QuejaUnificadaCrmInput, Dict[str, Any], Any]
    ) -> Dict[str, Any]:
        """Orquesta la actualización rutinaria de estados intermedios del caso."""
        return await self._orquestar_pipeline_momento_3(payload=payload)

    async def ejecutar_gestion_fraude(
        self, payload: Union[QuejaUnificadaCrmInput, Dict[str, Any], Any]
    ) -> Dict[str, Any]:
        """Orquesta la actualización de mitigación y reporte de Fraude."""
        target_file_name = (
            getattr(payload, "nombre_archivo_fraude", None) 
            if not isinstance(payload, dict) 
            else payload.get("nombre_archivo_fraude")
        )
        return await self._orquestar_pipeline_momento_3(
            payload=payload,
            target_file_name=target_file_name,
            afijo_regulatorio="INV_FRAUDE_SFC"
        )

    async def ejecutar_cierre_definitivo(
        self, payload: Union[QuejaUnificadaCrmInput, Dict[str, Any], Any]
    ) -> Dict[str, Any]:
        """Orquesta la clausura definitiva de la queja ante la SFC (Estado 4)."""
        target_file_name = (
            getattr(payload, "nombre_archivo_final", None) 
            if not isinstance(payload, dict) 
            else payload.get("nombre_archivo_final")
        )
        return await self._orquestar_pipeline_momento_3(
            payload=payload,
            target_file_name=target_file_name,
            afijo_regulatorio="RESP_FINAL_SFC"
        )

    # ======================================================================
    # ⚙️ MOTOR PRIVADO DE ORQUESTACIÓN ASÍNCRONA LIMPIA (MOMENTO 3)
    # ======================================================================
    async def _orquestar_pipeline_momento_3(
        self, 
        payload: Any, 
        target_file_name: Optional[str] = None, 
        afijo_regulatorio: Optional[str] = None
    ) -> Dict[str, Any]:
        if isinstance(payload, dict):
            crm_dict = payload
            smart_code = payload.get("Smart_Code__c")
            archivos_s3_raw = payload.get("archivos_s3", [])
        else:
            crm_dict = payload.model_dump()
            smart_code = payload.Smart_Code__c
            archivos_s3_raw = payload.archivos_s3

        sfc_id_largo = smart_code
        
        # 🛠️ 1. Transformación Íntegra con el Mapper Universal (Textos CRM -> Códigos SFC)
        sfc_raw_payload = SfcSalesforceMapper.crm_entity_to_sfc_payload(crm_dict)
        
        estado_cod = sfc_raw_payload.get("estado_cod", 2)
        logger.info(f"[Momento 3] Iniciando pipeline asíncrono para el caso: {sfc_id_largo} (Estado SFC: {estado_cod})")

        try:
            # REGLA DE ORO SFC: Primero se suben todos los archivos al Storage
            if archivos_s3_raw:
                logger.info(f"[Momento 3] Detectados {len(archivos_s3_raw)} anexos. Iniciando carga previa...")
                await self._procesar_y_enviar_adjuntos_m3(
                    archivos=archivos_s3_raw,
                    sfc_code=sfc_id_largo,
                    target_file_name=target_file_name,
                    afijo_regulatorio=afijo_regulatorio
                )

            # 🛠️ 2. Inyección de Metadatos Regulatorios de Control Operacional
            sfc_raw_payload["codigo_queja"] = sfc_id_largo
            sfc_raw_payload["anexo_queja"] = len(archivos_s3_raw) > 0
            sfc_raw_payload["fecha_actualizacion"] = datetime.now().strftime("%Y-%m-%d")

            # Fallbacks de contingencia por si no vienen mapeados desde el CRM
            sfc_defaults = {
                "sexo": 2, "lgbtiq": 2, "condicion_especial": 98,
                "queja_expres": 1, "tutela": 2, "ente_control": 99,
                "producto_digital": 1, "admision": 1, "desistimiento_queja": 2
            }
            for campo, valor_defecto in sfc_defaults.items():
                if campo not in sfc_raw_payload or sfc_raw_payload[campo] is None:
                    sfc_raw_payload[campo] = valor_defecto

            # 🛠️ 3. Validación estructural final con el esquema estricto de la SFC
            payload_validado = SfcActualizarQuejaPayload(**sfc_raw_payload)

            # 🚨 REGLA DE ORO SFC: Con los archivos arriba, disparamos el PUT definitivo
            logger.info(f"[Momento 3] Transmitiendo formulario de actualización de estado hacia la SFC...")
            await self.sfc_client.put_actualizar_queja(
                sfc_codigo_queja=sfc_id_largo, 
                payload=payload_validado.model_dump()
            )

            return {
                "status": "success",
                "message": f"Caso {smart_code} actualizado exitosamente en el Momento 3 (Estado SFC {estado_cod})",
                "codigo_queja_sfc": sfc_id_largo
            }

        except SfcIntegrationException:
            # Re-lanzamos para permitir la auto-recuperación en el Orquestador Unificado
            raise
        except Exception as e:
            logger.error(f"Fallo crítico en pipeline del Momento 3 para caso {smart_code}: {str(e)}")
            return {"status": "error", "message": f"Pipeline M3 interrumpido: {str(e)}"}

    # ======================================================================
    # 📂 GESTOR ASÍNCRONO DE ADJUNTOS CON RENOMBRADO EN VUELO
    # ======================================================================
    async def _procesar_y_enviar_adjuntos_m3(
        self, 
        archivos: List[Any], 
        sfc_code: str, 
        target_file_name: Optional[str], 
        afijo_regulatorio: Optional[str]
    ):
        tareas_envio = []

        if not self.s3_client:
            if settings.ENVIRONMENT == "development":
                logger.info("[LOCAL TEST M3] Generando streams locales con inyección de afijos regulatorios.")
                for item in archivos:
                    nombre_archivo = item.nombre_archivo if hasattr(item, "nombre_archivo") else item.get("nombre_archivo")
                    file_type = nombre_archivo.split(".")[-1] if "." in nombre_archivo else "pdf"
                    
                    file_name = nombre_archivo
                    if target_file_name and nombre_archivo == target_file_name:
                        if afijo_regulatorio and afijo_regulatorio not in nombre_archivo:
                            nombre_puro = nombre_archivo.rsplit(".", 1)[0]
                            file_name = f"{nombre_puro}_{afijo_regulatorio}.{file_type}"
                        logger.info(f"[LOCAL TEST M3] Aplicando afijo. Nombre de envío: {file_name}")

                    file_bytes = b"Contenido de resolucion digital simulado por Global66."
                    tareas_envio.append(self.sfc_client.post_adjunto_queja(
                        sfc_codigo_queja=sfc_code,
                        file_bytes=file_bytes,
                        file_type=file_type,
                        file_name=file_name
                    ))
                if tareas_envio:
                    await asyncio.gather(*tareas_envio)
                return
            else:
                raise ValueError("Error de infraestructura: El cliente S3 no está inicializado en producción.")

        for item in archivos:
            s3_key = item.s3_key if hasattr(item, "s3_key") else item.get("s3_key")
            bucket = (item.bucket if hasattr(item, "bucket") else item.get("bucket")) or settings.AWS_S3_BUCKET
            original_name = item.nombre_archivo if hasattr(item, "nombre_archivo") else item.get("nombre_archivo")
            file_type = original_name.split(".")[-1] if "." in original_name else "pdf"

            metadata = await asyncio.to_thread(self.s3_client.head_object, Bucket=bucket, Key=s3_key)
            if metadata.get("ContentLength", 0) > 30 * 1024 * 1024:
                raise ValueError(f"El archivo {original_name} supera el límite de 30MB permitido por la SFC.")

            s3_file = await asyncio.to_thread(self.s3_client.get_object, Bucket=bucket, Key=s3_key)
            file_bytes = s3_file["Body"].read()

            if target_file_name and original_name == target_file_name:
                if afijo_regulatorio and afijo_regulatorio not in original_name:
                    nombre_puro = original_name.rsplit(".", 1)[0]
                    final_send_name = f"{nombre_puro}_{afijo_regulatorio}.{file_type}"
                else:
                    final_send_name = original_name
                logger.info(f"[Momento 3] Aplicando afijo oficial. Transmutando '{original_name}' a '{final_send_name}'")
            else:
                final_send_name = original_name

            tareas_envio.append(self.sfc_client.post_adjunto_queja(
                sfc_codigo_queja=sfc_code,
                file_bytes=file_bytes,
                file_type=file_type,
                file_name=final_send_name
            ))

        if tareas_envio:
            logger.info(f"[Momento 3] Transmitiendo concurrentemente {len(tareas_envio)} anexos a la SFC.")
            await asyncio.gather(*tareas_envio)