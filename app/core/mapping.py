# app/core/mapping.py
import json
import logging
import os
import re
import unicodedata
from datetime import datetime, date
from typing import Dict, Any, Optional, Set
from app.core.config import settings

logger = logging.getLogger(__name__)


class SfcSalesforceMapper:
    HTML_REGEX = re.compile(r'<[^>]*>')
    CLEAN_PHONE_DOC_REGEX = re.compile(r'[^\d+]')

    # Almacenamiento dinámico de los catálogos en RAM
    CATALOGOS: Dict[str, Dict[str, str]] = {}
    
    # Versiones inversas (CRM -> SFC) generadas automáticamente
    INVERSE_CATALOGS: Dict[str, Dict[str, int]] = {}

    # DIVIPOLA
    DEPT_DIVIPOLA_INV = {"11": "Bogotá D.C.", "05": "Antioquia", "08": "Atlántico"}
    MUNI_DIVIPOLA_INV = {"11001": "Bogotá D.C.", "05001": "Medellín", "08001": "Barranquilla"}
    
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
    def cargar_catalogos(cls):
        """Carga catalogos_sfc_crm.json y genera las versiones inversas en RAM."""
        ruta = os.path.join(os.path.dirname(__file__), "catalogos_sfc_crm.json")
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                cls.CATALOGOS = json.load(f)
            
            # Auto-inversión para el flujo CRM -> SFC
            cls.INVERSE_CATALOGS = {}
            for cat_key, cat_dict in cls.CATALOGOS.items():
                cls.INVERSE_CATALOGS[cat_key] = {
                    cls._normalize_text(v): int(k) for k, v in cat_dict.items()
                }
            
            # Alias personalizados para casos especiales
            if "tipo_id" in cls.INVERSE_CATALOGS:
                cls.INVERSE_CATALOGS["tipo_id"].update({"cc": 1, "ce": 2, "rut": 3, "nit": 3, "dni": 4, "pass": 5})
            if "punto_recepcion" in cls.INVERSE_CATALOGS:
                cls.INVERSE_CATALOGS["punto_recepcion"].update({"activate b2c": 99, "form: change data": 99, "updatecom": 99})

            cls.DEPT_DIVIPOLA = {"bogota d.c.": "11", "bogota": "11", "antioquia": "05", "atlantico": "08"}
            cls.MUNI_DIVIPOLA = {"bogota d.c.": "11001", "bogota": "11001", "medellin": "05001", "barranquilla": "08001"}

            logger.info(f"✅ [SfcSalesforceMapper] Cargados {len(cls.CATALOGOS)} catálogos desde JSON.")
        except Exception as e:
            logger.error(f"❌ Error al cargar catalogos_sfc_crm.json: {e}")

    @classmethod
    def get_crm_allowed_values(cls, catalog_key: str) -> Set[str]:
        """Retorna un conjunto de valores permitidos para un catálogo dado (Usado por Pydantic)."""
        if not cls.CATALOGOS:
            cls.cargar_catalogos()
        return set(cls.CATALOGOS.get(catalog_key, {}).values())

# Inicializamos la carga en memoria al importar la clase
SfcSalesforceMapper.cargar_catalogos()


# ======================================================================
# 📑 MATRICES CANÓNICAS DE ENRUTAMIENTO DE ATRIBUTOS
# ======================================================================
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
    "replica": "replica__c", "argumento_replica": "smart_Argumento_replica__c"
}

