# app/api/routes_quejas.py
import logging
import httpx
from fastapi import APIRouter, Body, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from typing import List, Optional, Tuple

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
from app.core.distributed_lock import RedisLock

from app.db.redis import get_redis_client, ping_redis
from app.services.queue_service import QueueService, DESPACHO_LOCK_PREFIX
from app.core.metrics import emit_emf_metric
from app.core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


def _emitir_metrica_despacho(operacion_inferida: str, resultado: str, categoria_error: str = "N/A") -> None:
    """
    Métrica EMF de volumen del camino síncrono de despacho, por resultado --
    complementa (no sustituye) las métricas de cola que ya emite scheduler.py.
    `operacion_inferida` viene de IdempotencyService.infer_operation_type
    (M2_CREATION, M3_UPDATE, M3_CLOSE, M3_FRAUD, M3_FRAUD_AND_CLOSE) -- baja
    cardinalidad, ya calculada en otro lado del repo para el mismo propósito de
    clasificación. Se parte en 'momento' (M2/M3) y 'operacion' (creation/update/
    close/fraud/...) para poder filtrar por cualquiera de los dos en el dashboard.
    """
    partes = operacion_inferida.split("_", 1)
    momento = partes[0] if partes else "N/A"
    operacion = partes[1].lower() if len(partes) > 1 else "n/a"

    emit_emf_metric(
        namespace="SSV/RoutesQuejas",
        metrics={"dispatch_count": (1, "Count")},
        dimensions={
            "Environment": settings.ENVIRONMENT,
            "momento": momento,
            "operacion": operacion,
            "resultado": resultado,
            "categoria_error": categoria_error
        }
    )

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
    status.HTTP_409_CONFLICT: {
        "description": "⚠️ **Conflicto de Operación en Cola**: ya existe una operación pendiente de una categoría distinta (ej. fraude) para este mismo `Smart_Code__c`. Reintente más tarde.",
        "content": {
            "application/json": {
                "example": {
                    "status_code": 409,
                    "error_type": "QUEUE_OPERATION_CONFLICT",
                    "sfc_field": None,
                    "raw_message": "Ya existe una operación 'M3_FRAUD' pendiente en cola para el caso 1286SEQ_20260804_0001, distinta de la entrante ('M3_UPDATE').",
                    "crm_action_friendly": "Reintente esta operación más tarde, una vez se procese la operación distinta que ya está pendiente para este mismo caso."
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


def _construir_respuesta_idempotente(respuesta_idempotente: dict) -> JSONResponse:
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


def _motivo_lock_no_adquirido(despacho_lock: RedisLock, smart_code: str) -> Tuple[str, str, str]:
    """
    🔴 FIX (hallazgo de revisión, 2026-08-26): acquire()==False significaba tanto
    "lock ocupado" como "Redis falló al preguntar" -- distinguirlos vía
    `redis_error` para no afirmar "otra operación en curso" cuando en realidad
    Redis es el que está degradado (mensaje/categoría de métrica engañosos justo
    durante un incidente de infraestructura). Retorna
    (error_origen_titulo, error_detalle, categoria_error).
    """
    if despacho_lock.redis_error:
        return (
            "Fallo al verificar el lock de despacho (Redis)",
            f"No se pudo verificar el lock de despacho para el Smart_Code__c "
            f"{smart_code}: Redis no disponible.",
            "DESPACHO_LOCK_REDIS_ERROR"
        )
    return (
        "Despacho concurrente para el mismo caso",
        f"Ya hay otra operación en curso para el Smart_Code__c {smart_code}.",
        "CONCURRENT_DISPATCH_LOCKED"
    )


async def _encolar_despacho_por_contingencia(
    payload: QuejaUnificadaCrmInput,
    raw_payload: dict,
    idempotency_service: IdempotencyService,
    error_origen_titulo: str,
    error_detalle: str
) -> Tuple[JSONResponse, bool]:
    """
    🛠️ Manejo de Contingencia y Protección de Doble Falla (SFC + Redis).
    Retorna (response, operacion_exitosa_o_encolada).
    """
    logger.warning(f"⚠️ SFC no disponible ({error_origen_titulo}). Guardando caso {payload.Smart_Code__c} en cola Redis centralizada.")

    try:
        # 🔴 FIX (hallazgo de revisión externa, 2026-08-25): `encolar_despacho` guardaba
        # como payload_json de la cola un `payload.model_dump()` recalculado AQUÍ -- es
        # decir, DESPUÉS de que `orquestador.procesar_despacho` mutara `payload.archivos_s3`
        # in-place al resolver `directorio_s3`. Un reintento genuino del CRM (mismo request
        # original) siempre calcula su propio `raw_payload` ANTES de cualquier mutación --
        # nunca trae `archivos_s3` resuelto, porque esa resolución es un efecto interno de
        # ESTE servicio. `_item_de_cola_sigue_vigente` recalcula el hash del payload_json
        # ACTUAL del item de cola y lo compara contra el hash del reintento entrante -- con
        # el item guardado en su versión post-mutación, esos dos hashes NUNCA coinciden
        # para ningún caso con `directorio_s3`, así que el registro QUEUED se trataba
        # siempre como huérfano, anulando la barrera anti-duplicado de P0-12 justo para ese
        # subconjunto de casos. Se usa `raw_payload` (el mismo snapshot pre-mutación que ya
        # usa registrar_encolado, y que un reintento futuro volverá a producir) también para
        # el payload_json que se guarda en la cola -- consecuencia correcta y menor: el
        # scheduler vuelve a resolver `directorio_s3` en cada intento real en vez de reusar
        # un listado de S3 potencialmente desactualizado desde el primer intento fallido.
        queue_service = QueueService(get_redis_client())
        item_encolado = await queue_service.encolar_despacho(
            smart_code=payload.Smart_Code__c,
            tipo_operacion="AUTO",
            payload_json=raw_payload,
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

        if getattr(item_encolado, "es_duplicado", False):
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "status": "already_queued",
                    "smart_code": payload.Smart_Code__c,
                    "message": "El caso ya se encuentra encolado en Redis pendiente de reintento. Se actualizó la información con la última versión recibida.",
                    "error_origen": error_detalle
                }
            ), True

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "queued",
                "smart_code": payload.Smart_Code__c,
                "message": "La Superintendencia no se encuentra disponible en este momento. El caso ha sido encolado para reintento automático.",
                "error_origen": error_detalle
            }
        ), True
    except SfcIntegrationException as conflict_err:
        # 🔴 FIX (hallazgo de revisión, 2026-08-26): un conflicto de operación
        # (QueueService.encolar_despacho rechazando la sobrescritura de una operación
        # pendiente de categoría distinta -- ver ENQUEUE_LUA_SCRIPT) NO es una falla de
        # infraestructura -- no debe caer en el except genérico de abajo, que lo
        # reportaría como "fallo doble SFC + Redis" (ambos genuinamente caídos), una
        # categorización falsa que dispararía la alerta crítica equivocada.
        if conflict_err.error_type == "QUEUE_OPERATION_CONFLICT":
            return JSONResponse(
                status_code=conflict_err.status_code,
                content={
                    "status_code": conflict_err.status_code,
                    "error_type": conflict_err.error_type,
                    "sfc_field": conflict_err.sfc_field,
                    "raw_message": conflict_err.raw_message,
                    "crm_action_friendly": conflict_err.crm_action
                }
            ), False
        raise
    except Exception as redis_err:
        logger.critical(
            f"🔥 [CRÍTICO] Fallo doble de infraestructura para caso {payload.Smart_Code__c}: "
            f"SFC Unreachable ({error_detalle}) | Redis Unreachable ({redis_err})"
        )
        await EmailAlertService.notificar_falla_infraestructura(
            smart_code=payload.Smart_Code__c,
            error_msg=f"FALLA CRÍTICA DOBLE (SFC + REDIS): SFC Error: {error_detalle} | Redis Error: {str(redis_err)}",
            categoria="fallo_doble_sfc_y_redis"
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
        ), False


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

    raw_payload = payload.model_dump(by_alias=True, mode="json")
    body_clean = sanitizar_payload(raw_payload)

    # 🔴 FIX (hallazgo de revisión externa, 2026-08-25): `extra={...}` plano no llega a
    # ningún lado en producción -- JSONFormatter.format sólo lee `record.extra_data`
    # (ver app/core/logging_config.py), y logging.Logger.info() con `extra=` pone cada
    # clave del dict directamente como atributo del LogRecord (record.direction,
    # record.headers, etc.), no bajo `record.extra_data`. El log de auditoría de
    # entrada del CRM -- el único rastro del lado de entrada de toda la cadena de
    # auditoría regulatoria -- aparecía en CloudWatch sin ningún dato estructurado.
    # Mismo patrón correcto que ya usa sfc_client.py (log_request/log_response).
    logger.info(
        "AUDIT_HTTP_INCOMING_REQUEST_FROM_CRM",
        extra={
            "extra_data": {
                "direction": "INCOMING_REQUEST",
                "method": request.method,
                "path": request.url.path,
                "headers": headers_clean,
                "body": body_clean
            }
        }
    )

    operacion_inferida = IdempotencyService.infer_operation_type(raw_payload)

    # 1. 🛡️ VERIFICACIÓN EN IDEMPOTENCY STORE
    es_hit, respuesta_idempotente = await idempotency_service.verificar_o_iniciar_operacion(
        smart_code=payload.Smart_Code__c,
        payload_dict=raw_payload
    )

    if es_hit:
        _emitir_metrica_despacho(operacion_inferida, resultado=respuesta_idempotente.get("status", "idempotent_hit"))
        return _construir_respuesta_idempotente(respuesta_idempotente)

    logger.info(f"Petición unificada de despacho recibida para el caso: {payload.Smart_Code__c} [CID: {cid}]")

    # 🟢 Bandera de control para evitar liberar idempotencia si la operación culmina o se encola correctamente
    operacion_exitosa_o_encolada = False

    # 2. 🔒 LOCK POR CASO (hallazgo E, revisión externa v5): la verificación de
    # idempotencia de arriba es por smart_code+operacion+hash -- dos payloads
    # DISTINTOS para el mismo Smart_Code__c (ej. un trámite y un cierre concurrentes)
    # no se bloquean entre sí y llegarían a la SFC en paralelo, sin ningún orden
    # garantizado. Este lock serializa el despacho síncrono por caso; si ya hay una
    # operación en vuelo para este smart_code, este evento se encola en vez de
    # competir por la SFC -- reutiliza la misma cola de contingencia que ya maneja
    # correctamente el orden (un slot por smart_code, sobrescritura por versión,
    # cancelación consciente de la categoría de operación, ver hallazgo N1).
    despacho_lock = RedisLock(
        redis_client=redis_client,
        lock_key=f"{DESPACHO_LOCK_PREFIX}:lock:{payload.Smart_Code__c}",
        lease_segundos=180,
        intervalo_heartbeat=45
    )

    if not await despacho_lock.acquire():
        error_origen_titulo, error_detalle, categoria_error = _motivo_lock_no_adquirido(despacho_lock, payload.Smart_Code__c)

        respuesta, operacion_exitosa_o_encolada = await _encolar_despacho_por_contingencia(
            payload, raw_payload, idempotency_service,
            error_origen_titulo=error_origen_titulo,
            error_detalle=error_detalle
        )
        _emitir_metrica_despacho(operacion_inferida, resultado="queued", categoria_error=categoria_error)
        return respuesta

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
            _emitir_metrica_despacho(operacion_inferida, resultado="error", categoria_error="DESPACHO_ORCHESTRATION_ERROR")
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
                error_msg=f"Persistencia de idempotencia post-SFC fallida (riesgo de duplicado): {persist_err}",
                categoria="riesgo_duplicado_post_sfc"
            )

        # 🟢 FIX (hallazgo de code review, 2026-08-25): si un intento ANTERIOR de este
        # mismo smart_code había fallado y quedó encolado en la cola de contingencia, y
        # este despacho síncrono con contenido más reciente sí tuvo éxito, ese item viejo
        # ahora contiene datos obsoletos -- se cancela para que el scheduler no lo
        # reenvíe a la SFC y pise en silencio lo que este despacho ya corrigió.
        # QueueService.cancelar_pendiente_por_smart_code ya es best-effort y no lanza
        # por diseño; se envuelve igual (mismo criterio que registrar_exito arriba) como
        # defensa en profundidad, para que ningún fallo inesperado en este paso de
        # limpieza posterior pueda convertir un despacho ya exitoso en un error 500.
        #
        # 🔴 FIX (hallazgo N1, revisión externa v5, 2026-08-25): se pasa la operación
        # inferida de ESTE despacho (ya calculada arriba para la métrica EMF) para que
        # cancelar_pendiente_por_smart_code sólo borre el item pendiente si es la MISMA
        # categoría de operación -- nunca una obligación regulatoria distinta (ej. un
        # trámite exitoso ya no puede borrar un fraude que seguía genuinamente pendiente).
        try:
            await QueueService(redis_client).cancelar_pendiente_por_smart_code(
                payload.Smart_Code__c, operacion_actual=operacion_inferida
            )
        except Exception as cleanup_err:
            logger.warning(
                f"⚠️ [Cola Redis] No se pudo verificar/cancelar un item de cola obsoleto para "
                f"{payload.Smart_Code__c} tras el despacho síncrono exitoso: {cleanup_err}"
            )

        operacion_exitosa_o_encolada = True
        _emitir_metrica_despacho(operacion_inferida, resultado="success")

        return resultado

    except SfcIntegrationException as exc:
        # 🔴 FIX (hallazgo de revisión externa, 2026-08-25): la clasificación vive ahora
        # en SfcIntegrationException.es_transitoria -- única fuente de verdad,
        # reutilizada también por scheduler.py::_es_falla_infraestructura.
        if exc.es_transitoria:
            respuesta, operacion_exitosa_o_encolada = await _encolar_despacho_por_contingencia(
                payload, raw_payload, idempotency_service,
                error_origen_titulo=f"SFC Exception ({exc.status_code})",
                error_detalle=exc.raw_message or str(exc)
            )
            _emitir_metrica_despacho(operacion_inferida, resultado="queued", categoria_error=exc.error_type)
            return respuesta

        logger.warning(f"Error controlado de validación de la SFC durante el despacho: {exc.raw_message}")
        _emitir_metrica_despacho(operacion_inferida, resultado="error", categoria_error=exc.error_type)
        raise

    except (httpx.RequestError, httpx.TimeoutException, ConnectionError) as net_err:
        respuesta, operacion_exitosa_o_encolada = await _encolar_despacho_por_contingencia(
            payload, raw_payload, idempotency_service,
            error_origen_titulo="Network Error",
            error_detalle=str(net_err)
        )
        _emitir_metrica_despacho(operacion_inferida, resultado="queued", categoria_error="NETWORK_ERROR")
        return respuesta

    finally:
        # 🔓 Libera el lock por caso adquirido arriba, sin importar cómo haya terminado
        # el despacho -- de lo contrario un caso quedaría bloqueado hasta que expire el
        # lease (180s) tras cualquier error no contemplado explícitamente más arriba.
        await despacho_lock.release()

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


@router.post(
    "/queue/{registro_id}/reencolar",
    status_code=status.HTTP_200_OK,
    summary="Reencolar manualmente un caso en FALLIDO_DEFINITIVO (DLQ) para reintento",
    description=(
        "Hallazgo C2 (revisión externa v5): tooling administrativo interno -- no requiere "
        "ningún cambio del lado del CRM. Se niega con 409 si el item no está en "
        "FALLIDO_DEFINITIVO, o si ya existe un item más nuevo pendiente para el mismo "
        "Smart_Code__c (reencolar el viejo en ese caso rompería la invariante de "
        "'un smart_code = un slot en cola')."
    ),
    dependencies=[Depends(verificar_api_key_admin)]
)
async def reencolar_registro_fallido(registro_id: int):
    queue_service = QueueService(get_redis_client())
    resultado = await queue_service.reencolar_item_fallido(registro_id)

    if not resultado.get("success"):
        codigo = status.HTTP_404_NOT_FOUND if resultado.get("reason") == "item_not_found" else status.HTTP_409_CONFLICT
        return JSONResponse(status_code=codigo, content=resultado)

    return resultado


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