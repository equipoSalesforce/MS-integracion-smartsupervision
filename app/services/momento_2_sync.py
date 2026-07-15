# app/services/momento_2_sync.py
import asyncio
import logging
from types import SimpleNamespace
from typing import Dict, Any
from sqlalchemy.orm import Session

from app.integrations.sfc_client import SfcClient
from app.schemas.sfc_payloads import SfcNuevaQuejaPayload
from app.models.quejas_crud import QuejasCRUD 
from app.core.config import settings
from app.core.mapping import SfcSalesforceMapper

logger = logging.getLogger(__name__)

class Momento2SincronizacionService:
    def __init__(self, sfc_client: SfcClient, db: Session, s3_client=None):
        self.sfc_client = sfc_client
        self.db = db
        self.s3_client = s3_client

    async def ejecutar_envio_momento_2(self, smart_code: str) -> Dict[str, Any]:
        """
        Orquesta el flujo del Momento 2 usando el repositorio para la base de datos[cite: 5].
        """
        logger.info(f"[Momento 2] Iniciando pipeline de despacho para el caso: {smart_code}")
        
        # 1. Consolidación Relacional delegada en el Repositorio[cite: 5]
        datos_consolidados = QuejasCRUD.obtener_datos_consolidados_caso(self.db, smart_code)
        if not datos_consolidados:
            return {"status": "error", "message": f"Caso {smart_code} no encontrado en la base de datos."}

        try:
            
            # TODO: corregir posteriormente
            # 2. Transformación con el Mapper (Caja negra)
            # El mapper asume recibir un objeto SQLAlchemy. Simulamos uno con SimpleNamespace
            # para mantener la compatibilidad con el mapper y evitar dependencias de la tabla real
            sfc_raw_payload = SfcSalesforceMapper.db_entity_to_sfc_payload(datos_consolidados)

            # 3. Regla de Negocio: ID Compuesto regulatorio
            tipo_entidad = datos_consolidados.get("tipo_entidad", 1) or 1
            entidad_cod = datos_consolidados.get("entidad_cod", "423") or "423"
            sfc_id_largo = f"{tipo_entidad}{entidad_cod}{smart_code}"
            sfc_raw_payload["codigo_queja"] = sfc_id_largo

            # 4. Validación de salida utilizando el nuevo esquema SFC
            payload_validado = SfcNuevaQuejaPayload(**sfc_raw_payload)

            # 5. Envío del caso por red
            await self.sfc_client.post_nueva_queja(payload_validado.model_dump())
            
            # 6. Pipeline de archivos (S3 -> SFC)
            if payload_validado.anexo_queja:
                await self._procesar_y_enviar_adjuntos_s3(smart_code=smart_code, sfc_code=sfc_id_largo)

            # 7. Persistencia del ÉXITO vía el Repositorio
            QuejasCRUD.actualizar_estado_caso(
                db=self.db,
                smart_code=smart_code,
                status_smart="sendToSmart-OK",
                sfc_status="sendToSmart-OK"
            )

            return {
                "status": "success",
                "message": "Queja y documentos transmitidos correctamente a la SFC",
                "codigo_queja_sfc": sfc_id_largo
            }

        except Exception as e:
            logger.error(f"Fallo en Momento 2 para caso {smart_code}: {str(e)}")
            
            # 8. Persistencia de la FALLA vía el Repositorio
            QuejasCRUD.actualizar_estado_caso(
                db=self.db,
                smart_code=smart_code,
                status_smart="sendToSmart-Error",
                sfc_status="sendToSmart-Error"
            )
            
            return {"status": "error", "message": f"Pipeline interrumpido: {str(e)}"}

    async def _procesar_y_enviar_adjuntos_s3(self, smart_code: str, sfc_code: str):
        """Descarga de S3 y transmisión asíncrona concurrente a la SFC."""
        if not self.s3_client:
            logger.warning(f"[LOCAL DEV] Carga de adjuntos simulada para: {sfc_code}")
            return

        prefix = f"{smart_code}/"
        response_s3 = await asyncio.to_thread(
            self.s3_client.list_objects_v2,
            Bucket=settings.AWS_S3_BUCKET,
            Prefix=prefix
        )

        objetos = response_s3.get("Contents", [])
        tareas_envio = []

        for obj in objetos:
            s3_key = obj["Key"]
            file_size = obj["Size"]

            # Regla de Oro: Máximo 30MB por archivo[cite: 4, 5]
            if file_size > 30 * 1024 * 1024:
                raise ValueError(f"El archivo {s3_key} supera el límite de 30MB permitido por la SFC[cite: 4, 5].")

            file_type = s3_key.split(".")[-1] if "." in s3_key else "pdf"

            s3_file = await asyncio.to_thread(
                self.s3_client.get_object,
                Bucket=settings.AWS_S3_BUCKET,
                Key=s3_key
            )
            file_bytes = s3_file["Body"].read()

            tareas_envio.append(self.sfc_client.post_adjunto_queja(
                sfc_codigo_queja=sfc_code,
                file_bytes=file_bytes,
                file_type=file_type
            ))

        if tareas_envio:
            await asyncio.gather(*tareas_envio)