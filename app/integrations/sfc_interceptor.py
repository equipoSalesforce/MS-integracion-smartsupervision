# app/integrations/sfc_interceptor.py
import httpx
import json
from typing import Generator
from app.core.security.signatures import SfcSignatureContext

class SfcRequestInterceptor(httpx.Auth):
    """
    Interceptor HTTPX que automatiza la autenticación y firma de la SFC.
    """
    def __init__(self, secret_key: str, auth_manager):
        self.secret_key = secret_key
        self.auth_manager = auth_manager
        self.signature_context = SfcSignatureContext(secret_key)

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        raise NotImplementedError("Utilizar el flujo asíncrono (async_auth_flow) para FastAPI.")

    async def async_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        # 1. Inyectamos headers globales requeridos por la SFC
        request.headers["Cache-Control"] = "no-cache"
        request.headers["Accept-Language"] = "es"

        # Si es la petición de Login, no inyectamos firma ni token (evita bucle infinito)
        if "/api/login/" in str(request.url):
            request.headers["Accept"] = "application/json"  # Login espera JSON[cite: 2]
            yield request
            return

        # 2. Inyectamos el Bearer Token
        token = await self.auth_manager.get_valid_token()[cite: 2]
        request.headers["Authorization"] = f"Bearer {token}"[cite: 2]

        # 3. Metadatos y firmas para JSON
        is_file_upload = False
        payload = {}
        method = request.method.upper()

        if method in ["POST", "PUT", "PATCH"]:
            request.headers["Accept"] = "application/json"
            body_bytes = await request.read()  # Lee el cuerpo JSON de forma segura
            payload = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}
        else:
            # Peticiones GET normales (como obtener listado de quejas)[cite: 2]
            request.headers["Accept"] = "application/json"

        # 4. Calculamos y estampamos la firma digital
        signature = self.signature_context.get_signature(
            method=method,
            url=str(request.url),
            payload=payload,
            is_file_upload=is_file_upload
        )
        
        request.headers["X-SFC-Signature"] = signature

        yield request