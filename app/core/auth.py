import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, AsyncGenerator

import httpx
import jwt

from app.core.config import settings
from app.core.security.signatures import SfcSignatureContext, ssl_context
from app.db.redis import get_redis_client

logger = logging.getLogger(__name__)

# --- Claves y parámetros de coordinación distribuida en Redis --------------
REDIS_TOKEN_KEY = "{sfc:auth}:tokens"
REDIS_LOCK_KEY = "{sfc:auth}:lock"

# Tiempo máximo que se le da a UNA instancia para completar el login/refresh
# contra la SFC antes de que el lock se autolibere (self-healing si el proceso
# que tomó el lock muere/cuelga sin liberar).
LOCK_TTL_MS = 15_000

# Cuánto tiempo (en total) están dispuestas a esperar las demás instancias a
# que la que ganó el lock termine, antes de intentar renovar por su cuenta.
LOCK_WAIT_TIMEOUT_SECONDS = 10
LOCK_POLL_INTERVAL_SECONDS = 0.25

# TTL de retención del registro de tokens en Redis (limpieza automática de
# credenciales muertas si el servicio deja de operar).
TOKEN_RECORD_TTL_SECONDS = 7 * 24 * 3600

# 📜 Libera el lock distribuido SOLO si seguimos siendo los dueños (evita que
# una instancia libere por error el lock tomado por otra tras expirar el TTL).
RELEASE_LOCK_LUA_SCRIPT = """
local lock_key = KEYS[1]
local owner_id = ARGV[1]
if redis.call("GET", lock_key) == owner_id then
    return redis.call("DEL", lock_key)
end
return 0
"""

# 📜 Invalida el registro de tokens en Redis SOLO si el access_token guardado
# coincide con el que fue rechazado con 401 — evita pisar un token más nuevo
# que otra instancia ya haya publicado mientras tanto.
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


