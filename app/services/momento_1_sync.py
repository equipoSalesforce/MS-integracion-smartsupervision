# app/services/momento_1_sync.py
import asyncio
import httpx
import logging
from sqlalchemy.orm import Session
from typing import Dict, Any

from app.integrations.sfc_client import SfcClient
from app.models.quejas import Queja
from app.core.constants import SmartStatus
from app.core.mapping import SfcSalesforceMapper
from app.core.config import settings

logger = logging.getLogger(__name__)

class SincronizacionService:
    def __init__(self, sfc_client: SfcClient, db: Session, s3_client=None):
        self.sfc_client = sfc_client
        self.db = db
        self.s3_client = s3_client

    async def ejecutar_flujo_completo_momento_1(self) -> Dict[str, Any]:
        """
        Orquesta de forma secuencial las tres fases obligatorias del Momento 1.
        """
        res_quejas = await self.sincronizar_nuevas_quejas()
        res_archivos = await self.descargar_adjuntos_pendientes()
        res_ack = await self.reportar_ack_pendientes()

        return {
            "status": "success",
            "quejas": res_quejas,
            "archivos": res_archivos,
            "ack": res_ack
        }

    async def sincronizar_nuevas_quejas(self) -> Dict[str, Any]:
        """
        Fase 1: Descarga y persiste las quejas de la SFC traduciendo 
        sus campos y picklists al diccionario oficial de Salesforce[cite: 4].
        """
        url_actual = None
        quejas_procesadas = 0

        while True:
            respuesta = await self.sfc_client.fetch_quejas_pagina(url=url_actual)
            response_data = respuesta.get("Response") if "Response" in respuesta else respuesta
            lista_quejas = response_data.get("results", [])

            for item in lista_quejas:
                codigo = item.get("codigo_queja")
                
                # Búsqueda utilizando la columna real de Salesforce (Smart_Code__c)
                queja_existente = self.db.query(Queja).filter(Queja.Smart_Code__c == codigo).first()
                
                if not queja_existente:
                    # Instanciación limpia y homologada mediante el Mapper
                    nueva_queja = SfcSalesforceMapper.create_entity_from_sfc(item, SmartStatus.CREATED.value)
                    self.db.add(nueva_queja)
                    quejas_procesadas += 1

            self.db.commit()

            url_actual = response_data.get("next")
            if not url_actual:
                break

        return {"nuevas_quejas_descargadas": quejas_procesadas}

    async def descargar_adjuntos_pendientes(self) -> Dict[str, Any]:
        """
        Fase 2: Identifica quejas con anexos y transfiere los archivos binarios 
        de forma CONCURRENTE para ganarle a la expiración de 5 segundos de la SFC.
        """
        quejas_pendientes = self.db.query(Queja).filter(
            Queja.status_smart == SmartStatus.CREATED.value
        ).all()

        exitosos = 0
        fallidos = 0

        for queja in quejas_pendientes:
            if not queja.smart_anexo_queja__c:
                queja.status_smart = SmartStatus.FILE_DOWNLOAD_OK.value
                exitosos += 1
                continue

            try:
                # 1. Solicitamos los links. El reloj de 5 segundos empieza a correr AQUÍ.
                archivos_sfc = await self.sfc_client.get_adjuntos_list(queja.Smart_Code__c)
                response_data = archivos_sfc.get("Response") if "Response" in archivos_sfc else archivos_sfc
                lista_adjuntos = response_data.get("results", [])
                
                # 2. Creamos una bolsa de tareas para ejecutarlas en paralelo
                tareas_descarga = []
                for adjunto in lista_adjuntos:
                    file_url = adjunto.get("file")
                    file_id = adjunto.get("id")
                    file_type = adjunto.get("type")
                    
                    s3_key = f"{queja.Smart_Code__c}/{file_id}_{file_type}"
                    
                    # OJO: Agregamos la corrutina sin el 'await' para no bloquear el bucle
                    tareas_descarga.append(self._descargar_y_subir_a_s3(file_url, s3_key))
                
                # 3. Disparamos todas las descargas simultáneamente en el mismo microsegundo
                if tareas_descarga:
                    await asyncio.gather(*tareas_descarga)
                
                queja.status_smart = SmartStatus.FILE_DOWNLOAD_OK.value
                exitosos += 1

            except Exception as e:
                logger.error(f"Fallo en descarga concurrente para queja {queja.Smart_Code__c}: {str(e)}")
                queja.status_smart = SmartStatus.FILE_DOWNLOAD_ERROR.value
                fallidos += 1

        self.db.commit()
        return {"descargas_ok": exitosos, "descargas_error": fallidos}

    async def _descargar_y_subir_a_s3(self, url: str, s3_key: str):
        """
        Descarga el archivo binario desde Google Storage y lo sube a S3 en un hilo secundario.
        """
        async with httpx.AsyncClient() as clean_client:
            response = await clean_client.get(url, timeout=10.0)
            response.raise_for_status()
            file_bytes = response.content

        if self.s3_client:
            logger.info(f"Subiendo a S3 bucket: {settings.AWS_S3_BUCKET} -> Key: {s3_key}")
            await asyncio.to_thread(
                self.s3_client.put_object,
                Bucket=settings.AWS_S3_BUCKET,
                Key=s3_key,
                Body=file_bytes
            )
        else:
            logger.warning(f"[LOCAL DEV] Subida simulada a S3 para la llave: {s3_key}")

    async def reportar_ack_pendientes(self) -> Dict[str, Any]:
        """
        Fase 3: Envía confirmación masiva (ACK Batch) a la SFC para cerrar el ciclo del Momento 1.
        """
        quejas_para_ack = self.db.query(Queja).filter(
            Queja.status_smart == SmartStatus.FILE_DOWNLOAD_OK.value
        ).all()

        if not quejas_para_ack:
            return {"ack_exitosos": 0, "ack_fallidos": 0}

        lote_maximo = 100
        ids_lote = [q.Smart_Code__c for q in quejas_para_ack[:lote_maximo]]
        
        try:
            response = await self.sfc_client.send_ack_batch(ids_lote)
            response_data = response.get("Response") if "Response" in response else response
            errores_sfc = response_data.get("pqrs_error", [])

            for queja in quejas_para_ack[:lote_maximo]:
                if queja.Smart_Code__c in errores_sfc:
                    queja.status_smart = SmartStatus.REPORT_ACK_ERROR.value
                else:
                    queja.status_smart = SmartStatus.REPORT_ACK_OK.value

            self.db.commit()
            
            total_errores = len(errores_sfc)
            return {
                "ack_exitosos": len(ids_lote) - total_errores,
                "ack_fallidos": total_errores
            }

        except Exception as e:
            logger.error(f"Fallo crítico al reportar ACK: {str(e)}")
            for queja in quejas_para_ack[:lote_maximo]:
                queja.status_smart = SmartStatus.REPORT_ACK_ERROR.value
                
            self.db.commit()
            return {"ack_exitosos": 0, "ack_fallidos": len(ids_lote)}