import re
from datetime import date, datetime
from typing import Any, List, Optional
from zoneinfo import ZoneInfo
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
    Departamento__c: Optional[str] = Field(..., description="departamento_cod traducido a texto")
    SC_municipio__c: Optional[str] = Field(..., description="municipio_cod traducido a texto")
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
    Ente_de_control__c: Optional[str] = Field(..., description="ente_control traducido a texto")
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
    Case_id: Optional[str] = Field(None, description="Código original único de la base de datos de Salesforce", max_length=26)
    Smart_Code__c: Optional[str] = Field(None, description="Código único de la queja en SmartSupervision", max_length=30)

    CreatedDate: Optional[str] = Field(
        None, 
        description="Fecha/Hora de creación ISO. Si se omite o es null, se autogenera en hora Bogotá (UTC-5)."
    )
    Status: Optional[str] = Field(None, description="Estado del caso dentro del CRM")

    SuppliedName: str = Field(..., description="Nombre completo del cliente", max_length=100)
    SC_id_type__c: str = Field(..., description="Tipo de identificación")
    id_number__c: str = Field(..., max_length=15, description="Número de identificación sólo dígitos")
    sc_genero__c: Optional[str] = Field(None, description="Género, es opcional")
    tipo_de_persona__c: str = Field(..., description="Tipo de persona (B2C, B2B)")
    sc_LGBTIQ__c: Optional[str] = Field(None, description="Comunidad LGBTIQ (Si/No)")
    sc_Condicion_especial__c: Optional[str] = Field(None, description="Condición de vulnerabilidad")

    SuppliedPhone: Optional[str] = Field(None, description="Teléfono de contacto", max_length=15)
    SuppliedEmail: Optional[str] = Field(None, description="Correo electrónico del cliente", max_length=100)
    direccion__c: str = Field(..., description="Dirección física de correspondencia")
    Departamento__c: Optional[str] = Field(None, description="Departamento, opcional si no es de Colombia")
    SC_municipio__c: Optional[str] = Field(None, description="Municipio, opcional si no es de Colombia")

    canal__c: Optional[str] = Field(None, description="Canal de ingreso, opcional")
    punto_recepcion: str = Field(..., description="Punto de radicación")
    Instancia_de_recepcion__c: Optional[str] = Field("Entidad vigilada", description="Instancia de recepción")
    admision_col__c: str = Field("No Aplica", description="Estado inicial de admisión")

    Description: str = Field(..., max_length=4500, description="Descripción original del reclamo")
    smart_anexo_queja__c: Optional[bool] = Field(False, description="Indica si posee archivos adjuntos")
    Tutela__c: str = Field("No", description="Acción de tutela (Si/No)")
    Ente_de_control__c: Optional[str] = Field(None, description="Ente regulador involucrado")
    smart_escalamiento_DCF__c: str = Field("No", description="Escalamiento DCF")
    marcacion__c: Optional[str] = Field(None, description="Marcación, opcional")

    Product__c: str = Field(..., description="Línea de producto")
    smart_Producto_nombre__c: Optional[str] = Field(None, description="Nombre del producto digital", max_length=100)
    Categorias_COL__c: str = Field(..., description="Motivo de reclamación", max_length=150)

    archivos_s3: List[ArchivoS3Schema] = Field(default=[], description="Colección de archivos en S3")

    # 🛡️ SANITIZADOR PREVENTIVO CONTRA STORED XSS
    @field_validator("SuppliedName", "direccion__c", "Description", mode="before")
    @classmethod
    def sanitizar_campos_texto(cls, v: Optional[str]) -> Optional[str]:
        if isinstance(v, str):
            clean = v.replace("\x00", "")
            clean = re.sub(r"<(script|style|iframe)\b[^>]*>.*?</\1>", "", clean, flags=re.IGNORECASE | re.DOTALL)
            clean = re.sub(r"<[^>]*>", "", clean)
            return clean.strip()
        return v

    # 🚫 RESILIENCIA EN DIRECCIÓN: Si la dirección queda vacía por sanitización XSS, asigna un fallback válido
    @field_validator("direccion__c", mode="after")
    @classmethod
    def asegurar_direccion_valida(cls, v: Optional[str]) -> str:
        if not v or not v.strip():
            return "Dirección no registrada"
        return v.strip()

    # 🚫 VALIDADOR DE NOMBRE OBLIGATORIO Y NO VACÍO
    @field_validator("SuppliedName", mode="after")
    @classmethod
    def validar_nombre_no_vacio(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("El nombre completo del cliente ('SuppliedName') no puede estar vacío, ser nulo o contener únicamente espacios en blanco.")
        return v.strip()

    @model_validator(mode="after")
    def resolver_y_armar_smart_code(self) -> "Momento2QuejaCrmInput":
        if not self.Case_id and not self.Smart_Code__c:
            raise ValueError("Debe incluir al menos 'Case_id' o 'Smart_Code__c' en el payload de la petición.")

        prefix = f"{settings.SFC_TIPO_ENTIDAD}{settings.SFC_ENTIDAD_COD}"

        if not self.Smart_Code__c and self.Case_id:
            raw_id = str(self.Case_id).strip()
            self.Smart_Code__c = f"{prefix}{raw_id}"

        elif self.Smart_Code__c:
            clean_sc = str(self.Smart_Code__c).strip()
            self.Smart_Code__c = clean_sc if clean_sc.startswith(prefix) else f"{prefix}{clean_sc}"

        if not self.Case_id:
            self.Case_id = self.Smart_Code__c

        return self
    
    @field_validator("SuppliedEmail", mode="after")
    @classmethod
    def validar_formato_email(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            email_clean = v.strip()
            if email_clean:
                pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
                if not re.match(pattern, email_clean):
                    raise ValueError(f"El correo electrónico '{v}' no tiene un formato válido (debe incluir '@' y un dominio válido).")
                return email_clean
        return v

    @field_validator("id_number__c", mode="before")
    @classmethod
    def limpiar_id_caracteres_especiales(cls, v: str) -> str:
        if isinstance(v, str):
            cleaned = re.sub(r"[^a-zA-Z0-9]", "", v)
            if not cleaned:
                raise ValueError(
                    "El número de identificación ('id_number__c') debe contener al menos un carácter alfanumérico válido."
                )
            return cleaned
        return v

    @field_validator("CreatedDate", mode="after")
    @classmethod
    def validar_formato_iso_fecha(cls, v: str) -> str:
        if isinstance(v, str) and v.strip():
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"El campo 'CreatedDate' con valor '{v}' debe cumplir con un formato ISO 8601 válido.")
        return v

    @field_validator("Smart_Code__c", "Case_id", mode="before")
    @classmethod
    def limpiar_espacios_y_caracteres(cls, v: Optional[str], info: ValidationInfo) -> Optional[str]:
        if isinstance(v, str):
            cleaned = v.strip()
            if cleaned:
                if not re.match(r"^[a-zA-Z0-9_-]+$", cleaned):
                    raise ValueError(
                        f"El identificador '{info.field_name}' con valor '{cleaned}' contiene caracteres no permitidos. "
                        f"Únicamente se aceptan letras, números, guiones medios (-) y guiones bajos (_)."
                    )
                return cleaned
            return None
        return v

    # 📋 SEPARACIÓN DE PICKLISTS: ESTRICTOS vs. RESILIENTES CON FALLBACK
    @field_validator(
        "SC_id_type__c", "sc_genero__c", "tipo_de_persona__c", "sc_LGBTIQ__c",
        "sc_Condicion_especial__c", "canal__c", "punto_recepcion",
        "Instancia_de_recepcion__c", "Ente_de_control__c", "Categorias_COL__c",
        "Product__c", "Tutela__c", "smart_escalamiento_DCF__c",
        mode="after"
    )
    @classmethod
    def validar_picklist_contra_mapper(cls, value: Optional[str], info: ValidationInfo) -> Optional[str]:
        if value is None:
            return value

        # 🎯 CAMPOS CRÍTICOS DE NEGOCIO (RECHAZO ESTRICTO HTTP 400)
        campos_estrictos = {
            "SC_id_type__c": "tipo_id",
            "tipo_de_persona__c": "persona",
            "Categorias_COL__c": "macro_motivo",
            "Product__c": "producto"
        }

        # 🛡️ CAMPOS SECUNDARIOS / OPCIONALES (RESILIENTES CON FALLBACK A VALOR CANÓNICO)
        campos_resilientes_default = {
            "sc_genero__c": "No Aplica",
            "sc_LGBTIQ__c": "No",
            "sc_Condicion_especial__c": "No aplica",
            "canal__c": "Internet",
            "punto_recepcion": "Manual",
            "Instancia_de_recepcion__c": "Entidad vigilada",
            "Ente_de_control__c": "Otros",
            "Tutela__c": "No",
            "smart_escalamiento_DCF__c": "No"
        }

        field_name = info.field_name

        if field_name == "SC_id_type__c" and value.upper() in ("NIT", "N.I.T."):
            return "RUT"

        # 1. Tratamiento para campos estrictos
        if field_name in campos_estrictos:
            cat_key = campos_estrictos[field_name]
            allowed = SfcSalesforceMapper.get_crm_allowed_values(cat_key)
            if value not in allowed:
                normalized_val = SfcSalesforceMapper._normalize_text(value)
                norm_to_canonical = {SfcSalesforceMapper._normalize_text(a): a for a in allowed}
                if normalized_val in norm_to_canonical:
                    return norm_to_canonical[normalized_val]
                
                raise ValueError(
                    f"El valor '{value}' no es válido para {field_name}. "
                    f"Valores soportados: {sorted(list(allowed))[:5]}... (Total {len(allowed)})"
                )
            return value

        # 2. Tratamiento para campos resilientes (Si no coincide, se normaliza o autocompleta con fallback)
        if field_name in campos_resilientes_default:
            cat_key_res = {
                "sc_genero__c": "genero", "sc_LGBTIQ__c": "lgbtiq",
                "sc_Condicion_especial__c": "condicion_especial", "canal__c": "canal",
                "punto_recepcion": "punto_recepcion", "Instancia_de_recepcion__c": "instancia_recepcion",
                "Ente_de_control__c": "ente_control"
            }.get(field_name)

            if cat_key_res:
                allowed = SfcSalesforceMapper.get_crm_allowed_values(cat_key_res)
                if value in allowed:
                    return value
                normalized_val = SfcSalesforceMapper._normalize_text(value)
                norm_to_canonical = {SfcSalesforceMapper._normalize_text(a): a for a in allowed}
                if normalized_val in norm_to_canonical:
                    return norm_to_canonical[normalized_val]

            # Fallback seguro para que la petición continúe sin fallar
            return campos_resilientes_default[field_name]

        return value
    
    @field_validator("CreatedDate", mode="before")
    @classmethod
    def auto_completar_y_validar_fecha_creacion(cls, v: Optional[str]) -> str:
        if not v or not str(v).strip():
            return datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m-%dT%H:%M:%S")
        
        if isinstance(v, str):
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"El campo 'CreatedDate' con valor '{v}' debe cumplir con un formato ISO 8601 válido.")
        return v