class SfcAuthManager(httpx.Auth):
    """
    Gestor de autenticación contra la SFC con estado de tokens compartido en
    Redis (fuente de verdad única entre todas las tasks ECS).

    Estrategia de 3 niveles para obtener un token válido:
      1. Fast path LOCAL: cache en RAM del proceso, sin tocar Redis.
      2. Fast path REDIS: si otra instancia ya renovó el token, lo leemos sin
         necesidad de hacer login/refresh nosotros mismos.
      3. LOCK DISTRIBUIDO: si nadie tiene un token válido, una sola instancia
         en todo el cluster hace el login/refresh contra la SFC (las demás
         esperan el resultado). Esto evita que N tasks hagan login
         simultáneo con el mismo SFC_USERNAME, lo cual en sistemas que
         invalidan sesiones anteriores al hacer un nuevo login generaría una
         tormenta de 401 entre instancias.
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
        # antes de siquiera competir por el lock distribuido en Redis.
        self._local_lock: Optional[asyncio.Lock] = None

        # Identidad única de esta instancia/proceso para el lock distribuido.
        self._owner_id = f"auth-lock:{uuid.uuid4()}"

        # Cliente HTTP interno aislado para evitar recursión asíncrona al firmar login/refresh
        if http_client is not None:
            self.client = http_client
            self._owns_client = False
        else:
            self.client = httpx.AsyncClient(base_url=self.base_url, verify=ssl_context)
            self._owns_client = True

    def _get_local_lock(self) -> asyncio.Lock:
        """Inicialización perezosa (Lazy) del Lock para asegurar binding al Event Loop activo."""
        if self._local_lock is None:
            self._local_lock = asyncio.Lock()
        return self._local_lock

    def _is_access_token_valid(self) -> bool:
        """Verifica si el access_token actual (cache local) sigue siendo válido."""
        now = datetime.now(timezone.utc)
        return bool(
            self.access_token
            and self.access_exp
            and self.access_exp > (now + timedelta(minutes=1))
        )

    def _is_refresh_token_valid(self) -> bool:
        """Verifica si el refresh_token actual (cache local) sigue siendo válido."""
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
        """
        Intenta refrescar el cache local leyendo el registro compartido en Redis.
        Si Redis no está disponible, simplemente no hay fast-path compartido
        y cada instancia termina haciendo login por su cuenta (degradado, no roto).
        """
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
        """CAS: borra el registro en Redis SOLO si sigue apuntando al token que fue rechazado con 401."""
        redis = get_redis_client()
        if not redis or not rejected_access_token:
            return
        try:
            await redis.eval(INVALIDATE_IF_MATCHES_LUA_SCRIPT, 1, REDIS_TOKEN_KEY, rejected_access_token)
        except Exception as e:
            logger.warning(f"[SfcAuthManager] Error invalidando token compartido en Redis: {e}")

    # ======================================================================
    # 🔒 LOCK DISTRIBUIDO (una sola instancia hace login/refresh a la vez)
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
    # 🎯 OBTENCIÓN DE TOKEN VÁLIDO
    # ======================================================================

    async def get_valid_token(self) -> str:
        # 1. FAST PATH LOCAL: si el cache en RAM del proceso sigue vigente, no tocar Redis.
        if self._is_access_token_valid():
            return self.access_token

        # 2. Sincronizar corrutinas DENTRO de este mismo proceso primero.
        async with self._get_local_lock():
            if self._is_access_token_valid():
                return self.access_token

            # Si Redis no está disponible (ej. tests sin Redis o modo degradado),
            # no competir por lock distribuido ni esperar timeouts: renovar localmente de inmediato.
            redis = get_redis_client()
            if not redis:
                logger.info("[SfcAuthManager] Redis no disponible. Ejecutando renovación directa en memoria local.")
                return await self._renovar_token_directo_local()

            # 3. FAST PATH REDIS: quizás otra instancia ECS ya renovó el token.
            await self._load_from_redis()
            if self._is_access_token_valid():
                return self.access_token

            # 4. Nadie en el cluster tiene un token válido a mano: competir
            #    por el lock distribuido para que solo UNA instancia haga
            #    login/refresh contra la SFC.
            return await self._renovar_token_coordinado()
        
    async def _renovar_token_directo_local(self) -> str:
        """Flujo directo de renovación sin coordinación por Redis."""
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
            # Si Redis se desconecta en medio del bucle, romper y renovar localmente
            if not get_redis_client():
                logger.warning("[SfcAuthManager] Conexión a Redis perdida durante la coordinación. Continuando de forma local.")
                return await self._renovar_token_directo_local()

            if await self._acquire_distributed_lock():
                try:
                    # Doble verificación: entre que decidimos renovar y que
                    # conseguimos el lock, otra instancia pudo haber terminado.
                    await self._load_from_redis()
                    if self._is_access_token_valid():
                        return self.access_token

                    if self._is_refresh_token_valid():
                        try:
                            await self._refresh_access_token()
                            await self._save_to_redis()
                            return self.access_token
                        except Exception as e:
                            logger.warning(
                                f"[SfcAuthManager] Falló el refresh coordinado, "
                                f"intentando login completo: {e}"
                            )

                    await self._login()
                    await self._save_to_redis()
                    return self.access_token
                finally:
                    await self._release_distributed_lock()

            # No se consiguió el lock: otra instancia ya está renovando.
            if loop.time() >= deadline:
                logger.error(
                    "[SfcAuthManager] Timeout esperando a que otra instancia complete la renovación "
                    "del token compartido. Se procede con login propio como último recurso."
                )
                return await self._renovar_token_directo_local()

            await asyncio.sleep(LOCK_POLL_INTERVAL_SECONDS)

            # Antes de reintentar el lock, revisar si mientras tanto ya quedó listo.
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

        data = response.json()
        access = data.get("access") or data.get("access_token")
        refresh = data.get("refresh") or data.get("refresh_token")
        self._save_tokens_local(access, refresh)

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
        data = response.json()

        new_refresh = data.get("refresh") or data.get("refresh_token") or self.refresh_token
        new_access = data.get("access") or data.get("access_token")
        self._save_tokens_local(new_access, new_refresh)

    def _save_tokens_local(self, access: str, refresh: str):
        """Actualiza únicamente el cache en RAM local. La publicación hacia
        Redis (fuente de verdad compartida) la hace el llamador vía _save_to_redis()."""
        self.access_token = access
        self.refresh_token = refresh

        access_payload = jwt.decode(access, options={"verify_signature": False})
        refresh_payload = jwt.decode(refresh, options={"verify_signature": False})

        self.access_exp = datetime.fromtimestamp(access_payload["exp"], timezone.utc)
        self.refresh_exp = datetime.fromtimestamp(refresh_payload["exp"], timezone.utc)

    # ======================================================================
    # INTERCEPTOR DE FLUJO ASÍNCRONO PARA EL CLIENTE HTTPX
    # ======================================================================
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

        content_type = request.headers.get("content-type", "")
        if request.method in ("POST", "PUT", "PATCH") and "multipart/form-data" not in content_type:
            request.headers["content-type"] = "application/json"

        endpoint_for_sig = path
        payload = None

        # 🟢 FIX 1: Si X-SFC-Signature ya viene calculada (ej. post_adjunto_queja), respetarla
        if "X-SFC-Signature" not in request.headers:
            if request.method == "GET":
                endpoint_for_sig = str(request.url)
                payload = None
            else:
                endpoint_for_sig = path
                payload = None

                if "multipart/form-data" not in content_type and "api/storage" not in path:
                    try:
                        if hasattr(request, "content") and request.content:
                            payload = json.loads(request.content.decode("utf-8"))
                    except Exception:
                        payload = None

            signature = self.signature_context.get_signature(request.method, endpoint_for_sig, payload)
            request.headers["X-SFC-Signature"] = signature

        response = yield request

        # Recuperación ante 401: Proteger con Lock
        if response.status_code == 401:
            logger.warning("[SfcAuthManager] SFC rechazó la petición con 401. Iniciando flujo de recuperación sincronizado...")

            async with self._get_local_lock():
                await self._invalidar_token_compartido_si_coincide(token)

                if self.access_token == token:
                    self.access_token = None

                try:
                    nuevo_token = await self.get_valid_token()
                    request.headers["Authorization"] = f"Bearer {nuevo_token}"

                    # Re-firmar solo si era una petición JSON estándar
                    if "multipart/form-data" not in content_type and "api/storage" not in path:
                        nueva_firma = self.signature_context.get_signature(request.method, endpoint_for_sig, payload)
                        request.headers["X-SFC-Signature"] = nueva_firma

                    logger.info("[SfcAuthManager] Recuperación exitosa. Reintentando petición con credenciales nuevas.")
                    response = yield request

                except Exception as e:
                    logger.error(f"[SfcAuthManager] Falló la recuperación automática de credenciales: {str(e)}")

    def _parse_multipart_fields(self, request: httpx.Request) -> Dict[str, Any]:
        fields = {}
        content_type = request.headers.get("content-type", "")
        if "boundary=" in content_type:
            boundary = content_type.split("boundary=")[-1].strip().strip('"\'')
            content = request.content
            parts = content.split(f"--{boundary}".encode("utf-8"))

            for part in parts:
                if b"Content-Disposition:" in part:
                    headers_part, _, body_part = part.partition(b"\r\n\r\n")
                    headers_str = headers_part.decode("utf-8", errors="ignore")

                    if 'name="type"' in headers_str:
                        fields["type"] = body_part.rstrip(b"\r\n").decode("utf-8", errors="ignore").strip()
                    elif 'name="codigo_queja"' in headers_str:
                        fields["codigo_queja"] = body_part.rstrip(b"\r\n").decode("utf-8", errors="ignore").strip()
        return fields

    async def close(self):
        """Cierra ordenadamente las conexiones TCP del cliente asíncrono interno."""
        if self._owns_client and self.client and not self.client.is_closed:
            await self.client.aclose()
            logger.info("🛑 Cliente HTTP interno de SfcAuthManager cerrado limpiamente.")