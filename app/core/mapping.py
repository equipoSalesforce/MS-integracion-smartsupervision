import logging
import re
import unicodedata
from datetime import datetime, date
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

    # 6. Códigos Oficiales DIVIPOLA (SFC Departamentos / Municipios)[cite: 1]
    DEPT_DIVIPOLA = {
        "bogota": "11", "bogota d.c.": "11", "antioquia": "05", "atlantico": "08", 
        "bolivar": "13", "boyaca": "15", "caldas": "17", "caqueta": "18", "cauca": "19", 
        "cesar": "20", "cordoba": "23", "cundinamarca": "25", "choco": "27", "huila": "41", 
        "la guajira": "44", "magdalena": "47", "meta": "50", "narino": "52", "nariño": "52", 
        "norte de santander": "54", "quindio": "63", "risaralda": "66", "santander": "68", 
        "sucre": "70", "tolima": "73", "valle del cauca": "76", "arauca": "81", 
        "casanare": "85", "putumayo": "86", "san andres": "88", "amazonas": "91", 
        "guainia": "94", "guaviare": "95", "vaupes": "97", "vichada": "99"
    }
    DEPT_DIVIPOLA_INV = {v: k.title() for k, v in DEPT_DIVIPOLA.items()}

    MUNI_DIVIPOLA = {
        "bogota": "11001", "bogota d.c.": "11001", "medellin": "05001", "barranquilla": "08001", 
        "cartagena": "13001", "cali": "76001", "bucaramanga": "68001", "cucuta": "54001", 
        "pereira": "66001", "santa marta": "47001", "ibague": "73001", "bello": "05088", 
        "valledupar": "20001", "villavicencio": "50001", "soledad": "08758", "pasto": "52001", 
        "monteria": "23001", "soacha": "25754", "manizales": "17001", "neiva": "41001"
    }
    MUNI_DIVIPOLA_INV = {v: k.title() for k, v in MUNI_DIVIPOLA.items()}

    # ======================================================================
    # MAPEO DE ATRIBUTOS (CAMPOS SFC <-> SALESFORCE REAL)
    # ======================================================================
    MAPPING_SFC_TO_SF = {
        "codigo_queja": "Smart_Code__c",
        "fecha_creacion": "CreatedDate",
        "fecha_creación": "CreatedDate",
        "nombres": "SuppliedName",
        "apellido": "LastName",                             # Añadido[cite: 1]
        "numero_id_CF": "id_number__c",
        "fecha_nacimiento": "person_birthdate__c",           # Añadido[cite: 1]
        "correo": "SuppliedEmail",
        "telefono": "SuppliedPhone",
        "razon_social": "company_name__c",
        "direccion": "direccion__c",                         # Añadido[cite: 1]
        "departamento_cod": "Departamento__c",
        "municipio_cod": "SC_municipio__c",
        "texto_queja": "Description",
        "anexo_queja": "smart_anexo_queja__c",
        "fecha_actualizacion": "LastModifiedDate",           # Añadido[cite: 1]
        "documentacion_rta_final": "sinRespuestaFinal?",     # Añadido[cite: 1]
        "fecha_cierre": "ClosedDate",
        "marcacion": "Marcacion__c",                         # Añadido[cite: 1]
        "monto_reclamado": "monto_reclamado__c",             # Añadido GAP[cite: 1]
        "monto_reconocido": "Total_Devuelto_por_Desconocimiento__c",  # Homologado según Excel[cite: 1]
        "sexo": "sc_genero__c",
        "canal_cod": "canal__c",
        "ente_control": "Ente_de_control__c",
        "condicion_especial": "sc_Condicion_especial__c",
        "tipo_persona": "tipo_de_persona__c",
        "tutela": "Urgent_Case__c",
        "desistimiento_queja": "Desistimiento__c",
        "queja_expres": "Quejas_express__c",
        "insta_recepcion": "Instancia_de_recepcion__c",
        "producto_cod": "Product__c",
        "producto_nombre": "smart_Producto_nombre__c",
        "macro_motivo_cod": "Categorias_COL__c",
        "aceptacion_queja": "Aceptacion__c",
        "prorroga_queja": "Prorroga__c",
        "admision": "admision_col__c",
        "rectificacion_queja": "Rectificacion__c"
    }

    MAPPING_SF_TO_SFC = {v: k for k, v in MAPPING_SFC_TO_SF.items() if k != "fecha_creación"}

    # ======================================================================
    # MÉTODOS DE SOPORTE Y LIMPIEZA TÉCNICA
    # ======================================================================

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Elimina acentos, tildes y deja el texto en minúsculas para comparaciones estables."""
        if not text:
            return ""
        normalized = "".join(c for c in unicodedata.normalize('NFD', str(text)) if unicodedata.category(c) != 'Mn')
        return normalized.lower().strip()

    @staticmethod
    def _strip_html(text: str) -> str:
        """Sanitiza descripciones del CRM para limpiarlas de etiquetas HTML antes del envío[cite: 1]."""
        if not text:
            return ""
        return re.sub(r'<[^>]*>', '', str(text)).strip()

    @classmethod
    def _get_sf_field_value(cls, entity: Any, field_name: str) -> Any:
        """
        Recupera el valor del CRM resolviendo de forma automática las equivalencias y 
        fallbacks de columnas definidas en el Excel (GAPs del CRM)[cite: 1].
        """
        fallbacks = {
            "Smart_Code__c": ["CaseNumber"],
            "SuppliedName": ["FirstName"],
            "id_number__c": ["bank_account_dni__c"],
            "SuppliedEmail": ["ContactEmail"],
            "SuppliedPhone": ["numero_whatsapp__c"],
            "Description": ["Subject"]
        }
        
        # 1. Intentamos leer de un diccionario o del objeto directamente
        if isinstance(entity, dict):
            val = entity.get(field_name)
        else:
            val = getattr(entity, field_name, None)

        if val is not None:
            return val
            
        # 2. Si no hay valor, buscamos en la cadena de fallbacks oficiales[cite: 1]
        for fallback in fallbacks.get(field_name, []):
            if isinstance(entity, dict):
                val = entity.get(fallback)
            else:
                val = getattr(entity, fallback, None)
            if val is not None:
                return val
        return None

    # ======================================================================
    # TRADUCCIÓN DE VALORES (SFC <-> CRM)
    # ======================================================================

    @classmethod
    def _translate_value_to_sf(cls, db_key: str, sfc_value: Any) -> Any:
        if sfc_value is None: 
            return None
        try:
            val_int = int(sfc_value)
        except (ValueError, TypeError):
            val_int = None

        if db_key == "sc_genero__c" and val_int is not None:
            return cls.SEXO_SFC_TO_SF.get(val_int, "No Aplica")
        elif db_key == "canal__c" and val_int is not None:
            return cls.CANAL_SFC_TO_SF.get(val_int, "Internet")
        elif db_key == "Ente_de_control__c" and val_int is not None:
            return cls.ENTE_SFC_TO_SF.get(val_int, "Otros")
        elif db_key == "sc_Condicion_especial__c" and val_int is not None:
            return cls.CONDICION_SFC_TO_SF.get(val_int, "No aplica")
        elif db_key == "tipo_de_persona__c" and val_int is not None:
            return cls.PERSONA_SFC_TO_SF.get(val_int, "Natural")
        elif db_key == "Departamento__c":
            # Traducimos de código SFC a nombre de departamento legible para el CRM[cite: 1]
            return cls.DEPT_DIVIPOLA_INV.get(str(sfc_value).strip(), sfc_value)
        elif db_key == "SC_municipio__c":
            # Traducimos de código SFC a nombre de municipio legible para el CRM[cite: 1]
            return cls.MUNI_DIVIPOLA_INV.get(str(sfc_value).strip(), sfc_value)
        elif db_key in ("smart_anexo_queja__c", "Urgent_Case__c", "sinRespuestaFinal?"):
            return bool(sfc_value)
        elif db_key == "Marcacion__c" and val_int is not None:
            return str(val_int)
            
        return sfc_value

    @classmethod
    def _translate_value_to_sfc(cls, sf_key: str, sf_value: Any) -> Any:
        if sf_value is None: 
            return None
            
        if isinstance(sf_value, bool) and sf_key in ("smart_anexo_queja__c", "Urgent_Case__c", "sinRespuestaFinal?"):
            return 1 if sf_value else 2

        sf_val_str = str(sf_value).strip()
        normalized = cls._normalize_text(sf_val_str)

        if sf_key == "sc_genero__c":
            return cls.SEXO_SF_TO_SFC.get(normalized, 10)
        elif sf_key == "canal__c":
            return cls.CANAL_SF_TO_SFC.get(normalized, 13)
        elif sf_key == "Ente_de_control__c":
            return cls.ENTE_SF_TO_SFC.get(normalized, 99)
        elif sf_key == "sc_Condicion_especial__c":
            return cls.CONDICION_SF_TO_SFC.get(normalized, 98)
        elif sf_key == "tipo_de_persona__c":
            return cls.PERSONA_SF_TO_SFC.get(normalized, 1)
        elif sf_key == "Departamento__c":
            # Traduce texto (ej. "Bogotá") a su respectivo código DIVIPOLA (SFC)[cite: 1]
            return cls.DEPT_DIVIPOLA.get(normalized, sf_value)
        elif sf_key == "SC_municipio__c":
            # Traduce texto (ej. "Bogotá D.C.") a su respectivo código DIVIPOLA (SFC)[cite: 1]
            return cls.MUNI_DIVIPOLA.get(normalized, sf_value)
        elif sf_key == "Description":
            # Limpieza HTML obligatoria para el texto_queja y recorte a 4,500 caracteres[cite: 1]
            return cls._strip_html(sf_val_str)[:4500].strip()
        elif sf_key in ("SuppliedPhone", "numero_whatsapp__c"):
            # Limpia y restringe teléfonos a un máximo de 15 caracteres[cite: 1]
            clean_phone = re.sub(r'[^\d+]', '', sf_val_str)
            return clean_phone[:15]
        elif sf_key in ("id_number__c", "bank_account_dni__c"):
            # Limpia y restringe documentos ID a un máximo de 15 caracteres alfanuméricos[cite: 1]
            clean_id = re.sub(r'[^a-zA-Z0-9]', '', sf_val_str)
            return clean_id[:15]
        elif sf_key == "SuppliedEmail":
            return sf_val_str[:50]
        elif sf_key == "Marcacion__c":
            try:
                return int(sf_value)
            except (ValueError, TypeError):
                return None

        return sf_value

    # ======================================================================
    # MÉTODOS DE CONVERSION PRINCIPALES
    # ======================================================================

    @classmethod
    def sfc_payload_to_db_dict(cls, sfc_data: Dict[str, Any]) -> Dict[str, Any]:
        """Convierte los payloads recibidos desde la SFC en un diccionario limpio para la DB."""
        db_data = {}
        for key, value in sfc_data.items():
            if key in cls.MAPPING_SFC_TO_SF:
                db_key = cls.MAPPING_SFC_TO_SF[key]
                
                # Procesamos campos de tipo fecha hacia el CRM con máxima estabilidad
                if db_key in ("CreatedDate", "ClosedDate", "LastModifiedDate", "person_birthdate__c") and isinstance(value, str):
                    try:
                        cleaned_val = value.replace(" ", "T").strip()
                        if "T" in cleaned_val:
                            parsed_dt = datetime.fromisoformat(cleaned_val)
                            # person_birthdate__c maneja únicamente tipo date[cite: 1]
                            db_data[db_key] = parsed_dt.date() if db_key == "person_birthdate__c" else parsed_dt
                        else:
                            parsed_d = datetime.strptime(cleaned_val, "%Y-%m-%d").date()
                            db_data[db_key] = parsed_d if db_key == "person_birthdate__c" else datetime.combine(parsed_d, datetime.min.time())
                    except ValueError:
                        db_data[db_key] = None
                else:
                    db_data[db_key] = cls._translate_value_to_sf(db_key, value)
            else:
                if key in ("tipo_entidad", "entidad_cod", "status_smart"):
                    db_data[key] = value
        return db_data

    @classmethod
    def db_entity_to_sfc_payload(cls, entity: Any) -> Dict[str, Any]:
        """Convierte los registros del CRM en un payload estructurado apto para ser enviado a la SFC."""
        sfc_data = {}
        for sf_field, sfc_field in cls.MAPPING_SF_TO_SFC.items():
            # Buscamos el valor utilizando fallbacks automáticos para evadir inconsistencias[cite: 1]
            value = cls._get_sf_field_value(entity, sf_field)
            
            if isinstance(value, (datetime, date)):
                # Formateo de acuerdo con las especificaciones de la SFC[cite: 1]
                if sf_field == "CreatedDate":
                    sfc_data[sfc_field] = value.strftime("%Y-%m-%dT%H:%M:%S")
                else:
                    sfc_data[sfc_field] = value.strftime("%Y-%m-%d")
            else:
                sfc_data[sfc_field] = cls._translate_value_to_sfc(sf_field, value)

        for metadata in ("tipo_entidad", "entidad_cod"):
            val = getattr(entity, metadata, None)
            if val is not None: 
                sfc_data[metadata] = val
        return sfc_data

    @classmethod
    def create_entity_from_sfc(cls, sfc_data: Dict[str, Any], status_smart: str) -> Queja:
        db_dict = cls.sfc_payload_to_db_dict(sfc_data)
        db_dict["status_smart"] = status_smart
        return Queja(**db_dict)