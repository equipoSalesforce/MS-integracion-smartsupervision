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
    ente_control: int
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
    """
    Valida la estructura canónica limpia requerida estrictamente por la SFC 
    en el cuerpo del mensaje (Body) para tramitar actualizaciones y cierres en M3.
    """
    codigo_queja: str = Field(..., max_length=30, description="Identificador único de la queja (ID largo)")
    sexo: int = Field(2, description="Sexo del consumidor financiero (Código del catálogo)")
    lgbtiq: int = Field(2, description="Identidad LGBTIQ+")
    condicion_especial: int = Field(98, description="Condición especial del consumidor")
    canal_cod: int = Field(..., description="Canal de atención de la gestión")
    producto_cod: int = Field(..., description="Código del producto financiero")
    macro_motivo_cod: int = Field(..., description="Código del motivo de la queja")
    estado_cod: int = Field(..., description="Estado actual del trámite (4 para clausura definitiva)")
    
    # 🎯 CORRECCIÓN M3: Debe ser String y el docstring aclara el formato completo ISO 8601
    fecha_actualizacion: str = Field(..., description="Fecha y hora de actualización (YYYY-MM-DDThh:mm:ss)")
    
    producto_digital: int = Field(1, description="Indica si corresponde a un producto digital")
    admision: int = Field(1, description="Estado de admisión del caso")
    desistimiento_queja: int = Field(2, description="Desistimiento del consumidor financiero")
    anexo_queja: bool = Field(..., description="Informa la presencia de archivos anexos en la transacción")
    tutela: int = Field(2, description="Relación con acción de tutela")
    ente_control: int = Field(99, description="Remisión a entes de control externos")
    queja_expres: int = Field(1, description="Marca de queja exprés")
    
    # Campos operacionales condicionales (Opcionales en trámite ordinario)
    a_favor_de: Optional[int] = Field(None, description="Sentido de la decisión final (Exigido en cierres)")
    aceptacion_queja: Optional[int] = Field(None, description="Aceptación por la entidad")
    rectificacion_queja: Optional[int] = Field(None, description="Rectificación de información")
    prorroga_queja: Optional[int] = Field(None, description="Uso de prórroga por la entidad")
    documentacion_rta_final: Optional[bool] = Field(None, description="Referencia a la presencia de respuesta final")
    
    # 🎯 CORRECCIÓN M3: Debe ser String para mantener el formato completo si lo enviamos en ISO
    fecha_cierre: Optional[str] = Field(None, description="Fecha y hora de cierre definitivo (YYYY-MM-DDThh:mm:ss)")
    marcacion: Optional[int] = Field(None, description="Marcaciones adicionales del caso")
    
    # Campos obligatorios exclusivos para la mitigación y gestión de fraudes
    tipo_fraude: Optional[int] = Field(1, description="Clasificación del fraude según catálogo SFC")
    modalidad_fraude: Optional[int] = Field(1, description="Modalidad detectada según catálogo SFC")
    monto_reclamado: Optional[int] = Field(0, description="Valor total reclamado por el consumidor")
    monto_reconocido: Optional[int] = Field(0, description="Valor final reconocido/devuelto por la entidad")
    
    @field_validator("tipo_fraude", "modalidad_fraude", mode="before")
    @classmethod
    def normalizar_enteros_fraude(cls, v):
        return v if v is not None else 1

    @field_validator("monto_reclamado", "monto_reconocido", mode="before")
    @classmethod
    def normalizar_montos_fraude(cls, v):
        return v if v is not None else 0