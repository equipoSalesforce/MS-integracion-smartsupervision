# app/schemas/sfc_payloads.py
from pydantic import BaseModel, Field, field_validator
from typing import Optional
import re

# ======================================================================
# CONTRATO MOMENTO 2: DETALLE DE NUEVA QUEJA (Exactamente 18 campos)
# ======================================================================
class SfcNuevaQuejaPayload(BaseModel):
    model_config = {"extra": "ignore"}                   # Descarta de forma transparente campos de M3

    codigo_queja: str = Field(..., max_length=30)
    codigo_pais: str = Field("COL", max_length=3)
    departamento_cod: str = Field(..., max_length=3)
    municipio_cod: str = Field(..., max_length=5)
    canal_cod: int
    producto_cod: int
    macro_motivo_cod: int
    fecha_creación: str                                  # Tilde obligatoria[cite: 1]
    nombres: str = Field(..., max_length=50)
    tipo_id_CF: int
    numero_id_CF: str = Field(..., max_length=15)
    tipo_Persona: int                                    # P mayúscula obligatoria[cite: 1]
    insta_recepcion: int
    punto_recepcion: int = Field(1)
    admision: int
    texto_queja: str = Field(..., max_length=4500)
    anexo_queja: bool
    ente_control: int

    @field_validator("texto_queja")
    @classmethod
    def limpiar_y_recortar_texto(cls, value: str) -> str:
        if not value: return ""
        texto_limpio = re.sub(r'<[^>]*>', '', value)     # Limpieza obligatoria de HTML[cite: 1]
        return texto_limpio[:4500].strip()


# ======================================================================
# CONTRATO MOMENTO 3: CIERRE / ACTUALIZACIÓN DE QUEJA (Campos reglamentarios)[cite: 1]
# ======================================================================
class SfcCierreQuejaPayload(BaseModel):
    model_config = {"extra": "ignore"}                   # Descarta de forma transparente campos de M2[cite: 1]

    codigo_queja: str
    fecha_cierre: str                                    # Formato YYYY-MM-DD[cite: 1]
    monto_reconocido: float
    aceptacion_queja: bool
    prorroga_queja: bool
    rectificacion_queja: bool
    marcacion: Optional[int] = None
    documentacion_rta_final: bool