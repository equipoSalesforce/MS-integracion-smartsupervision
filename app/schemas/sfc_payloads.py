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
    
# ======================================================================
# 🔄 ESQUEMAS MOMENTO 3 (ENTIDAD -> SFC)
# ======================================================================

class SfcActualizarQuejaPayload(BaseModel):
    """
    Valida la estructura canónica limpia requerida estrictamente por la SFC 
    en el cuerpo del mensaje (Body) para tramitar actualizaciones y cierres en M3.
    """
    codigo_queja: str = Field(..., max_length=30, description="Identificador único de la queja (ID largo)")
    sexo: int = Field(2, description="Sexo del consumidor financiero (Código del catálogo)")
    lgbtiq: int = Field(2, description="Identidad LGBTIQ+")
    condicion_especial: int = Field(98, description="Condición especial del consumidor[cite: 2]")
    canal_cod: int = Field(..., description="Canal de atención de la gestión[cite: 2]")
    producto_cod: int = Field(..., description="Código del producto financiero[cite: 2]")
    macro_motivo_cod: int = Field(..., description="Código del motivo de la queja[cite: 2]")
    estado_cod: int = Field(..., description="Estado actual del trámite (4 para clausura definitiva)[cite: 2]")
    fecha_actualizacion: str = Field(..., description="Fecha de la última actualización en formato YYYY-MM-DD[cite: 2]")
    producto_digital: int = Field(1, description="Indica si corresponde a un producto digital[cite: 2]")
    admision: int = Field(1, description="Estado de admisión del caso[cite: 2]")
    desistimiento_queja: int = Field(2, description="Desistimiento del consumidor financiero[cite: 2]")
    anexo_queja: bool = Field(..., description="Informa la presencia de archivos anexos en la transacción[cite: 2]")
    tutela: int = Field(2, description="Relación con acción de tutela[cite: 2]")
    ente_control: int = Field(99, description="Remisión a entes de control externos[cite: 2]")
    queja_expres: int = Field(1, description="Marca de queja exprés[cite: 2]")
    
    # Campos operacionales condicionales (Opcionales en trámite ordinario)[cite: 2]
    a_favor_de: Optional[int] = Field(None, description="Sentido de la decisión final (Exigido en cierres)[cite: 2]")
    aceptacion_queja: Optional[int] = Field(None, description="Aceptación por la entidad[cite: 2]")
    rectificacion_queja: Optional[int] = Field(None, description="Rectificación de información[cite: 2]")
    prorroga_queja: Optional[int] = Field(None, description="Uso de prórroga por la entidad[cite: 2]")
    documentacion_rta_final: Optional[bool] = Field(None, description="Referencia a la presencia de respuesta final[cite: 2]")
    fecha_cierre: Optional[str] = Field(None, description="Fecha de cierre definitivo en formato YYYY-MM-DD[cite: 2]")
    marcacion: Optional[int] = Field(None, description="Marcaciones adicionales del caso[cite: 2]")
    
    # Campos obligatorios exclusivos para la mitigación y gestión de fraudes[cite: 2]
    tipo_fraude: Optional[int] = Field(None, description="Clasificación del fraude según catálogo SFC[cite: 2]")
    modalidad_fraude: Optional[int] = Field(None, description="Modalidad detectada según catálogo SFC[cite: 2]")
    monto_reclamado: Optional[float] = Field(None, description="Valor total reclamado por el consumidor[cite: 2]")
    monto_reconocido: Optional[float] = Field(None, description="Valor final reconocido/devuelto por la entidad[cite: 2]")