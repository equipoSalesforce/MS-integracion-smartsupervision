import httpx
import jwt
from datetime import datetime, timedelta, timezone
from typing import Optional
from app.core.config import settings
from app.core.security.signatures import SfcSignatureContext

class SfcAuthManager:
    def __init__(self, signature_context: SfcSignatureContext):
        self.signature_context = signature_context
        self.base_url = settings.SFC_API_BASE_URL.rstrip('/')
        
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.access_exp: Optional[datetime] = None
        self.refresh_exp: Optional[datetime] = None

        self.client = httpx.AsyncClient(base_url=self.base_url, verify=True)

    async def get_valid_token(self) -> str:
        now = datetime.now(timezone.utc)
        
        if self.access_token and self.access_exp and self.access_exp > (now + timedelta(minutes=1)):
            return self.access_token

        if self.refresh_token and self.refresh_exp and self.refresh_exp > (now + timedelta(minutes=1)):
            try:
                await self._refresh_access_token()
                return self.access_token
            except Exception:
                pass 

        await self._login()
        return self.access_token

    async def _login(self):
        endpoint = "/api/login/"
        payload = {
            "username": settings.SFC_USERNAME,
            "password": settings.SFC_PASSWORD
        }
        
        # Corrección: Se utiliza get_signature de acuerdo al patrón Strategy
        signature = self.signature_context.get_signature("POST", endpoint, payload)
        
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-SFC-Signature": signature
        }

        response = await self.client.post(endpoint, json=payload, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        self._save_tokens(data["access"], data["refresh"])

    async def _refresh_access_token(self):
        endpoint = "/api/token/refresh"
        payload = {
            "refresh": self.refresh_token
        }
        
        # Corrección: Se utiliza get_signature de acuerdo al patrón Strategy
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
        
        new_refresh = data.get("refresh", self.refresh_token)
        self._save_tokens(data["access"], new_refresh)

    def _save_tokens(self, access: str, refresh: str):
        self.access_token = access
        self.refresh_token = refresh
        
        access_payload = jwt.decode(access, options={"verify_signature": False})
        refresh_payload = jwt.decode(refresh, options={"verify_signature": False})
        
        self.access_exp = datetime.fromtimestamp(access_payload["exp"], timezone.utc)
        self.refresh_exp = datetime.fromtimestamp(refresh_payload["exp"], timezone.utc)