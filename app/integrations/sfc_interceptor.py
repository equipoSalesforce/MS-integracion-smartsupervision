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
        # 1. Inyectamos headers globales requeridos por la SFC[cite: 1]
        request.headers["Cache-Control"] = "no-cache"
        request.headers["Accept"] = "application/json"
        request.headers["Accept-Language"] = "es"

        # Si es la petición de Login, no inyectamos firma ni token (evita bucle infinito)[cite: 1, 3]
        if "/api/login/" in str(request.url):
            yield request
            return

        # 2. Inyectamos el Bearer Token (el gestor hace login o refresh si expiró)[cite: 2]
        token = await self.auth_manager.get_valid_token()
        request.headers["Authorization"] = f"Bearer {token}"

        # 3. Detectamos si es carga de archivos (Multipart)[cite: 1, 2, 3]
        is_file_upload = False
        payload = {}
        method = request.method.upper()

        if method in ["POST", "PUT", "PATCH"]:
            content_type = request.headers.get("content-type", "")
            
            if "multipart/form-data" in content_type:
                is_file_upload = True
                # Reconstruimos los metadatos necesarios para la firma de archivos[cite: 1, 2]
                # Nota: HTTPX procesa multipart codificando los campos en request.stream.
                # Para no parsear binarios complejos, podemos enviar temporalmente los campos clave en headers personalizados
                # creados en el cliente SFC, los cuales leemos aquí y luego eliminamos.
                payload = {
                    "codigo_queja": request.headers.get("X-Meta-Codigo-Queja"),
                    "type": request.headers.get("X-Meta-Type")
                }
                # Limpiamos los headers temporales para que no lleguen a la SFC
                request.headers.pop("X-Meta-Codigo-Queja", None)
                request.headers.pop("X-Meta-Type", None)
            else:
                # Si es JSON común, leemos el stream de la petición
                body_bytes = request.read()
                payload = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}

        # 4. Calculamos y estampamos la firma digital[cite: 1, 2]
        signature = self.signature_context.get_signature(
            method=method,
            url=str(request.url),
            payload=payload,
            is_file_upload=is_file_upload
        )
        
        request.headers["X-SFC-Signature"] = signature

        yield request