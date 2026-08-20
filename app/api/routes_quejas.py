# app/api/routes_quejas.py
import json
import logging
import httpx
from fastapi import APIRouter, Body, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from typing import List, Optional

from app.api.dependencies import (
    get_sfc_client, 
    get_s3_client, 
    verificar_api_key_crm,
    verificar_api_key_admin
)
from app.integrations.sfc_client import SfcClient
from app.services.email_service import EmailAlertService
from app.services.momento_1_sync import SincronizacionService
from app.services.despacho_queja_orchestrator import DespachoQuejaOrquestador
from app.services.momento_4_sync import UserSync
from app.core.exceptions import SfcIntegrationException
from app.core.middleware import get_correlation_id
from app.core.security.sanitizer import sanitizar_headers, sanitizar_payload
from app.schemas.crm_payloads import (
    ConfirmacionAckUsuariosInput,
    QuejaMapeadaCrmResponse, 
    QuejaUnificadaCrmInput,
    ConfirmacionAckInput
)
from app.services.idempotency_service import IdempotencyService

from app.db.redis import get_redis_client, ping_redis
from app.services.queue_service import QueueService

router = APIRouter()
logger = logging.getLogger(__name__)

RESPUESTAS_DESPACHO_OPENAPI = {
    status.HTTP_200_OK: {
        "description": "✅ **Procesamiento Síncrono Exitoso**: La queja o actualización fue recibida y aceptada directamente por la SFC.",
        "content": {
            "application/json": {
                "example": {
                    "status": "success",
                    "message": "Caso 1286SEQ_20260804_0001 actualizado en M3 (Estado SFC 4)",
                    "codigo_queja_sfc": "1286SEQ_20260804_0001"
                }
            }
        }
    },
    status.HTTP_202_ACCEPTED: {
        "description": "📦 **Contingencia por Saturación / Latencia**: La SFC está lenta (>3s) o agotó su cuota (HTTP 429). El caso fue guardado en la cola de Redis para reintento automático.",
        "content": {
            "application/json": {
                "example": {
                    "status": "queued",
                    "smart_code": "1286SEQ_20260804_0001",
                    "message": "La Superintendencia no se encuentra disponible en este momento. El caso ha sido encolado para reintento automático.",
                    "error_origen": "RESOURCE_EXHAUSTED: Quota exceeded for quota metric 'Read Requests' per minute"
                }
            }
        }
    },
    status.HTTP_400_BAD_REQUEST: {
        "description": "🛡️ **Error de Validación de Entrada o Regla de Negocio**: Payload inválido (catálogo, tipo de dato, cierres inconsistentes) o rechazado por la SFC.",
        "content": {
            "application/json": {
                "example": {
                    "status_code": 400,
                    "error_type": "CRM_PAYLOAD_VALIDATION_ERROR",
                    "sfc_field": "id_number__c",
                    "raw_message": "String should have at most 15 characters",
                    "crm_action_friendly": "Verifique el campo 'id_number__c' en Salesforce. Asegúrese de cumplir con la longitud permitida."
                }
            }
        }
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": "🚨 **Falla Crítica Doble de Infraestructura**: Tanto la SFC como la cola centralizada de Redis están inalcanzables.",
        "content": {
            "application/json": {
                "example": {
                    "status_code": 503,
                    "error_type": "CRITICAL_INFRASTRUCTURE_FAILURE",
                    "sfc_field": None,
                    "raw_message": "Tanto la Superintendencia como la cola de contingencia local están temporalmente no disponibles.",
                    "crm_action_friendly": "Reintente la operación en unos minutos. El incidente ha sido notificado automáticamente al equipo de ingeniería."
                }
            }
        }
    }
}

