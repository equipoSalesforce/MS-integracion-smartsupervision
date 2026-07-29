# app/api/routes_quejas.py
import logging
import httpx
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from typing import List, Optional

from app.api.dependencies import (
    get_sfc_client, 
    get_s3_client, 
    verificar_api_key_crm 
)
from app.integrations.sfc_client import SfcClient
from app.services.email_service import EmailAlertService
from app.services.momento_1_sync import SincronizacionService
from app.services.despacho_queja_orchestrator import DespachoQuejaOrquestador
from app.services.momento_4_sync import UserSync
from app.core.exceptions import SfcIntegrationException
from app.schemas.crm_payloads import (
    ConfirmacionAckUsuariosInput,
    QuejaMapeadaCrmResponse, 
    QuejaUnificadaCrmInput,
    ConfirmacionAckInput
)

# 🛠️ Imports para la Cola Local SQLite
from app.db.database import AsyncSessionLocal
from app.services.queue_service import QueueService

# 🛡️ Aplicamos la verificación de API Key a nivel global para todo el Router
router = APIRouter(dependencies=[Depends(verificar_api_key_crm)])
logger = logging.getLogger(__name__)

# ======================================================================
# 📥 MOMENTO 1: Sincronización y ACK (SFC -> CRM)
# ======================================================================
@router.get(
    "/sync/momento-1", 
    status_code=status.HTTP_200_OK, 
    summary="Obtener Quejas Nuevas de la SFC y procesar adjuntos a S3",
    response_model=List[QuejaMapeadaCrmResponse]
)
async def ejecutar_sync_momento_1(
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    try:
        servicio = SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
        return await servicio.ejecutar_flujo_completo_momento_1()
        
    except SfcIntegrationException:
        raise  # 🎯 El Handler global en main.py se encarga de formatear
        
    except Exception as e:
        logger.error(f"Fallo crítico en endpoint de Sincronización de Momento 1: {str(e)}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status_code": 500,
                "error_type": "MOMENTO_1_SYNC_ERROR",
                "sfc_field": "system",
                "raw_message": f"Error al sincronizar quejas de la SFC: {str(e)}",
                "crm_action_friendly": "Contactar al equipo de soporte/backend para revisar logs de sincronización de Momento 1."
            }
        )


@router.post(
    "/sync/momento-1/ack",
    status_code=status.HTTP_200_OK,
    summary="Confirmar recepción exitosa de quejas (ACK) a la SFC"
)
async def confirmar_ack_momento_1(
    payload: ConfirmacionAckInput,
    sfc_client: SfcClient = Depends(get_sfc_client)
):
    try:
        servicio = SincronizacionService(sfc_client=sfc_client)
        return await servicio.confirmar_recepcion_ack(ids_quejas=payload.ids_quejas)
        
    except SfcIntegrationException:
        raise

    except Exception as e:
        logger.error(f"Fallo crítico al reportar ACK de Momento 1: {str(e)}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status_code": 500,
                "error_type": "MOMENTO_1_ACK_ERROR",
                "sfc_field": "ids_quejas",
                "raw_message": f"Error al enviar confirmación ACK a la SFC: {str(e)}",
                "crm_action_friendly": "Verifique los IDs de quejas enviados y reintente el despacho de confirmación ACK."
            }
        )


