import httpx
import jwt
import json
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, AsyncGenerator
from app.core.config import settings
from app.core.security.signatures import SfcSignatureContext, ssl_context

logger = logging.getLogger(__name__)

class SfcAuthManager(httpx.Auth):
    def __init__(self, signature_context: SfcSignatureContext, http_client: Optional[httpx.AsyncClient] = None):
        self.signature_context = signature_context
        self.base_url = settings.SFC_URL_BASE.rstrip('/')
        
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.access_exp: Optional[datetime] = None
        self.refresh_exp: Optional[datetime] = None

        # 🔒 Candado de exclusión mutua para evitar Thundering Herd / Estampidas de Autenticación
        self._lock: Optional[asyncio.Lock] = None

        # Cliente HTTP interno aislado para evitar recursión asíncrona al firmar login/refresh
        if http_client is not None:
            self.client = http_client
            self._owns_client = False
        else:
            self.client = httpx.AsyncClient(base_url=self.base_url, verify=ssl_context)
            self._owns_client = True

    def _get_lock(self) -> asyncio.Lock:
        """Inicialización perezosa (Lazy) del Lock para asegurar binding al Event Loop activo."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _is_access_token_valid(self) -> bool:
        """Verifica si el access_token actual sigue siendo válido en memoria RAM."""
        now = datetime.now(timezone.utc)
        return bool(
            self.access_token 
            and self.access_exp 
            and self.access_exp > (now + timedelta(minutes=1))
        )

    def _is_refresh_token_valid(self) -> bool:
        """Verifica si el refresh_token actual sigue siendo válido en memoria RAM."""
        now = datetime.now(timezone.utc)
        return bool(
            self.refresh_token 
            and self.refresh_exp 
            and self.refresh_exp > (now + timedelta(minutes=1))
        )

    async def get_valid_token(self) -> str:
        """
        Obtiene un token de acceso válido aplicando Double-Checked Locking.
        Sincroniza renovaciones concurrentes para prevenir estampidas de peticiones (Thundering Herd).
        """
        # 1. FAST PATH: Si el token actual es válido, retornarlo directamente sin adquirir bloqueo
        if self._is_access_token_valid():
            return self.access_token

        # 2. SLOW PATH: Adquirir candado asíncrono para sincronizar renovación
        async with self._get_lock():
            # 3. DOBLE VERIFICACIÓN: Re-evaluar tras obtener el candado por si otra corrutina ya renovó el token
            if self._is_access_token_valid():
                return self.access_token

            # Intentar refresco si el refresh_token sigue vigente
            if self._is_refresh_token_valid():
                try:
                    await self._refresh_access_token()
                    return self.access_token
                except Exception as e:
                    logger.warning(f"Error al refrescar token: {str(e)}. Reintentando login completo.")

            # Si no hay refresh_token válido o falló el refresco, hacer login completo
            await self._login()
            return self.access_token

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
        self._save_tokens(access, refresh)

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
        self._save_tokens(new_access, new_refresh)

    def _save_tokens(self, access: str, refresh: str):
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

        if request.method == "GET":
            endpoint_for_sig = str(request.url)
            payload = None
        else:
            endpoint_for_sig = path
            payload = None
            
            if "api/storage" in path and "multipart/form-data" in content_type:
                payload = self._parse_multipart_fields(request)
            else:
                if request.content:
                    try:
                        payload = json.loads(request.content.decode("utf-8"))
                    except Exception:
                        payload = None

        signature = self.signature_context.get_signature(request.method, endpoint_for_sig, payload)
        request.headers["X-SFC-Signature"] = signature

        response = yield request
        
        # Recuperación ante 401: Proteger con Lock para evitar borrado/refresh masivo concurrente
        if response.status_code == 401:
            logger.warning("[SfcAuthManager] SFC rechazó la petición con 401. Iniciando flujo de recuperación sincronizado...")
            
            async with self._get_lock():
                # Forzar invalidez local del token si aún coincide con el rechazado
                self.access_token = None
                
                try:
                    nuevo_token = await self.get_valid_token()
                    
                    request.headers["Authorization"] = f"Bearer {nuevo_token}"
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