import json
import logging
import os
import re
import unicodedata
from datetime import datetime, date
from typing import Dict, Any, Optional, Set
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.sfc_payloads import SfcNuevaQuejaPayload, SfcActualizarQuejaPayload

logger = logging.getLogger(__name__)


class SfcSalesforceMapper:
    HTML_REGEX = re.compile(r'<[^>]*>')
    CLEAN_PHONE_DOC_REGEX = re.compile(r'[^\d+]')

    CATALOGOS: Dict[str, Dict[str, str]] = {}
    INVERSE_CATALOGS: Dict[str, Dict[str, int]] = {}

    DEPT_DIVIPOLA_INV: Dict[str, str] = {}  
    MUNI_DIVIPOLA_INV: Dict[str, str] = {}  
    DEPT_DIVIPOLA: Dict[str, str] = {}      
    MUNI_DIVIPOLA: Dict[str, str] = {}      
    
    PRODUCTO_SFC_TEXTO_TO_SF = {
        "wallet": "Wallet", "exchange": "Exchange", "transactions": "Transactions",
        "p2p": "P2P", "tarjeta digital": "Tarjeta Digital", "tarjeta fisica": "Tarjeta Fisica",
        "cuenta perfil": "Cuenta perfil", "otro": "Otro"
    }

    @staticmethod
    def _normalize_text(text: str) -> str:
        if not text:
            return ""
        normalized = "".join(c for c in unicodedata.normalize('NFD', str(text)) if unicodedata.category(c) != 'Mn')
        return normalized.lower().strip()

    @classmethod
    def cargar_divipola(cls):
        """Carga y construye los diccionarios bidireccionales de DIVIPOLA desde JSON."""
        ruta = os.path.join(os.path.dirname(__file__), "resources/divipola_sfc_crm.json")
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 1. Mapeo Inverso (SFC -> CRM)
            cls.DEPT_DIVIPOLA_INV = data.get("departamentos", {})
            cls.MUNI_DIVIPOLA_INV = data.get("municipios", {})

            # 2. Mapeo Directo (CRM -> SFC) con Normalización
            cls.DEPT_DIVIPOLA = {
                cls._normalize_text(nombre): cod
                for cod, nombre in cls.DEPT_DIVIPOLA_INV.items()
            }
            cls.MUNI_DIVIPOLA = {
                cls._normalize_text(nombre): cod
                for cod, nombre in cls.MUNI_DIVIPOLA_INV.items()
            }

            # Aliases comunes para Bogotá u otras ciudades
            cls.DEPT_DIVIPOLA["bogota"] = "11"
            cls.DEPT_DIVIPOLA["bogota d.c."] = "11"
            cls.MUNI_DIVIPOLA["bogota"] = "11001"
            cls.MUNI_DIVIPOLA["bogota d.c."] = "11001"

            logger.info(
                f"✅ [SfcSalesforceMapper] Cargar DIVIPOLA exitosa: "
                f"{len(cls.DEPT_DIVIPOLA_INV)} deptos y {len(cls.MUNI_DIVIPOLA_INV)} municipios."
            )
        except Exception as e:
            logger.error(f"❌ Error al cargar divipola_sfc_crm.json: {e}")
    
    
    @classmethod
    def cargar_catalogos(cls):
        ruta = os.path.join(os.path.dirname(__file__), "resources/catalogos_sfc_crm.json")
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                cls.CATALOGOS = json.load(f)
            
            cls.INVERSE_CATALOGS = {}
            for cat_key, cat_dict in cls.CATALOGOS.items():
                cls.INVERSE_CATALOGS[cat_key] = {
                    cls._normalize_text(v): int(k) for k, v in cat_dict.items()
                }
            
            if "tipo_id" in cls.INVERSE_CATALOGS:
                cls.INVERSE_CATALOGS["tipo_id"].update({
                    "cc": 1, "ce": 2, "rut": 3, "nit": 3, "dni": 4, "pass": 5, "passport": 5, "pasaporte": 5
                })
            if "punto_recepcion" in cls.INVERSE_CATALOGS:
                cls.INVERSE_CATALOGS["punto_recepcion"].update({
                    "activate b2c": 99, "form: change data": 99, "updatecom": 99, "manual": 1, "internet": 2
                })

            cls.cargar_divipola()

            logger.info(f"✅ [SfcSalesforceMapper] Cargados {len(cls.CATALOGOS)} catálogos desde JSON.")
        except Exception as e:
            logger.error(f"❌ Error al cargar catalogos_sfc_crm.json: {e}")

    @classmethod
    def get_crm_allowed_values(cls, catalog_key: str) -> Set[str]:
        if not cls.CATALOGOS:
            cls.cargar_catalogos()
        return set(cls.CATALOGOS.get(catalog_key, {}).values())


