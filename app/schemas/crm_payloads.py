# app/schemas/crm_payloads.py
import re
from datetime import date, datetime, timedelta
from typing import Annotated, Any, List, Optional
from zoneinfo import ZoneInfo
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
    ValidationInfo,
)

from app.core.mapping import SfcSalesforceMapper
from app.core.config import settings
from app.core.clasificacion_operacion import es_estado_cierre as _es_estado_cierre


class ArchivoS3Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 🟢 FIX HALLAZGO 41: el límite de 50 archivos en 'archivos_s3' no acota nada si cada
    # string individual puede ser arbitrariamente largo; se acotan también aquí.
    # 🔴 FIX (hallazgo de revisión externa, 2026-08-25): 'nombre_archivo'/'s3_key' eran
    # obligatorios pero sin min_length -- un string vacío "" pasaba la validación. Con
    # AMBOS vacíos, S3StorageService._resolver_identidad_adjunto no podía derivar
    # ningún nombre y descartaba el adjunto en silencio (retorna None), mientras que
    # Momento3SincronizacionService._orquestar_pipeline_momento_3 seguía calculando
    # 'anexo_queja' sobre len(archivos_s3_raw) (lo SOLICITADO), no sobre lo
    # efectivamente transmitido -- pudiendo declararle a la SFC que sí hay anexo
    # cuando ninguno se transmitió. Se cierra en el schema, más temprano que la
    # detección silenciosa aguas abajo.
    nombre_archivo: str = Field(..., min_length=1, max_length=255, description="Nombre final del archivo guardado")
    s3_key: str = Field(..., min_length=1, max_length=1024, description="Ruta/Clave única de acceso en el bucket S3")
    bucket: str = Field(..., max_length=63, description="Bucket de S3 donde se alojó")


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
    sc_genero__c: Optional[str] = Field(None, description="sexo traducido a texto, puede ser None")
    sc_LGBTIQ__c: Optional[str] = Field(None, description="lgbtiq traducido, puede ser None")
    canal__c: Optional[str] = Field(None, description="canal_cod traducido a texto, Puede ser None")
    sc_Condicion_especial__c: Optional[str] = Field(None, description="condicion_especial traducido a texto, puede ser None")
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
    model_config = ConfigDict(extra="forbid")

    Case_id: Optional[str] = Field(None, description="Código original único de la base de datos de Salesforce", max_length=30)
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
    codigo_pais__c: Optional[str] = Field("Colombia", description="País del usuario")

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

    archivos_s3: List[ArchivoS3Schema] = Field(default=[], max_length=50, description="Colección de archivos en S3")
    Quejas_express__c: Optional[str] = Field("No", description="Indica si es una queja expres")

    @field_validator("CreatedDate", mode="after")
    @classmethod
    def validar_rango_30_dias_created_date(cls, v: Optional[str]) -> Optional[str]:
        if v and str(v).strip():
            try:
                dt_created = datetime.fromisoformat(v.replace("Z", "+00:00")).date()
                hoy_bogota = datetime.now(ZoneInfo("America/Bogota")).date()
                limite_30_dias = hoy_bogota - timedelta(days=30)

                if dt_created < limite_30_dias:
                    raise ValueError(
                        f"La fecha de creación 'CreatedDate' ({dt_created}) "
                        f"no puede ser anterior a 30 días respecto a la fecha actual ({hoy_bogota})."
                    )
                if dt_created > hoy_bogota:
                    raise ValueError(
                        f"La fecha de creación 'CreatedDate' ({dt_created}) "
                        f"no puede ser posterior a la fecha actual ({hoy_bogota})."
                    )
            except ValueError as ve:
                if "30 días" in str(ve) or "posterior" in str(ve):
                    raise ve
        return v
    
    @field_validator("SuppliedName", "direccion__c", "Description", mode="before")
    @classmethod
    def sanitizar_campos_texto(cls, v: Optional[str]) -> Optional[str]:
        if isinstance(v, str):
            clean = v.replace("\x00", "")
            clean = re.sub(r"<(script|style|iframe)\b[^>]*>.*?</\1>", "", clean, flags=re.IGNORECASE | re.DOTALL)
            clean = re.sub(r"<[^>]*>", "", clean)
            return clean.strip()
        return v
    
    @field_validator("archivos_s3", mode="before")
    @classmethod
    def normalizar_archivos_s3(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, dict):
            if not v:
                return []
            if "s3_key" in v or "nombre_archivo" in v:
                return [v]
            # 🟢 FIX P1-11: un dict no vacío con forma irreconocible es un payload
            # malformado del CRM, no "sin adjuntos" — antes se descartaba en
            # silencio como []. Se rechaza explícitamente para no entregar la
            # queja al CRM/SFC como si no tuviera anexos cuando en realidad sí
            # se recibió información de archivos, sólo que corrupta/inesperada.
            raise ValueError(
                f"'archivos_s3' recibió un objeto con forma no reconocida (claves: {sorted(v.keys())}). "
                "Se espera una lista de objetos con 's3_key' y 'nombre_archivo'."
            )
        return v

    @field_validator("direccion__c", mode="after")
    @classmethod
    def asegurar_direccion_valida(cls, v: Optional[str]) -> str:
        if not v or not v.strip():
            return "Dirección no registrada"
        return v.strip()

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
            self.Smart_Code__c = f"{prefix}{raw_id}" if not raw_id.startswith(prefix) else raw_id

        elif self.Smart_Code__c:
            clean_sc = str(self.Smart_Code__c).strip()
            self.Smart_Code__c = clean_sc if clean_sc.startswith(prefix) else f"{prefix}{clean_sc}"

        if not self.Case_id:
            self.Case_id = self.Smart_Code__c

        # 🟢 FIX HALLAZGO 21: Re-validar la longitud del Smart_Code__c final tras agregar el prefijo.
        # Evita que un valor sin prefijo de hasta 30 caracteres supere el límite al anteponer el prefijo.
        if self.Smart_Code__c and len(self.Smart_Code__c) > 30:
            raise ValueError(
                f"El 'Smart_Code__c' final con prefijo ('{self.Smart_Code__c}') tiene {len(self.Smart_Code__c)} "
                f"caracteres, superando el límite máximo permitido de 30."
            )

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
            except ValueError as e:
                raise ValueError(f"El campo 'CreatedDate' con valor '{v}' debe cumplir con un formato ISO 8601 válido.") from e
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
        "Product__c", "Tutela__c", "smart_escalamiento_DCF__c", "admision_col__c",
        "codigo_pais__c", "producto_digital__c", "Quejas_express__c",
        mode="after",
        check_fields=False
    )
    @classmethod
    def validar_picklist_contra_mapper(cls, value: Optional[str], info: ValidationInfo) -> Optional[str]:
        if value is None:
            return value
        val_str = str(value).strip()
        if not val_str:
            return value

        field_name = info.field_name

        field_to_catalog = {
            "SC_id_type__c": "tipo_id",
            "tipo_de_persona__c": "persona",
            "Categorias_COL__c": "macro_motivo",
            "Product__c": "producto",
            "sc_genero__c": "genero",
            "sc_LGBTIQ__c": "lgbtiq",
            "sc_Condicion_especial__c": "condicion_especial",
            "canal__c": "canal",
            "punto_recepcion": "punto_recepcion",
            "Instancia_de_recepcion__c": "instancia_recepcion",
            "Ente_de_control__c": "ente_control",
            "admision_col__c": "admision",
            "codigo_pais__c": "codigo_pais",
        }

        boolean_si_no_fields = {
            "Tutela__c", "smart_escalamiento_DCF__c", "producto_digital__c", "Quejas_express__c"
        }

        if field_name == "SC_id_type__c" and val_str.upper() in ("NIT", "N.I.T."):
            return "RUT"

        if field_name in boolean_si_no_fields:
            v_lower = val_str.lower()
            if v_lower in ("si", "sí", "true", "1"):
                return "Si"
            elif v_lower in ("no", "false", "0", "2"):
                return "No"
            else:
                raise ValueError(
                    f"El valor '{val_str}' no es válido para el campo '{field_name}'. "
                    f"Valores permitidos: ['Si', 'No']."
                )

        if field_name in field_to_catalog:
            cat_key = field_to_catalog[field_name]
            allowed = SfcSalesforceMapper.get_crm_allowed_values(cat_key)

            if val_str in allowed:
                return val_str

            normalized_val = SfcSalesforceMapper._normalize_text(val_str)
            norm_to_canonical = {SfcSalesforceMapper._normalize_text(a): a for a in allowed}
            if normalized_val in norm_to_canonical:
                return norm_to_canonical[normalized_val]

            raise ValueError(
                f"El valor '{val_str}' no es válido para {field_name}. "
                f"Valores soportados: {sorted(list(allowed))[:5]}... (Total {len(allowed)})"
            )

        return value
    
    @field_validator("CreatedDate", mode="before")
    @classmethod
    def auto_completar_y_validar_fecha_creacion(cls, v: Optional[str]) -> str:
        if not v or not str(v).strip():
            return datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m-%dT%H:%M:%S")
        
        if isinstance(v, str):
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError as e:
                raise ValueError(f"El campo 'CreatedDate' con valor '{v}' debe cumplir con un formato ISO 8601 válido.") from e
        return v


