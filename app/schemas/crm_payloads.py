# app/schemas/crm_payloads.py
import re

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Optional, Literal
from datetime import date

class ArchivoS3Schema(BaseModel):
    nombre_archivo: str = Field(..., description="Nombre final del archivo guardado")
    s3_key: str = Field(..., description="Ruta/Clave única de acceso en el bucket S3")
    bucket: str = Field(..., description="Bucket de S3 donde se alojó")


class QuejaMapeadaCrmResponse(BaseModel):
    """
    Equivalente exacto y mapeado de los campos que entrega la SFC en el Momento 1.
    """
    CreatedDate: str = Field(..., description="fecha_creacion traducida a ISO")
    Smart_Code__c: str = Field(..., description="codigo_queja traducido")
    codigo_pais__c: str = Field(..., description="codigo_pais traducido")
    Departamento__c: str = Field(..., description="departamento_cod traducido a texto")
    SC_municipio__c: str = Field(..., description="municipio_cod traducido a texto")
    SuppliedName: str = Field(..., description="nombres traducido")
    SC_id_type__c: str = Field(..., description="tipo_id_CF traducido")
    id_number__c: str = Field(..., description="numero_id_CF traducido")
    SuppliedPhone: Optional[str] = Field(None, description="telefono traducido")
    SuppliedEmail: Optional[str] = Field(None, description="correo traducido")
    tipo_de_persona__c: str = Field(..., description="tipo_persona traducido a texto")
    sc_genero__c: str = Field(..., description="sexo traducido a texto")
    sc_LGBTIQ__c: str = Field(..., description="lgbtiq traducido")
    canal__c: str = Field(..., description="canal_cod traducido a texto")
    sc_Condicion_especial__c: str = Field(..., description="condicion_especial traducido a texto")
    Product__c: str = Field(..., description="producto_cod traducido")
    smart_Producto_nombre__c: Optional[str] = Field(None, description="producto_nombre traducido")
    Categorias_COL__c: str = Field(..., description="macro_motivo_cod traducido")
    Description: str = Field(..., description="texto_queja traducido y libre de HTML")
    smart_anexo_queja__c: bool = Field(..., description="anexo_queja traducido")
    Tutela__c: str = Field(..., description="tutela traducida")
    Ente_de_control__c: str = Field(..., description="ente_control traducido a texto")
    smart_escalamiento_DCF__c: str = Field(..., description="escalamiento_DCF traducido")
    replica__c: str = Field(..., description="replica traducida")
    argumento_replica__c: Optional[str] = Field(None, description="argumento_replica traducido")
    Desistimiento__c: str = Field(..., description="desistimiento_queja traducido")
    Quejas_express__c: str = Field(..., description="queja_expres traducido")
    direccion__c: Optional[str] = Field(None, description="Dirección física de domicilio del cliente")

    archivos_s3: List[ArchivoS3Schema] = Field(
        default=[],
        description="Colección de metadatos de archivos alojados en S3"
    )


# ======================================================================
# 📦 ESQUEMA BASE CON DATOS OBLIGATORIOS (MOMENTO 2)
# ======================================================================