SfcSalesforceMapper.cargar_catalogos()


SfcSalesforceMapper.MAPPING_MOMENTO_1_SFC_TO_CRM = {
    "codigo_queja": "Smart_Code__c", "fecha_creacion": "CreatedDate", "nombres": "SuppliedName",
    "numero_id_CF": "id_number__c", "correo": "SuppliedEmail", "telefono": "SuppliedPhone",
    "direccion": "direccion__c", "departamento_cod": "Departamento__c", "municipio_cod": "SC_municipio__c",
    "texto_queja": "Description", "anexo_queja": "smart_anexo_queja__c", "tipo_id_CF": "SC_id_type__c",
    "tipo_persona": "tipo_de_persona__c", "sexo": "sc_genero__c", "lgbtiq": "sc_LGBTIQ__c",
    "canal_cod": "canal__c", "condicion_especial": "sc_Condicion_especial__c", "producto_cod": "Product__c",
    "macro_motivo_cod": "Categorias_COL__c", "tutela": "Tutela__c", "ente_control": "Ente_de_control__c",
    "desistimiento_queja": "Desistimiento__c", "queja_expres": "Quejas_express__c", "codigo_pais": "codigo_pais__c",
    "producto_nombre": "smart_Producto_nombre__c", "escalamiento_DCF": "smart_escalamiento_DCF__c",
    "replica": "replica__c", "argumento_replica": "argumento_replica__c"
}

# 🎯 MAPEO MOMENTO 4 (Información de Usuarios SFC -> CRM)
SfcSalesforceMapper.MAPPING_MOMENTO_4_SFC_TO_CRM = {
    "numero_id_CF": "id_number__c",
    "tipo_id_CF": "SC_id_type__c",
    "nombre": "FirstName",
    "nombres": "FirstName",
    "Nombres": "FirstName",
    "apellido": "LastName",
    "apellidos": "LastName",
    "Apellidos": "LastName",
    "fecha_nacimiento": "fecha_nacimiento__c",
    "correo": "SuppliedEmail",
    "Correo": "SuppliedEmail",
    "telefono": "SuppliedPhone",
    "Teléfono": "SuppliedPhone",
    "Telefono": "SuppliedPhone",
    "razon_social": "company_name__c",
    "direccion": "direccion__c",
    "Dirección": "direccion__c",
    "Direccion": "direccion__c",
    "departamento_cod": "Departamento__c",
    "municipio_cod": "SC_municipio__c",
}


@classmethod
def _get_sf_field_value(cls, entity: Any, field_name: str) -> Any:
    if isinstance(entity, dict):
        if field_name in ("Smart_Code__c", "codigo_queja"):
            for k in ("Smart_Code__c", "codigo_queja", "Case_id", "case_id"):
                if k in entity and entity[k] is not None:
                    return entity[k]

        if field_name in ("Status", "status", "estado_cod"):
            for k in ("Status", "status", "Estado__c", "estado", "estado_cod"):
                if k in entity and entity[k] is not None:
                    return entity[k]

        if field_name in entity and entity[field_name] is not None:
            return entity[field_name]

        lower_map = {str(k).lower(): v for k, v in entity.items() if v is not None}
        return lower_map.get(field_name.lower())

    if field_name in ("Smart_Code__c", "codigo_queja"):
        for attr in ("Smart_Code__c", "codigo_queja", "Case_id", "CaseNumber", "case_id"):
            if hasattr(entity, attr) and getattr(entity, attr) is not None:
                return getattr(entity, attr)

    if field_name in ("Status", "status", "estado_cod"):
        for attr in ("Status", "status", "Estado__c", "estado", "estado_cod"):
            if hasattr(entity, attr) and getattr(entity, attr) is not None:
                return getattr(entity, attr)

    if hasattr(entity, field_name) and getattr(entity, field_name) is not None:
        return getattr(entity, field_name)

    return None


@classmethod
def _strip_html(cls, text: str) -> str:
    if not text: return ""
    return cls.HTML_REGEX.sub('', str(text)).strip()