# ======================================================================
# 🚀 ESQUEMA UNIFICADO DE DESPACHO (SIN TIPO_OPERACION)
# ======================================================================

class QuejaUnificadaCrmInput(Momento2QuejaCrmInput):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
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
    )

    producto_digital__c: Optional[str] = Field(None, description="Producto digital (Si/No)")

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
            
            if "T" in v_clean:
                try:
                    return datetime.fromisoformat(v_clean.replace("Z", "+00:00")).date()
                except ValueError:
                    pass

            try:
                return date.fromisoformat(v_clean)
            except ValueError as e:
                raise ValueError(
                    f"El campo 'ClosedDate' con valor '{v}' debe cumplir con un formato de fecha válido (YYYY-MM-DD o ISO 8601)."
                ) from e
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

    def _validar_closed_date(self, hoy_bogota: date, limite_30_dias: date) -> None:
        if not self.ClosedDate:
            self.ClosedDate = hoy_bogota

        if self.ClosedDate > hoy_bogota:
            raise ValueError(f"La fecha de cierre 'ClosedDate' ({self.ClosedDate}) no puede ser posterior a la fecha actual.")

        if self.ClosedDate < limite_30_dias:
            raise ValueError(
                f"La fecha de cierre 'ClosedDate' ({self.ClosedDate}) "
                f"no puede ser anterior a 30 días respecto a la fecha actual ({hoy_bogota})."
            )

        if self.CreatedDate and self.ClosedDate:
            dt_created = None
            try:
                dt_created = datetime.fromisoformat(self.CreatedDate.replace("Z", "+00:00")).date()
            except (ValueError, TypeError):
                pass

            if dt_created and self.ClosedDate < dt_created:
                raise ValueError(
                    f"La fecha de cierre 'ClosedDate' ({self.ClosedDate}) "
                    f"no puede ser anterior a la fecha de creación 'CreatedDate' ({dt_created})."
                )

    def _validar_reglas_cierre(self) -> None:
        status_clean = (self.Status or "").strip().lower()
        if not self.Status or status_clean not in ("closed", "cerrado"):
            self.Status = "Closed"

        if not self.Favorabilidad__c and not self.Aceptacion__c:
            raise ValueError("Para ejecutar un Cierre Definitivo es obligatorio proveer 'Favorabilidad__c' y 'Aceptacion__c'.")
        elif not self.Favorabilidad__c:
            raise ValueError("Falta el campo obligatorio 'Favorabilidad__c' para el cierre del caso.")
        elif not self.Aceptacion__c:
            raise ValueError("Falta el campo obligatorio 'Aceptacion__c' para el cierre del caso.")

        hoy_bogota = datetime.now(ZoneInfo("America/Bogota")).date()
        limite_30_dias = hoy_bogota - timedelta(days=30)
        self._validar_closed_date(hoy_bogota, limite_30_dias)

        if not self.cuerpo_respuesta_final or not self.cuerpo_respuesta_final.strip():
            self.cuerpo_respuesta_final = (
                "Se emite respuesta formal y cierre definitivo al caso de reclamación "
                "conforme a los términos de ley y políticas de la entidad."
            )

    def _validar_reglas_fraude(self, num_archivos: int, tiene_directorio: bool) -> None:
        if num_archivos == 0 and not tiene_directorio:
            raise ValueError("No se envió un documento de investigación de fraude (INV_FRAUDE_SFC).")

        # 🟢 FIX HALLAZGO 26: un monto ausente ya no se reescribe silenciosamente a 0.0.
        # "$0 reclamado" y "monto no informado" son hechos distintos para un reporte
        # regulatorio de fraude; se exige que el CRM envíe el valor explícitamente
        # (incluido 0.0 si de verdad no hubo impacto económico).
        if self.card_amount__c is None:
            raise ValueError(
                "Falta el campo obligatorio 'card_amount__c' (monto reclamado) para un caso de fraude. "
                "Envíe 0.0 explícitamente si no hubo impacto económico."
            )
        if self.Total_Devuelto_por_Desconocimiento__c is None:
            raise ValueError(
                "Falta el campo obligatorio 'Total_Devuelto_por_Desconocimiento__c' (monto devuelto) "
                "para un caso de fraude. Envíe 0.0 explícitamente si no hubo impacto económico."
            )

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

    @model_validator(mode="after")
    def validar_reglas_segun_datos_presentes(self) -> "QuejaUnificadaCrmInput":
        num_archivos = len(self.archivos_s3)
        tiene_directorio = bool(self.directorio_s3 and self.directorio_s3.strip())

        es_cierre = _es_estado_cierre(self.Status, self.ClosedDate, self.Favorabilidad__c, self.Aceptacion__c)
        es_evento_fraude = self.tipo_fraude__c is not None or self.modalidad_fraude__c is not None

        if es_cierre:
            self._validar_reglas_cierre()

        if es_evento_fraude:
            self._validar_reglas_fraude(num_archivos, tiene_directorio)

        return self


class ConfirmacionAckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 🟢 FIX HALLAZGO 41: el tope de 500 elementos no acota nada si cada string individual
    # puede ser arbitrariamente largo; se acota también el tamaño de cada identificador.
    ids_quejas: List[Annotated[str, Field(max_length=50)]] = Field(
        ...,
        min_length=1,
        max_length=500,
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
    model_config = ConfigDict(extra="forbid")

    # 🟢 FIX HALLAZGO 41: mismo criterio que ids_quejas — acota también cada elemento.
    numeros_id_cf: List[Annotated[str, Field(max_length=30)]] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Lista de números de identificación (numero_id_CF) procesados exitosamente por el CRM."
    )