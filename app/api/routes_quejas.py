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
from app.schemas.crm_payloads import (
    Momento3TramiteCrmInput,
    Momento3FraudeCrmInput,
    Momento3CierreCrmInput
)
from app.services.momento_3_sync import Momento3SincronizacionService

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
        # 🎯 CAPTURA CONTROLADA: Interceptamos errores de la SFC traducidos con acción sugerida
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
# 📤 MOMENTO 2: Envío de Quejas Nuevas (CRM -> SFC)
# ======================================================================
@router.post(
    "/sync/momento-2",
    status_code=status.HTTP_200_OK,
    summary="Trigger del Momento 2: Despachar queja nueva desde el CRM hacia la SFC",
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
        # 🎯 CAPTURA CONTROLADA: Interceptamos fallas de datos enviadas por el CRM a la SFC (ej. DNI inválido)
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

# ======================================================================
# 🏁 MOMENTO 3: Gestión, Fraudes y Cierre Definitivo (CRM -> SFC)
# ======================================================================

@router.put(
    "/sync/momento-3/tramite",
    status_code=status.HTTP_200_OK,
    summary="Trigger del Momento 3: Actualizar trámite o estados intermedios de la queja"
)
async def actualizar_tramite_crm(
    payload: Momento3TramiteCrmInput,
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    """
    Endpoint síncronizado mediante PUT para el cambio de variables operativas 
    o transiciones de estados intermedios del caso antes de su resolución.
    """
    logger.info(f"Petición de actualización de trámite para el caso: {payload.Smart_Code__c}")
    service = Momento3SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
    
    try:
        resultado = await service.ejecutar_actualizacion_tramite(payload=payload)
        return resultado

    except SfcIntegrationException as exc:
        logger.warning(f"Error controlado de la SFC en M3 (Trámite): {exc.raw_message}")
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
        logger.error(f"Fallo crítico al actualizar trámite en el Momento 3: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error interno del microservicio al actualizar trámite: {str(e)}"
        )


@router.put(
    "/sync/momento-3/fraude",
    status_code=status.HTTP_200_OK,
    summary="Trigger del Momento 3: Reportar o actualizar investigación de Fraude"
)
async def actualizar_fraude_crm(
    payload: Momento3FraudeCrmInput,
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    """
    Endpoint dedicado para hitos de Fraude. Pydantic garantiza de forma estricta 
    que se provea al menos un anexo de soporte para inyectarle el afijo INV_FRAUDE_SFC.
    """
    logger.info(f"Petición de actualización de Fraude para el caso: {payload.Smart_Code__c}")
    service = Momento3SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
    
    try:
        resultado = await service.ejecutar_gestion_fraude(payload=payload)
        return resultado

    except SfcIntegrationException as exc:
        logger.warning(f"Error controlado de la SFC en M3 (Fraude): {exc.raw_message}")
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
        logger.error(f"Fallo crítico al reportar fraude en el Momento 3: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error interno del microservicio al reportar fraude: {str(e)}"
        )


@router.put(
    "/sync/momento-3/cierre",
    status_code=status.HTTP_200_OK,
    summary="Trigger del Momento 3: Ejecutar Clausura y Cierre definitivo del caso (Estado 4)"
)
async def cerrar_caso_crm(
    payload: Momento3CierreCrmInput,
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    """
    Endpoint crítico para congelar la queja (Estado 4). Pydantic valida preventivamente 
    que exista la resolución para asignarle la nomenclatura RESP_FINAL_SFC de forma automática.
    """
    logger.info(f"Petición de Clausura definitiva enviada para el caso: {payload.Smart_Code__c}")
    service = Momento3SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
    
    try:
        resultado = await service.ejecutar_cierre_definitivo(payload=payload)
        return resultado

    except SfcIntegrationException as exc:
        logger.warning(f"Error controlado de la SFC en M3 (Cierre): {exc.raw_message}")
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
        logger.error(f"Fallo crítico al procesar cierre del caso en el Momento 3: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error interno del microservicio al congelar la queja: {str(e)}"
        )