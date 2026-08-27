# app/core/exceptions.py
import asyncio
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple
import httpx
from pydantic import ValidationError as PydanticValidationError

from app.core.config import settings

logger = logging.getLogger(__name__)


class SfcIntegrationException(Exception):
    """Excepción personalizada para controlar fallas devueltas por la SFC."""

    # 🔴 FIX (hallazgo de revisión externa, 2026-08-25): antes esta clasificación vivía
    # duplicada -- routes_quejas.py::es_error_contingencia ya la calculaba sobre campos
    # estructurados (status_code/error_type), mientras que scheduler.py::
    # _es_falla_infraestructura hacía match de subcadenas ("503", "429", etc.) sobre el
    # texto libre del mensaje de error de la SFC -- un monto, código de caso o
    # timestamp que contuviera esos dígitos por coincidencia podía clasificar mal un
    # rechazo de negocio real como caída transitoria de infraestructura. Única fuente
    # de verdad ahora: ambos call sites usan esta property.
    ERROR_TYPES_TRANSITORIOS = frozenset({
        "SERVER_ERROR", "SFC_DOWN", "TIMEOUT", "NETWORK_ERROR",
        "INFRASTRUCTURE_ERROR", "THROTTLED_ERROR", "RATE_LIMIT_ERROR", "RESOURCE_EXHAUSTED"
    })

    def __init__(
        self,
        status_code: int,
        error_type: str,
        sfc_field: Optional[str],
        raw_message: str,
        crm_action: str,
    ):
        self.status_code = status_code
        self.error_type = error_type
        self.sfc_field = sfc_field
        self.raw_message = raw_message
        self.crm_action = crm_action
        super().__init__(f"[{error_type}] {raw_message}")

    @property
    def es_transitoria(self) -> bool:
        """True si el error representa una caída/degradación TRANSITORIA de la SFC
        (5xx, 429, o un error_type ya clasificado como de infraestructura) en vez de
        un rechazo de negocio real -- usado tanto para decidir si un despacho síncrono
        cae a la cola de contingencia (routes_quejas.py) como si un reintento de la
        cola debe consumir presupuesto de intentos (scheduler.py)."""
        return (
            self.status_code >= 500
            or self.status_code in (429, 503)
            or self.error_type in self.ERROR_TYPES_TRANSITORIOS
        )