# ======================================================================
# 📥 MOMENTO 1: Sincronización y ACK (SFC -> CRM)
# ======================================================================
@router.get(
    "/sync/momento-1", 
    status_code=status.HTTP_200_OK, 
    summary="Obtener Quejas Nuevas de la SFC y procesar adjuntos a S3",
    response_model=List[QuejaMapeadaCrmResponse],
    dependencies=[Depends(verificar_api_key_crm)]
)
async def ejecutar_sync_momento_1(
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    servicio = SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
    return await servicio.ejecutar_flujo_completo_momento_1()


@router.post(
    "/sync/momento-1/ack",
    status_code=status.HTTP_200_OK,
    summary="Confirmar recepción exitosa de quejas (ACK) a la SFC",
    dependencies=[Depends(verificar_api_key_crm)]
)
async def confirmar_ack_momento_1(
    payload: ConfirmacionAckInput,
    sfc_client: SfcClient = Depends(get_sfc_client)
):
    servicio = SincronizacionService(sfc_client=sfc_client)
    return await servicio.confirmar_recepcion_ack(ids_quejas=payload.ids_quejas)


# ======================================================================
# 📤 ENDPOINT UNIFICADO DE DESPACHO (CRM -> SFC) [MOMENTO 2 & MOMENTO 3]
# ======================================================================
@router.post(
    "/sync/despacho",
    status_code=status.HTTP_200_OK,
    responses=RESPUESTAS_DESPACHO_OPENAPI,
    summary="Trigger Unificado de Despacho con Cola Centralizada Redis",
    dependencies=[Depends(verificar_api_key_crm)],
    description="""
        ### 🚀 Orquestador de Despacho Unificado Stateless

        Este endpoint actúa como la **puerta de entrada principal** para las transmisiones desde Salesforce CRM hacia la Superintendencia Financiera de Colombia (SFC).

        #### 🛠️ Comportamiento del Sistema:
        1. **Sanitización y Validación Local (< 40ms):**
        * Previene ataques Stored XSS en campos libres (`Description`, `SuppliedName`).
        * Valida catálogos normativos (DIVIPOLA, Categorías SFC) y restricciones de negocio.
        2. **Inferencia Automática de Fase:**
        * **Momento 2 (Alta Nueva):** Activado para casos nuevos sin trámite previo ni fraude.
        * **Momento 3 (Trámite / Fraude / Cierre):** Activado para actualizaciones de estado, reportes de investigación de fraude o emision de respuestas finales (PDF).
        3. **Mecanismo de Resiliencia y Contingencia (HTTP 202):**
        * Si la SFC no responde en $<3\text{ segundos}$ o agota la cuota (`429`), el microservicio **encola el registro en Redis** y responde **202 Accepted** instantáneamente.
        4. **Mecanismo de Auto-Recuperación (Self-Healing):**
        * Si la SFC retorna un error `404 Not Found` al intentar actualizar un caso, el sistema radicará automáticamente la queja base en Momento 2 y completará el trámite de M3 en secuencia.
            """
)
async def despachar_queja_crm(
    request: Request,
    payload: QuejaUnificadaCrmInput = Body(...),
    sfc_client: SfcClient = Depends(get_sfc_client),
    s3_client = Depends(get_s3_client)
):
    cid = get_correlation_id()
    redis_client = get_redis_client()
    idempotency_service = IdempotencyService(redis_client=redis_client, ttl_days=30)

    # 🛠️ AUDITORÍA HTTP: Petición Entrante recibida desde Salesforce/CRM
    headers_clean = sanitizar_headers(dict(request.headers))
    headers_formatted = "\n".join([f"   {k}: {v}" for k, v in headers_clean.items()])
    
    raw_payload = payload.model_dump(by_alias=True, mode="json")
    body_clean = sanitizar_payload(raw_payload)
    body_str = json.dumps(body_clean, ensure_ascii=False)

    logger.info(
    "AUDIT_HTTP_INCOMING_REQUEST_FROM_CRM",
    extra={
        "direction": "INCOMING_REQUEST",
        "method": request.method,
        "path": request.url.path,
        "headers": headers_clean,
        "body": body_clean
    }
)
    
    # 1. 🛡️ VERIFICACIÓN EN IDEMPOTENCY STORE
    es_hit, respuesta_idempotente = await idempotency_service.verificar_o_iniciar_operacion(
        smart_code=payload.Smart_Code__c,
        payload_dict=raw_payload
    )
    
    if es_hit:
        status_hit = respuesta_idempotente.get("status")
        
        # 🚨 CASE CRÍTICO: Caída de Redis (Fail-Closed)
        if status_hit == "redis_unavailable":
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status_code": 503,
                    "error_type": "IDEMPOTENCY_STORE_UNAVAILABLE",
                    "sfc_field": None,
                    "raw_message": respuesta_idempotente.get("message"),
                    "crm_action_friendly": "El almacén de idempotencia no está disponible. Reintente en unos minutos."
                }
            )
        
        # 🎯 CASE A: Happy Path duplicado (Respuesta 200 OK previa)
        if status_hit == "success":
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                headers={"X-Idempotent-Hit": "true"},
                content=respuesta_idempotente.get("sfc_response") or respuesta_idempotente
            )
        
        # 🎯 CASE B: Operación ya en proceso o encolada (202 Accepted)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            headers={"X-Idempotent-Hit": "true"},
            content=respuesta_idempotente
        )

    logger.info(f"Petición unificada de despacho recibida para el caso: {payload.Smart_Code__c} [CID: {cid}]")

    # 🟢 Bandera de control para evitar liberar idempotencia si la operación culmina o se encola correctamente
    operacion_exitosa_o_encolada = False

    # 🛠️ Helper Interno para Manejo de Contingencia y Protección de Doble Falla (SFC + Redis)
    async def _intentar_encolar_y_responder(error_origen_titulo: str, error_detalle: str):
        nonlocal operacion_exitosa_o_encolada
        logger.warning(f"⚠️ SFC no disponible ({error_origen_titulo}). Guardando caso {payload.Smart_Code__c} en cola Redis centralizada.")
        
        try:
            queue_service = QueueService(get_redis_client())
            item_encolado = await queue_service.encolar_despacho(
                smart_code=payload.Smart_Code__c,
                tipo_operacion="AUTO",
                payload_json=payload.model_dump(by_alias=True, mode="json"),
                error_inicial=error_detalle
            )
            
            # 📌 REGISTRAR EN IDEMPOTENCY STORE COMO QUEUED
            # 🟢 FIX P0-12: se pasa el id real del item de cola para poder detectar más
            # adelante si este registro de idempotencia quedó huérfano (item sobrescrito
            # por un evento más nuevo del mismo smart_code).
            await idempotency_service.registrar_encolado(
                smart_code=payload.Smart_Code__c,
                payload_dict=raw_payload,
                error_msg=error_detalle,
                registro_id=item_encolado.id
            )
            operacion_exitosa_o_encolada = True
            
            if getattr(item_encolado, "es_duplicado", False):
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content={
                        "status": "already_queued",
                        "smart_code": payload.Smart_Code__c,
                        "message": "El caso ya se encuentra encolado en Redis pendiente de reintento. Se actualizó la información con la última versión recibida.",
                        "error_origen": error_detalle
                    }
                )
            
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "status": "queued",
                    "smart_code": payload.Smart_Code__c,
                    "message": "La Superintendencia no se encuentra disponible en este momento. El caso ha sido encolado para reintento automático.",
                    "error_origen": error_detalle
                }
            )
        except Exception as redis_err:
            logger.critical(
                f"🔥 [CRÍTICO] Fallo doble de infraestructura para caso {payload.Smart_Code__c}: "
                f"SFC Unreachable ({error_detalle}) | Redis Unreachable ({redis_err})"
            )
            await EmailAlertService.notificar_falla_infraestructura(
                smart_code=payload.Smart_Code__c,
                error_msg=f"FALLA CRÍTICA DOBLE (SFC + REDIS): SFC Error: {error_detalle} | Redis Error: {str(redis_err)}"
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status_code": 503,
                    "error_type": "CRITICAL_INFRASTRUCTURE_FAILURE",
                    "sfc_field": None,
                    "raw_message": "Tanto la Superintendencia como la cola de contingencia local están temporalmente no disponibles.",
                    "crm_action_friendly": "Reintente la operación en unos minutos. El incidente ha sido notificado automáticamente al equipo de ingeniería."
                }
            )
    
    try:
        orquestador = DespachoQuejaOrquestador(sfc_client=sfc_client, s3_client=s3_client)
        # 🟢 FIX P1-13: el candado PROCESSING de idempotencia tenía un TTL fijo de 3
        # minutos sin renovación; si el despacho real tardaba más, un reintento del
        # mismo payload durante esa ventana ya no lo veía "processing" y disparaba un
        # segundo envío concurrente a la SFC. Se mantiene vivo el candado mientras dura
        # la llamada real.
        async with idempotency_service.mantener_processing_vivo(payload.Smart_Code__c, raw_payload):
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
            
        # 📌 REGISTRAR ÉXITO EN IDEMPOTENCY STORE (Camino Exitoso Síncrono)
        # 🟢 FIX P0-06: SFC ya proceso exitosamente el caso en este punto. Si SÓLO falla la
        # persistencia del registro de idempotencia, esto NO debe convertirse en un 500 para
        # el CRM: el bloque `finally` de abajo liberaría la llave (por `operacion_exitosa_o_
        # encolada` seguir en False) y un reintento del CRM ante ese 500, sin idempotencia
        # activa, volvería a llegar a la SFC — exactamente el duplicado que se quiere evitar.
        # Se alerta como falla crítica de infraestructura en su lugar, sin alterar la
        # respuesta exitosa que sí corresponde a lo que realmente ocurrió en la SFC.
        try:
            await idempotency_service.registrar_exito(
                smart_code=payload.Smart_Code__c,
                payload_dict=raw_payload,
                sfc_response=resultado
            )
        except Exception as persist_err:
            logger.critical(
                f"🔥 [Idempotency] SFC procesó exitosamente el caso {payload.Smart_Code__c} pero no fue "
                f"posible persistir el registro de idempotencia: {persist_err}. "
                f"Ventana de riesgo ante un duplicado inmediato del mismo request."
            )
            await EmailAlertService.notificar_falla_infraestructura(
                smart_code=payload.Smart_Code__c,
                error_msg=f"Persistencia de idempotencia post-SFC fallida (riesgo de duplicado): {persist_err}"
            )

        operacion_exitosa_o_encolada = True

        return resultado

    except SfcIntegrationException as exc:
        es_error_contingencia = (
            exc.status_code >= 500 or
            exc.status_code in (429, 503) or
            exc.error_type in [
                "SERVER_ERROR", "SFC_DOWN", "TIMEOUT", "NETWORK_ERROR",
                "INFRASTRUCTURE_ERROR", "THROTTLED_ERROR", "RATE_LIMIT_ERROR", "RESOURCE_EXHAUSTED"
            ]
        )

        if es_error_contingencia:
            return await _intentar_encolar_y_responder(
                error_origen_titulo=f"SFC Exception ({exc.status_code})",
                error_detalle=exc.raw_message or str(exc)
            )

        logger.warning(f"Error controlado de validación de la SFC durante el despacho: {exc.raw_message}")
        raise

    except (httpx.RequestError, httpx.TimeoutException, ConnectionError) as net_err:
        return await _intentar_encolar_y_responder(
            error_origen_titulo="Network Error",
            error_detalle=str(net_err)
        )

    finally:
        # 🛡️ GARANTÍA DE LIBERACIÓN: Si la operación no se completó exitosamente ni fue encolada
        # (por un error 400 de validación de la SFC o cualquier excepción de Pydantic/Python),
        # libera la llave de idempotencia en Redis inmediatamente.
        if not operacion_exitosa_o_encolada:
            await idempotency_service.liberar_operacion_por_error(
                smart_code=payload.Smart_Code__c,
                payload_dict=raw_payload
            )


