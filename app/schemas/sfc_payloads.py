# app/api/schemas/sfc_payloads.py
from pydantic import BaseModel, Field, field_validator
import re

class SfcNuevaQuejaPayload(BaseModel):
    """
    Contrato estricto de salida para validar que la data consolidada
    cumpla con las longitudes y tipos requeridos por la SFC antes del envío[cite: 4, 5].
    """
    codigo_queja: str = Field(..., max_length=30)
    codigo_pais: str = Field(..., max_length=3)
    departamento_cod: str = Field(..., max_length=3)
    municipio_cod: str = Field(..., max_length=5)
    canal_cod: int
    producto_cod: int
    macro_motivo_cod: int
    fecha_creacion: str  # Estándar ISO: YYYY-MM-DDTHH:MM:SS[cite: 4]
    nombres: str = Field(..., max_length=50)
    tipo_id_CF: int
    numero_id_CF: str = Field(..., max_length=15)
    tipo_persona: int
    insta_recepcion: int
    punto_recepcion: int
    admision: int
    texto_queja: str = Field(..., max_length=4500)
    anexo_queja: bool
    ente_control: int

    @field_validator("texto_queja")
    @classmethod
    def limpiar_y_recortar_texto(cls, value: str) -> str:
        """Sanea el texto eliminando tags HTML y controlando el límite de la SFC[cite: 3, 4]."""
        if not value:
            return ""
        # Remueve etiquetas HTML del CRM si existen[cite: 3, 4]
        texto_limpio = re.sub(r'<[^>]*>', '', value)
        return texto_limpio[:4500].strip()