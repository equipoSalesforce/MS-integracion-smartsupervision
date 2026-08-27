import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, AsyncGenerator, Tuple

import httpx
import jwt

from app.core.config import settings
from app.core.exceptions import SfcIntegrationException
from app.core.security.signatures import SfcSignatureContext, ssl_context
from app.db.redis import get_redis_client

logger = logging.getLogger(__name__)

# --- Claves y parámetros de coordinación distribuida en Redis --------------
REDIS_TOKEN_KEY = "{sfc:auth}:tokens"
REDIS_LOCK_KEY = "{sfc:auth}:lock"

LOCK_TTL_MS = 15_000
LOCK_WAIT_TIMEOUT_SECONDS = 10
LOCK_POLL_INTERVAL_SECONDS = 0.25
TOKEN_RECORD_TTL_SECONDS = 7 * 24 * 3600

RELEASE_LOCK_LUA_SCRIPT = """
local lock_key = KEYS[1]
local owner_id = ARGV[1]
if redis.call("GET", lock_key) == owner_id then
    return redis.call("DEL", lock_key)
end
return 0
"""

INVALIDATE_IF_MATCHES_LUA_SCRIPT = """
local token_key = KEYS[1]
local rejected_access_token = ARGV[1]
local raw = redis.call("GET", token_key)
if not raw then
    return 0
end
local ok, data = pcall(cjson.decode, raw)
if not ok then
    return 0
end
if data["access_token"] == rejected_access_token then
    redis.call("DEL", token_key)
    return 1
end
return 0
"""

# 🔴 FIX (hallazgo propio, 2026-08-27): _login/_refresh_access_token parseaban la
# respuesta de la SFC (response.json() + jwt.decode sin verificar firma, sólo para
# leer 'exp') sin ningún try/except propio. Si la SFC respondiera alguna vez con un
# cuerpo no-JSON (ej. una página de error de un proxy/LB mal configurado durante una
# caída real) o con un JWT sin 'exp'/mal formado, la excepción cruda
# (json.JSONDecodeError/jwt.PyJWTError/KeyError/TypeError) no coincide con ningún
# `except` de sfc_client.py (httpx.HTTPStatusError/httpx.RequestError) ni de
# routes_quejas.py (SfcIntegrationException/httpx.RequestError/ConnectionError) --
# escapa sin clasificar hasta el handler genérico de FastAPI, con un 500 desnudo que
# NUNCA se encola para reintento automático (a diferencia de una caída real de
# conectividad contra la SFC, que sí se encola). Se envuelve en SfcIntegrationException
# (502, es_transitoria=True vía status_code>=500) para que este caso reciba el mismo
# tratamiento que cualquier otra falla transitoria de la SFC.
_ERRORES_RESPUESTA_AUTH_SFC = (json.JSONDecodeError, jwt.exceptions.PyJWTError, KeyError, TypeError, ValueError)


def _envolver_error_respuesta_auth_sfc(e: Exception) -> SfcIntegrationException:
    return SfcIntegrationException(
        status_code=502,
        error_type="SFC_AUTH_RESPONSE_INVALID",
        sfc_field=None,
        raw_message=f"La SFC devolvió una respuesta de autenticación inválida o inesperada: {e}",
        crm_action="Reintente la operación más tarde; puede tratarse de un problema transitorio del servicio de autenticación de la SFC."
    )


