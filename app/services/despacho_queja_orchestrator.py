# app/services/despacho_queja_orchestrator.py
import logging
from typing import Dict, Any, Optional
from pydantic import ValidationError

from app.integrations.sfc_client import SfcClient
from app.schemas.crm_payloads import ArchivoS3Schema, QuejaUnificadaCrmInput
from app.services.momento_2_sync import Momento2SincronizacionService
from app.services.momento_3_sync import Momento3SincronizacionService
from app.services.idempotency_service import IdempotencyService
from app.db.redis import get_redis_client
from app.core.exceptions import SfcIntegrationException, resumir_validation_error_sin_pii
from app.core.clasificacion_operacion import es_estado_cierre

logger = logging.getLogger(__name__)


def _es_error_caso_ya_cerrado(exc: Exception) -> bool:
    """
    Evalúa si la SFC rechazó la petición porque la queja YA se encuentra cerrada /
    ya cuenta con documento de respuesta final — un estado terminal que debe tratarse
    como éxito idempotente, no como un error real.

    🟢 FIX P0-08: se retiró el fragmento genérico "respuesta final" (y "diferente de
    (4) cerrado", que no corresponde a ninguna entrada real de la matriz de errores
    SFC). Ese fragmento también coincidía con mensajes de la matriz
    (errores_sfc.json) que significan justo lo contrario — un documento de respuesta
    final FALTANTE, no ya entregado — ej. "documento de respuesta final debe haber
    sido enviado" o "fijado en True". Quedan sólo frases completas que en la matriz
    real significan inequívocamente "la queja ya está cerrada".
    """
    raw_msg = (getattr(exc, "raw_message", "") or str(exc)).lower()
    frases_ya_cerrada = [
        "ya cuenta con un documento de respuesta final",
        "la queja se encuentra con estado cerrado",
        "no se puede actualizar el anexo debido a que la queja se encuentra cerrada",
        "queja ya esta cerrada",
        "already closed"
    ]
    return any(frase in raw_msg for frase in frases_ya_cerrada)


def _resultado_por_cierre_confirmado(smart_code: str, es_cierre: bool) -> Dict[str, Any]:
    """
    🔴 FIX (hallazgo de revisión externa, 2026-08-26, ronda 4 -- W5/V7/X7): antes
    este resultado era SIEMPRE {"status": "success"}, sin importar si el paso que
    la SFC rechazó (por "el caso ya está cerrado") era el CIERRE o el TRÁMITE. Para
    el cierre, "success" es correcto -- el estado final deseado (caso cerrado) ya
    se cumplió, sea porque este request lo cerró o porque otro lo hizo antes. Para
    un TRÁMITE (actualización de gestor/novedades/etc. sobre un caso que ya está
    cerrado), la actualización NUNCA se aplicó -- la SFC la rechazó por completo --
    y reportar "success" le afirma al CRM que esos campos quedaron sincronizados
    cuando en realidad no se tocó nada. Se distingue con status="noop": no es un
    error (no hay nada que reintentar, el caso seguirá cerrado en el próximo
    intento), pero tampoco fue una escritura exitosa.
    """
    if es_cierre:
        return {
            "status": "success",
            "message": f"Caso {smart_code} ya se encuentra cerrado en la SFC (Estado 4).",
            "codigo_queja_sfc": smart_code
        }
    return {
        "status": "noop",
        "message": (
            f"La actualización de trámite para el caso {smart_code} no se aplicó: la SFC "
            f"reporta que el caso ya se encuentra cerrado (Estado 4)."
        ),
        "codigo_queja_sfc": smart_code
    }


async def _ejecutar_paso_o_exito_si_ya_cerrado(coro, smart_code: str, es_cierre: bool) -> Dict[str, Any]:
    """
    Ejecuta un paso de Momento 3 que debe tratar "la SFC ya tiene el caso cerrado"
    como resultado idempotente en vez de propagar el rechazo -- compartido por el
    paso de cierre y el de trámite (hallazgo E, revisión externa v5): sin
    serialización por caso, cualquiera de los dos puede llegarle a la SFC después
    de que otro request para el mismo Smart_Code__c ya cerró el caso. `es_cierre`
    decide si el resultado idempotente es "success" (cierre) o "noop" (trámite --
    ver _resultado_por_cierre_confirmado).
    """
    try:
        return await coro
    except SfcIntegrationException as exc:
        if _es_error_caso_ya_cerrado(exc):
            resultado = _resultado_por_cierre_confirmado(smart_code, es_cierre)
            logger.info(
                f"✅ [Orquestador] El caso {smart_code} ya figuraba como cerrado en SFC. "
                f"Marcando la operación como '{resultado['status']}'."
            )
            return resultado
        raise

