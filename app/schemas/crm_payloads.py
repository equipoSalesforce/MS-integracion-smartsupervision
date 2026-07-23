# app/schemas/crm_payloads.py
import re
from datetime import date
from typing import List, Optional
from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
    ValidationInfo,
)

from app.core.mapping import SfcSalesforceMapper
from app.core.config import settings


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
    canal__c: Optional[str] = Field(None, description="canal_cod traducido a texto, Puede ser None")
    sc_Condicion_especial__c: str = Field(..., description="condicion_especial traducido a texto")
    Product__c: str = Field(..., description="producto_cod traducido")
    smart_Producto_nombre__c: Optional[str] = Field(None, description="producto_nombre traducido")
    Categorias_COL__c: str = Field(..., description="macro_motivo_cod traducido")
    Description: str = Field(..., description="texto_queja traducido y libre de HTML")
    smart_anexo_queja__c: bool = Field(..., description="anexo_queja traducido")
    Tutela__c: str = Field("No", description="tutela traducida, opcional y su valor por defecto es 'No'")
    Ente_de_control__c: str = Field(..., description="ente_control traducido a texto")
    smart_escalamiento_DCF__c: str = Field(..., description="escalamiento_DCF traducido")
    replica__c: str = Field(..., description="replica traducida")
    argumento_replica__c: Optional[str] = Field(None, description="argumento_replica traducido")
    Desistimiento__c: str = Field(..., description="desistimiento_queja traducido")
    Quejas_express__c: Optional[str] = Field("No", description="queja_expres traducido, valor por defecto No")
    direccion__c: Optional[str] = Field(None, description="Dirección física de domicilio del cliente")

    archivos_s3: List[ArchivoS3Schema] = Field(default=[], description="Colección de metadatos de archivos alojados en S3")


# ======================================================================
# 📦 ESQUEMA BASE CON VALIDACIONES DE CATÁLOGO (MOMENTO 2)
# ======================================================================

class Momento2QuejaCrmInput(BaseModel):
    # 🎯 ID Interno y Smart Code son opcionales individualmente, pero al menos uno debe estar presente
    Case_id: Optional[str] = Field(None, description="Código original único de la base de datos de Salesforce")
    Smart_Code__c: Optional[str] = Field(None, description="Código único de la queja en SmartSupervision")

    CreatedDate: str = Field(..., description="Fecha/Hora de creación ISO")
    Status: Optional[str] = Field("New", description="Estado del caso dentro del CRM")

    # Campos de Picklist validados dinámicamente desde el Mapper/JSON
    SuppliedName: str = Field(..., description="Nombre completo del cliente")
    SC_id_type__c: str = Field(..., description="Tipo de identificación")
    id_number__c: str = Field(..., max_length=15, description="Número de identificación sólo dígitos")
    sc_genero__c: Optional[str] = Field(None, description="Género, es opcional y puede ser None")
    tipo_de_persona__c: str = Field(..., description="Tipo de persona (B2C, B2B)")
    sc_LGBTIQ__c: Optional[str] = Field(None, description="Comunidad LGBTIQ (Si/No), es opcional y puede ser None")
    sc_Condicion_especial__c: Optional[str] = Field(None, description="Condición de vulnerabilidad, es opcional y puede ser None")

    # Ubicación y Contacto
    SuppliedPhone: Optional[str] = Field(None, description="Teléfono de contacto")
    SuppliedEmail: Optional[str] = Field(None, description="Correo electrónico del cliente")
    direccion__c: str = Field(..., description="Dirección física de correspondencia")
    Departamento__c: Optional[str] = Field(None, description="Departamento, opcional si no es de Colombia")
    SC_municipio__c: Optional[str] = Field(None, description="Municipio, opcional si no es de Colombia")

    # Recepción y Clasificación
    canal__c: Optional[str] = Field(None, description="Canal de ingreso, opcional y puede ser None")
    punto_recepcion: str = Field(..., description="Punto de radicación")
    Instancia_de_recepcion__c: str = Field(..., description="Instancia de recepción")
    admision_col__c: str = Field("No Aplica", description="Estado inicial de admisión")

    # Detalles de la Reclamación
    Description: str = Field(..., max_length=4500, description="Descripción original del reclamo")
    smart_anexo_queja__c: Optional[bool] = Field(False, description="Indica si posee archivos adjuntos, por defecto es False")
    Tutela__c: str = Field("No", description="Acción de tutela (Si/No)")
    Ente_de_control__c: Optional[str] = Field(None, description="Ente regulador involucrado, opcional y admite None")
    smart_escalamiento_DCF__c: str = Field(..., description="Escalamiento DCF")
    marcacion__c: Optional[str] = Field(None, description="Marcación, es opcional y admite None")

    # Tipificación
    Product__c: str = Field(..., description="Línea de producto")
    smart_Producto_nombre__c: Optional[str] = Field(None, description="Nombre del producto digital")
    Categorias_COL__c: str = Field(..., description="Motivo de reclamación")

    # Adjuntos
    archivos_s3: List[ArchivoS3Schema] = Field(default=[], description="Colección de archivos en S3")

    @model_validator(mode="after")
    def resolver_y_armar_smart_code(self) -> "Momento2QuejaCrmInput":
        if not self.Case_id and not self.Smart_Code__c:
            raise ValueError("Debe incluir al menos 'Case_id' o 'Smart_Code__c' en el payload de la petición.")

        prefix = f"{settings.SFC_TIPO_ENTIDAD}{settings.SFC_ENTIDAD_COD}"  # Ej: "1423"

        if not self.Smart_Code__c and self.Case_id:
            raw_id = str(self.Case_id).strip()
            self.Smart_Code__c = f"{prefix}{raw_id}"

        elif self.Smart_Code__c:
            clean_sc = str(self.Smart_Code__c).strip()
            self.Smart_Code__c = clean_sc if clean_sc.startswith(prefix) else f"{prefix}{clean_sc}"

        if not self.Case_id:
            self.Case_id = self.Smart_Code__c

        return self

    @field_validator("id_number__c", mode="before")
    @classmethod
    def limpiar_id_solo_numeros(cls, v: str) -> str:
        if isinstance(v, str):
            cleaned = re.sub(r"\D", "", v)
            return cleaned if cleaned else v
        return v

    @field_validator("Smart_Code__c", "Case_id", mode="before")
    @classmethod
    def limpiar_espacios_y_caracteres(cls, v: Optional[str]) -> Optional[str]:
        if isinstance(v, str):
            cleaned = v.strip()
            return cleaned if cleaned else None
        return v

    @field_validator(
        "SC_id_type__c", "sc_genero__c", "tipo_de_persona__c", "sc_LGBTIQ__c",
        "sc_Condicion_especial__c", "canal__c", "punto_recepcion",
        "Instancia_de_recepcion__c", "Ente_de_control__c", "Categorias_COL__c",
        mode="after"
    )
    @classmethod
    def validar_picklist_contra_mapper(cls, value: Optional[str], info: ValidationInfo) -> Optional[str]:
        if value is None:
            return value

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
                normalized_val = SfcSalesforceMapper._normalize_text(value)
                allowed_normalized = {SfcSalesforceMapper._normalize_text(a) for a in allowed}
                if normalized_val not in allowed_normalized:
                    raise ValueError(
                        f"El valor '{value}' no es válido para {info.field_name}. "
                        f"Valores soportados: {sorted(list(allowed))[:5]}... (Total {len(allowed)})"
                    )
        return value


