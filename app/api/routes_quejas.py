import logging
from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from app.api.dependencies import get_db, get_sfc_client, get_s3_client
from app.integrations.sfc_client import SfcClient
from app.services.momento_1_sync import SincronizacionService
from app.models.quejas import Queja
from app.services.momento_2_sync import Momento2SincronizacionService
from app.schemas.crm_payloads import Momento2Trigger

router = APIRouter()

logger = logging.getLogger(__name__)

@router.post(
    "/sync/momento-1", 
    status_code=status.HTTP_200_OK, 
    summary="Ejecutar Sincronización Completa del Momento 1 (Para Cron Job)"
)
async def ejecutar_sync_momento_1(
    db: Session = Depends(get_db),
    sfc_client: SfcClient = Depends(get_sfc_client)
):
    """
    Inicia el flujo automatizado secuencial para capturar la información de la SFC.
    
    1. Trae quejas nuevas de la nube de la SFC.
    2. Descarga de forma asíncrona sus archivos adjuntos y los envía a S3.
    3. Envía el reporte de confirmación recibido (ACK) en lotes para liberar el backlog.
    """
    try:
        # Instanciamos el servicio utilizando las dependencias de la petición
        servicio = SincronizacionService(sfc_client=sfc_client, db=db)
        
        # Ejecutamos el pipeline unificado de la máquina de estados
        resultado = await servicio.ejecutar_flujo_completo_momento_1()
        return resultado
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fallo crítico en el proceso de sincronización automática de la SFC: {str(e)}"
        )
        
@router.post(
    "/sync/momento-2",
    status_code=status.HTTP_200_OK,
    summary="Trigger del Momento 2: Despachar queja nueva desde el CRM hacia la SFC"
)
async def procesar_envio_queja_crm(
    payload: Momento2Trigger,
    db: Session = Depends(get_db),
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    """
    Endpoint consumido de forma directa por el CRM (Salesforce)
    para despachar una queja recién creada y sus soportes de S3 a la SFC[cite: 5].
    """
    logger.info(f"Petición del CRM para procesar envío del caso local: {payload.Smart_Code__c}")
    
    # Instanciación limpia
    service = Momento2SincronizacionService(sfc_client=sfc_client, db=db, s3_client=s3_client)
    
    try:
        resultado = await service.ejecutar_envio_momento_2(smart_code=payload.Smart_Code__c)
        
        if resultado.get("status") == "error":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=resultado.get("message", "Error en el procesamiento o envío de datos")
            )
            
        return resultado

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Fallo crítico al despachar el caso {payload.Smart_Code__c} en el Momento 2: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno del microservicio al enviar datos a la Superintendencia"
        )