async def limpiar_checkpoint_si_cierre_exitoso(
    resultado: Dict[str, Any], smart_code: str, es_cierre: bool, limpiar_checkpoint_en_exito: bool = True
) -> Dict[str, Any]:
    """
    🟡 FIX (hallazgo de revisión externa, 2026-08-25, §6): libera el checkpoint de
    adjuntos (IdempotencyService.limpiar_checkpoint_archivos, antes sin ningún
    caller) apenas el CIERRE del caso se confirma exitoso -- después de esto no hay
    más pasos de Momento 3 esperados para este smart_code, así que no hay razón
    para seguir bloqueando el reenvío de un archivo bajo la misma s3_key hasta que
    expire el TTL de 30 días del checkpoint (ej. una corrección/reemplazo del mismo
    documento si el caso se reabre más adelante). Best-effort: nunca lanza, un
    fallo aquí sólo implica que el checkpoint sigue vivo hasta su propio TTL, no
    pérdida ni corrupción de datos.

    🔴 FIX (hallazgo N2, revisión externa v5, 2026-08-25): `limpiar_checkpoint_en_exito`
    permite que el CALLER decida si este es el momento correcto para limpiar. El
    camino síncrono (routes_quejas.py) no pasa nada -- ahí el 200 al CRM ya es la
    confirmación terminal, no hay ningún paso de persistencia posterior. El camino
    del worker (scheduler.py) pasa False: ahí SIGUE un paso de persistencia durable
    (marcar_sfc_completado) después de este punto, y si ese paso falla, el próximo
    ciclo reejecuta todo Momento 3 -- limpiar el checkpoint acá, antes de esa
    persistencia, retransmitiría TODOS los adjuntos a la SFC por segunda vez.
    scheduler.py limpia por su cuenta, llamando a esta misma función, sólo después
    de confirmar que SFC_DONE quedó persistido.

    🔴 Es una función de MÓDULO (no un método de DespachoQuejaOrquestador) a propósito:
    scheduler.py la importa y llama directamente, independiente de cómo los tests
    mockeen la clase DespachoQuejaOrquestador -- si fuera un método/staticmethod
    accedido como DespachoQuejaOrquestador._algo(...), cualquier test que reemplace
    la clase completa por un MagicMock (patrón ya establecido en test_scheduler_
    retry_job.py) rompería este await en silencio.
    """
    if not limpiar_checkpoint_en_exito:
        return resultado
    if es_cierre and isinstance(resultado, dict) and resultado.get("status") == "success":
        try:
            await IdempotencyService(get_redis_client()).limpiar_checkpoint_archivos(smart_code)
        except Exception as e:
            logger.warning(f"⚠️ [Orquestador] No se pudo limpiar el checkpoint de adjuntos para {smart_code}: {e}")
    return resultado


