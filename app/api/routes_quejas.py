# app/api/routes_quejas.py
import logging
import httpx
from fastapi import APIRouter, Body, Depends, status
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
    payload: QuejaUnificadaCrmInput = Body(
        ...,
        openapi_examples={
            "creacion_queja_nueva": {
                "summary": "1. Creación de Queja Nueva (M2)",
                "description": "Alta inicial de una queja ordinaria por cobro de comisiones.",
                "value": {
                    "Case_id": "TEST-NOFILE-M2-001",
                    "Smart_Code__c": "TEST-NOFILE-M2-001",
                    "Status": "New",
                    "SuppliedName": "Andrés Felipe Gómez",
                    "SC_id_type__c": "CC",
                    "id_number__c": "1015443322",
                    "sc_genero__c": "Masculino",
                    "tipo_de_persona__c": "B2C",
                    "SuppliedPhone": "3114445566",
                    "SuppliedEmail": "andres.gomez@test.com",
                    "direccion__c": "Calle 127 # 45-20 Apto 301",
                    "Departamento__c": "Bogotá D.C.",
                    "SC_municipio__c": "Bogotá D.C.",
                    "canal__c": "Internet",
                    "punto_recepcion": "Web",
                    "Description": "El cliente presenta inconformidad por un cobro de comisión no informado.",
                    "smart_anexo_queja__c": False,
                    "Product__c": "Wallet",
                    "Categorias_COL__c": "Inconsistencia en el cobro de comisiones - Descuentos injustificados",
                    "archivos_s3": []
                }
            },
            "actualizacion_tramite": {
                "summary": "2. Actualización de Trámite (M3 Ordinario)",
                "description": "Actualización de información del caso mientras está en gestión.",
                "value": {
                    "Case_id": "TEST-NOFILE-M3-002",
                    "Smart_Code__c": "TEST-NOFILE-M3-002",
                    "Status": "In Progress",
                    "SuppliedName": "Luz Marina Palacios",
                    "SC_id_type__c": "CE",
                    "id_number__c": "529883011",
                    "sc_genero__c": "Femenino",
                    "tipo_de_persona__c": "B2C",
                    "sc_Condicion_especial__c": "Adulto mayor",
                    "SuppliedPhone": "3201112233",
                    "SuppliedEmail": "luz.palacios@test.com",
                    "direccion__c": "Carrera 7 # 114-33",
                    "Departamento__c": "Bogotá D.C.",
                    "SC_municipio__c": "Bogotá D.C.",
                    "canal__c": "Aplicaciones móviles",
                    "punto_recepcion": "WhatsApp",
                    "Description": "Demora en la acreditación de envío de remesa desde el exterior.",
                    "Product__c": "P2P",
                    "Categorias_COL__c": "Remesas",
                    "archivos_s3": []
                }
            },
            "cierre_favorable_b2c": {
                "summary": "3. Cierre Definitivo Favorable (B2C)",
                "description": "Respuesta final favorable con PDF auto-generado.",
                "value": {
                    "Case_id": "TEST-NOFILE-CIERRE-003",
                    "Smart_Code__c": "TEST-NOFILE-CIERRE-003",
                    "Status": "Closed",
                    "ClosedDate": "2026-07-30",
                    "SuppliedName": "Carlos Eduardo Mendoza",
                    "SC_id_type__c": "CC",
                    "id_number__c": "1018234563",
                    "sc_genero__c": "Masculino",
                    "tipo_de_persona__c": "B2C",
                    "SuppliedPhone": "3101234567",
                    "SuppliedEmail": "carlos.mendoza@test.com",
                    "direccion__c": "Calle 100 # 15-20 Apto 502",
                    "Departamento__c": "Bogotá D.C.",
                    "SC_municipio__c": "Bogotá D.C.",
                    "canal__c": "Internet",
                    "punto_recepcion": "Web",
                    "Description": "Reclamación por débito duplicado en transacción de envío.",
                    "Product__c": "Wallet",
                    "Categorias_COL__c": "Transferencias erradas o duplicadas",
                    "Favorabilidad__c": "Favorable",
                    "a_favor_de__c": "1",
                    "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
                    "cuerpo_respuesta_final": "<html><body><p>Estimado Don Carlos,</p><p>Se realizó el ajuste técnico por la transferencia duplicada y el caso concluye de forma FAVORABLE.</p></body></html>",
                    "archivos_s3": []
                }
            },
            "cierre_no_favorable_b2b": {
                "summary": "4. Cierre Definitivo No Favorable (B2B)",
                "description": "Respuesta final no favorable para persona jurídica.",
                "value": {
                    "Case_id": "TEST-NOFILE-CIERRE-004",
                    "Smart_Code__c": "TEST-NOFILE-CIERRE-004",
                    "Status": "Closed",
                    "ClosedDate": "2026-07-30",
                    "SuppliedName": "Inversiones y Soluciones Tech SAS",
                    "SC_id_type__c": "NIT",
                    "id_number__c": "901455822",
                    "tipo_de_persona__c": "B2B",
                    "SuppliedPhone": "3008889900",
                    "SuppliedEmail": "contacto@inversiones-tech.test",
                    "direccion__c": "Calle 93B # 13-14 Oficina 401",
                    "Departamento__c": "Bogotá D.C.",
                    "SC_municipio__c": "Bogotá D.C.",
                    "canal__c": "Internet",
                    "punto_recepcion": "Email",
                    "Description": "Solicitud de reliquidación por diferencia en tasa de cambio.",
                    "Product__c": "Transactions",
                    "Categorias_COL__c": "Diferencias en monetización",
                    "Favorabilidad__c": "No favorable",
                    "a_favor_de__c": "2",
                    "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
                    "cuerpo_respuesta_final": "<html><body><p>Validada la transacción corporativa, la tasa aplicada correspondió a la cotización pactada. El caso concluye como NO FAVORABLE.</p></body></html>",
                    "archivos_s3": []
                }
            }
        }
    ),
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