@router.get(
    "/queue",
    status_code=status.HTTP_200_OK,
    summary="Consultar el estado de la cola de reintentos centralizada (Redis)",
    dependencies=[Depends(verificar_api_key_admin)]
)
async def consultar_cola_local(
    estado: Optional[str] = None
):
    queue_service = QueueService(get_redis_client())
    registros = await queue_service.obtener_todos_los_encolados(estado=estado)

    return [r.to_summary_dict() for r in registros]


@router.get(
    "/_health/ready",
    status_code=status.HTTP_200_OK,
    summary="Readiness de SSV para el smoke funcional post-deploy (ALB compartido con CRM)",
)
async def health_ready_ssv(response: Response):
    """
    Ruta propia dentro del prefijo de SSV (no /health/ready) porque el ALB es
    compartido con otros servicios de CRM Global66: un 200 en un path genérico
    no confirma que la respuesta venga del target group de SSV. El campo
    "servicio" permite al smoke post-deploy detectar un routing equivocado del
    ALB además de la disponibilidad real de Redis.
    """
    redis_ok = await ping_redis()
    if not redis_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"servicio": "SSV", "redis": False}
    return {"servicio": "SSV", "redis": True}


# ======================================================================
# 📥 MOMENTO 4: Actualización y ACK de usuarios
# ======================================================================

@router.get(
    "/sync/momento-4",
    status_code=status.HTTP_200_OK,
    summary="Obtener información actualizada de usuarios desde la SFC",
    dependencies=[Depends(verificar_api_key_crm)]
)
async def actualizar_usuarios(
    sfc_client: SfcClient = Depends(get_sfc_client),
):
    servicio = UserSync(sfc_client=sfc_client)
    return await servicio.sincronizar_usuarios()


@router.post(
    "/sync/momento-4/ack",
    status_code=status.HTTP_200_OK,
    summary="Confirmar recepción exitosa de datos de usuarios (ACK) a la SFC",
    dependencies=[Depends(verificar_api_key_crm)]
)
async def confirmar_ack_momento_4(
    payload: ConfirmacionAckUsuariosInput,
    sfc_client: SfcClient = Depends(get_sfc_client)
):
    servicio = UserSync(sfc_client=sfc_client)
    return await servicio.confirmar_recepcion_ack_usuarios(numeros_id_cf=payload.numeros_id_cf)