class DespachoQuejaOrquestador:
    """
    Servicio Stateless que actúa como fachada/orquestador único para la transmisión
    de quejas desde Salesforce hacia la SFC (Momento 2 + Momento 3).
    Incluye lógica de inferencia automática y auto-recuperación (Self-Healing).
    """

    def __init__(
        self,
        sfc_client: SfcClient,
        s3_client=None,
        m2_service: Optional[Momento2SincronizacionService] = None,
        m3_service: Optional[Momento3SincronizacionService] = None
    ):
        self.sfc_client = sfc_client
        self.s3_client = s3_client
        # 🎯 Inyección de Dependencias con Fallback por defecto:
        # Si se pasan instancias (ej. Mocks en tests), usa esas; si vienen en None (en producción),
        # instancia los servicios reales usando sfc_client y s3_client.
        self.m2_service = m2_service or Momento2SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)
        self.m3_service = m3_service or Momento3SincronizacionService(sfc_client=sfc_client, s3_client=s3_client)

    async def procesar_despacho_raw_json(
        self, payload_dict: Dict[str, Any], limpiar_checkpoint_en_exito: bool = True
    ) -> Dict[str, Any]:
        """Rehidrata un diccionario/JSON desde Redis al esquema Pydantic 'QuejaUnificadaCrmInput'."""
        try:
            payload = QuejaUnificadaCrmInput.model_validate(payload_dict)
            return await self.procesar_despacho(
                payload=payload, limpiar_checkpoint_en_exito=limpiar_checkpoint_en_exito
            )
        except ValidationError as ve:
            resumen_sin_pii = resumir_validation_error_sin_pii(ve)
            logger.error(f"[Orquestador] Error de validación Pydantic al rehidratar desde la cola Redis: {resumen_sin_pii}")
            raise SfcIntegrationException(
                status_code=500,
                error_type="REDIS_PAYLOAD_INVALIDO",
                sfc_field=None,
                raw_message=f"Estructura inválida en el payload rehidratado de Redis: {resumen_sin_pii}",
                crm_action="Contactar al equipo de infraestructura: un ítem de la cola quedó con datos corruptos/incompletos."
            ) from ve

    async def procesar_despacho(
        self, payload: QuejaUnificadaCrmInput, limpiar_checkpoint_en_exito: bool = True
    ) -> Dict[str, Any]:
        smart_code = payload.Smart_Code__c
        status_raw = (payload.Status or "").strip().lower()

        # Inspección y listado dinámico desde S3 si viene solo la ruta del directorio
        if payload.directorio_s3 and not payload.archivos_s3:
            s3_service = self.m3_service.s3_service
            case_id_esperado = payload.Case_id or smart_code
            archivos_remotos = await s3_service.listar_archivos_en_directorio(
                prefix=payload.directorio_s3, case_id_esperado=case_id_esperado
            )
            
            if archivos_remotos:
                logger.info(f"📂 [Orquestador] Encontrados {len(archivos_remotos)} archivos en directorio '{payload.directorio_s3}'")
                payload.archivos_s3 = [
                    ArchivoS3Schema(**a) for a in archivos_remotos
                ]
            else:
                logger.warning(f"⚠️ [Orquestador] No se encontraron archivos en el directorio S3 '{payload.directorio_s3}'")    
        
        es_cierre = es_estado_cierre(payload.Status, payload.ClosedDate, payload.Favorabilidad__c, payload.Aceptacion__c)
        es_fraude = (
            payload.tipo_fraude__c is not None or 
            payload.modalidad_fraude__c is not None
        )
        
        if es_fraude and not payload.archivos_s3:
            raise SfcIntegrationException(
                status_code=400,
                error_type="CRM_PAYLOAD_VALIDATION_ERROR",
                sfc_field="archivos_s3",
                raw_message="No se encontraron documentos de investigación de fraude (INV_FRAUDE_SFC) en S3.",
                crm_action="Asegúrese de cargar los documentos de soporte de la investigación de fraude en S3 antes de enviar el caso."
            )
        
        tiene_campos_m3 = any([
            payload.sc_genero__c is not None,
            payload.sc_LGBTIQ__c is not None,
            payload.sc_Condicion_especial__c is not None,
            payload.producto_digital__c is not None,
            payload.admision_col__c != "No Aplica"
        ])
        
        es_m2_puro = (status_raw in ("new", "nuevo")) and not tiene_campos_m3 and not es_fraude and not es_cierre

        logger.info(
            f"[Orquestador] Procesando solicitud para el caso {smart_code} "
            f"(Es Cierre: {es_cierre}, Es Fraude: {es_fraude}, Es M2 Puro: {es_m2_puro})"
        )

        try:
            # 1. Caso de Alta Nueva Puro (Creación inicial vía Momento 2)
            if es_m2_puro:
                logger.info(f"[Orquestador] Ejecutando despacho directo de QUEJA NUEVA (M2) para {smart_code}")
                return await self.m2_service.ejecutar_envio_momento_2(payload=payload)

            # 2. Intento de Momento 3 (Trámite, Fraude o Cierre)
            # Si el caso no existe en la SFC, saltará el 404 y el bloque except ejecutará el Self-Healing (M2 -> M3)
            resultado_m3 = await self._ejecutar_pasos_momento_3(payload, es_fraude=es_fraude, es_cierre=es_cierre)
            return await limpiar_checkpoint_si_cierre_exitoso(
                resultado_m3, smart_code, es_cierre, limpiar_checkpoint_en_exito
            )

        except SfcIntegrationException as exc:
            error_tipo = getattr(exc, "error_type", None)
            is_unmapped = getattr(exc, "is_unmapped", False) or error_tipo == "UNKNOWN_SFC_ERROR"

            # 🚨 AUTO-RECUPERACIÓN (SELF-HEALING): 404 Estándar o NOT_FOUND_ERROR estructurado.
            # 🟢 FIX P0-09: se retiraron los fallbacks de texto libre ("404" in raw_msg,
            # "no encontrado" in raw_msg). error_type ya lo asigna de forma confiable
            # SfcErrorTranslator a partir de la matriz curada (subcadenas "not found",
            # "no existe", "does not exist"), pensada específicamente para "la queja no
            # existe en la SFC". Un error de catálogo/mapeo (ej. "producto no encontrado
            # en catálogo") no debe disparar la recreación M2 sólo por compartir esa
            # subcadena en el mensaje.
            es_caso_no_encontrado = (
                exc.status_code == 404 or
                error_tipo == "NOT_FOUND_ERROR"
            )

            if es_caso_no_encontrado:
                logger.warning(
                    f"⚠️ [Orquestador] La SFC indica que la queja {smart_code} NO existe en su BD. "
                    f"Iniciando secuencia de auto-recuperación (Momento 2 -> Momento 3)..."
                )

                # Paso A: Crear la queja base vía Momento 2 OMITIENDO adjuntos para que M3 los transmita con afijo
                logger.info(f"[Auto-Recuperación 1/2] Radicando queja base vía Momento 2 para {smart_code}...")
                
                payload_m2_sin_adjuntos = payload.model_copy()
                payload_m2_sin_adjuntos.archivos_s3 = []
                payload_m2_sin_adjuntos.directorio_s3 = None
                
                res_m2 = await self.m2_service.ejecutar_envio_momento_2(payload=payload_m2_sin_adjuntos)

                if res_m2.get("status") != "success":
                    return res_m2

                # Paso B: Aplicar la actualización de Momento 3 (procesa adjuntos con sus afijos normativos)
                logger.info(f"[Auto-Recuperación 2/2] Re-ejecutando pipeline de Momento 3 para {smart_code}...")
                resultado_m3_healing = await self._ejecutar_pasos_momento_3(payload, es_fraude=es_fraude, es_cierre=es_cierre)
                return await limpiar_checkpoint_si_cierre_exitoso(
                    resultado_m3_healing, smart_code, es_cierre, limpiar_checkpoint_en_exito
                )
            
            # ⚠️ EVALUACIÓN DE ERROR NO MAPEADO
            if is_unmapped:
                logger.warning(f"⚠️ [Orquestador] Se detectó un error no mapeado en SFC para el caso {smart_code}.")
                raise 

            raise

    async def _ejecutar_pasos_momento_3(
        self, 
        payload: QuejaUnificadaCrmInput, 
        es_fraude: bool, 
        es_cierre: bool
    ) -> Dict[str, Any]:
        resultado = {}

        if es_fraude:
            logger.info(f"[Momento 3 Pipeline] Transmitiendo gestión de FRAUDE para {payload.Smart_Code__c}...")
            try:
                resultado = await self.m3_service.ejecutar_gestion_fraude(payload=payload)
                if isinstance(resultado, dict) and resultado.get("status") == "error":
                    return resultado
            except SfcIntegrationException as exc:
                # 🛡️ CAPTURA DE ERROR SI EL CASO YA TIENE RESPUESTA FINAL Y VIENE UN CIERRE
                if es_cierre and _es_error_caso_ya_cerrado(exc):
                    logger.warning(
                        f"⚠️ [Orquestador] El caso {payload.Smart_Code__c} ya cuenta con respuesta final/está cerrado en SFC. "
                        f"Omitiendo la falla intermedia de Fraude y procediendo con el Cierre..."
                    )
                else:
                    raise

        if es_cierre:
            logger.info(f"[Momento 3 Pipeline] Transmitiendo CIERRE DEFINITIVO para {payload.Smart_Code__c}...")
            resultado = await _ejecutar_paso_o_exito_si_ya_cerrado(
                self.m3_service.ejecutar_cierre_definitivo(payload=payload), payload.Smart_Code__c, es_cierre=True
            )

        if not es_fraude and not es_cierre:
            logger.info(f"[Momento 3 Pipeline] Transmitiendo ACTUALIZACIÓN DE TRÁMITE para {payload.Smart_Code__c}...")
            resultado = await _ejecutar_paso_o_exito_si_ya_cerrado(
                self.m3_service.ejecutar_actualizacion_tramite(payload=payload), payload.Smart_Code__c, es_cierre=False
            )

        return resultado