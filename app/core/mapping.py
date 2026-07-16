# app/core/mapping.py
import logging
import re
import unicodedata
from datetime import datetime, date
from typing import Dict, Any, Optional
from app.core.config import settings

logger = logging.getLogger(__name__)

class SfcSalesforceMapper:
    
    # ======================================================================
    # DICCIONARIOS DE EQUIVALENCIAS DE VALORES (DIVIPOLA & Picklists)
    # ======================================================================
    SEXO_SFC_TO_SF = {1: "Femenino", 2: "Masculino", 3: "Trans", 4: "No binario", 10: "No Aplica"}
    SEXO_SF_TO_SFC = {v.lower(): k for k, v in SEXO_SFC_TO_SF.items()}

    CANAL_SFC_TO_SF = {
        1: "Aplicaciones móviles", 2: "Cajeros automáticos administrados", 3: "Cajeros automáticos no propios",
        4: "Cajeros automáticos propios", 5: "Centro de atención telefónica (Call center/Contac center)",
        6: "Asistente virtual", 7: "Corresponsales digitales propios", 8: "Corresponsales digitales tercerizados",
        9: "Corresponsales físicos propios", 10: "Corresponsales físicos tercerizados",
        11: "Corresponsales móviles propios", 12: "Corresponsales móviles tercerizados",
        13: "Internet", 14: "Oficinas", 15: "POS administrados", 16: "POS no propios",
        17: "POS propios", 18: "Sistema de acceso remoto para clientes (RAS)", 19: "Sistema de Audio Respuesta (IVR)"
    }
    
    CANAL_SF_TO_SFC = {v.lower(): k for k, v in CANAL_SFC_TO_SF.items()}

    ENTE_SFC_TO_SF = {1: "Procuraduría", 2: "Contraloría", 3: "Defensoría del pueblo", 4: "Personerías", 99: "Otros"}
    ENTE_SF_TO_SFC = {v.lower(): k for k, v in ENTE_SFC_TO_SF.items()}

    CONDICION_SFC_TO_SF = {
        1: "Adulto mayor", 2: "Pensionado", 3: "Receptor de subsidio", 4: "Discapacidad auditiva",
        5: "Discapacidad física", 6: "Menor de edad", 7: "Indígena", 8: "Mujer embarazada",
        9: "Reinsertado", 10: "Víctima del conflicto armado", 11: "Afrocolombiano", 12: "Desplazado",
        13: "Madre cabeza de familia", 14: "Sordomudo", 15: "Discapacidad cognitive", 16: "Discapacidad visual",
        17: "Periodista", 90: "Otra", 98: "No aplica"
    }
    CONDICION_SF_TO_SFC = {v.lower(): k for k, v in CONDICION_SFC_TO_SF.items()}

    PERSONA_SFC_TO_SF = {1: "Natural", 2: "Jurídica"}
    PERSONA_SF_TO_SFC = {v.lower(): k for k, v in PERSONA_SFC_TO_SF.items()}

    DEPT_DIVIPOLA = {"bogota": "11", "bogota d.c.": "11", "antioquia": "05", "atlantico": "08"}
    DEPT_DIVIPOLA_INV = {v: k.title() for k, v in DEPT_DIVIPOLA.items()}

    MUNI_DIVIPOLA = {"bogota": "11001", "bogota d.c.": "11001", "medellin": "05001", "barranquilla": "08001"}
    MUNI_DIVIPOLA_INV = {v: k.title() for k, v in MUNI_DIVIPOLA.items()}

    # ======================================================================
    # ⬇️ MOTOR 1: SFC -> CRM (MOMENTO 1)
    # Solo los 29 campos exactos que la SFC envía cuando nace una queja.
    # ======================================================================
    MAPPING_MOMENTO_1_SFC_TO_CRM = {
        "fecha_creacion": "CreatedDate",
        "codigo_queja": "Smart_Code__c",
        "codigo_pais": "codigo_pais__c",
        "departamento_cod": "Departamento__c",
        "municipio_cod": "SC_municipio__c",
        "nombres": "SuppliedName",
        "tipo_id_CF": "id_type__c",
        "numero_id_CF": "id_number__c",
        "telefono": "SuppliedPhone",
        "correo": "SuppliedEmail",
        "tipo_persona": "tipo_de_persona__c",
        "sexo": "sc_genero__c",
        "lgbtiq": "lgbtiq__c",
        "canal_cod": "canal__c",
        "condicion_especial": "sc_Condicion_especial__c",
        "producto_cod": "Product__c",
        "producto_nombre": "smart_Producto_nombre__c",
        "macro_motivo_cod": "Categorias_COL__c",
        "texto_queja": "Description",
        "anexo_queja": "smart_anexo_queja__c",
        "tutela": "Urgent_Case__c",
        "ente_control": "Ente_de_control__c",
        "escalamiento_DCF": "escalamiento_DCF__c",
        "replica": "replica__c",
        "argumento_replica": "argumento_replica__c",
        "desistimiento_queja": "Desistimiento__c",
        "queja_expres": "Quejas_express__c"
    }

    # ======================================================================
    # ⬆️ MOTOR 2: CRM -> SFC (MOMENTOS 2 y 3)
    # Todos los campos, incluyendo los requeridos para envío y cierre.
    # ======================================================================
    MAPPING_CRM_TO_SFC_MASTER = {
        # --- Datos Base (M2) ---
        "Smart_Code__c": "codigo_queja",
        "Departamento__c": "departamento_cod",
        "SC_municipio__c": "municipio_cod",
        "canal__c": "canal_cod",
        "Product__c": "producto_cod",
        "Categorias_COL__c": "macro_motivo_cod",
        "CreatedDate": "fecha_creación",              # M2/M3 requiere tilde
        "SuppliedName": "nombres",
        "id_type__c": "tipo_id_CF",
        "id_number__c": "numero_id_CF",
        "tipo_de_persona__c": "tipo_Persona",          # M2/M3 requiere 'P' mayúscula
        "Description": "texto_queja",
        "smart_anexo_queja__c": "anexo_queja",
        "Ente_de_control__c": "ente_control",
        
        # --- Variables específicas de envío del Momento 2 ---
        "Instancia_de_recepcion__c": "insta_recepcion",
        "admision_col__c": "admision",

        # --- Datos de Cierre (Momento 3) ---
        "ClosedDate": "fecha_cierre",
        "Marcacion__c": "marcacion",
        "monto_reclamado__c": "monto_reclamado",
        "Total_Devuelto_por_Desconocimiento__c": "monto_reconocido",
        "sinRespuestaFinal?": "documentacion_rta_final",
        "Aceptacion__c": "aceptacion_queja",
        "Prorroga__c": "prorroga_queja",
        "Rectificacion__c": "rectificacion_queja"
    }

    # ======================================================================
    # MÉTODOS DE UTILIDAD
    # ======================================================================
    @staticmethod
    def _normalize_text(text: str) -> str:
        if not text: return ""
        normalized = "".join(c for c in unicodedata.normalize('NFD', str(text)) if unicodedata.category(c) != 'Mn')
        return normalized.lower().strip()

    @staticmethod
    def _strip_html(text: str) -> str:
        if not text: return ""
        return re.sub(r'<[^>]*>', '', str(text)).strip()

    @classmethod
    def _get_sf_field_value(cls, entity: Any, field_name: str) -> Any:
        if isinstance(entity, dict):
            return entity.get(field_name)
        return getattr(entity, field_name, None)

    # ======================================================================
    # TRADUCTORES (CÓDIGOS <-> TEXTOS)
    # ======================================================================
    @classmethod
    def _translate_value_to_crm(cls, sfc_key: str, sfc_value: Any) -> Any:
        """[SFC -> CRM] Momento 1: Convierte códigos SFC a textos descriptivos."""
        if sfc_value is None: return None
        if sfc_key in ("sexo", "sc_genero__c"): return cls.SEXO_SFC_TO_SF.get(int(sfc_value), "No Aplica")
        elif sfc_key in ("canal_cod", "canal__c"): return cls.CANAL_SFC_TO_SF.get(int(sfc_value), "Internet")
        elif sfc_key in ("ente_control", "Ente_de_control__c"): return cls.ENTE_SFC_TO_SF.get(int(sfc_value), "Otros")
        elif sfc_key in ("condicion_especial", "sc_Condicion_especial__c"): return cls.CONDICION_SFC_TO_SF.get(int(sfc_value), "No aplica")
        elif sfc_key in ("tipo_persona", "tipo_de_persona__c"): return cls.PERSONA_SFC_TO_SF.get(int(sfc_value), "Natural")
        elif sfc_key in ("departamento_cod", "Departamento__c"): return cls.DEPT_DIVIPOLA_INV.get(str(sfc_value), sfc_value)
        elif sfc_key in ("municipio_cod", "SC_municipio__c"): return cls.MUNI_DIVIPOLA_INV.get(str(sfc_value), sfc_value)
        elif sfc_key in ("anexo_queja", "smart_anexo_queja__c", "tutela", "Urgent_Case__c", 
                         "desistimiento_queja", "Desistimiento__c", "queja_expres", "Quejas_express__c",
                         "lgbtiq", "lgbtiq__c", "escalamiento_DCF", "escalamiento_DCF__c", "replica", "replica__c"):
            return bool(sfc_value)
        return sfc_value

    @classmethod
    def _translate_value_to_sfc(cls, sf_key: str, sf_value: Any) -> Any:
        """[CRM -> SFC] Momentos 2 y 3: Convierte textos descriptivos a códigos SFC."""
        if sf_value is None: return None
        if sf_key in ("smart_anexo_queja__c", "Urgent_Case__c", "sinRespuestaFinal?", "Aceptacion__c", "Prorroga__c", "Rectificacion__c"):
            return bool(sf_value)

        sf_val_str = str(sf_value).strip()
        normalized = cls._normalize_text(sf_val_str)

        if sf_key == "canal__c": return cls.CANAL_SF_TO_SFC.get(normalized, 13)
        elif sf_key == "Ente_de_control__c": return cls.ENTE_SF_TO_SFC.get(normalized, 99)
        elif sf_key == "tipo_de_persona__c": return cls.PERSONA_SF_TO_SFC.get(normalized, 1)
        elif sf_key == "sc_genero__c": return cls.SEXO_SF_TO_SFC.get(normalized, 10)
        elif sf_key == "sc_Condicion_especial__c": return cls.CONDICION_SF_TO_SFC.get(normalized, 98)
        elif sf_key == "Departamento__c": return cls.DEPT_DIVIPOLA.get(normalized, sf_value)
        elif sf_key == "SC_municipio__c": return cls.MUNI_DIVIPOLA.get(normalized, sf_value)
        elif sf_key == "Description": return cls._strip_html(sf_val_str)[:4500].strip()
        elif sf_key in ("id_number__c", "bank_account_dni__c", "SuppliedPhone", "numero_whatsapp__c"):
            return re.sub(r'[^\d+]', '', sf_val_str)[:15]
        return sf_value

    # ======================================================================
    # MÉTODOS PÚBLICOS DE MAPEO (LAS PUERTAS DE ENTRADA Y SALIDA)
    # ======================================================================
    @classmethod
    def sfc_payload_to_db_dict(cls, sfc_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        [MOMENTO 1] (SFC -> CRM)
        Traduce el JSON original de la SFC a tu CRM local (solo 29 variables).
        """
        crm_data = {}
        for sfc_key, value in sfc_data.items():
            if sfc_key in cls.MAPPING_MOMENTO_1_SFC_TO_CRM:
                crm_key = cls.MAPPING_MOMENTO_1_SFC_TO_CRM[sfc_key]
                if sfc_key == "fecha_creacion" and isinstance(value, str):
                    try:
                        crm_data[crm_key] = datetime.fromisoformat(value.replace(" ", "T")).isoformat()
                    except ValueError:
                        crm_data[crm_key] = value
                else:
                    crm_data[crm_key] = cls._translate_value_to_crm(sfc_key, value)
        return crm_data

    @classmethod
    def crm_entity_to_sfc_payload(cls, entity: Any) -> Dict[str, Any]:
        """
        [MOMENTO 2 y 3] (CRM -> SFC)
        Traduce el JSON/Diccionario que llega de tu CRM a la estructura estricta de la SFC.
        """
        sfc_data = {
            "codigo_pais": "COL",
            "punto_recepcion": 1,
            "tipo_entidad": settings.SFC_TIPO_ENTIDAD,  # <-- INYECCIÓN DINÁMICA
            "entidad_cod": settings.SFC_ENTIDAD_COD     # <-- INYECCIÓN DINÁMICA
        }
        
        for sf_field, sfc_field in cls.MAPPING_CRM_TO_SFC_MASTER.items():
            value = cls._get_sf_field_value(entity, sf_field)
            
            if isinstance(value, (datetime, date)):
                # Manejo de fechas para la SFC (Requiere T en creación)
                if sf_field == "CreatedDate":
                    sfc_data[sfc_field] = value.strftime("%Y-%m-%dT%H:%M:%S")
                    sfc_data["fecha_creacion"] = value.strftime("%Y-%m-%d")
                else:
                    sfc_data[sfc_field] = value.strftime("%Y-%m-%d")
            elif isinstance(value, str) and sf_field in ("CreatedDate", "ClosedDate") and "T" in value:
                # Si llega como ISO string, lo formateamos correctamente
                try:
                    dt_val = datetime.fromisoformat(value)
                    if sf_field == "CreatedDate":
                        sfc_data[sfc_field] = dt_val.strftime("%Y-%m-%dT%H:%M:%S")
                        sfc_data["fecha_creacion"] = dt_val.strftime("%Y-%m-%d")
                    else:
                        sfc_data[sfc_field] = dt_val.strftime("%Y-%m-%d")
                except ValueError:
                    sfc_data[sfc_field] = value
            else:
                sfc_data[sfc_field] = cls._translate_value_to_sfc(sf_field, value)

        # Inyectamos duplicado de seguridad tipográfica si aplica
        if "tipo_Persona" in sfc_data:
            sfc_data["tipo_persona"] = sfc_data["tipo_Persona"]

        return sfc_data