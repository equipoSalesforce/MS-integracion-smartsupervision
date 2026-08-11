import hmac
import hashlib
import json
from abc import ABC, abstractmethod
import ssl
from typing import Any, Dict

ssl_context = ssl.create_default_context()
ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
ssl_context.maximum_version = ssl.TLSVersion.TLSv1_2

class SignatureStrategy(ABC):
    """Interfaz base para las estrategias de firma de la SFC."""
    def __init__(self, secret_key: str):
        self.secret_bytes = bytes(secret_key, 'utf-8')

    @abstractmethod
    def sign(self, target: Any) -> str:
        """Genera la firma HMAC-SHA256 en mayúsculas."""
        pass

class UrlSignatureStrategy(SignatureStrategy):
    """Estrategia para peticiones GET: Firma la URL completa."""
    def sign(self, url: str) -> str:
        return hmac.new(
            self.secret_bytes,
            msg=url.encode('utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest().upper()

class PayloadSignatureStrategy(SignatureStrategy):
    """Estrategia para POST, PUT, PATCH: Firma el body JSON completo."""
    def sign(self, data: Dict[str, Any]) -> str:
        serialized = json.dumps(data, ensure_ascii=False)
        return hmac.new(
            self.secret_bytes,
            msg=serialized.encode('utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest().upper()

class FileTransferSignatureStrategy(SignatureStrategy):
    """
    Estrategia para /api/storage/ (Momentos 2 y 3).
    Omitimos el archivo binario y firmamos únicamente 'codigo_queja' y 'type'.
    """
    def sign(self, data: Dict[str, Any]) -> str:
        filtered_data = {
            "codigo_queja": data.get("codigo_queja"),
            "type": data.get("type")
        }
        serialized = json.dumps(filtered_data, ensure_ascii=False)
        return hmac.new(
            self.secret_bytes,
            msg=serialized.encode('utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest().upper()
        
class SfcSignatureContext:
    def __init__(self, secret_key: str):
        self.secret_key = secret_key

    def get_signature(self, method: str, url: str, payload: Dict[str, Any] = None, is_file_upload: bool = False) -> str:
        """Selecciona la estrategia y retorna la firma correspondiente."""
        if method.upper() == "GET":
            strategy = UrlSignatureStrategy(self.secret_key)
            return strategy.sign(url)
            
        elif method.upper() in ["POST", "PUT", "PATCH"]:
            if is_file_upload:
                strategy = FileTransferSignatureStrategy(self.secret_key)
            else:
                strategy = PayloadSignatureStrategy(self.secret_key)
            return strategy.sign(payload or {})
            
        raise ValueError(f"Método {method} no soportado para generación de firmas.")