@classmethod
def _translate_value_to_crm(cls, sfc_key: str, sfc_value: Any) -> Any:
    if sfc_value is None: 
        return None
    str_key = str(sfc_value)
    
    if sfc_key == "codigo_pais":
        if sfc_value == "COL" or sfc_value == "170":
            return "Colombia"
    
    key_to_cat = {
        "sexo": ("genero", "No Aplica"),
        "tipo_id_CF": ("tipo_id", "Cedula de ciudadanía"),
        "tipo_persona": ("persona", "B2C"),
        "lgbtiq": ("lgbtiq", "No"),
        "sc_LGBTIQ__c": ("lgbtiq", "No"),
        "condicion_especial": ("condicion_especial", "No aplica"),
        "canal_cod": ("canal", "Internet"),
        "ente_control": ("ente_control", "Otros"),
        "insta_recepcion": ("instancia_recepcion", "Entidad vigilada"),
        "admision": ("admision", "No Aplica"),
        "a_favor_de": ("favorabilidad", "No favorable"),
        "aceptacion_queja": ("aceptacion", "Respuesta final a favor del consumidor financiero no aceptadas por la entidad"),
        "rectificacion_queja": ("rectificacion", "Queja o reclamo no rectificada por la entidad vigilada antes de la decisión del DCF"),
        "desistimiento_queja": ("desistimiento", "Queja o reclamo no desistida por el CF"),
        "tipo_fraude": ("tipo_fraude", "Externo"),
        "modalidad_fraude": ("modalidad_fraude", "Otra"),
        "punto_recepcion": ("punto_recepcion", "Manual"),
        "macro_motivo_cod": ("macro_motivo", "Transacción no reconocida"),
        "Categorias_COL__c": ("macro_motivo", "Transacción no reconocida")
    }

    if sfc_key in key_to_cat:
        cat_key, default_val = key_to_cat[sfc_key]
        return cls.CATALOGOS.get(cat_key, {}).get(str_key, default_val)

    if sfc_key in ("departamento_cod", "Departamento__c"): return cls.DEPT_DIVIPOLA_INV.get(str_key, str(sfc_value))
    if sfc_key in ("municipio_cod", "SC_municipio__c"): return cls.MUNI_DIVIPOLA_INV.get(str_key, str(sfc_value))

    if sfc_key in ("tutela", "queja_expres", "escalamiento_DCF", "replica", "producto_digital"):
        val_int = int(sfc_value) if str_key.isdigit() else sfc_value
        return "No" if (val_int == 2 or sfc_value is False) else "Si"

    return sfc_value


@classmethod
def _translate_value_to_sfc(cls, sf_key: str, sf_value: Any) -> Any:
    if sf_value is None: 
        return None
    
    if sf_key == "codigo_pais__c":
        if str(sf_value) == "Colombia":
            return "170"

    if sf_key in ("Aceptacion__c", "Rectificacion__c", "Tutela__c", "Quejas_express__c"):
        v_clean = str(sf_value).lower().strip()
        if v_clean in ("si", "sí", "true", "1"): return 1
        if v_clean in ("no", "false", "2"): return 2
        return 1 if bool(sf_value) else 2

    if sf_key == "sinRespuestaFinal__c":
        v_clean = str(sf_value).lower().strip()
        return v_clean in ("si", "sí", "true", "1")

    normalized = cls._normalize_text(str(sf_value))

    sf_to_cat = {
        "sc_genero__c": ("genero", 10),
        "SC_id_type__c": ("tipo_id", 1),
        "tipo_de_persona__c": ("persona", 1),
        "sc_LGBTIQ__c": ("lgbtiq", 2),
        "sc_Condicion_especial__c": ("condicion_especial", 98),
        "canal__c": ("canal", 13),
        "Ente_de_control__c": ("ente_control", 99),
        "Instancia_de_recepcion__c": ("instancia_recepcion", 2),
        "admision_col__c": ("admision", 1),
        "Favorabilidad__c": ("favorabilidad", 3),
        "Desistimiento__c": ("desistimiento", 2),
        "tipo_fraude__c": ("tipo_fraude", 2),
        "Tipo_Fraude__c": ("tipo_fraude", 2),
        "modalidad_fraude__c": ("modalidad_fraude", 90),
        "Modalidad_Fraude__c": ("modalidad_fraude", 90),
        "punto_recepcion": ("punto_recepcion", 1),
        "Categorias_COL__c": ("macro_motivo", 958)
    }

    if sf_key in sf_to_cat:
        cat_key, default_val = sf_to_cat[sf_key]
        return cls.INVERSE_CATALOGS.get(cat_key, {}).get(normalized, default_val)

    if sf_key == "Departamento__c": return cls.DEPT_DIVIPOLA.get(normalized, str(sf_value))
    if sf_key == "SC_municipio__c": return cls.MUNI_DIVIPOLA.get(normalized, str(sf_value))
    if sf_key == "Product__c": return 207

    if sf_key == "Status":
        status_map = {
            "new": 1, "nuevo": 1, 
            "in progress": 2, "en progreso": 2, "stand by": 2, "espera": 2, 
            "closed": 4, "cerrado": 4, "resolved": 4, "resuelto": 4
        }
        return status_map.get(normalized, 2)

    if sf_key in ("producto_digital__c", "Producto_digital__c"):
        return 1 if normalized in ("si", "sí", "true") else 2

    if sf_key == "Description":
        return cls._strip_html(str(sf_value))[:4500].strip()

    if sf_key in ("id_number__c", "SuppliedPhone"):
        return cls.CLEAN_PHONE_DOC_REGEX.sub('', str(sf_value))[:15]
    
    if sf_key == "Prorroga__c":
        return int(sf_value)

    return sf_value