def resumir_validation_error_sin_pii(ve: PydanticValidationError) -> str:
    """
    🔴 FIX (hallazgo propio, 2026-08-27): `ValidationError.json()` y `str(ValidationError)`
    incluyen por defecto el valor RECHAZADO de cada campo (`input`/`input_value`) -- para
    los schemas de este microservicio (QuejaUnificadaCrmInput, SfcNuevaQuejaPayload) eso
    puede ser el email, nombre, número de identificación o dirección real de un
    consumidor financiero. Único punto de conversión "ValidationError -> texto loggeable"
    del repositorio: úsese en cualquier `except ValidationError` cuyo mensaje pueda llegar
    a un log, `raw_message`/`ultimo_error` (se propaga a la cola y a alertas de correo) o
    cualquier otro destino que no pase por `sanitizar_payload`. Se listan sólo `loc`
    (nombre del campo) y `msg` (descripción del tipo de error) por cada error -- suficiente
    para diagnosticar sin volcar el dato personal que lo causó.
    """
    partes = [f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', 'error de validación')}" for e in ve.errors()]
    return "; ".join(partes) or "Error de validación sin detalle."


class SfcErrorTranslator:
    # Matriz cargada dinámicamente en RAM desde Google Sheets con fallback a errores_sfc.json
    MATRIZ_ERRORES_TEXTO: List[Dict[str, str]] = []
    # ULTIMA_ACTUALIZACION marca el último INTENTO (éxito o fallo) — usada para el
    # backoff/TTL del fast-path. ULTIMO_EXITO_TIMESTAMP marca el último ÉXITO real —
    # usada sólo para la antigüedad/alerta de 24h (🟢 FIX P1-04, mismo patrón que
    # SfcSalesforceMapper).
    ULTIMA_ACTUALIZACION: float = 0
    ULTIMO_EXITO_TIMESTAMP: float = 0
    CACHE_TTL_SEGUNDOS: int = 600  # 10 Minutos en RAM
    MAX_STALE_TTL_SEGUNDOS: int = 86400  # 🟢 FIX HALLAZGO 49: Umbral máximo de obsolescencia (24 Horas)

    # 🟢 FIX HALLAZGO 48: Candado de refresco Single-Flight para evitar estampidas contra Google Sheets API
    _REFRESH_LOCK: Optional[asyncio.Lock] = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._REFRESH_LOCK is None:
            cls._REFRESH_LOCK = asyncio.Lock()
        return cls._REFRESH_LOCK

    @classmethod
    async def _obtener_google_access_token(cls) -> Optional[str]:
        client_id = settings.GOOGLE_CLIENT_ID
        client_secret = settings.GOOGLE_CLIENT_SECRET
        refresh_token = settings.GOOGLE_REFRESH_TOKEN

        if not all([client_id, client_secret, refresh_token]):
            logger.warning(
                "⚠️ [SfcErrorTranslator] Faltan credenciales de Google OAuth en settings. "
                "No se agregará header de autorización."
            )
            return None

        url_oauth = "https://oauth2.googleapis.com/token"
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post(url_oauth, data=payload)
                if res.status_code == 200:
                    data = res.json()
                    return data.get("access_token")
                else:
                    logger.warning(
                        f"⚠️ [SfcErrorTranslator] Falló renovación de token Google OAuth: HTTP {res.status_code}"
                    )
        except Exception as e:
            logger.warning(f"⚠️ [SfcErrorTranslator] Error solicitando Access Token a Google: {e}")

        return None

    @classmethod
    async def _manejar_falla_sincronizacion_matriz(cls, e: Exception, ahora: float) -> None:
        # 🟢 FIX P1-04: antigüedad calculada sobre ULTIMO_EXITO_TIMESTAMP (último
        # éxito real), no sobre ULTIMA_ACTUALIZACION (que avanza en cada intento,
        # exitoso o no, para el backoff). Antes compartían variable y la alerta de
        # 24h nunca podía dispararse.
        edad_segundos = (ahora - cls.ULTIMO_EXITO_TIMESTAMP) if cls.ULTIMO_EXITO_TIMESTAMP > 0 else 0
        edad_horas = edad_segundos / 3600.0

        logger.warning(
            f"⚠️ [SfcErrorTranslator] Falló la sincronización con Google Sheets API v4: {e}. "
            f"Antigüedad de la matriz en RAM: {edad_horas:.1f} horas ({int(edad_segundos)}s)."
        )

        if cls.ULTIMO_EXITO_TIMESTAMP > 0 and edad_segundos > cls.MAX_STALE_TTL_SEGUNDOS:
            logger.critical(
                f"🚨 [SfcErrorTranslator] ALERTA CRÍTICA: La matriz de errores en RAM tiene {edad_horas:.1f}h "
                f"de antigüedad (supera el umbral máximo de {cls.MAX_STALE_TTL_SEGUNDOS // 3600}h)."
            )
            try:
                from app.services.email_service import EmailAlertService
                asyncio.create_task(
                    EmailAlertService.notificar_catalogo_stale(
                        nombre_componente="SfcErrorTranslator (Matriz de Errores)",
                        edad_horas=edad_horas,
                        error_msg=str(e)
                    )
                )
            except Exception as alert_err:
                logger.warning(f"No se pudo disparar la alerta por matriz stale: {alert_err}")

    @classmethod
    async def _refrescar_matriz_desde_sheets(cls, spreadsheet_id: str, sheet_range: str, ahora: float) -> bool:
        """Intenta refrescar MATRIZ_ERRORES_TEXTO desde Google Sheets. Si retorna True, ya dejó todo el estado actualizado."""
        try:
            logger.info("🔄 [SfcErrorTranslator] Sincronizando matriz mediante Google Sheets API v4...")

            access_token = await cls._obtener_google_access_token()
            if not access_token:
                raise ValueError("No se pudo obtener el Access Token de Google OAuth.")

            headers = {"Authorization": f"Bearer {access_token}"}
            url_api_v4 = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{sheet_range}"

            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(url_api_v4, headers=headers)

                if response.status_code != 200:
                    logger.warning(
                        f"⚠️ [SfcErrorTranslator] Google Sheets API devolvió HTTP {response.status_code}: {response.text}"
                    )
                    return False

                data = response.json()
                rows = data.get("values", [])

                reglas = []
                for row in rows[1:]:
                    if not row or not row[0]:
                        continue
                    reglas.append({
                        "subcadena": str(row[0]).strip(),
                        "tipo": str(row[1]).strip() if len(row) > 1 else "UNKNOWN_SFC_ERROR",
                        "accion": str(row[2]).strip() if len(row) > 2 else "Revisar logs del payload."
                    })

                if not reglas:
                    return False

                cls.MATRIZ_ERRORES_TEXTO = reglas
                cls.ULTIMA_ACTUALIZACION = ahora
                cls.ULTIMO_EXITO_TIMESTAMP = ahora  # 🟢 FIX P1-04
                logger.info(
                    f"✅ [SfcErrorTranslator] Matriz actualizada desde Google Sheets API v4: "
                    f"{len(reglas)} reglas cargadas."
                )
                return True
        except Exception as e:
            await cls._manejar_falla_sincronizacion_matriz(e, ahora)
            return False

    @classmethod
    async def obtener_matriz_errores(cls) -> List[Dict[str, str]]:
        """
        Retorna la matriz en RAM. Si la caché expiró o está vacía, realiza una
        petición HTTP GET a la API v4 de Google Sheets usando OAuth 2.0.
        Implementa el patrón Single-Flight para evitar estampidas de peticiones concurrentes.
        """
        ahora = time.time()

        # 1. Fast Path: Si ya está cargada en RAM y no ha vencido el TTL (10 min), usar RAM sin bloqueo
        if cls.MATRIZ_ERRORES_TEXTO and cls.ULTIMA_ACTUALIZACION > 0 and (ahora - cls.ULTIMA_ACTUALIZACION) < cls.CACHE_TTL_SEGUNDOS:
            return cls.MATRIZ_ERRORES_TEXTO

        # 2. Bloqueo Single-Flight: Solo una petición concurrente refresca la matriz
        async with cls._get_lock():
            ahora = time.time()
            # Double-check locking
            if cls.MATRIZ_ERRORES_TEXTO and cls.ULTIMA_ACTUALIZACION > 0 and (ahora - cls.ULTIMA_ACTUALIZACION) < cls.CACHE_TTL_SEGUNDOS:
                return cls.MATRIZ_ERRORES_TEXTO

            spreadsheet_id = settings.GOOGLE_SPREADSHEET_ID
            sheet_range = settings.GOOGLE_SHEET_RANGE

            if spreadsheet_id:
                if await cls._refrescar_matriz_desde_sheets(spreadsheet_id, sheet_range, ahora):
                    return cls.MATRIZ_ERRORES_TEXTO

            if not cls.MATRIZ_ERRORES_TEXTO:
                cls.cargar_matriz_local()

            # ULTIMA_ACTUALIZACION se actualiza siempre (éxito o fallo) para el backoff
            # del fast-path; ULTIMO_EXITO_TIMESTAMP (antigüedad real) sólo se tocó arriba
            # en el punto de éxito genuino.
            cls.ULTIMA_ACTUALIZACION = ahora

            return cls.MATRIZ_ERRORES_TEXTO

    @classmethod
    def cargar_matriz_local(cls) -> None:
        """Carga el respaldo local desde errores_sfc.json."""
        ruta_archivo = os.path.join(os.path.dirname(__file__), "resources/errores_sfc.json")
        try:
            with open(ruta_archivo, "r", encoding="utf-8") as f:
                cls.MATRIZ_ERRORES_TEXTO = json.load(f)
            cls.ULTIMA_ACTUALIZACION = time.time()
            logger.info(
                f"📂 [SfcErrorTranslator] Respaldo local cargado en RAM con {len(cls.MATRIZ_ERRORES_TEXTO)} reglas."
            )
        except Exception as e:
            logger.error(f"❌ [SfcErrorTranslator] Error al cargar errores_sfc.json local: {e}")
            cls.MATRIZ_ERRORES_TEXTO = []

    @classmethod
    def _extraer_informacion_error(cls, response_text: str) -> Tuple[Optional[str], str]:
        """
        🔴 FIX (hallazgo de revisión externa, 2026-08-26, ronda 4): sólo leía
        `data.get("message")` (singular). El envoltorio de error ESTÁNDAR real de la
        SFC (colección Postman oficial, la forma que cubre la mayoría de los
        endpoints) usa `"messages"`, en PLURAL:
        `{"status_code": 400, "messages": {"codigo_queja": [...]}, "detail": "Error APIException"}`.
        Con sólo `message`, ese envoltorio caía en la rama de `detail`
        (`raw_message = "Error APIException"`, `sfc_field = None`) -- toda la
        información estructurada de error se perdía antes de llegar a la
        clasificación, a los logs y a las métricas, y la prioridad de
        `codigo_queja` en `procesar_y_lanzar` (que depende de `sfc_field`) nunca se
        activaba para esta forma, la más común. De paso se corrige el literal
        genérico comparado en la rama de `detail`: era `"Error en API"`, un valor
        que no aparece en ningún response real de la SFC (verificado contra la
        colección Postman) -- el placeholder genérico real es `"Error APIException"`.
        """
        sfc_field = None
        raw_message = response_text

        try:
            data = json.loads(response_text)
            if not isinstance(data, dict):
                return sfc_field, raw_message

            msg_obj = data.get("message")
            if msg_obj is None:
                msg_obj = data.get("messages")

            if isinstance(msg_obj, dict):
                fields_list = []
                messages_list = []
                for key, value in msg_obj.items():
                    val_str = str(value[0]) if isinstance(value, list) and value else str(value)
                    fields_list.append(key)
                    messages_list.append(f"{key}: {val_str}")

                sfc_field = ", ".join(fields_list) if fields_list else None
                raw_message = " | ".join(messages_list) if messages_list else response_text

            elif isinstance(msg_obj, str):
                raw_message = msg_obj
            elif "detail" in data and data["detail"] != "Error APIException":
                raw_message = str(data["detail"])
            else:
                fields_list = []
                messages_list = []
                for key, value in data.items():
                    if key in ("status_code", "detail"):
                        continue
                    val_str = str(value[0]) if isinstance(value, list) and value else str(value)
                    fields_list.append(key)
                    messages_list.append(f"{key}: {val_str}")
                
                if fields_list:
                    sfc_field = ", ".join(fields_list)
                    raw_message = " | ".join(messages_list)
        except (json.JSONDecodeError, TypeError):
            pass

        return sfc_field, raw_message

    @classmethod
    def _notificar_desconocido_async(
        cls, status_code: int, raw_message: str, sfc_field: Optional[str]
    ) -> None:
        try:
            from app.services.email_service import EmailAlertService

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    EmailAlertService.notificar_error_no_mapeado(
                        status_code=status_code,
                        raw_message=raw_message,
                        sfc_field=sfc_field,
                    )
                )
            except RuntimeError:
                logger.warning(
                    "⚠️ [SfcErrorTranslator] No se detectó un event loop activo para disparar la alerta de correo."
                )
        except Exception as mail_err:
            logger.warning(f"⚠️ [SfcErrorTranslator] Error al intentar notificar por correo: {mail_err}")

    @staticmethod
    def _coincide(subcadena: str, texto: str) -> bool:
        """
        🔴 FIX (hallazgo de revisión, 2026-08-26): las reglas de UN SOLO TOKEN sin
        espacios (nombres de campo como 'codigo_queja', o códigos cortos como
        '556240') se anclan a límites de palabra -- si no, matchean como falso
        positivo cuando aparecen INCRUSTADAS dentro de un identificador más largo
        que el cliente controla. Reproducido: un Smart_Code__c que contenga
        '556240' en medio de su parte numérica (ej. '1286SEQ556240ABC', dentro de
        la restricción real `^[a-zA-Z0-9_-]{1,30}$`) hacía que CUALQUIER error de
        la SFC sobre ese caso -- sin relación alguna con un archivo duplicado -- se
        clasificara DUPLICATE_FILE, absorbiéndose como éxito y marcando el
        checkpoint como entregado. Las frases de varias palabras (con espacio,
        ej. "El anexo ya existe") ya son suficientemente específicas por su
        longitud -- seguir comparándolas tal cual, sin anclar, no cambia su riesgo.
        """
        # 🔴 FIX (hallazgo de revisión externa, 2026-08-26, ronda 4): el límite
        # [a-zA-Z0-9] no cubre el alfabeto real de Smart_Code__c
        # (^[a-zA-Z0-9_-]{1,30}$), que también permite guion y guion bajo.
        # Reproducido: "SC-556240-01" y "SC_556240_01" seguían matcheando '556240'
        # como embebido (el '-'/'_' contaba como límite válido) -- el mismo falso
        # positivo que este fix ya cerraba para "SC556240001". Se ancla ahora al
        # mismo alfabeto que valida el propio Smart_Code__c.
        if " " in subcadena:
            return subcadena in texto
        return re.search(
            r"(?<![a-zA-Z0-9_-])" + re.escape(subcadena) + r"(?![a-zA-Z0-9_-])", texto
        ) is not None

    @classmethod
    def _buscar_primera_coincidencia(cls, reglas: List[Dict[str, str]], textos_lower: Tuple[str, ...]) -> Optional[Dict[str, str]]:
        for regla in reglas:
            subcadena = regla.get("subcadena", "").lower()
            if not subcadena:
                continue
            if any(cls._coincide(subcadena, texto) for texto in textos_lower):
                return regla
        return None

    @classmethod
    async def procesar_y_lanzar(cls, status_code: int, response_text: str) -> None:
        """
        🔴 FIX (hallazgo de revisión, 2026-08-26; corregido de nuevo el mismo día
        tras encontrarse una regresión propia -- ver abajo): la regla
        'codigo_queja'→ALREADY_EXISTS es una coincidencia por NOMBRE de campo, no
        por contenido del mensaje -- 'codigo_queja' aparece en prácticamente
        cualquier error de la SFC sobre una queja, incluido el 400 de "Add File"
        cuando el caso NO existe todavía
        (`{"codigo_queja": ["Object with codigo_queja=X does not exist."]}`).
        Reproducido: con el orden original de la matriz, ese caso se clasificaba
        como ALREADY_EXISTS -- lo que
        Momento2SincronizacionService._es_error_queja_ya_existe_m2 tolera como
        éxito idempotente, y que despacho_queja_orchestrator._es_error_caso_no_
        encontrado nunca reconoce como 404 -- dejando el self-healing M2->M3
        permanentemente inalcanzable para esa forma de error, con un crm_action
        que además afirma lo contrario de lo que realmente pasó.

        La primera versión de este fix le daba prioridad a NOT_FOUND_ERROR sobre
        TODA la matriz, sin importar el campo -- demasiado amplio: reclasificaba
        también errores de VALIDACIÓN genuinos de OTROS campos catálogo
        (departamento_cod, municipio_cod, canal_cod, etc.) que la SFC también
        puede reportar con la misma frase "does not exist" (mismo patrón
        SlugRelatedField de Django REST Framework) para SUS PROPIOS valores
        inválidos -- reproducido con
        `{"departamento_cod": ["Object with departamento_cod=999 does not exist."]}`,
        que antes de esa primera versión clasificaba correctamente VALIDATION_ERROR
        y con ella pasó a clasificar (mal) NOT_FOUND_ERROR, dañando el self-healing
        para un caso que en realidad necesitaba corrección de datos, no
        recuperación automática. Las reglas de nombre de campo para esos OTROS
        campos ya mapean correctamente a VALIDATION_ERROR (el tipo correcto para
        "este valor de catálogo no existe") -- sólo 'codigo_queja' tiene un tipo
        (ALREADY_EXISTS) que CONTRADICE lo que "does not exist" realmente
        significa. La prioridad se restringe entonces a mensajes que son
        específicamente sobre 'codigo_queja' (vía sfc_field, reconstruido por
        _extraer_informacion_error a partir de las llaves del JSON de error) --
        el resto de la matriz conserva su orden real, sin tocar ninguna otra
        clasificación ya correcta.
        """
        matriz = await cls.obtener_matriz_errores()
        sfc_field, raw_message = cls._extraer_informacion_error(response_text)

        textos_lower = (raw_message.lower(), (sfc_field or "").lower(), response_text.lower())

        regla = None
        if "codigo_queja" in (sfc_field or "").lower():
            reglas_not_found = [r for r in matriz if r.get("tipo") == "NOT_FOUND_ERROR"]
            regla = cls._buscar_primera_coincidencia(reglas_not_found, textos_lower)
        regla = regla or cls._buscar_primera_coincidencia(matriz, textos_lower)

        if regla:
            error_type = regla.get("tipo", "UNKNOWN_SFC_ERROR")
            crm_action = regla.get("accion", "Error no mapeado por la SFC. Por favor revisar los logs del payload.")
        else:
            error_type = "UNKNOWN_SFC_ERROR"
            crm_action = "Error no mapeado por la SFC. Por favor revisar los logs del payload."

        if not regla or error_type == "UNKNOWN_SFC_ERROR":
            cls._notificar_desconocido_async(status_code, raw_message, sfc_field)

        raise SfcIntegrationException(
            status_code=status_code,
            error_type=error_type,
            sfc_field=sfc_field,
            raw_message=raw_message,
            crm_action=crm_action,
        )