SfcSalesforceMapper.MAPPING_CRM_TO_SFC_MASTER = {
    "Smart_Code__c": "codigo_queja", "CreatedDate": "fecha_creación", "SuppliedName": "nombres",
    "id_number__c": "numero_id_CF", "SuppliedEmail": "correo", "SuppliedPhone": "telefono",
    "direccion__c": "direccion", "Departamento__c": "departamento_cod", "SC_municipio__c": "municipio_cod",
    "Description": "texto_queja", "smart_anexo_queja__c": "anexo_queja", "LastModifiedDate": "fecha_actualizacion",
    "sinRespuestaFinal__c": "documentacion_rta_final", "ClosedDate": "fecha_cierre", "card_amount__c": "monto_reclamado",
    "Total_Devuelto_por_Desconocimiento__c": "monto_reconocido", "SC_id_type__c": "tipo_id_CF",
    "tipo_de_persona__c": "tipo_Persona", "sc_LGBTIQ__c": "lgbtiq", "sc_Condicion_especial__c": "condicion_especial",
    "canal__c": "canal_cod", "Product__c": "producto_cod", "Categorias_COL__c": "macro_motivo_cod",
    "Ente_de_control__c": "ente_control", "Instancia_de_recepcion__c": "insta_recepcion",
    "admision_col__c": "admision", "Status": "estado_cod", "Producto_digital__c": "producto_digital",
    "Favorabilidad__c": "a_favor_de", "Aceptacion__c": "aceptacion_queja", "Rectificacion__c": "rectificacion_queja",
    "Desistimiento__c": "desistimiento_queja", "Prorroga__c": "prorroga_queja", "Tutela__c": "tutela",
    "Quejas_express__c": "queja_expres", "Tipo_Fraude__c": "tipo_fraude", "Modalidad_Fraude__c": "modalidad_fraude",
    "marcacion__c": "marcacion", "punto_recepcion": "punto_recepcion", "smart_Argumento_replica__c": "argumento_replica",
    "smart_escalamiento_DCF__c": "escalamiento_DCF"
}


# ======================================================================
# 🔁 MÉTODOS DE TRADUCCIÓN Y PIPELINE
# ======================================================================
@classmethod
def _strip_html(cls, text: str) -> str:
    if not text: return ""
    return cls.HTML_REGEX.sub('', str(text)).strip()

@classmethod
def _get_sf_field_value(cls, entity: Any, field_name: str) -> Any:
    if isinstance(entity, dict):
        if field_name == "Smart_Code__c" and "Smart_Code__c" not in entity:
            return entity.get("CaseNumber")
        return entity.get(field_name)
    if field_name == "Smart_Code__c" and not hasattr(entity, "Smart_Code__c"):
        return getattr(entity, "CaseNumber", None)
    return getattr(entity, field_name, None)

@classmethod
def _translate_value_to_crm(cls, sfc_key: str, sfc_value: Any) -> Any:
    """[SFC ➔ CRM] Convierte códigos numéricos de la SFC a picklists legibles."""
    if sfc_value is None: return None
    str_key = str(sfc_value)
    
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

    if sfc_key == "departamento_cod": return cls.DEPT_DIVIPOLA_INV.get(str_key, sfc_value)
    if sfc_key == "municipio_cod": return cls.MUNI_DIVIPOLA_INV.get(str_key, sfc_value)

    if sfc_key in ("tutela", "queja_expres", "escalamiento_DCF", "replica", "producto_digital", "prorroga_queja"):
        val_int = int(sfc_value) if str_key.isdigit() else sfc_value
        return "No" if (val_int == 2 or sfc_value is False) else "Si"

    return sfc_value

@classmethod
def _translate_value_to_sfc(cls, sf_key: str, sf_value: Any) -> Any:
    """[CRM ➔ SFC] Convierte strings de Salesforce a códigos numéricos SFC."""
    if sf_value is None: return None

    if sf_key in ("sinRespuestaFinal__c", "Aceptacion__c", "Prorroga__c", "Rectificacion__c", "Tutela__c", "Quejas_express__c"):
        v_clean = str(sf_value).lower().strip()
        if v_clean in ("si", "sí", "true", "1"): return 1
        if v_clean in ("no", "false", "2"): return 2
        return 1 if bool(sf_value) else 2

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
        "admision_col__c": ("admision", 9),
        "Favorabilidad__c": ("favorabilidad", 3),
        "Desistimiento__c": ("desistimiento", 2),
        "Tipo_Fraude__c": ("tipo_fraude", 2),
        "Modalidad_Fraude__c": ("modalidad_fraude", 90),
        "punto_recepcion": ("punto_recepcion", 4),
        "Categorias_COL__c": ("macro_motivo", 940)
    }

    if sf_key in sf_to_cat:
        cat_key, default_val = sf_to_cat[sf_key]
        return cls.INVERSE_CATALOGS.get(cat_key, {}).get(normalized, default_val)

    if sf_key == "Departamento__c": return cls.DEPT_DIVIPOLA.get(normalized, sf_value)
    if sf_key == "SC_municipio__c": return cls.MUNI_DIVIPOLA.get(normalized, sf_value)
    if sf_key == "Product__c": return 207

    if sf_key == "Status":
        status_map = {"new": 1, "nuevo": 1, "in progress": 2, "en progreso": 2, "stand by": 2, "espera": 2, "closed": 4, "cerrado": 4, "resolved": 4}
        return status_map.get(normalized, 2)

    if sf_key == "Producto_digital__c": return 1 if normalized in ("si", "sí", "true") else 2
    if sf_key == "Description": return cls._strip_html(str(sf_value))[:4500].strip()
    if sf_key in ("id_number__c", "SuppliedPhone"): return cls.CLEAN_PHONE_DOC_REGEX.sub('', str(sf_value))[:15]

    return sf_value