@classmethod
def sfc_payload_to_db_dict(cls, sfc_data: Dict[str, Any]) -> Dict[str, Any]:
    crm_data = {}
    for sfc_key, value in sfc_data.items():
        if sfc_key in cls.MAPPING_MOMENTO_1_SFC_TO_CRM:
            crm_key = cls.MAPPING_MOMENTO_1_SFC_TO_CRM[sfc_key]
            if sfc_key == "fecha_creacion" and isinstance(value, str):
                try: 
                    crm_data[crm_key] = datetime.fromisoformat(value.replace(" ", "T")).isoformat()
                except ValueError: 
                    crm_data[crm_key] = value
            elif sfc_key == "producto_cod":
                sfc_prod_nombre = sfc_data.get("producto_nombre", "")
                normalized_prod = cls._normalize_text(str(sfc_prod_nombre))
                crm_data[crm_key] = cls.PRODUCTO_SFC_TEXTO_TO_SF.get(normalized_prod, "Cuenta perfil")
            else:
                crm_data[crm_key] = cls._translate_value_to_crm(sfc_key, value)
    return crm_data


# 🎯 NUEVO MÉTODO PARA MOMENTO 4
@classmethod
def sfc_user_payload_to_db_dict(cls, sfc_data: Dict[str, Any]) -> Dict[str, Any]:
    """Mapea el JSON de un usuario del Momento 4 (SFC) al formato del CRM local."""
    crm_data = {}
    if not isinstance(sfc_data, dict):
        return crm_data

    for sfc_key, value in sfc_data.items():
        if value is None:
            continue

        crm_key = cls.MAPPING_MOMENTO_4_SFC_TO_CRM.get(sfc_key)
        if not crm_key:
            crm_key = cls.MAPPING_MOMENTO_4_SFC_TO_CRM.get(str(sfc_key).lower())

        if crm_key:
            if sfc_key == "fecha_nacimiento" and isinstance(value, str) and value.strip():
                try:
                    clean_date = value.replace(" ", "T")
                    crm_data[crm_key] = datetime.fromisoformat(clean_date).isoformat()
                except ValueError:
                    crm_data[crm_key] = value
            else:
                crm_data[crm_key] = cls._translate_value_to_crm(sfc_key, value)

    # Construir SuppliedName combinando Nombres y Apellidos si existen
    first_name = crm_data.get("FirstName", "")
    last_name = crm_data.get("LastName", "")
    if first_name or last_name:
        crm_data["SuppliedName"] = f"{first_name} {last_name}".strip()

    return crm_data