class Momento2QuejaCrmInput(BaseModel):
    """
    Estructura obligatoria requerida por el CRM para la identificación y 
    clasificación base del caso en el pipeline de sincronización.
    """
    # Identificadores y Control
    Smart_Code__c: str = Field(..., description="Código único de la queja")
    CreatedDate: str = Field(..., description="Fecha/Hora de creación ISO")
    Status: Optional[str] = Field("New", description="Estado del caso dentro del CRM")

    # Información Demográfica
    SuppliedName: str = Field(..., description="Nombre completo del cliente")
    SC_id_type__c: Literal["CC", 
        "CE", 
        "RUT", 
        "DNI",
        "PASS", 
        "Carné diplomático", 
        "Sociedad extranjera sin NIT", 
        "PEP",
        "NUIP", 
        "PPT"] = Field(..., description="Tipo de identificación")
    id_number__c: str = Field(..., max_length=15, description="Número de identificación")
    sc_genero__c: str = Field(..., description="Género")
    tipo_de_persona__c: Literal["B2C", "B2B"] = Field(..., description="Tipo de persona (B2C, B2B)")
    sc_LGBTIQ__c: str = Field(..., description="Comunidad LGBTIQ (Si/No)")
    sc_Condicion_especial__c: str = Field(..., description="Condición de vulnerabilidad")

    # Ubicación y Contacto
    SuppliedPhone: Optional[str] = Field(None, description="Teléfono")
    SuppliedEmail: Optional[str] = Field(None, description="Correo electrónico")
    direccion__c: str = Field(..., description="Dirección física")
    Departamento__c: str = Field(..., description="Departamento")
    SC_municipio__c: str = Field(..., description="Municipio")

    # Recepción y Clasificación
    canal__c: str = Field(..., description="Canal de ingreso")
    punto_recepcion: str = Field(..., description="Punto de radicación")
    Instancia_de_recepcion__c: str = Field(..., description="Instancia de recepción")
    admision_col__c: str = Field("No Aplica", description="Estado inicial de admisión")

    # Detalles de la Reclamación
    Description: str = Field(..., max_length=4500 , description="Descripción original del reclamo")
    smart_anexo_queja__c: bool = Field(..., description="Indica si posee archivos adjuntos")
    Tutela__c: str = Field("No", description="Acción de tutela (Si/No)")
    Ente_de_control__c: str = Field("Otros", description="Ente regulador involucrado")
    smart_escalamiento_DCF__c: str = Field(..., description="Escalamiento DCF")
    
    # Tipificación
    Product__c: str = Field(..., description="Línea de producto")
    smart_Producto_nombre__c: Optional[str] = Field(None, description="Nombre del producto digital")
    Categorias_COL__c: str = Field(..., description="Motivo de reclamación")
    
    # Adjuntos
    archivos_s3: List[ArchivoS3Schema] = Field(default=[], description="Colección de archivos en S3")
    
    @field_validator("Smart_Code__c", mode="before")
    @classmethod
    def limpiar_espacios_y_caracteres(cls, v: str) -> str:
        if isinstance(v, str):
            return v.strip()
        return v
    
    @field_validator("id_number__c", mode="before")
    @classmethod
    def limpiar_id_solo_numeros(cls, v: str) -> str:
        if isinstance(v, str):
            # r"\D" significa "cualquier cosa que NO sea un dígito numérico".
            # Lo reemplazamos por "" (nada).
            # Ejemplo: "P-123.456 A" -> "123456"
            return re.sub(r"\D", "", v)
        return v
    
    @field_validator("Smart_Code__c", "id_number__c", mode="before")
    @classmethod
    def limpiar_espacios_y_caracteres(cls, v: str) -> str:
        if isinstance(v, str):
            return v.strip()
        return v


# ======================================================================
# 🚀 ESQUEMA UNIFICADO DE DESPACHO (MOMENTO 2 + MOMENTO 3)
# ======================================================================

