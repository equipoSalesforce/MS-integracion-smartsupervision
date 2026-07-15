# app/api/routes_quejas.py
import logging
from fastapi import APIRouter, Depends, status, HTTPException
from typing import List, Dict, Any

from app.api.dependencies import get_sfc_client, get_s3_client
from app.integrations.sfc_client import SfcClient
from app.services.momento_1_sync import SincronizacionService
from app.services.momento_2_sync import Momento2SincronizacionService

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post(
    "/sync/momento-1", 
    status_code=status.HTTP_200_OK, 
    summary="Obtener Quejas Nuevas de la SFC y procesar adjuntos a S3",
    response_model=List[Dict[str, Any]]
)
async def ejecutar_sync_momento_1(
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    """
    Endpoint síncrono que el CRM local (FastAPI) consume para jalar quejas nuevas:
    
    1. Descarga las quejas nuevas directamente de la SFC.
    2. Almacena de forma asíncrona sus archivos adjuntos en el bucket de AWS S3.
    3. Traduce las quejas a la nomenclatura del CRM local usando el Mapper Universal.
    4. Retorna el listado unificado con las rutas S3 asignadas para que el CRM las persista.
    """
    try:
        # El servicio ya no necesita base de datos relacional local
        servicio = SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
        
        # Ejecuta el flujo y devuelve la lista de quejas mapeadas listas para guardar
        resultado = await servicio.ejecutar_flujo_completo_momento_1()
        return resultado
        
    except Exception as e:
        logger.error(f"Fallo crítico en endpoint de Sincronización de Momento 1: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al sincronizar quejas de la SFC: {str(e)}"
        )
        

@router.post(
    "/sync/momento-2",
    status_code=status.HTTP_200_OK,
    summary="Trigger del Momento 2: Despachar queja nueva desde el CRM hacia la SFC"
)
async def procesar_envio_queja_crm(
    payload: Dict[str, Any],  # Recibe el JSON completo directamente del CRM local
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    """
    Endpoint síncrono consumido de forma directa por el CRM local 
    para despachar una queja recién creada y sus soportes de S3 a la SFC.
    """
    logger.info("Petición del CRM local para procesar envío del caso al Momento 2.")
    
    # Instanciación limpia sin acoplamiento a base de datos
    service = Momento2SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
    
    try:
        # Procesamos enviándole el payload recibido directamente
        resultado = await service.ejecutar_envio_momento_2(payload=payload)
        
        if resultado.get("status") == "error":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=resultado.get("message", "Error en el procesamiento o envío de datos a la SFC")
            )
            
        return resultado

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Fallo crítico al despachar el caso en el Momento 2: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error interno del microservicio al enviar datos a la Superintendencia: {str(e)}"
        )