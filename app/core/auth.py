import httpx
import jwt
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, AsyncGenerator
from app.core.config import settings
from app.core.security.signatures import SfcSignatureContext

logger = logging.getLogger(__name__)

class SfcAuthManager(httpx.Auth):
    def __init__(self, signature_context: SfcSignatureContext):
        self.signature_context = signature_context
        self.base_url = settings.SFC_URL_BASE.rstrip('/')
        
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.access_exp: Optional[datetime] = None
        self.refresh_exp: Optional[datetime] = None

        # Cliente HTTP interno aislado para evitar recursión asíncrona al firmar login/refresh
        self.client = httpx.AsyncClient(base_url=self.base_url, verify=True)

    async def get_valid_token(self) -> str:
        now = datetime.now(timezone.utc)
        
        if self.access_token and self.access_exp and self.access_exp > (now + timedelta(minutes=1)):
            return self.access_token

        if self.refresh_token and self.refresh_exp and self.refresh_exp > (now + timedelta(minutes=1)):
            try:
                await self._refresh_access_token()
                return self.access_token
            except Exception as e:
                logger.warning(f"Error al refrescar token: {str(e)}. Reintentando login completo.")

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
        # Soporte para claves 'access'/'refresh' estándar de la SFC y Postman
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
        """
        Intercepta y prepara asíncronamente cada petición saliente para cumplir con
        los estándares de seguridad y headers de la Superintendencia Financiera.
        """
        path = request.url.path

        # 1. Evitamos interceptar las llamadas internas de login y refresh para no caer en bucle infinito
        if "/api/login" in path or "/api/token/refresh" in path:
            yield request
            return

        # 2. Obtenemos de forma segura un token JWT asíncrono vigente
        token = await self.get_valid_token()

        # 3. Inyectamos los encabezados canónicos requeridos por la SFC
        request.headers["Authorization"] = f"Bearer {token}"
        request.headers["Cache-Control"] = "no-cache"
        request.headers["Accept"] = "application/json"
        if "accept-language" not in request.headers:
            request.headers["accept-language"] = "es"

        # Inyectamos Content-Type para peticiones con cuerpo (excepto cargas de archivos multipart)
        content_type = request.headers.get("content-type", "")
        if request.method in ("POST", "PUT", "PATCH") and "multipart/form-data" not in content_type:
            request.headers["content-type"] = "application/json"

        # 4. Cálculo de firma criptográfica dinámica (HMAC-SHA256)
        # Métodos GET: Se firma la URL completa con query params
        if request.method == "GET":
            endpoint_for_sig = str(request.url)
            payload = None
        else:
            endpoint_for_sig = path
            payload = None
            
            # REGLA DE EXCEPCIÓN SFC: Cargas de archivos (api/storage/) Momento 2/3
            # La firma se calcula omitiendo el archivo pesado, usando solo "codigo_queja" y "type"
            if "api/storage" in path and "multipart/form-data" in content_type:
                payload = self._parse_multipart_fields(request)
            else:
                # JSON ordinario
                if request.content:
                    try:
                        payload = json.loads(request.content.decode("utf-8"))
                    except Exception:
                        payload = None

        # Generamos e inyectamos la firma HMAC en los encabezados finales
        signature = self.signature_context.get_signature(request.method, endpoint_for_sig, payload)
        request.headers["X-SFC-Signature"] = signature

        response = yield request
        
        if response.status_code == 401:
            logger.warning("[SfcAuthManager] SFC rechazó la petición con 401. Iniciando flujo de recuperación...")
            
            # Borramos el token de acceso local inválido para obligar la renovación
            self.access_token = None
            
            try:
                # get_valid_token() intentará hacer refresh. Si el refresh falla con 401,
                # levantará excepción y se irá directo a ejecutar _login().
                nuevo_token = await self.get_valid_token()
                
                # Actualizamos las cabeceras con las credenciales frescas
                request.headers["Authorization"] = f"Bearer {nuevo_token}"
                
                # Re-calculamos la firma por seguridad
                nueva_firma = self.signature_context.get_signature(request.method, endpoint_for_sig, payload)
                request.headers["X-SFC-Signature"] = nueva_firma
                
                logger.info("[SfcAuthManager] Recuperación exitosa. Reintentando la petición con credenciales nuevas.")
                
                # Volvemos a despachar la petición
                response = yield request
                
            except Exception as e:
                logger.error(f"[SfcAuthManager] Falló la recuperación automática de credenciales: {str(e)}")
                # Si de verdad todo falla, dejamos que el 401 original suba al cliente

    def _parse_multipart_fields(self, request: httpx.Request) -> Dict[str, Any]:
        """
        Parsea el stream multipart en memoria de manera segura y no bloqueante
        para recuperar únicamente las llaves requeridas por la SFC para la firma de archivos.
        """
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
        await self.client.aclose()