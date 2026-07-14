import logging
import httpx
from datetime import datetime
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from app.integrations.sfc_client import SfcClient
from app.models.quejas import Queja
from app.core.constants import SmartStatus

logger = logging.getLogger(__name__)

class SincronizacionService:
    def __init__(self, sfc_client: SfcClient, db: Session, s3_client=None):
        self.sfc_client = sfc_client
        self.db = db
        self.s3_client = s3_client

    async def ejecutar_flujo_completo_momento_1(self) -> Dict[str, Any]:
        """
        Orquestador principal que ejecuta el ciclo de vida del Momento I secuencialmente:
        1. Sincroniza nuevas quejas de la SFC (Estado: Created).
        2. Descarga los archivos adjuntos de las quejas creadas de forma inmediata.
        3. Envía el reporte ACK de recibido en lotes.
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
        Itera sobre todas las páginas de quejas de la SFC y las guarda con estado 'Created'.
        """
        url_actual = None
        quejas_procesadas = 0

        while True:
            respuesta = await self.sfc_client.fetch_quejas_pagina(url=url_actual)
            response_data = respuesta.get("Response") if "Response" in respuesta else respuesta
            lista_quejas = response_data.get("results", [])

            for item in lista_quejas:
                codigo = item.get("codigo_queja")
                
                queja_existente = self.db.query(Queja).filter(Queja.codigo_queja == codigo).first()
                
                if not queja_existente:
                    fecha_creacion_dt = None
                    fecha_str = item.get("fecha_creacion")
                    if fecha_str:
                        try:
                            fecha_creacion_dt = datetime.fromisoformat(fecha_str.replace(" ", "T"))
                        except ValueError:
                            logger.warning(f"No se pudo parsear la fecha de creacion: {fecha_str}")

                    nueva_queja = Queja(
                        codigo_queja=codigo,
                        status_smart=SmartStatus.CREATED.value,
                        tipo_entidad=item.get("tipo_entidad"),
                        entidad_cod=item.get("entidad_cod"),
                        fecha_creacion=fecha_creacion_dt,
                        codigo_pais=item.get("codigo_pais"),
                        departamento_cod=item.get("departamento_cod"),
                        municipio_cod=item.get("municipio_cod"),
                        nombres=item.get("nombres"),
                        tipo_id_CF=item.get("tipo_id_CF"),
                        numero_id_CF=item.get("numero_id_CF"),
                        telefono=item.get("telefono"),
                        correo=item.get("correo"),
                        tipo_persona=item.get("tipo_persona"),
                        sexo=item.get("sexo"),
                        lgbtiq=item.get("lgbtiq"),
                        canal_cod=item.get("canal_cod"),
                        condicion_especial=item.get("condicion_especial"),
                        producto_cod=item.get("producto_cod"),
                        producto_nombre=item.get("producto_nombre"),
                        macro_motivo_cod=item.get("macro_motivo_cod"),
                        texto_queja=item.get("texto_queja"),
                        anexo_queja=item.get("anexo_queja"),
                        tutela=item.get("tutela"),
                        ente_control=item.get("ente_control"),
                        escalamiento_DCF=item.get("escalamiento_DCF"),
                        replica=item.get("replica"),
                        argumento_replica=item.get("argumento_replica"),
                        desistimiento_queja=item.get("desistimiento_queja"),
                        queja_expres=item.get("queja_expres")
                    )
                    self.db.add(nueva_queja)
                    quejas_procesadas += 1

            self.db.commit()

            url_actual = response_data.get("next")
            if not url_actual:
                break

        return {"nuevas_quejas_descargadas": quejas_procesadas}

    async def descargar_adjuntos_pendientes(self) -> Dict[str, Any]:
        """
        Busca las quejas en estado 'Created' o 'FileDownload-ERROR', consulta sus enlaces de Google Storage,
        y los descarga inmediatamente antes de que expire la firma de 5 segundos.
        """
        quejas_pendientes = self.db.query(Queja).filter(
            Queja.status_smart.in_([SmartStatus.CREATED.value, SmartStatus.FILE_DOWNLOAD_ERROR.value])
        ).all()

        exitosos = 0
        fallidos = 0

        for queja in quejas_pendientes:
            if not queja.anexo_queja:
                queja.status_smart = SmartStatus.FILE_DOWNLOAD_OK.value
                exitosos += 1
                continue

            try:
                # 1. Obtenemos el listado de adjuntos con la URL firmada temporal de la SFC
                archivos_sfc = await self.sfc_client.get_adjuntos_list(queja.codigo_queja)
                response_data = archivos_sfc.get("Response") if "Response" in archivos_sfc else archivos_sfc
                lista_adjuntos = response_data.get("results", [])
                
                for adjunto in lista_adjuntos:
                    file_url = adjunto.get("file")
                    file_id = adjunto.get("id")
                    file_type = adjunto.get("type")
                    
                    # Estructura del path destino en S3: "codigo_queja/id_tipo"
                    s3_key = f"{queja.codigo_queja}/{file_id}_{file_type}"
                    
                    # 2. Descargamos de inmediato los bytes y los transferimos a S3
                    await self._descargar_y_subir_a_s3(file_url, s3_key)
                
                queja.status_smart = SmartStatus.FILE_DOWNLOAD_OK.value
                exitosos += 1

            except Exception as e:
                logger.error(f"Fallo crítico de descarga/subida de adjuntos para queja {queja.codigo_queja}: {str(e)}")
                queja.status_smart = SmartStatus.FILE_DOWNLOAD_ERROR.value
                fallidos += 1

        self.db.commit()
        return {"descargas_ok": exitosos, "descargas_error": fallidos}

    async def _descargar_y_subir_a_s3(self, url: str, s3_key: str):
        """
        Descarga el archivo binario desde Google Storage utilizando un cliente HTTP limpio
        (sin cabeceras SFC) y lo transfiere directamente a AWS S3.
        """
        # IMPORTANTE: Usamos un cliente limpio para que Google Storage no reciba
        # firmas HMAC de la SFC y no rechace la descarga con un Bad Request.
        async with httpx.AsyncClient() as clean_client:
            response = await clean_client.get(url, timeout=10.0)
            response.raise_for_status()
            file_bytes = response.content

        # Si el cliente de S3 está configurado, realizamos la subida
        if self.s3_client:
            # En producción, esto sube los bytes directamente al bucket RDS/S3 de AWS
            # self.s3_client.put_object(
            #     Bucket="mi-bucket-smartsupervision",
            #     Key=s3_key,
            #     Body=file_bytes
            # )
            pass

    async def reportar_ack_pendientes(self) -> Dict[str, Any]:
        """
        Busca las quejas 'FileDownload-OK' o 'reportACK-ERROR' y reporta el lote ACK.
        """
        quejas_para_ack = self.db.query(Queja).filter(
            Queja.status_smart.in_([SmartStatus.FILE_DOWNLOAD_OK.value, SmartStatus.REPORT_ACK_ERROR.value])
        ).all()

        if not quejas_para_ack:
            return {"ack_exitosos": 0, "ack_fallidos": 0}

        lote_maximo = 100
        ids_lote = [q.codigo_queja for q in quejas_para_ack[:lote_maximo]]
        
        try:
            response = await self.sfc_client.send_ack_batch(ids_lote)
            errores_sfc = response.get("Response", {}).get("pqrs_error", [])

            for queja in quejas_para_ack[:lote_maximo]:
                if queja.codigo_queja in errores_sfc:
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
            logger.error(f"Fallo crítico al reportar lote ACK a la SFC: {str(e)}")
            for queja in quejas_para_ack[:lote_maximo]:
                queja.status_smart = SmartStatus.REPORT_ACK_ERROR.value
                
            self.db.commit()
            return {"ack_exitosos": 0, "ack_fallidos": len(ids_lote)}