class QuejaUnificadaCrmInput(Momento2QuejaCrmInput):
    """
    Esquema unificado para la puerta de entrada única del CRM.
    Incluye todos los campos de Creación (M2) y de forma OPCIONAL las 
    variables de actualización, investigación de Fraude o Cierre Definitivo (M3).
    """
    # Flag Opcional de Operación Explicita
    tipo_operacion: Optional[Literal["AUTO", "TRAMITE", "FRAUDE", "CIERRE"]] = Field(
        "AUTO", 
        description="Fuerza un tipo de flujo o permite la inferencia automática basada en campos"
    )

    # --- Campos Opcionales de Trámite / Actualización ---
    producto_digital__c: Optional[str] = Field("Si", description="Indica si corresponde a un producto digital")

    # --- Campos Opcionales de Fraude ---
    tipo_fraude__c: Optional[str] = Field(None, description="Código de clasificación del fraude")
    modalidad_fraude__c: Optional[str] = Field(None, description="Código de modalidad de fraude")
    card_amount__c: Optional[float] = Field(None, description="Monto reclamado por fraude")
    Total_Devuelto_por_Desconocimiento__c: Optional[float] = Field(None, description="Monto devuelto por fraude")
    nombre_archivo_fraude: Optional[str] = Field(None, description="Archivo objetivo para INV_FRAUDE_SFC")

    # --- Campos Opcionales de Cierre Definitivo ---
    ClosedDate: Optional[date] = Field(None, description="Fecha de cierre definitivo (YYYY-MM-DD)")
    Favorabilidad__c: Optional[str] = Field(None, description="Sentido de la decisión final")
    a_favor_de__c: Optional[int] = Field(1, description="Mapeo de a favor de quien se resuelve")
    Aceptacion__c: Optional[str] = Field(None, description="Indica si hubo aceptación")
    Rectificacion__c: Optional[bool] = Field(False, description="Indica si hubo rectificación")
    Prorroga__c: Optional[bool] = Field(False, description="Indica uso de prórroga")
    nombre_archivo_final: Optional[str] = Field(None, description="Archivo objetivo para RESP_FINAL_SFC")

    @model_validator(mode="after")
    def validar_reglas_segun_tipo_evento(self) -> "QuejaUnificadaCrmInput":
        """
        Infiere el tipo de evento y valida la presencia obligatoria de anexos y parámetros
        según las regulaciones de la SFC para Fraude o Cierre.
        """
        num_archivos = len(self.archivos_s3)
        es_estado_cierre = self.Status == "Closed" or self.ClosedDate is not None
        es_evento_fraude = self.tipo_fraude__c is not None or self.modalidad_fraude__c is not None

        # ------------------------------------------------------------------
        # 1. VALIDACIÓN DE REGLAS DE CIERRE DEFINITIVO
        # ------------------------------------------------------------------
        if es_estado_cierre or self.tipo_operacion == "CIERRE":
            if not self.ClosedDate or not self.Favorabilidad__c or not self.Aceptacion__c:
                raise ValueError(
                    "Para ejecutar el Cierre Definitivo (Estado 4) es obligatorio proveer "
                    "'ClosedDate', 'Favorabilidad__c' y 'Aceptacion__c'."
                )

            if num_archivos == 0:
                raise ValueError("No se envió un documento de cierre del caso (RESP_FINAL_SFC).")

            if not self.nombre_archivo_final:
                if num_archivos == 1:
                    self.nombre_archivo_final = self.archivos_s3[0].nombre_archivo
                else:
                    raise ValueError(
                        f"Se recibieron {num_archivos} archivos. Es obligatorio especificar "
                        f"'nombre_archivo_final' para asociar la carta de resolución de cierre."
                    )
            else:
                nombres_en_lista = [a.nombre_archivo for a in self.archivos_s3]
                if self.nombre_archivo_final not in nombres_en_lista:
                    raise ValueError(
                        f"El archivo especificado '{self.nombre_archivo_final}' "
                        f"no se encuentra dentro del listado de archivos_s3 provistos."
                    )

        # ------------------------------------------------------------------
        # 2. VALIDACIÓN DE REGLAS DE INVESTIGACIÓN DE FRAUDE
        # ------------------------------------------------------------------
        if es_evento_fraude or self.tipo_operacion == "FRAUDE":
            if num_archivos == 0:
                raise ValueError("No se envió un documento de investigación de fraude (INV_FRAUDE_SFC).")

            if not self.nombre_archivo_fraude:
                if num_archivos == 1:
                    self.nombre_archivo_fraude = self.archivos_s3[0].nombre_archivo
                else:
                    raise ValueError(
                        f"Se recibieron {num_archivos} archivos. Es obligatorio especificar "
                        f"'nombre_archivo_fraude' para asociar el dictamen de investigación."
                    )
            else:
                nombres_en_lista = [a.nombre_archivo for a in self.archivos_s3]
                if self.nombre_archivo_fraude not in nombres_en_lista:
                    raise ValueError(
                        f"El archivo especificado '{self.nombre_archivo_fraude}' "
                        f"no se encuentra dentro del listado de archivos_s3 provistos."
                    )

        return self

class ConfirmacionAckInput(BaseModel):
    ids_quejas: List[str] = Field(
        ..., 
        min_length=1, 
        description="Lista de IDs / Smart_Codes de las quejas persistidas exitosamente en el CRM."
    )