# app/schemas/sfc_payloads.py
from pydantic import BaseModel, Field
from typing import List, Optional

# ======================================================================
# ⬇️ ESQUEMAS MOMENTO 1 (SFC -> CRM)
# ======================================================================

class SfcQuejaItem(BaseModel):
    """Estructura de una queja individual tal cual la entrega la SFC."""
    tipo_entidad: int
    entidad_cod: str
    fecha_creacion: str
    codigo_queja: str
    codigo_pais: str
    departamento_cod: str
    municipio_cod: str
    nombres: str
    tipo_id_CF: int
    numero_id_CF: str
    telefono: Optional[str] = None
    correo: Optional[str] = None
    tipo_persona: int
    sexo: int
    lgbtiq: bool
    canal_cod: int
    condicion_especial: int
    producto_cod: int
    producto_nombre: Optional[str] = None
    macro_motivo_cod: int
    texto_queja: str
    anexo_queja: bool
    tutela: bool
    ente_control: int
    escalamiento_DCF: bool
    replica: bool
    argumento_replica: Optional[str] = None
    desistimiento_queja: bool
    queja_expres: bool


class SfcFetchQuejasResponse(BaseModel):
    """
    Representa la estructura interna del objeto 'Response' de la SFC
    cuando se listan múltiples quejas de forma paginada.
    """
    count: int
    pages: int
    current_page: Optional[int] = None
    next: Optional[str] = None
    previous: Optional[str] = None
    results: List[SfcQuejaItem]


# ======================================================================
# ⬆️ ESQUEMAS MOMENTO 2 (CRM -> SFC)
# ======================================================================

class SfcNuevaQuejaPayload(BaseModel):
    """
    Valida la estructura de salida de datos requerida estrictamente 
    por la SFC para registrar una queja nueva en su sistema.
    """
    codigo_queja: str = Field(..., description="ID Compuesto regulatorio tipo+entidad+smartcode")
    departamento_cod: str
    municipio_cod: str
    canal_cod: int
    producto_cod: int
    macro_motivo_cod: int
    fecha_creación: str = Field(..., description="Fecha de creación formateada con tilde para M2 SFC")
    fecha_creacion: Optional[str] = None  # Fallback de compatibilidad
    nombres: str
    tipo_id_CF: int
    numero_id_CF: str
    tipo_Persona: int = Field(..., description="Tipo de persona con P mayúscula")
    tipo_persona: Optional[int] = None   # Fallback de compatibilidad
    texto_queja: str
    anexo_queja: bool
    ente_control: int
    insta_recepcion: int
    admision: int
    codigo_pais: str = "COL"
    punto_recepcion: int = 1