class SfcAuthManager(httpx.Auth):
    """
    Gestor de autenticación contra la SFC con estado de tokens compartido en
    Redis (fuente de verdad única entre todas las tasks ECS).
    """

    def __init__(self, signature_context: SfcSignatureContext, http_client: Optional[httpx.AsyncClient] = None):
        self.signature_context = signature_context
        self.base_url = settings.SFC_URL_BASE.rstrip('/')

        # Cache local (fast-path). La fuente de verdad real vive en Redis.
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.access_exp: Optional[datetime] = None
        self.refresh_exp: Optional[datetime] = None

        # 🔒 Candado LOCAL: sincroniza corrutinas dentro del MISMO proceso
        self._local_lock: Optional[asyncio.Lock] = None
        self._owner_id = f"auth-lock:{uuid.uuid4()}"

        if http_client is not None:
            self.client = http_client
            self._owns_client = False
        else:
            self.client = httpx.AsyncClient(base_url=self.base_url, verify=ssl_context)
            self._owns_client = True

    def _get_local_lock(self) -> asyncio.Lock:
        if self._local_lock is None:
            self._local_lock = asyncio.Lock()
        return self._local_lock

    def _is_access_token_valid(self) -> bool:
        now = datetime.now(timezone.utc)
        return bool(
            self.access_token
            and self.access_exp
            and self.access_exp > (now + timedelta(minutes=1))
        )

    def _is_refresh_token_valid(self) -> bool:
        now = datetime.now(timezone.utc)
        return bool(
            self.refresh_token
            and self.refresh_exp
            and self.refresh_exp > (now + timedelta(minutes=1))
        )

    # ======================================================================
    # 🌐 PERSISTENCIA COMPARTIDA EN REDIS
    # ======================================================================

    def _load_local_from_dict(self, data: Dict[str, Any]):
        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token")
        access_exp = data.get("access_exp")
        refresh_exp = data.get("refresh_exp")
        self.access_exp = datetime.fromisoformat(access_exp) if access_exp else None
        self.refresh_exp = datetime.fromisoformat(refresh_exp) if refresh_exp else None

    async def _load_from_redis(self) -> bool:
        redis = get_redis_client()
        if not redis:
            return False
        try:
            raw = await redis.get(REDIS_TOKEN_KEY)
            if not raw:
                return False
            self._load_local_from_dict(json.loads(raw))
            return True
        except Exception as e:
            logger.warning(f"[SfcAuthManager] No se pudo leer el token compartido desde Redis: {e}")
            return False

    async def _save_to_redis(self):
        redis = get_redis_client()
        if not redis:
            logger.warning(
                "[SfcAuthManager] Redis no disponible: el token quedará únicamente en RAM local "
                "de esta instancia (sin compartir con el resto del cluster)."
            )
            return
        data = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "access_exp": self.access_exp.isoformat() if self.access_exp else None,
            "refresh_exp": self.refresh_exp.isoformat() if self.refresh_exp else None,
        }
        try:
            await redis.set(REDIS_TOKEN_KEY, json.dumps(data), ex=TOKEN_RECORD_TTL_SECONDS)
        except Exception as e:
            logger.warning(f"[SfcAuthManager] No se pudo persistir el token compartido en Redis: {e}")

    async def _invalidar_token_compartido_si_coincide(self, rejected_access_token: Optional[str]):
        redis = get_redis_client()
        if not redis or not rejected_access_token:
            return
        try:
            await redis.eval(INVALIDATE_IF_MATCHES_LUA_SCRIPT, 1, REDIS_TOKEN_KEY, rejected_access_token)
        except Exception as e:
            logger.warning(f"[SfcAuthManager] Error invalidando token compartido en Redis: {e}")

    # ======================================================================
    # 🔒 LOCK DISTRIBUIDO
    # ======================================================================

    async def _acquire_distributed_lock(self) -> bool:
        redis = get_redis_client()
        if not redis:
            return False
        try:
            acquired = await redis.set(REDIS_LOCK_KEY, self._owner_id, nx=True, px=LOCK_TTL_MS)
            return bool(acquired)
        except Exception as e:
            logger.warning(f"[SfcAuthManager] Error adquiriendo lock distribuido de autenticación: {e}")
            return False

    async def _release_distributed_lock(self):
        redis = get_redis_client()
        if not redis:
            return
        try:
            await redis.eval(RELEASE_LOCK_LUA_SCRIPT, 1, REDIS_LOCK_KEY, self._owner_id)
        except Exception as e:
            logger.warning(f"[SfcAuthManager] Error liberando lock distribuido de autenticación: {e}")

    # ======================================================================
    # 🎯 OBTENCIÓN DE TOKEN VÁLIDO (MÉTODOS REFACTORIZADOS SIN DEADLOCK)
    # ======================================================================

    async def get_valid_token(self) -> str:
        """Punto de entrada público protegido por el lock local."""
        # 1. FAST PATH LOCAL: si el cache en RAM del proceso sigue vigente, no tocar Redis ni el lock.
        if self._is_access_token_valid():
            return self.access_token

        async with self._get_local_lock():
            return await self._get_valid_token_unlocked()

    async def _get_valid_token_unlocked(self) -> str:
        """
        🟢 LÓGICA INTERNA SIN LOCK:
        Ejecuta el flujo de obtención/renovación asumiendo que el caller ya posee
        el `self._get_local_lock()`. Esto previene deadlocks en llamadas recursivas.
        """
        # Doble verificación por si otra corrutina renovó el token mientras esperábamos el lock
        if self._is_access_token_valid():
            return self.access_token

        redis = get_redis_client()
        if not redis:
            logger.info("[SfcAuthManager] Redis no disponible. Ejecutando renovación directa en memoria local.")
            return await self._renovar_token_directo_local()

        # FAST PATH REDIS: quizás otra instancia ECS ya renovó el token
        await self._load_from_redis()
        if self._is_access_token_valid():
            return self.access_token

        # Competir por el lock distribuido
        return await self._renovar_token_coordinado()

    async def _renovar_token_directo_local(self) -> str:
        if self._is_refresh_token_valid():
            try:
                await self._refresh_access_token()
                return self.access_token
            except Exception as e:
                logger.warning(f"[SfcAuthManager] Falló refresh local, intentando login completo: {e}")

        await self._login()
        return self.access_token

    async def _renovar_token_coordinado(self) -> str:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + LOCK_WAIT_TIMEOUT_SECONDS

        while True:
            if not get_redis_client():
                logger.warning("[SfcAuthManager] Conexión a Redis perdida durante la coordinación. Continuando de forma local.")
                return await self._renovar_token_directo_local()

            if await self._acquire_distributed_lock():
                try:
                    await self._load_from_redis()
                    if self._is_access_token_valid():
                        return self.access_token

                    if self._is_refresh_token_valid():
                        try:
                            await self._refresh_access_token()
                            await self._save_to_redis()
                            return self.access_token
                        except Exception as e:
                            logger.warning(f"[SfcAuthManager] Falló refresh coordinado, intentando login completo: {e}")

                    await self._login()
                    await self._save_to_redis()
                    return self.access_token
                finally:
                    await self._release_distributed_lock()

            if loop.time() >= deadline:
                logger.error("[SfcAuthManager] Timeout esperando renovación por otra instancia. Procediendo con login propio.")
                return await self._renovar_token_directo_local()

            await asyncio.sleep(LOCK_POLL_INTERVAL_SECONDS)

            await self._load_from_redis()
            if self._is_access_token_valid():
                return self.access_token

    # ======================================================================
    # 🌐 LLAMADAS HTTP DE LOGIN / REFRESH CONTRA LA SFC
    # ======================================================================

    async def _login(self):
        endpoint = "/api/login/"
        payload = {
            "username": settings.SFC_USERNAME,
            "password": settings.SFC_PASSWORD
        }

        if settings.ENVIRONMENT == "local":
            logger.info(f"El username utilizado para login es {settings.SFC_USERNAME}")

        signature = self.signature_context.get_signature("POST", endpoint, payload)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-SFC-Signature": signature
        }

        response = await self.client.post(endpoint, json=payload, headers=headers)
        response.raise_for_status()

        try:
            data = response.json()
            access = data.get("access") or data.get("access_token")
            refresh = data.get("refresh") or data.get("refresh_token")
            self._save_tokens_local(access, refresh)
        except _ERRORES_RESPUESTA_AUTH_SFC as e:
            raise _envolver_error_respuesta_auth_sfc(e) from e

    async def _refresh_access_token(self):
        endpoint = "/api/token/refresh"
        payload = {
            "refresh": self.refresh_token
        }

        signature = self.signature_context.get_signature("POST", endpoint, payload)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-SFC-Signature": signature
        }

        response = await self.client.post(endpoint, json=payload, headers=headers)

        if response.status_code == 401:
            raise ValueError("Refresh Token inválido o expirado en la SFC")

        response.raise_for_status()

        try:
            data = response.json()
            new_refresh = data.get("refresh") or data.get("refresh_token") or self.refresh_token
            new_access = data.get("access") or data.get("access_token")
            self._save_tokens_local(new_access, new_refresh)
        except _ERRORES_RESPUESTA_AUTH_SFC as e:
            raise _envolver_error_respuesta_auth_sfc(e) from e

    def _save_tokens_local(self, access: str, refresh: str):
        self.access_token = access
        self.refresh_token = refresh

        access_payload = jwt.decode(access, options={"verify_signature": False})
        refresh_payload = jwt.decode(refresh, options={"verify_signature": False})

        self.access_exp = datetime.fromtimestamp(access_payload["exp"], timezone.utc)
        self.refresh_exp = datetime.fromtimestamp(refresh_payload["exp"], timezone.utc)

    # ======================================================================
    # 🟢 INTERCEPTOR DE FLUJO ASÍNCRONO DE AUTENTICACIÓN (REFACTORIZADO)
    # ======================================================================
    async def _preparar_headers_y_firma(self, request: httpx.Request, path: str) -> Tuple[str, Any, bool]:
        """
        Fija content-type e is_file_upload, y calcula X-SFC-Signature si no viene ya provista.
        Retorna (endpoint_for_sig, payload, is_file_upload) para reutilizar en el reintento post-401.
        """
        content_type = request.headers.get("content-type", "")
        if request.method in ("POST", "PUT", "PATCH") and "multipart/form-data" not in content_type:
            request.headers["content-type"] = "application/json"

        endpoint_for_sig = path
        payload = None
        # 🟢 FIX HALLAZGO 19: Detección única de archivo/multipart, reutilizada también en el
        # reintento post-401 más abajo. Los campos a firmar viajan en `request.extensions`
        # (ver SfcClient.post_adjunto_queja), evitando tener que re-parsear el body multipart.
        is_file_upload = "multipart/form-data" in content_type or "api/storage" in path

        if "X-SFC-Signature" in request.headers:
            return endpoint_for_sig, payload, is_file_upload

        if request.method == "GET":
            endpoint_for_sig = str(request.url)
            payload = None
        elif is_file_upload:
            endpoint_for_sig = path
            payload = request.extensions.get("sfc_signature_fields")
        else:
            endpoint_for_sig = path
            payload = None

            try:
                if hasattr(request, "content") and request.content:
                    payload = json.loads(request.content.decode("utf-8"))
            except Exception:
                payload = None

        signature = self.signature_context.get_signature(
            request.method, endpoint_for_sig, payload, is_file_upload=is_file_upload
        )
        request.headers["X-SFC-Signature"] = signature

        return endpoint_for_sig, payload, is_file_upload

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        path = request.url.path

        if "/api/login" in path or "/api/token/refresh" in path:
            yield request
            return

        token = await self.get_valid_token()

        request.headers["Authorization"] = f"Bearer {token}"
        request.headers["Cache-Control"] = "no-cache"
        request.headers["Accept"] = "application/json"
        if "accept-language" not in request.headers:
            request.headers["accept-language"] = "es"

        endpoint_for_sig, payload, is_file_upload = await self._preparar_headers_y_firma(request, path)

        response = yield request

        # 🟢 RECUPERACIÓN ANTE HTTP 401 SIN DEADLOCK
        if response.status_code == 401:
            logger.warning("[SfcAuthManager] SFC rechazó la petición con 401. Iniciando flujo de recuperación sincronizado...")

            async with self._get_local_lock(): # 🔒 Candado tomado una sola vez
                await self._invalidar_token_compartido_si_coincide(token)

                if self.access_token == token:
                    self.access_token = None

                try:
                    # 🟢 LLAMADA A MÉTODOS UNLOCKED PARA EVITAR ADQUIRIR EL MISMO LOCK
                    nuevo_token = await self._get_valid_token_unlocked()
                    request.headers["Authorization"] = f"Bearer {nuevo_token}"

                    # 🟢 FIX HALLAZGO 19: Reutiliza el mismo `payload`/`is_file_upload` calculados
                    # arriba (incluye multipart vía `request.extensions`) en vez del método
                    # inexistente `_parse_multipart_fields`, que nunca llegó a implementarse y
                    # hacía que el reintento post-401 de adjuntos fallara silenciosamente.
                    nueva_firma = self.signature_context.get_signature(
                        request.method, endpoint_for_sig, payload, is_file_upload=is_file_upload
                    )
                    request.headers["X-SFC-Signature"] = nueva_firma

                    # 🟡 Este `yield request` reenvía el MISMO objeto Request -- para una subida
                    # multipart (ver SfcClient.post_adjunto_queja) eso significa reenviar el
                    # MISMO file-like object que ya se leyó por completo en el primer envío. No
                    # se hace ningún seek(0) explícito acá porque no hace falta: httpx (pineado en
                    # 0.28.1) ya reseekea el file-like object a 0 en cada iteración del stream
                    # multipart (ver httpx/_multipart.py::FileField.render_data()), así que este
                    # reintento retransmite el archivo completo, no vacío. Verificado end-to-end
                    # (MockTransport, 401 seguido de 200, sin mockear el envío) en
                    # tests/test_auth_flow_interceptor.py::
                    # test_reintento_post_401_con_adjunto_multipart_reenvia_el_archivo_completo.
                    # Si se actualiza httpx (o se cambia de librería HTTP) y esa prueba empieza a
                    # fallar, ESE es el punto donde habría que agregar el seek(0) explícito.
                    logger.info("[SfcAuthManager] Recuperación exitosa. Reintentando petición con credenciales nuevas.")
                    response = yield request

                except Exception as e:
                    logger.error(f"[SfcAuthManager] Falló la recuperación automática de credenciales: {str(e)}")

    async def close(self):
        if self._owns_client and self.client and not self.client.is_closed:
            await self.client.aclose()
            logger.info("🛑 Cliente HTTP interno de SfcAuthManager cerrado limpiamente.")