# ======================================================================
# 🚀 ESQUEMA UNIFICADO DE DESPACHO (SIN TIPO_OPERACION)
# ======================================================================

class QuejaUnificadaCrmInput(Momento2QuejaCrmInput):
    producto_digital__c: Optional[str] = Field("Si", description="Producto digital (Si/No)")

    tipo_fraude__c: Optional[str] = Field(None, description="Tipo de fraude")
    modalidad_fraude__c: Optional[str] = Field(None, description="Modalidad de fraude")
    card_amount__c: Optional[float] = Field(None, ge=0.0, description="Monto reclamado")
    Total_Devuelto_por_Desconocimiento__c: Optional[float] = Field(None, ge=0.0, description="Monto devuelto")
    nombre_archivo_fraude: Optional[str] = Field(None, description="Archivo INV_FRAUDE_SFC")

    ClosedDate: Optional[date] = Field(None, description="Fecha de cierre (YYYY-MM-DD)")
    Favorabilidad__c: Optional[str] = Field(None, description="Favorabilidad del caso")
    a_favor_de__c: Optional[str] = Field(None, description="A favor de")
    Aceptacion__c: Optional[str] = Field(None, description="Aceptación de la decisión")
    Rectificacion__c: Optional[str] = Field(None, description="Rectificación")
    Prorroga__c: Optional[int] = Field(None, ge=0, le=9, description="Prórroga solicitada, va desde 0 hasta 9")
    
    cuerpo_respuesta_final: Optional[str] = Field(
        "Hola:\nTe escribimos desde el equipo de Experiencia al Cliente.\nPara nosotros es un placer haberte atendido.",
        description="Cuerpo del correo en HTML"
    )
    
    directorio_s3: Optional[str] = Field(None, description="Ruta/Prefix en S3")
    
    @field_validator("ClosedDate", mode="before")
    @classmethod
    def normalizar_closed_date(cls, v: Any) -> Optional[Any]:
        if isinstance(v, str):
            v_clean = v.strip()
            if not v_clean:
                return None
            
            # Si viene en formato ISO completo con hora (ej. "2026-08-05T14:20:00" o "2026-08-05T14:20:00Z")
            if "T" in v_clean:
                try:
                    return datetime.fromisoformat(v_clean.replace("Z", "+00:00")).date()
                except ValueError:
                    pass

            # Si viene en formato fecha estándar YYYY-MM-DD
            try:
                return date.fromisoformat(v_clean)
            except ValueError:
                raise ValueError(
                    f"El campo 'ClosedDate' con valor '{v}' debe cumplir con un formato de fecha válido (YYYY-MM-DD o ISO 8601)."
                )
        return v

    @field_validator("Favorabilidad__c", "Aceptacion__c", "Status", mode="before")
    @classmethod
    def limpiar_y_strip_cierre_strings(cls, v: Optional[str]) -> Optional[str]:
        if isinstance(v, str):
            v_clean = v.strip()
            return v_clean if v_clean else None
        return v

    @field_validator("Favorabilidad__c", "Aceptacion__c", mode="after")
    @classmethod
    def validar_picklist_cierre(cls, value: Optional[str], info: ValidationInfo) -> Optional[str]:
        if not value:
            return value

        cat_key = "favorabilidad" if info.field_name == "Favorabilidad__c" else "aceptacion"
        allowed = SfcSalesforceMapper.get_crm_allowed_values(cat_key)
        
        if allowed and value not in allowed:
            normalized_val = SfcSalesforceMapper._normalize_text(value)
            norm_to_canonical = {SfcSalesforceMapper._normalize_text(a): a for a in allowed}
            
            if normalized_val in norm_to_canonical:
                return norm_to_canonical[normalized_val]

            raise ValueError(
                f"El valor '{value}' no es válido para {info.field_name}. "
                f"Valores permitidos: {sorted(list(allowed))}"
            )
        return value

    @model_validator(mode="after")
    def validar_reglas_segun_datos_presentes(self) -> "QuejaUnificadaCrmInput":
        num_archivos = len(self.archivos_s3)
        tiene_directorio = bool(self.directorio_s3 and self.directorio_s3.strip())
        
        status_clean = (self.Status or "").strip().lower()
        
        es_estado_cierre = (
            status_clean in ("closed", "cerrado") or 
            self.ClosedDate is not None or 
            self.Favorabilidad__c is not None or
            self.Aceptacion__c is not None
        )
        es_evento_fraude = self.tipo_fraude__c is not None or self.modalidad_fraude__c is not None

        if es_estado_cierre:
            if not self.Status or status_clean not in ("closed", "cerrado"):
                self.Status = "Closed"

            # 🚫 CO-DEPENDENCIA DE CAMPOS DE CIERRE
            if not self.Favorabilidad__c and not self.Aceptacion__c:
                raise ValueError("Para ejecutar un Cierre Definitivo es obligatorio proveer 'Favorabilidad__c' y 'Aceptacion__c'.")
            elif not self.Favorabilidad__c:
                raise ValueError("Falta el campo obligatorio 'Favorabilidad__c' para el cierre del caso.")
            elif not self.Aceptacion__c:
                raise ValueError("Falta el campo obligatorio 'Aceptacion__c' para el cierre del caso.")

            hoy_bogota = datetime.now(ZoneInfo("America/Bogota")).date()
            
            if not self.ClosedDate:
                self.ClosedDate = hoy_bogota

            # 🚫 VALIDACIÓN DE FECHA DE CIERRE VS FECHA ACTUAL
            if self.ClosedDate > hoy_bogota:
                raise ValueError(f"La fecha de cierre 'ClosedDate' ({self.ClosedDate}) no puede ser posterior a la fecha actual.")

            # 🚫 VALIDACIÓN: ClosedDate no puede ser anterior a CreatedDate
            if self.CreatedDate and self.ClosedDate:
                try:
                    dt_created = datetime.fromisoformat(self.CreatedDate.replace("Z", "+00:00")).date()
                    if self.ClosedDate < dt_created:
                        raise ValueError(
                            f"La fecha de cierre 'ClosedDate' ({self.ClosedDate}) "
                            f"no puede ser anterior a la fecha de creación 'CreatedDate' ({dt_created})."
                        )
                except (ValueError, TypeError):
                    pass

            if not self.cuerpo_respuesta_final or not self.cuerpo_respuesta_final.strip():
                self.cuerpo_respuesta_final = (
                    "Se emite respuesta formal y cierre definitivo al caso de reclamación "
                    "conforme a los términos de ley y políticas de la entidad."
                )

        if es_evento_fraude:
            if num_archivos == 0 and not tiene_directorio:
                raise ValueError("No se envió un documento de investigación de fraude (INV_FRAUDE_SFC).")
            
            if self.card_amount__c is None:
                self.card_amount__c = 0.0
            if self.Total_Devuelto_por_Desconocimiento__c is None:
                self.Total_Devuelto_por_Desconocimiento__c = 0.0
            
            if not tiene_directorio and num_archivos > 0:
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
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "Case_id": "SC-EJEMPLO-0001",
                "Status": "Closed",
                "SuppliedName": "María Alejandra Bermúdez",
                "SC_id_type__c": "CC",
                "id_number__c": "1020304054",
                "sc_genero__c": "Femenino",
                "tipo_de_persona__c": "B2C",
                "sc_LGBTIQ__c": "No",
                "sc_Condicion_especial__c": "No aplica",
                "SuppliedPhone": "3109876543",
                "SuppliedEmail": "maria.bermudez@test.com",
                "direccion__c": "Calle 53 # 70-12 Apto 402",
                "Departamento__c": "Bogotá D.C.",
                "SC_municipio__c": "Bogotá D.C.",
                "canal__c": "Internet",
                "punto_recepcion": "Web",
                "Instancia_de_recepcion__c": "Entidad vigilada",
                "admision_col__c": "No Aplica",
                "Description": "Prueba completa con todos los campos del formulario cargados simultáneamente para verificación de esquema.",
                "smart_anexo_queja__c": True,
                "Tutela__c": "No",
                "Ente_de_control__c": "Otros",
                "smart_escalamiento_DCF__c": "No",
                "marcacion__c": "Revisión técnica",
                "Product__c": "Tarjeta Digital",
                "smart_Producto_nombre__c": "Global Card Digital",
                "Categorias_COL__c": "Transacción no reconocida",
                "archivos_s3": [],
                "producto_digital__c": "Si",
                "tipo_fraude__c": "Externo",
                "modalidad_fraude__c": "Suplantación de identidad",
                "card_amount__c": 1500000.0,
                "Total_Devuelto_por_Desconocimiento__c": 1500000.0,
                "nombre_archivo_fraude": "informe_fraude.pdf",
                "Favorabilidad__c": "Favorable",
                "a_favor_de__c": "1",
                "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
                "Rectificacion__c": "Queja o reclamo rectificada por la entidad vigilada antes de la decisión del DCF",
                "Prorroga__c": 1,
                "cuerpo_respuesta_final": "<html><body><p>Estimada María,</p><p>Le informamos que tras la investigación realizada por el equipo de seguridad, confirmamos que su solicitud ha sido resuelta de forma <strong>FAVORABLE</strong> con el reembolso total de los fondos.</p><p>Atentamente,<br>Global66 Colombia</p></body></html>",
                "directorio_s3": "caso/TEST-ALL-FIELDS-SSV-999/"
                }
        }
    }


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


class ConfirmacionAckUsuariosInput(BaseModel):
    numeros_id_cf: List[str] = Field(
        ..., 
        description="Lista de números de identificación (numero_id_CF) procesados exitosamente por el CRM."
    )