# ======================================================================
# 🚀 ESQUEMA UNIFICADO DE DESPACHO (SIN TIPO_OPERACION)
# ======================================================================

class QuejaUnificadaCrmInput(Momento2QuejaCrmInput):
    """
    Payload unificado del CRM. Infiere automáticamente las intenciones de negocio
    basándose exclusivamente en los campos provistos.
    """
    # --- Opcionales Trámite ---
    producto_digital__c: Optional[str] = Field("Si", description="Producto digital (Si/No)")

    # --- Opcionales Fraude ---
    tipo_fraude__c: Optional[str] = Field(None, description="Tipo de fraude")
    modalidad_fraude__c: Optional[str] = Field(None, description="Modalidad de fraude")
    card_amount__c: Optional[float] = Field(None, ge=0.0, description="Monto reclamado")
    Total_Devuelto_por_Desconocimiento__c: Optional[float] = Field(None, ge=0.0, description="Monto devuelto")
    nombre_archivo_fraude: Optional[str] = Field(None, description="Archivo INV_FRAUDE_SFC")

    # --- Opcionales Cierre ---
    ClosedDate: Optional[date] = Field(None, description="Fecha de cierre")
    Favorabilidad__c: Optional[str] = Field(None, description="Favorabilidad del caso")
    a_favor_de__c: Optional[str] = Field(None, description="A favor de")
    Aceptacion__c: Optional[str] = Field(None, description="Aceptación de la decisión")
    Rectificacion__c: Optional[str] = Field(None, description="Rectificación")
    Prorroga__c: Optional[str] = Field(None, description="Prórroga solicitada")
    #TODO: Dejar listo el mensaje final real
    cuerpo_respuesta_final: Optional[str] = Field(
        "Se emite respuesta formal y cierre definitivo al caso de reclamación conforme a los términos de ley y políticas de la entidad.",
        description="Cuerpo del correo en HTML con la respuesta final al caso. Si no se envía, se autogenera una respuesta genérica."
    )
    @model_validator(mode="after")
    def validar_reglas_segun_datos_presentes(self) -> "QuejaUnificadaCrmInput":
        num_archivos = len(self.archivos_s3)
        es_estado_cierre = self.Status == "Closed" or self.ClosedDate is not None or self.Favorabilidad__c is not None
        es_evento_fraude = self.tipo_fraude__c is not None or self.modalidad_fraude__c is not None

        # Validaciones para intenciones de CIERRE
        if es_estado_cierre:
            if not self.ClosedDate or not self.Favorabilidad__c or not self.Aceptacion__c:
                raise ValueError("Para ejecutar un Cierre Definitivo es obligatorio proveer 'ClosedDate', 'Favorabilidad__c' y 'Aceptacion__c'.")

            if self.ClosedDate > date.today():
                raise ValueError(f"La fecha de cierre 'ClosedDate' ({self.ClosedDate}) no puede ser posterior a la fecha actual.")

            # REGLA DE ORO: Debe venir el contenido del correo para construir el PDF
            if not self.cuerpo_respuesta_final or not self.cuerpo_respuesta_final.strip():
                self.cuerpo_respuesta_final = (
                    "Se emite respuesta formal y cierre definitivo al caso de reclamación "
                    "conforme a los términos de ley y políticas de la entidad."
                )
        # Validaciones para intenciones de FRAUDE
        if es_evento_fraude:
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
    ids_quejas: List[str] = Field(
        ...,
        min_length=1,
        description="Lista de IDs / Smart_Codes persistidos en CRM."
    )

    @field_validator("ids_quejas")
    @classmethod
    def validar_elementos_no_vacios(cls, v: List[str]) -> List[str]:
        cleaned = [item.strip() for item in v if item and item.strip()]
        if not cleaned:
            raise ValueError("La lista 'ids_quejas' debe contener al menos un identificador válido no vacío.")
        return cleaned