@classmethod
def sfc_payload_to_db_dict(cls, sfc_data: Dict[str, Any]) -> Dict[str, Any]:
    crm_data = {}
    for sfc_key, value in sfc_data.items():
        if sfc_key in cls.MAPPING_MOMENTO_1_SFC_TO_CRM:
            crm_key = cls.MAPPING_MOMENTO_1_SFC_TO_CRM[sfc_key]
            if sfc_key == "fecha_creacion" and isinstance(value, str):
                try: crm_data[crm_key] = datetime.fromisoformat(value.replace(" ", "T")).isoformat()
                except ValueError: crm_data[crm_key] = value
            elif sfc_key == "producto_cod":
                sfc_prod_nombre = sfc_data.get("producto_nombre", "")
                normalized_prod = cls._normalize_text(str(sfc_prod_nombre))
                crm_data[crm_key] = cls.PRODUCTO_SFC_TEXTO_TO_SF.get(normalized_prod, "Cuenta perfil")
            else:
                crm_data[crm_key] = cls._translate_value_to_crm(sfc_key, value)
    return crm_data

@classmethod
def crm_entity_to_sfc_payload(cls, entity: Any) -> Dict[str, Any]:
    sfc_data = {
        "codigo_pais": "COL", "punto_recepcion": 1,
        "tipo_entidad": settings.SFC_TIPO_ENTIDAD, "entidad_cod": settings.SFC_ENTIDAD_COD
    }
    for sf_field, sfc_field in cls.MAPPING_CRM_TO_SFC_MASTER.items():
        value = cls._get_sf_field_value(entity, sf_field)
        if isinstance(value, (datetime, date)):
            if sf_field == "CreatedDate":
                sfc_data[sfc_field] = value.strftime("%Y-%m-%dT%H:%M:%S")
                sfc_data["fecha_creacion"] = value.strftime("%Y-%m-%d")
            else:
                sfc_data[sfc_field] = value.strftime("%Y-%m-%d")
        elif isinstance(value, str) and sf_field in ("CreatedDate", "ClosedDate", "LastModifiedDate", "person_birthdate__c") and ("T" in value or "-" in value):
            try:
                clean_date = value.split(".")[0] if "." in value else value
                dt_val = datetime.fromisoformat(clean_date) if "T" in clean_date else datetime.strptime(clean_date, "%Y-%m-%d")
                if sf_field == "CreatedDate":
                    sfc_data[sfc_field] = dt_val.strftime("%Y-%m-%dT%H:%M:%S")
                    sfc_data["fecha_creacion"] = dt_val.strftime("%Y-%m-%d")
                else:
                    sfc_data[sfc_field] = dt_val.strftime("%Y-%m-%d")
            except ValueError: sfc_data[sfc_field] = value
        else:
            sfc_data[sfc_field] = cls._translate_value_to_sfc(sf_field, value)

    if "tipo_Persona" in sfc_data:
        sfc_data["tipo_persona"] = sfc_data["tipo_Persona"]

    return sfc_data

SfcSalesforceMapper._strip_html = _strip_html
SfcSalesforceMapper._get_sf_field_value = _get_sf_field_value
SfcSalesforceMapper._translate_value_to_crm = _translate_value_to_crm
SfcSalesforceMapper._translate_value_to_sfc = _translate_value_to_sfc
SfcSalesforceMapper.sfc_payload_to_db_dict = sfc_payload_to_db_dict
SfcSalesforceMapper.crm_entity_to_sfc_payload = crm_entity_to_sfc_payload