@classmethod
def crm_entity_to_sfc_momento2_payload(cls, entity: Any) -> Dict[str, Any]:
    prefix = f"{settings.SFC_TIPO_ENTIDAD}{settings.SFC_ENTIDAD_COD}"
    raw_code = cls._get_sf_field_value(entity, "Smart_Code__c") or ""
    if raw_code and not str(raw_code).startswith(prefix):
        codigo_queja = f"{prefix}{raw_code}"
    else:
        codigo_queja = raw_code

    created_raw = str(cls._get_sf_field_value(entity, "CreatedDate") or datetime.now().isoformat())
    fecha_iso = created_raw
    fecha_str = created_raw.split("T")[0] if "T" in created_raw else created_raw

    if "T" in created_raw or "-" in created_raw:
        try:
            clean_date = created_raw.split(".")[0] if "." in created_raw else created_raw
            dt_val = datetime.fromisoformat(clean_date) if "T" in clean_date else datetime.strptime(clean_date, "%Y-%m-%d")
            fecha_iso = dt_val.strftime("%Y-%m-%dT%H:%M:%S")
            fecha_str = dt_val.strftime("%Y-%m-%d")
        except ValueError:
            pass

    payload_obj = SfcNuevaQuejaPayload(
        codigo_queja=str(codigo_queja),
        departamento_cod=str(cls._translate_value_to_sfc("Departamento__c", cls._get_sf_field_value(entity, "Departamento__c")) or "11"),
        municipio_cod=str(cls._translate_value_to_sfc("SC_municipio__c", cls._get_sf_field_value(entity, "SC_municipio__c")) or "11001"),
        canal_cod=int(cls._translate_value_to_sfc("canal__c", cls._get_sf_field_value(entity, "canal__c")) or 13),
        producto_cod=int(cls._translate_value_to_sfc("Product__c", cls._get_sf_field_value(entity, "Product__c")) or 207),
        macro_motivo_cod=int(cls._translate_value_to_sfc("Categorias_COL__c", cls._get_sf_field_value(entity, "Categorias_COL__c")) or 958),
        fecha_creacion=fecha_iso,
        nombres=str(cls._get_sf_field_value(entity, "SuppliedName") or ""),
        tipo_id_CF=int(cls._translate_value_to_sfc("SC_id_type__c", cls._get_sf_field_value(entity, "SC_id_type__c")) or 1),
        numero_id_CF=str(cls._translate_value_to_sfc("id_number__c", cls._get_sf_field_value(entity, "id_number__c")) or ""),
        tipo_persona=int(cls._translate_value_to_sfc("tipo_de_persona__c", cls._get_sf_field_value(entity, "tipo_de_persona__c")) or 1),
        texto_queja=str(cls._translate_value_to_sfc("Description", cls._get_sf_field_value(entity, "Description")) or ""),
        anexo_queja=bool(cls._get_sf_field_value(entity, "smart_anexo_queja__c") or False),
        ente_control=int(cls._translate_value_to_sfc("Ente_de_control__c", cls._get_sf_field_value(entity, "Ente_de_control__c")) or 99),
        insta_recepcion=int(cls._translate_value_to_sfc("Instancia_de_recepcion__c", cls._get_sf_field_value(entity, "Instancia_de_recepcion__c")) or 1),
        admision=int(cls._translate_value_to_sfc("admision_col__c", cls._get_sf_field_value(entity, "admision_col__c")) or 1),
        codigo_pais=str(cls._translate_value_to_sfc("codigo_pais__c", cls._get_sf_field_value(entity, "codigo_pais__c")) or "170"),        
        punto_recepcion=int(cls._translate_value_to_sfc("punto_recepcion", cls._get_sf_field_value(entity, "punto_recepcion")) or 1)
    )

    return payload_obj.model_dump()


