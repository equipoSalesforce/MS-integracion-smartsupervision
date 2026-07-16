# app/api/routes_quejas.py
import logging
from fastapi import APIRouter, Depends, status, HTTPException
from typing import List, Dict, Any

from app.api.dependencies import get_sfc_client, get_s3_client
from app.integrations.sfc_client import SfcClient
from app.services.momento_1_sync import SincronizacionService
from app.services.momento_2_sync import Momento2SincronizacionService
from app.core.exceptions import SfcIntegrationException
from app.schemas.crm_payloads import QuejaMapeadaCrmResponse

router = APIRouter()
logger = logging.getLogger(__name__)

# ======================================================================
# 📥 MOMENTO 1: Sincronización (SFC -> CRM)
# ======================================================================
@router.post(
    "/sync/momento-1", 
    status_code=status.HTTP_200_OK, 
    summary="Obtener Quejas Nuevas de la SFC y procesar adjuntos a S3",
    response_model=List[QuejaMapeadaCrmResponse]
)
async def ejecutar_sync_momento_1(
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    """
    Endpoint síncrono que el CRM local (FastAPI) consume para jalar quejas nuevas.
    """
    try:
        servicio = SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
        resultado = await servicio.ejecutar_flujo_completo_momento_1()
        return resultado
        
    except SfcIntegrationException as exc:
        # 🎯 CAPTURA CONTROLADA: Interceptamos errores de la SFC traducidos con acción sugerida[cite: 4]
        logger.warning(f"Error controlado de la SFC en Momento 1: {exc.raw_message}")
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "status": "error",
                "error_type": exc.error_type,
                "sfc_field": exc.sfc_field,
                "raw_sfc_message": exc.raw_message,
                "crm_action_friendly": exc.crm_action
            }
        )
    except Exception as e:
        logger.error(f"Fallo crítico en endpoint de Sincronización de Momento 1: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al sincronizar quejas de la SFC: {str(e)}"
        )
        

# ======================================================================
# 📤 MOMENTO 2: Envío de Quejas Nuevas (CRM -> SFC)[cite: 1]
# ======================================================================
@router.post(
    "/sync/momento-2",
    status_code=status.HTTP_200_OK,
    summary="Trigger del Momento 2: Despachar queja nueva desde el CRM hacia la SFC"
)
async def procesar_envio_queja_crm(
    payload: Dict[str, Any],
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    """
    Endpoint síncrono consumido por el CRM local para despachar una queja recién creada.
    """
    logger.info("Petición del CRM local para procesar envío del caso al Momento 2.")
    service = Momento2SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
    
    try:
        resultado = await service.ejecutar_envio_momento_2(payload=payload)
        
        if resultado.get("status") == "error":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=resultado.get("message", "Error en el procesamiento o envío de datos a la SFC")
            )
            
        return resultado

    except SfcIntegrationException as exc:
        # 🎯 CAPTURA CONTROLADA: Interceptamos fallas de datos enviadas por el CRM a la SFC (ej. DNI inválido)[cite: 4]
        logger.warning(f"Error controlado de la SFC en Momento 2: {exc.raw_message}")
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "status": "error",
                "error_type": exc.error_type,
                "sfc_field": exc.sfc_field,
                "raw_sfc_message": exc.raw_message,
                "crm_action_friendly": exc.crm_action
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Fallo crítico al despachar el caso en el Momento 2: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error interno del microservicio al enviar datos a la Superintendencia: {str(e)}"
        )