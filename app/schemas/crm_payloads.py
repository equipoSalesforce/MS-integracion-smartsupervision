# app/schemas/crm_payloads.py
import re
from pydantic import BaseModel, Field, field_validator, model_validator, ValidationInfo
from typing import List, Optional, Literal
from datetime import date

from app.core.mapping import SfcSalesforceMapper

class ArchivoS3Schema(BaseModel):
    nombre_archivo: str = Field(..., description="Nombre final del archivo guardado")
    s3_key: str = Field(..., description="Ruta/Clave única de acceso en el bucket S3")
    bucket: str = Field(..., description="Bucket de S3 donde se alojó")


class QuejaMapeadaCrmResponse(BaseModel):
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

    archivos_s3: List[ArchivoS3Schema] = Field(default=[], description="Colección de metadatos de archivos alojados en S3")


# ======================================================================
# 📦 ESQUEMA BASE CON VALIDACIONES DE CATÁLOGO (MOMENTO 2)
# ======================================================================

class Momento2QuejaCrmInput(BaseModel):
    Smart_Code__c: str = Field(..., description="Código único de la queja")
    CreatedDate: str = Field(..., description="Fecha/Hora de creación ISO")
    Status: Optional[str] = Field("New", description="Estado del caso dentro del CRM")

    # Campos de Picklist validados dinámicamente desde el Mapper/JSON
    SuppliedName: str = Field(..., description="Nombre completo del cliente")
    SC_id_type__c: str = Field(..., description="Tipo de identificación")
    id_number__c: str = Field(..., max_length=15, description="Número de identificación")
    sc_genero__c: str = Field(..., description="Género")
    tipo_de_persona__c: str = Field(..., description="Tipo de persona (B2C, B2B)")
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
    Description: str = Field(..., max_length=4500, description="Descripción original del reclamo")
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

    # ------------------------------------------------------------------
    # 🧼 VALIDACIONES DE LIMPIEZA Y FORMATEO
    # ------------------------------------------------------------------
    @field_validator("id_number__c", mode="before")
    @classmethod
    def limpiar_id_solo_numeros(cls, v: str) -> str:
        return re.sub(r"\D", "", v) if isinstance(v, str) else v

    @field_validator("Smart_Code__c", mode="before")
    @classmethod
    def limpiar_espacios_y_caracteres(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    # ------------------------------------------------------------------
    # 🛡️ VALIDACIÓN DINÁMICA DE VALORES DE CATÁLOGO DESDE EL MAPPER
    # ------------------------------------------------------------------
    @field_validator(
        "SC_id_type__c", "sc_genero__c", "tipo_de_persona__c", "sc_LGBTIQ__c",
        "sc_Condicion_especial__c", "canal__c", "punto_recepcion",
        "Instancia_de_recepcion__c", "Ente_de_control__c", "Categorias_COL__c",
        mode="after"
    )
    @classmethod
    def validar_picklist_contra_mapper(cls, value: str, info: ValidationInfo) -> str:
        """Valida que el valor ingresado exista en el catálogo oficial en RAM."""
        field_to_catalog = {
            "SC_id_type__c": "tipo_id",
            "sc_genero__c": "genero",
            "tipo_de_persona__c": "persona",
            "sc_LGBTIQ__c": "lgbtiq",
            "sc_Condicion_especial__c": "condicion_especial",
            "canal__c": "canal",
            "punto_recepcion": "punto_recepcion",
            "Instancia_de_recepcion__c": "instancia_recepcion",
            "Ente_de_control__c": "ente_control",
            "Categorias_COL__c": "macro_motivo"
        }

        cat_key = field_to_catalog.get(info.field_name)
        if cat_key:
            allowed = SfcSalesforceMapper.get_crm_allowed_values(cat_key)
            if value not in allowed:
                # Búsqueda tolerante en minusculas
                normalized_val = SfcSalesforceMapper._normalize_text(value)
                allowed_normalized = {SfcSalesforceMapper._normalize_text(a) for a in allowed}
                
                if normalized_val not in allowed_normalized:
                    raise ValueError(
                        f"El valor '{value}' no es válido para {info.field_name}. "
                        f"Valores soportados: {sorted(list(allowed))[:5]}... (Total {len(allowed)})"
                    )
        return value


# ======================================================================
# 🚀 ESQUEMA UNIFICADO DE DESPACHO
# ======================================================================

class QuejaUnificadaCrmInput(Momento2QuejaCrmInput):
    tipo_operacion: Optional[Literal["AUTO", "TRAMITE", "FRAUDE", "CIERRE"]] = Field("AUTO", description="Tipo de flujo")

    # --- Opcionales Trámite ---
    producto_digital__c: Optional[str] = Field("Si", description="Producto digital")

    # --- Opcionales Fraude ---
    tipo_fraude__c: Optional[str] = Field(None, description="Tipo de fraude")
    modalidad_fraude__c: Optional[str] = Field(None, description="Modalidad de fraude")
    card_amount__c: Optional[float] = Field(None, description="Monto reclamado")
    Total_Devuelto_por_Desconocimiento__c: Optional[float] = Field(None, description="Monto devuelto")
    nombre_archivo_fraude: Optional[str] = Field(None, description="Archivo INV_FRAUDE_SFC")

    # --- Opcionales Cierre ---
    ClosedDate: Optional[date] = Field(None, description="Fecha de cierre")
    Favorabilidad__c: Optional[str] = Field(None, description="Favorabilidad")
    a_favor_de__c: Optional[int] = Field(1, description="A favor de")
    Aceptacion__c: Optional[str] = Field(None, description="Aceptación")
    Rectificacion__c: Optional[bool] = Field(False, description="Rectificación")
    Prorroga__c: Optional[bool] = Field(False, description="Prórroga")
    nombre_archivo_final: Optional[str] = Field(None, description="Archivo RESP_FINAL_SFC")

    @model_validator(mode="after")
    def validar_reglas_segun_tipo_evento(self) -> "QuejaUnificadaCrmInput":
        num_archivos = len(self.archivos_s3)
        es_estado_cierre = self.Status == "Closed" or self.ClosedDate is not None
        es_evento_fraude = self.tipo_fraude__c is not None or self.modalidad_fraude__c is not None

        if es_estado_cierre or self.tipo_operacion == "CIERRE":
            if not self.ClosedDate or not self.Favorabilidad__c or not self.Aceptacion__c:
                raise ValueError("Para ejecutar el Cierre Definitivo (Estado 4) es obligatorio proveer 'ClosedDate', 'Favorabilidad__c' y 'Aceptacion__c'.")
            if num_archivos == 0:
                raise ValueError("No se envió un documento de cierre del caso (RESP_FINAL_SFC).")
            if not self.nombre_archivo_final:
                if num_archivos == 1:
                    self.nombre_archivo_final = self.archivos_s3[0].nombre_archivo
                else:
                    raise ValueError(f"Se recibieron {num_archivos} archivos. Es obligatorio especificar 'nombre_archivo_final'.")
            else:
                nombres_en_lista = [a.nombre_archivo for a in self.archivos_s3]
                if self.nombre_archivo_final not in nombres_en_lista:
                    raise ValueError(f"El archivo especificado '{self.nombre_archivo_final}' no se encuentra dentro de archivos_s3.")

        if es_evento_fraude or self.tipo_operacion == "FRAUDE":
            if num_archivos == 0:
                raise ValueError("No se envió un documento de investigación de fraude (INV_FRAUDE_SFC).")
            if not self.nombre_archivo_fraude:
                if num_archivos == 1:
                    self.nombre_archivo_fraude = self.archivos_s3[0].nombre_archivo
                else:
                    raise ValueError(f"Se recibieron {num_archivos} archivos. Es obligatorio especificar 'nombre_archivo_fraude'.")
            else:
                nombres_en_lista = [a.nombre_archivo for a in self.archivos_s3]
                if self.nombre_archivo_fraude not in nombres_en_lista:
                    raise ValueError(f"El archivo especificado '{self.nombre_archivo_fraude}' no se encuentra dentro de archivos_s3.")

        return self


class ConfirmacionAckInput(BaseModel):
    ids_quejas: List[str] = Field(..., min_length=1, description="Lista de IDs / Smart_Codes persistidos en CRM.")