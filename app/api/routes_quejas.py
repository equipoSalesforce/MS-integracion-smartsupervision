from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from app.api.dependencies import get_db, get_sfc_client
from app.integrations.sfc_client import SfcClient
from app.services.momento_1_sync import SincronizacionService
from app.models.quejas import Queja

router = APIRouter()

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