@classmethod
def crm_entity_to_sfc_momento3_payload(cls, entity: Any) -> Dict[str, Any]:
    prefix = f"{settings.SFC_TIPO_ENTIDAD}{settings.SFC_ENTIDAD_COD}"
    raw_code = cls._get_sf_field_value(entity, "Smart_Code__c") or ""
    if raw_code and not str(raw_code).startswith(prefix):
        codigo_queja = f"{prefix}{raw_code}"
    else:
        codigo_queja = raw_code

    fecha_act = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    closed_date_raw = cls._get_sf_field_value(entity, "ClosedDate")
    fecha_cierre_val = None

    if closed_date_raw:
        hora_actual = datetime.now().strftime("%H:%M:%S")

        if isinstance(closed_date_raw, datetime):
            fecha_cierre_val = closed_date_raw.strftime("%Y-%m-%dT%H:%M:%S")
            
        elif isinstance(closed_date_raw, date):
            fecha_cierre_val = f"{closed_date_raw.isoformat()}T{hora_actual}"
            
        elif isinstance(closed_date_raw, str) and closed_date_raw.strip():
            clean_str = closed_date_raw.strip()
            if "T" in clean_str:
                fecha_cierre_val = clean_str
            else:
                fecha_cierre_val = f"{clean_str.split()[0]}T{hora_actual}"
            
    status_val = cls._get_sf_field_value(entity, "Status")
    estado_cod_val = cls._translate_value_to_sfc("Status", status_val) or 2

    doc_rta_final = cls._get_sf_field_value(entity, "sinRespuestaFinal__c")
    doc_rta_final_val = bool(doc_rta_final) if doc_rta_final is not None else False

    payload_obj = SfcActualizarQuejaPayload(
        codigo_queja=str(codigo_queja),
        sexo=int(cls._translate_value_to_sfc("sc_genero__c", cls._get_sf_field_value(entity, "sc_genero__c")) or 2),
        lgbtiq=int(cls._translate_value_to_sfc("sc_LGBTIQ__c", cls._get_sf_field_value(entity, "sc_LGBTIQ__c")) or 2),
        condicion_especial=int(cls._translate_value_to_sfc("sc_Condicion_especial__c", cls._get_sf_field_value(entity, "sc_Condicion_especial__c")) or 98),
        canal_cod=int(cls._translate_value_to_sfc("canal__c", cls._get_sf_field_value(entity, "canal__c")) or 13),
        producto_cod=int(cls._translate_value_to_sfc("Product__c", cls._get_sf_field_value(entity, "Product__c")) or 207),
        macro_motivo_cod=int(cls._translate_value_to_sfc("Categorias_COL__c", cls._get_sf_field_value(entity, "Categorias_COL__c")) or 958),
        estado_cod=int(estado_cod_val),
        fecha_actualizacion=fecha_act,
        producto_digital=int(cls._translate_value_to_sfc("producto_digital__c", cls._get_sf_field_value(entity, "producto_digital__c")) or 1),
        admision=int(cls._translate_value_to_sfc("admision_col__c", cls._get_sf_field_value(entity, "admision_col__c")) or 1),
        desistimiento_queja=2,
        anexo_queja=bool(cls._get_sf_field_value(entity, "smart_anexo_queja__c") or False),
        tutela=int(cls._translate_value_to_sfc("Tutela__c", cls._get_sf_field_value(entity, "Tutela__c")) or 2),
        ente_control=int(cls._translate_value_to_sfc("Ente_de_control__c", cls._get_sf_field_value(entity, "Ente_de_control__c")) or 99),
        queja_expres=1,

        a_favor_de=cls._translate_value_to_sfc("Favorabilidad__c", cls._get_sf_field_value(entity, "Favorabilidad__c")),
        aceptacion_queja=cls._translate_value_to_sfc("Aceptacion__c", cls._get_sf_field_value(entity, "Aceptacion__c")),
        rectificacion_queja=cls._translate_value_to_sfc("Rectificacion__c", cls._get_sf_field_value(entity, "Rectificacion__c")),
        prorroga_queja=cls._translate_value_to_sfc("Prorroga__c", cls._get_sf_field_value(entity, "Prorroga__c")),
        documentacion_rta_final=doc_rta_final_val,
        fecha_cierre=fecha_cierre_val,
        marcacion=cls._translate_value_to_sfc("marcacion__c", cls._get_sf_field_value(entity, "marcacion__c")),
        tipo_fraude=cls._translate_value_to_sfc("tipo_fraude__c", cls._get_sf_field_value(entity, "tipo_fraude__c")),
        modalidad_fraude=cls._translate_value_to_sfc("modalidad_fraude__c", cls._get_sf_field_value(entity, "modalidad_fraude__c")),
        monto_reclamado=cls._get_sf_field_value(entity, "card_amount__c"),
        monto_reconocido=cls._get_sf_field_value(entity, "Total_Devuelto_por_Desconocimiento__c")
    )

    return payload_obj.model_dump()


@classmethod
def crm_entity_to_sfc_payload(cls, entity: Any, momento: int = 2) -> Dict[str, Any]:
    if momento == 3:
        return cls.crm_entity_to_sfc_momento3_payload(entity)
    return cls.crm_entity_to_sfc_momento2_payload(entity)


SfcSalesforceMapper._strip_html = _strip_html
SfcSalesforceMapper._get_sf_field_value = _get_sf_field_value
SfcSalesforceMapper._translate_value_to_crm = _translate_value_to_crm
SfcSalesforceMapper._translate_value_to_sfc = _translate_value_to_sfc
SfcSalesforceMapper.sfc_payload_to_db_dict = sfc_payload_to_db_dict
SfcSalesforceMapper.sfc_user_payload_to_db_dict = sfc_user_payload_to_db_dict
SfcSalesforceMapper.crm_entity_to_sfc_momento2_payload = crm_entity_to_sfc_momento2_payload
SfcSalesforceMapper.crm_entity_to_sfc_momento3_payload = crm_entity_to_sfc_momento3_payload
SfcSalesforceMapper.crm_entity_to_sfc_payload = crm_entity_to_sfc_payload