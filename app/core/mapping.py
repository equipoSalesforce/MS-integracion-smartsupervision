import logging
import json
from datetime import datetime
from typing import Dict, Any, Optional
from app.models.quejas import Queja

logger = logging.getLogger(__name__)

class SfcSalesforceMapper:
    
    # ======================================================================
    # DICCIONARIOS OFICIALES DE EQUIVALENCIAS DE VALORES
    # ======================================================================
    
    # 1. Sexo / Género (sc_genero__c)
    SEXO_SFC_TO_SF = {1: "Femenino", 2: "Masculino", 3: "Trans", 4: "No binario", 10: "No Aplica"}
    SEXO_SF_TO_SFC = {v.lower(): k for k, v in SEXO_SFC_TO_SF.items()}

    # 2. Canal de Atención (canal__c)
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

    # 3. Ente de Control (Ente_de_control__c)
    ENTE_SFC_TO_SF = {1: "Procuraduría", 2: "Contraloría", 3: "Defensoría del pueblo", 4: "Personerías", 99: "Otros"}
    ENTE_SF_TO_SFC = {v.lower(): k for k, v in ENTE_SFC_TO_SF.items()}

    # 4. Condición Especial (sc_Condicion_especial__c)
    CONDICION_SFC_TO_SF = {
        1: "Adulto mayor", 2: "Pensionado", 3: "Receptor de subsidio", 4: "Discapacidad auditiva",
        5: "Discapacidad física", 6: "Menor de edad", 7: "Indígena", 8: "Mujer embarazada",
        9: "Reinsertado", 10: "Víctima del conflicto armado", 11: "Afrocolombiano", 12: "Desplazado",
        13: "Madre cabeza de familia", 14: "Sordomudo", 15: "Discapacidad cognitive", 16: "Discapacidad visual",
        17: "Periodista", 90: "Otra", 98: "No aplica"
    }
    CONDICION_SF_TO_SFC = {v.lower(): k for k, v in CONDICION_SFC_TO_SF.items()}

    # 5. Tipo de Persona (tipo_de_persona__c)
    PERSONA_SFC_TO_SF = {1: "Natural", 2: "Jurídica"}
    PERSONA_SF_TO_SFC = {v.lower(): k for k, v in PERSONA_SFC_TO_SF.items()}

    # ======================================================================
    # MAPEO DE ATRIBUTOS (CAMPOS SFC <-> SALESFORCE REAL)
    # ======================================================================
    MAPPING_SFC_TO_SF = {
        "codigo_queja": "Smart_Code__c",
        "fecha_creacion": "CreatedDate",
        "fecha_creación": "CreatedDate",
        "nombres": "SuppliedName",
        "tipo_id_CF": "id_type__c",
        "numero_id_CF": "id_number__c",
        "telefono": "SuppliedPhone",
        "correo": "SuppliedEmail",
        "razon_social": "company_name__c",
        "sexo": "sc_genero__c",                           # Corregido
        "canal_cod": "canal__c",                           # Corregido
        "ente_control": "Ente_de_control__c",
        "condicion_especial": "sc_Condicion_especial__c",  # Corregido
        "tipo_persona": "tipo_de_persona__c",              # Corregido
        "anexo_queja": "smart_anexo_queja__c",             # Corregido
        "tutela": "Urgent_Case__c",
        "desistimiento_queja": "Desistimiento__c",
        "queja_expres": "Quejas_express__c",
        "insta_recepcion": "Instancia_de_recepcion__c",
        "producto_cod": "Product__c",
        "producto_nombre": "smart_Producto_nombre__c",
        "macro_motivo_cod": "Categorias_COL__c",
        "texto_queja": "Description",
        
        # Mapeos de Cierre / Momento 3
        "estado_cod": "Smart_Status__c",                   # Corregido
        "fecha_cierre": "ClosedDate",
        "monto_reconocido": "card_amount__c",               # Corregido
        "aceptacion_queja": "Aceptacion__c",
        "prorroga_queja": "Prorroga__c",
        "admision": "admision_col__c",
        "rectificacion_queja": "Rectificacion__c"
    }

    MAPPING_SF_TO_SFC = {v: k for k, v in MAPPING_SFC_TO_SF.items() if k != "fecha_creación"}

    @classmethod
    def _translate_value_to_sf(cls, db_key: str, sfc_value: Any) -> Any:
        if sfc_value is None: return None
        try:
            val_int = int(sfc_value)
        except (ValueError, TypeError):
            return sfc_value

        if db_key == "sc_genero__c":
            return cls.SEXO_SFC_TO_SF.get(val_int, "No Aplica")
        elif db_key == "canal__c":
            return cls.CANAL_SFC_TO_SF.get(val_int, "Internet")
        elif db_key == "Ente_de_control__c":
            return cls.ENTE_SFC_TO_SF.get(val_int, "Otros")
        elif db_key == "sc_Condicion_especial__c":
            return cls.CONDICION_SFC_TO_SF.get(val_int, "No aplica")
        elif db_key == "tipo_de_persona__c":
            return cls.PERSONA_SFC_TO_SF.get(val_int, "Natural")
        elif db_key in ("smart_anexo_queja__c", "Urgent_Case__c"):
            return bool(sfc_value)
            
        return sfc_value

    @classmethod
    def _translate_value_to_sfc(cls, sf_key: str, sf_value: Any) -> Any:
        if sf_value is None: return None
        if isinstance(sf_value, bool) and sf_key in ("smart_anexo_queja__c", "Urgent_Case__c"):
            return 1 if sf_value else 2

        sf_val_str = str(sf_value).strip().lower()

        if sf_key == "sc_genero__c":
            return cls.SEXO_SF_TO_SFC.get(sf_val_str, 10)
        elif sf_key == "canal__c":
            return cls.CANAL_SF_TO_SFC.get(sf_val_str, 13)
        elif sf_key == "Ente_de_control__c":
            return cls.ENTE_SF_TO_SFC.get(sf_val_str, 99)
        elif sf_key == "sc_Condicion_especial__c":
            return cls.CONDICION_SF_TO_SFC.get(sf_val_str, 98)
        elif sf_key == "tipo_de_persona__c":
            return cls.PERSONA_SF_TO_SFC.get(sf_val_str, 1)

        return sf_value

    @classmethod
    def sfc_payload_to_db_dict(cls, sfc_data: Dict[str, Any]) -> Dict[str, Any]:
        db_data = {}
        for key, value in sfc_data.items():
            if key in cls.MAPPING_SFC_TO_SF:
                db_key = cls.MAPPING_SFC_TO_SF[key]
                if db_key in ("CreatedDate", "ClosedDate") and isinstance(value, str):
                    try:
                        db_data[db_key] = datetime.fromisoformat(value.replace(" ", "T"))
                    except ValueError:
                        db_data[db_key] = None
                else:
                    db_data[db_key] = cls._translate_value_to_sf(db_key, value)
            else:
                if key in ("tipo_entidad", "entidad_cod", "status_smart"):
                    db_data[key] = value
        return db_data

    @classmethod
    def db_entity_to_sfc_payload(cls, entity: Queja) -> Dict[str, Any]:
        sfc_data = {}
        for sf_field, sfc_field in cls.MAPPING_SF_TO_SFC.items():
            value = getattr(entity, sf_field, None)
            if isinstance(value, datetime):
                sfc_data[sfc_field] = value.isoformat()
            else:
                sfc_data[sfc_field] = cls._translate_value_to_sfc(sf_field, value)

        for metadata in ("tipo_entidad", "entidad_cod"):
            val = getattr(entity, metadata, None)
            if val is not None: sfc_data[metadata] = val
        return sfc_data

    @classmethod
    def create_entity_from_sfc(cls, sfc_data: Dict[str, Any], status_smart: str) -> Queja:
        db_dict = cls.sfc_payload_to_db_dict(sfc_data)
        db_dict["status_smart"] = status_smart
        return Queja(**db_dict)