# ======================================================================
# 📤 ENDPOINT UNIFICADO DE DESPACHO (CRM -> SFC) [MOMENTO 2 & MOMENTO 3]
# ======================================================================
@router.post(
    "/sync/despacho",
    status_code=status.HTTP_200_OK,
    summary="Trigger Unificado de Despacho con Cola de Contingencia SQLite",
)
async def despachar_queja_crm(
    payload: QuejaUnificadaCrmInput,
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    logger.info(f"Petición unificada de despacho recibida para el caso: {payload.Smart_Code__c}")
    orquestador = DespachoQuejaOrquestador(sfc_client=sfc_client, s3_client=s3_client)
    
    try:
        resultado = await orquestador.procesar_despacho(payload=payload)
        
        if isinstance(resultado, dict) and resultado.get("status") == "error":
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "status_code": 400,
                    "error_type": "DESPACHO_ORCHESTRATION_ERROR",
                    "sfc_field": "payload",
                    "raw_message": resultado.get("message", "Error durante el despacho o actualización del caso ante la SFC."),
                    "crm_action_friendly": "Revise los campos enviados en el caso y valide que cumplan con la normativa."
                }
            )
            
        return resultado

    except SfcIntegrationException as exc:
        # 🛡️ Si el error es de servidor/indisponibilidad de la SFC (>= 500)
        if exc.status_code >= 500 or exc.error_type in ["SERVER_ERROR", "SFC_DOWN", "TIMEOUT", "NETWORK_ERROR"]:
            logger.warning(f"⚠️ SFC no disponible ({exc.status_code}). Guardando caso {payload.Smart_Code__c} en cola SQLite local.")
            
            async with AsyncSessionLocal() as session:
                queue_service = QueueService(session)
                await queue_service.encolar_despacho(
                    smart_code=payload.Smart_Code__c,
                    tipo_operacion="AUTO",
                    payload_json=payload.model_dump(mode="json"),
                    error_inicial=exc.raw_message or str(exc)
                )
                
            await EmailAlertService.notificar_falla_infraestructura(
                smart_code=payload.Smart_Code__c,
                error_msg=f"SFC Exception ({exc.status_code}): {exc.raw_message}"
            )
                
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "status": "queued",
                    "smart_code": payload.Smart_Code__c,
                    "message": "La Superintendencia no se encuentra disponible en este momento. El caso ha sido encolado para reintento automático.",
                    "error_origen": exc.raw_message
                }
            )

        # 🚨 Si es un error de datos/validación del cliente (< 500), se re-lanza para que el handler global lo procese
        logger.warning(f"Error controlado de validación de la SFC durante el despacho: {exc.raw_message}")
        raise

    except (httpx.RequestError, httpx.TimeoutException, ConnectionError) as net_err:
        # 🛡️ Intercepta caídas directas de red/puerto inalcanzable
        logger.warning(f"⚠️ Fallo de conexión contra la SFC ({str(net_err)}). Guardando caso {payload.Smart_Code__c} en cola SQLite.")
        
        async with AsyncSessionLocal() as session:
            queue_service = QueueService(session)
            await queue_service.encolar_despacho(
                smart_code=payload.Smart_Code__c,
                tipo_operacion="AUTO",  # 🎯 CORREGIDO: Se pasó la cadena fija "AUTO"
                payload_json=payload.model_dump(mode="json"),
                error_inicial=str(net_err)
            )
            
        await EmailAlertService.notificar_falla_infraestructura(
            smart_code=payload.Smart_Code__c,
            error_msg=f"Network Error: {str(net_err)}"
        )
            
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "queued",
                "smart_code": payload.Smart_Code__c,
                "message": "Fallo de comunicación con la SFC. El caso fue encolado localmente y se transmitirá automáticamente cuando se restablezca el servicio.",
                "error_origen": str(net_err)
            }
        )

    except Exception as e:
        logger.error(f"Fallo crítico no controlado en orquestador de despacho para caso {payload.Smart_Code__c}: {str(e)}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status_code": 500,
                "error_type": "UNEXPECTED_SERVER_ERROR",
                "sfc_field": "system",
                "raw_message": f"Error interno del microservicio al despachar el caso a la Superintendencia: {str(e)}",
                "crm_action_friendly": "Contactar al equipo técnico. Revisar la traza completa de logs en el microservicio."
            }
        )


@router.get(
    "/queue",
    status_code=status.HTTP_200_OK,
    summary="Consultar el estado de la cola de reintentos local (SQLite)"
)
async def consultar_cola_local(
    estado: Optional[str] = None
):
    async with AsyncSessionLocal() as session:
        queue_service = QueueService(session)
        registros = await queue_service.obtener_todos_los_encolados(estado=estado)
        
        return [
            {
                "id": r.id,
                "smart_code": r.smart_code,
                "tipo_operacion": r.tipo_operacion,
                "estado": r.estado,
                "intentos": r.intentos,
                "max_intentos": r.max_intentos,
                "ultimo_error": r.ultimo_error,
                "proximo_reintento_at": r.proximo_reintento_at.isoformat() if r.proximo_reintento_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in registros
        ]


# ======================================================================
# 📥 MOMENTO 4: Actualización y ACK de usuarios
# ======================================================================

@router.get(
    "/sync/momento-4",
    status_code=status.HTTP_200_OK,
    summary="Obtener información actualizada de usuarios desde la SFC",
)
async def actualizar_usuarios(
    sfc_client: SfcClient = Depends(get_sfc_client),
):
    try:
        servicio = UserSync(sfc_client=sfc_client)
        return await servicio.sincronizar_usuarios()
    
    except SfcIntegrationException:
        raise

    except Exception as e:
        logger.error(f"Fallo crítico en endpoint de Sincronización de Momento 4: {str(e)}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status_code": 500,
                "error_type": "MOMENTO_4_SYNC_ERROR",
                "sfc_field": "system",
                "raw_message": f"Error al sincronizar usuarios de la SFC: {str(e)}",
                "crm_action_friendly": "Contactar al equipo técnico. Revisar la comunicación con el endpoint del Momento 4 de la SFC."
            }
        )


@router.post(
    "/sync/momento-4/ack",
    status_code=status.HTTP_200_OK,
    summary="Confirmar recepción exitosa de datos de usuarios (ACK) a la SFC"
)
async def confirmar_ack_momento_4(
    payload: ConfirmacionAckUsuariosInput,
    sfc_client: SfcClient = Depends(get_sfc_client)
):
    try:
        servicio = UserSync(sfc_client=sfc_client)
        return await servicio.confirmar_recepcion_ack_usuarios(numeros_id_cf=payload.numeros_id_cf)
        
    except SfcIntegrationException:
        raise

    except Exception as e:
        logger.error(f"Fallo crítico al reportar ACK de Momento 4: {str(e)}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status_code": 500,
                "error_type": "MOMENTO_4_ACK_ERROR",
                "sfc_field": "numeros_id_cf",
                "raw_message": f"Error al enviar confirmación ACK de usuarios a la SFC: {str(e)}",
                "crm_action_friendly": "Verifique la lista de identificaciones procesadas e intente nuevamente la confirmación ACK de Momento 4."
            }
        )