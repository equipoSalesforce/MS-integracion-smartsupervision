# app/api/routes_quejas.py
import logging
from fastapi import APIRouter, Depends, status, HTTPException
from typing import List, Dict, Any

from app.api.dependencies import (
    get_sfc_client, 
    get_s3_client, 
    verificar_api_key_crm  # 🛡️ Protección de cabecera X-API-Key
)
from app.integrations.sfc_client import SfcClient
from app.services.momento_1_sync import SincronizacionService
from app.services.despacho_queja_orchestrator import DespachoQuejaOrquestador
from app.core.exceptions import SfcIntegrationException
from app.schemas.crm_payloads import QuejaMapeadaCrmResponse, QuejaUnificadaCrmInput

# 🛡️ Aplicamos la verificación de API Key a nivel global para todo el Router
router = APIRouter(dependencies=[Depends(verificar_api_key_crm)])
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
# 📤 ENDPOINT UNIFICADO DE DESPACHO (CRM -> SFC) [MOMENTO 2 & MOMENTO 3]
# ======================================================================
@router.post(
    "/sync/despacho",
    status_code=status.HTTP_200_OK,
    summary="Trigger Unificado de Despacho: Creación (M2), Trámite, Fraude o Cierre (M3)",
)
async def despachar_queja_crm(
    payload: QuejaUnificadaCrmInput,
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    """
    Endpoint síncrono unificado consumido por el CRM para procesar tanto 
    el despacho inicial de una queja nueva (Momento 2) como sus actualizaciones 
    operativas, hitos de fraude o clausura definitiva (Momento 3).
    """
    logger.info(f"Petición unificada de despacho recibida para el caso: {payload.Smart_Code__c}")
    orquestador = DespachoQuejaOrquestador(sfc_client=sfc_client, s3_client=s3_client)
    
    try:
        resultado = await orquestador.procesar_despacho(payload=payload)
        
        if resultado.get("status") == "error":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=resultado.get("message", "Error durante el despacho o actualización del caso ante la SFC")
            )
            
        return resultado

    except SfcIntegrationException as exc:
        logger.warning(f"Error controlado de la SFC durante el despacho: {exc.raw_message}")
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
        logger.error(f"Fallo crítico en orquestador de despacho para caso {payload.Smart_Code__c}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error interno del microservicio al despachar el caso a la Superintendencia: {str(e)}"
        )