# app/schemas/sfc_payloads.py
from pydantic import BaseModel, Field, field_validator
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
    sexo: Optional[int] = None
    lgbtiq: bool
    canal_cod: int
    condicion_especial: Optional[int]
    producto_cod: int
    producto_nombre: Optional[str] = None
    macro_motivo_cod: int
    texto_queja: str
    anexo_queja: bool
    tutela: bool
    ente_control: Optional[int]
    escalamiento_DCF: bool
    replica: int
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
    
    # 🎯 CORRECCIÓN M2: Alias obligatorio para que el JSON de salida tenga tilde
    fecha_creacion: str = Field(
        ..., 
        alias="fecha_creación", 
        description="Fecha de creación ISO-8601 (Se exporta como 'fecha_creación')"
    )
    
    nombres: str
    tipo_id_CF: int
    numero_id_CF: str
    
    # 🎯 CORRECCIÓN M2: Alias obligatorio para que el JSON de salida tenga 'P' mayúscula
    tipo_persona: int = Field(
        ..., 
        alias="tipo_Persona", 
        description="Tipo de persona (Se exporta como 'tipo_Persona')"
    )
    
    texto_queja: str
    anexo_queja: bool
    ente_control: Optional[int] = Field(None)
    insta_recepcion: int
    admision: int
    codigo_pais: str = "COL"
    punto_recepcion: int = 1

    model_config = {
        # Permite construir el objeto usando 'fecha_creacion' pero lo exporta como 'fecha_creación'
        "populate_by_name": True,
        # Obliga a Pydantic a usar el alias ("fecha_creación", "tipo_Persona") al hacer model_dump()
        "populate_by_alias": True 
    }


# ======================================================================
# 🔄 ESQUEMAS MOMENTO 3 (ENTIDAD -> SFC)
# ======================================================================

class SfcActualizarQuejaPayload(BaseModel):
    codigo_queja: str = Field(..., max_length=30, description="Identificador único de la queja (ID largo)")
    sexo: Optional[int] = Field(None, description="Sexo del consumidor financiero (Código del catálogo)")
    lgbtiq: Optional[int] = Field(None, description="Identidad LGBTIQ+")
    condicion_especial: Optional[int] = Field(None, description="Condición especial del consumidor")
    canal_cod: int = Field(..., description="Canal de atención de la gestión")
    producto_cod: int = Field(..., description="Código del producto financiero")
    macro_motivo_cod: int = Field(..., description="Código del motivo de la queja")
    estado_cod: int = Field(..., description="Estado actual del trámite (4 para clausura definitiva)")
    
    fecha_actualizacion: str = Field(..., description="Fecha y hora de actualización (YYYY-MM-DDThh:mm:ss)")
    
    producto_digital: Optional[int] = Field(1, description="Indica si corresponde a un producto digital")
    admision: Optional[int] = Field(1, description="Estado de admisión del caso")
    desistimiento_queja: Optional[int] = Field(2, description="Desistimiento del consumidor financiero")
    anexo_queja: bool = Field(..., description="Informa la presencia de archivos anexos en la transacción")
    tutela: Optional[int] = Field(2, description="Relación con acción de tutela")
    ente_control: Optional[int] = Field(None, description="Remisión a entes de control externos")
    queja_expres: Optional[int] = Field(2, description="Marca de queja exprés")
    
    a_favor_de: Optional[int] = Field(None)
    aceptacion_queja: Optional[int] = Field(None)
    rectificacion_queja: Optional[int] = Field(None)
    prorroga_queja: Optional[int] = Field(None)
    documentacion_rta_final: Optional[bool] = Field(None)
    
    fecha_cierre: Optional[str] = Field(None)
    marcacion: Optional[int] = Field(None)
    
    tipo_fraude: Optional[int] = Field(None)
    modalidad_fraude: Optional[int] = Field(None)
    monto_reclamado: Optional[int] = Field(None)
    monto_reconocido: Optional[int] = Field(None)

    @field_validator("producto_digital", "admision", mode="before")
    @classmethod
    def default_uno_si_none(cls, v):
        return 1 if v is None else v

    @field_validator("tutela", "queja_expres", "desistimiento_queja", mode="before")
    @classmethod
    def default_dos_si_none(cls, v):
        return 2 if v is None else v

    @field_validator("monto_reclamado", "monto_reconocido", mode="before")
    @classmethod
    def redondear_y_convertir_a_entero(cls, v):
        if v is None:
            return None
        try:
            return int(round(float(v)))
        except (ValueError, TypeError):
            return v