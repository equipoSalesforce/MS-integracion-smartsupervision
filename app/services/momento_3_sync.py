# app/services/momento_3_sync.py
import asyncio
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from app.integrations.sfc_client import SfcClient
from app.core.config import settings
from app.core.exceptions import SfcIntegrationException
from app.core.mapping import SfcSalesforceMapper  # 🎯 Importación del Mapper Universal
from app.schemas.sfc_payloads import SfcActualizarQuejaPayload  # 🎯 Importación del Esquema SFC M3
from app.schemas.crm_payloads import (
    Momento3TramiteCrmInput,
    Momento3FraudeCrmInput,
    Momento3CierreCrmInput
)

logger = logging.getLogger(__name__)

class Momento3SincronizacionService:
    def __init__(self, sfc_client: SfcClient, s3_client=None):
        self.sfc_client = sfc_client
        self.s3_client = s3_client
        # Valores regulatorios por defecto de la entidad (Global66)
        # TODO: aplicar que lo tomen de los parametros de env o settings
        self.tipo_entidad = 1
        self.entidad_cod = "423"

    async def ejecutar_actualizacion_tramite(self, payload: Momento3TramiteCrmInput) -> Dict[str, Any]:
        """Orquesta la actualización rutinaria de estados intermedios del caso."""
        return await self._orquestar_pipeline_momento_3(
            payload=payload,
            estado_cod=payload.estado_cod__c,
            extra_fields={
                "producto_digital": payload.producto_digital__c,
                "admision": payload.admision_col__c
            }
        )

    async def ejecutar_gestion_fraude(self, payload: Momento3FraudeCrmInput) -> Dict[str, Any]:
        """Orquesta la actualización de mitigación y reporte de Fraude."""
        return await self._orquestar_pipeline_momento_3(
            payload=payload,
            estado_cod=payload.estado_cod__c,
            target_file_name=payload.nombre_archivo_fraude,
            afijo_regulatorio="INV_FRAUDE_SFC",
            extra_fields={
                "tipo_fraude": payload.tipo_fraude__c,
                "modalidad_fraude": payload.modalidad_fraude__c,
                "monto_reclamado": payload.monto_reclamado__c,
                "monto_reconocido": payload.monto_reconocido__c
            }
        )

    async def ejecutar_cierre_definitivo(self, payload: Momento3CierreCrmInput) -> Dict[str, Any]:
        """Orquesta la clausura definitiva de la queja ante la SFC (Estado 4)."""
        return await self._orquestar_pipeline_momento_3(
            payload=payload,
            estado_cod=4,
            target_file_name=payload.nombre_archivo_final,
            afijo_regulatorio="RESP_FINAL_SFC",
            extra_fields={
                "fecha_cierre": payload.ClosedDate.isoformat(),
                "a_favor_de": payload.a_favor_de__c,
                "aceptacion_queja": 1 if payload.Aceptacion__c else 2,
                "rectificacion_queja": 1 if payload.Rectificacion__c else 2,
                "prorroga_queja": 1 if payload.Prorroga__c else 2,
                "documentacion_rta_final": True
            }
        )

    # ======================================================================
    # ⚙️ MOTOR PRIVADO DE ORQUESTACIÓN ASÍNCRONA (MOMENTO 3)
    # ======================================================================
    async def _orquestar_pipeline_momento_3(
        self, 
        payload: Any, 
        estado_cod: int, 
        target_file_name: Optional[str] = None, 
        afijo_regulatorio: Optional[str] = None,
        extra_fields: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        smart_code = payload.Smart_Code__c
        sfc_id_largo = f"{self.tipo_entidad}{self.entidad_cod}{smart_code}"
        
        logger.info(f"[Momento 3] Iniciando pipeline asíncrono para el caso: {sfc_id_largo} (Estado: {estado_cod})")

        try:
            # 🚨 REGLA DE ORO SFC: Primero se suben todos los archivos al Storage
            if payload.archivos_s3:
                logger.info(f"[Momento 3] Detectados {len(payload.archivos_s3)} anexos. Iniciando carga previa...")
                await self._procesar_y_enviar_adjuntos_m3(
                    archivos=payload.archivos_s3,
                    sfc_code=sfc_id_largo,
                    target_file_name=target_file_name,
                    afijo_regulatorio=afijo_regulatorio
                )

            # 🛠️ 1. Transformación con el Mapper Universal (Textos CRM -> Códigos SFC)
            # Convertimos el modelo Pydantic de entrada a dict para procesarlo con tu mapper relacional
            crm_dict = payload.model_dump()
            sfc_raw_payload = SfcSalesforceMapper.crm_entity_to_sfc_payload(crm_dict)

            # 🛠️ 2. Inyección de Reglas de Negocio y Metadatos de Control de M3
            sfc_raw_payload["codigo_queja"] = sfc_id_largo
            sfc_raw_payload["estado_cod"] = estado_cod
            sfc_raw_payload["anexo_queja"] = len(payload.archivos_s3) > 0
            sfc_raw_payload["fecha_actualizacion"] = datetime.now().strftime("%Y-%m-%d")

            # Fallbacks obligatorios de proforma por si no vienen mapeados desde el CRM
            sfc_defaults = {
                "sexo": 2, "lgbtiq": 2, "condicion_especial": 98,
                "queja_expres": 1, "tutela": 2, "ente_control": 99,
                "producto_digital": 1, "admision": 1, "desistimiento_queja": 2
            }
            for campo, valor_defecto in sfc_defaults.items():
                if campo not in sfc_raw_payload or sfc_raw_payload[campo] is None:
                    sfc_raw_payload[campo] = valor_defecto

            # Inyectamos los campos dinámicos calculados en el hito (Fraude, Trámite o Cierre)
            if extra_fields:
                sfc_raw_payload.update(extra_fields)

            # 🛠️ 3. Validación de salida utilizando el esquema estricto de la SFC
            payload_validado = SfcActualizarQuejaPayload(**sfc_raw_payload)

            # 🚨 REGLA DE ORO SFC: Con los archivos arriba, disparamos el PUT definitivo con el formulario
            logger.info(f"[Momento 3] Transmitiendo formulario de actualización de estado hacia la SFC...")
            await self.sfc_client.put_actualizar_queja(
                sfc_codigo_queja=sfc_id_largo, 
                payload=payload_validado.model_dump()  # Despachamos el diccionario sanitizado
            )

            return {
                "status": "success",
                "message": f"Caso {smart_code} actualizado exitosamente en el Momento 3 (Estado {estado_cod})",
                "codigo_queja_sfc": sfc_id_largo
            }

        except SfcIntegrationException:
            # Permitimos que las excepciones controladas de la SFC viajen directo al Router
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

        # 🧪 FALLBACK: Simulación local idéntica a tu Momento 2 si S3 no está activo
        if not self.s3_client:
            if settings.ENVIRONMENT == "development":
                logger.info("[LOCAL TEST M3] Generando streams locales con inyección de afijos regulatorios.")
                for archivo in archivos:
                    file_name = archivo.nombre_archivo
                    file_type = file_name.split(".")[-1] if "." in file_name else "pdf"
                    
                    if target_file_name and file_name == target_file_name:
                        nombre_puro = file_name.rsplit(".", 1)[0]
                        file_name = f"{nombre_puro}_{afijo_regulatorio}.{file_type}"
                        logger.info(f"[LOCAL TEST M3] Aplicando afijo. Renombrado exitoso a: {file_name}")

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

        # 🪐 FLUJO DE PRODUCCIÓN DE ADJUNTOS (S3 REAL)
        for archivo in archivos:
            s3_key = archivo.s3_key
            bucket = archivo.bucket
            original_name = archivo.nombre_archivo
            file_type = original_name.split(".")[-1] if "." in original_name else "pdf"

            metadata = await asyncio.to_thread(self.s3_client.head_object, Bucket=bucket, Key=s3_key)
            if metadata.get("ContentLength", 0) > 30 * 1024 * 1024:
                raise ValueError(f"El archivo {original_name} supera el límite de 30MB permitido por la SFC.")

            s3_file = await asyncio.to_thread(self.s3_client.get_object, Bucket=bucket, Key=s3_key)
            file_bytes = s3_file["Body"].read()

            if target_file_name and original_name == target_file_name:
                nombre_puro = original_name.rsplit(".", 1)[0]
                final_send_name = f"{nombre_puro}_{afijo_regulatorio}.{file_type}"
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