# app/core/mapping.py
import logging
import re
import unicodedata
from datetime import datetime, date
from typing import Dict, Any, Optional
from app.models.quejas import Queja

logger = logging.getLogger(__name__)

class SfcSalesforceMapper:
    
    # ======================================================================
    # DICCIONARIOS OFICIALES DE EQUIVALENCIAS DE VALORES (DIVIPOLA & Picklists)
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
    # MAPA MAESTRO DE TRADUCCIÓN UNIFICADO (Salesforce CRM <-> SFC)
    # ======================================================================
    MAPPING_SF_TO_SFC_MASTER = {
        # --- Datos Básicos y Momento 2 ---
        "Smart_Code__c": "codigo_queja",
        "Departamento__c": "departamento_cod",
        "SC_municipio__c": "municipio_cod",
        "canal__c": "canal_cod",
        "Product__c": "producto_cod",
        "Categorias_COL__c": "macro_motivo_cod",
        "CreatedDate": "fecha_creación",              # Mapeado para M2/M3 con tilde
        "SuppliedName": "nombres",
        "id_type__c": "tipo_id_CF",
        "id_number__c": "numero_id_CF",
        "tipo_de_persona__c": "tipo_Persona",          # Capital P para M2/M3
        "Instancia_de_recepcion__c": "insta_recepcion",
        "admision_col__c": "admision",
        "Description": "texto_queja",
        "smart_anexo_queja__c": "anexo_queja",
        "Ente_de_control__c": "ente_control",
        
        # --- Datos Momento 1 Adicionales ---
        "LastName": "apellido",
        "person_birthdate__c": "fecha_nacimiento",
        "SuppliedEmail": "correo",
        "SuppliedPhone": "telefono",
        "company_name__c": "razon_social",
        "direccion__c": "direccion",
        "LastModifiedDate": "fecha_actualizacion",
        "sc_genero__c": "sexo",
        "sc_Condicion_especial__c": "condicion_especial",
        "Urgent_Case__c": "tutela",
        "Desistimiento__c": "desistimiento_queja",
        "Quejas_express__c": "queja_expres",
        "smart_Producto_nombre__c": "producto_nombre",

        # --- Datos Momento 3 (Cierre) ---
        "ClosedDate": "fecha_cierre",
        "Marcacion__c": "marcacion",
        "monto_reclamado__c": "monto_reclamado",
        "Total_Devuelto_por_Desconocimiento__c": "monto_reconocido",
        "sinRespuestaFinal?": "documentacion_rta_final",
        "Aceptacion__c": "aceptacion_queja",
        "Prorroga__c": "prorroga_queja",
        "Rectificacion__c": "rectificacion_queja"
    }

    # Mapa de Entrada para Reconstrucción (Momento 1)
    MAPPING_SFC_TO_SF = {v: k for k, v in MAPPING_SF_TO_SFC_MASTER.items()}
    # Añadimos aliases alternativos recibidos por la SFC en Momento 1 para evitar caídas
    MAPPING_SFC_TO_SF.update({
        "fecha_creacion": "CreatedDate",
        "tipo_persona": "tipo_de_persona__c"
    })

    # ======================================================================
    # MÉTODOS DE SOPORTE TÉCNICO
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
        fallbacks = {
            "Smart_Code__c": ["CaseNumber"],
            "SuppliedName": ["FirstName"],
            "id_number__c": ["bank_account_dni__c"],
            "Description": ["Subject"],
            "SuppliedEmail": ["ContactEmail"],
            "SuppliedPhone": ["numero_whatsapp__c"]
        }
        if isinstance(entity, dict):
            val = entity.get(field_name)
        else:
            val = getattr(entity, field_name, None)
        if val is not None: return val
            
        for fallback in fallbacks.get(field_name, []):
            if isinstance(entity, dict):
                val = entity.get(fallback)
            else:
                val = getattr(entity, fallback, None)
            if val is not None: return val
        return None

    @classmethod
    def _translate_value_to_sfc(cls, sf_key: str, sf_value: Any) -> Any:
        if sf_value is None: 
            return None
        if sf_key in ("smart_anexo_queja__c", "Urgent_Case__c", "sinRespuestaFinal?", "Desistimiento__c", "Quejas_express__c"):
            return bool(sf_value)                        # Forzamos booleanos reales para la SFC

        sf_val_str = str(sf_value).strip()
        normalized = cls._normalize_text(sf_val_str)

        if sf_key == "canal__c":
            return cls.CANAL_SF_TO_SFC.get(normalized, 13)
        elif sf_key == "Ente_de_control__c":
            return cls.ENTE_SF_TO_SFC.get(normalized, 99)
        elif sf_key == "tipo_de_persona__c":
            return cls.PERSONA_SF_TO_SFC.get(normalized, 1)
        elif sf_key == "sc_genero__c":
            return cls.SEXO_SF_TO_SFC.get(normalized, 10)
        elif sf_key == "sc_Condicion_especial__c":
            return cls.CONDICION_SF_TO_SFC.get(normalized, 98)
        elif sf_key == "Departamento__c":
            return cls.DEPT_DIVIPOLA.get(normalized, sf_value)
        elif sf_key == "SC_municipio__c":
            return cls.MUNI_DIVIPOLA.get(normalized, sf_value)
        elif sf_key == "Description":
            return cls._strip_html(sf_val_str)[:4500].strip()
        elif sf_key in ("id_number__c", "bank_account_dni__c"):
            return re.sub(r'[^a-zA-Z0-9]', '', sf_val_str)[:15]
        elif sf_key in ("SuppliedPhone", "numero_whatsapp__c"):
            return re.sub(r'[^\d+]', '', sf_val_str)[:15]

        return sf_value

    # ======================================================================
    # SERIALIZADORES CONSOLIDADOS (CONVERTIDORES)
    # ======================================================================
    @classmethod
    def db_entity_to_sfc_payload(cls, entity: Any) -> Dict[str, Any]:
        """
        [Unificado para M2 y M3] Traduce TODOS los atributos relacionales de la DB
        a un diccionario maestro etiquetado en el lenguaje de la SFC[cite: 1].
        """
        sfc_data = {
            "codigo_pais": "COL",
            "punto_recepcion": 1
        }
        for sf_field, sfc_field in cls.MAPPING_SF_TO_SFC_MASTER.items():
            value = cls._get_sf_field_value(entity, sf_field)
            
            if isinstance(value, (datetime, date)):
                # El Momento 2 requiere ISO completa con 'T', Momento 3 requiere 'YYYY-MM-DD'[cite: 1]
                if sf_field == "CreatedDate":
                    sfc_data[sfc_field] = value.strftime("%Y-%m-%dT%H:%M:%S")
                    sfc_data["fecha_creacion"] = value.strftime("%Y-%m-%d") # Fallback M1/M3
                else:
                    sfc_data[sfc_field] = value.strftime("%Y-%m-%d")
            else:
                sfc_data[sfc_field] = cls._translate_value_to_sfc(sf_field, value)

        # Inyectamos duplicados alternativos por seguridad tipográfica de momentos
        if "tipo_Persona" in sfc_data:
            sfc_data["tipo_persona"] = sfc_data["tipo_Persona"]

        return sfc_data

    @classmethod
    def sfc_payload_to_db_dict(cls, sfc_data: Dict[str, Any]) -> Dict[str, Any]:
        """[Para Momento 1] Traduce el JSON nativo de la SFC a un diccionario Salesforce."""
        db_data = {}
        for key, value in sfc_data.items():
            if key in cls.MAPPING_SFC_TO_SF:
                db_key = cls.MAPPING_SFC_TO_SF[key]
                if db_key in ("CreatedDate", "ClosedDate", "LastModifiedDate") and isinstance(value, str):
                    try:
                        db_data[db_key] = datetime.fromisoformat(value.replace(" ", "T"))
                    except ValueError:
                        db_data[db_key] = None
                else:
                    db_data[db_key] = value
        return db_data

    @classmethod
    def create_entity_from_sfc(cls, sfc_data: Dict[str, Any], status_smart: str) -> Queja:
        db_dict = cls.sfc_payload_to_db_dict(sfc_data)
        db_dict["status_smart"] = status_smart
        return Queja(**db_dict)