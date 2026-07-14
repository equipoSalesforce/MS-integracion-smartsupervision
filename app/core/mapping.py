import logging
from datetime import datetime
from typing import Dict, Any, Optional
from app.models.quejas import Queja

logger = logging.getLogger(__name__)

class SfcSalesforceMapper:
    
    # ======================================================================
    # TABLAS DE HOMOLOGACIÓN DE VALORES (SFC Código <-> Salesforce Texto)
    # ======================================================================
    
    # 1. Sexo (tabla-sexo.xlsx)
    SEXO_SFC_TO_SF = {
        1: "Femenino",
        2: "Masculino",
        3: "Eliminado",
        4: "No binario",
        10: "No aplica"
    }
    SEXO_SF_TO_SFC = {v.lower(): k for k, v in SEXO_SFC_TO_SF.items()}

    # 2. Canal (tabla-canal.xlsx)
    CANAL_SFC_TO_SF = {
        1: "Aplicaciones móviles",
        2: "Cajeros automáticos administrados",
        3: "Cajeros automáticos no propios",
        4: "Cajeros automáticos propios",
        5: "Centro de atención telefónica (Call center/Contac center)",
        6: "Asistente virtual",
        7: "Corresponsales digitales propios",
        8: "Corresponsales digitales tercerizados",
        9: "Corresponsales físicos propios",
        10: "Corresponsales físicos tercerizados",
        11: "Corresponsales móviles propios",
        12: "Corresponsales móviles tercerizados",
        13: "Internet",
        14: "Oficinas",
        15: "POS administrados",
        16: "POS no propios",
        17: "POS propios",
        18: "Sistema de acceso remoto para clientes (RAS)",
        19: "Sistema de Audio Respuesta (IVR)"
    }
    CANAL_SF_TO_SFC = {v.lower(): k for k, v in CANAL_SFC_TO_SF.items()}

    # 3. Ente de Control (tabla-ente-de-control.xlsx)
    ENTE_SFC_TO_SF = {
        1: "Procuraduría",
        2: "Contraloría",
        3: "Defensoría del pueblo",
        4: "Personerías",
        99: "Otros"
    }
    ENTE_SF_TO_SFC = {v.lower(): k for k, v in ENTE_SFC_TO_SF.items()}

    # 4. Condición Especial (tabla-condición-especial)
    CONDICION_SFC_TO_SF = {
        1: "Adulto mayor",
        2: "Pensionado",
        3: "Receptor de subsidio",
        4: "Discapacidad auditiva",
        5: "Discapacidad física",
        6: "Menor de edad",
        7: "Indígena",
        8: "Mujer embarazada",
        9: "Reinsertado",
        10: "Víctima del conflicto armado",
        11: "Afrocolombiano",
        12: "Desplazado",
        13: "Madre cabeza de familia",
        14: "Sordomudo",
        15: "Discapacidad cognitiva",
        16: "Discapacidad visual",
        17: "Periodista",
        90: "Otra",
        98: "No aplica"
    }
    CONDICION_SF_TO_SFC = {v.lower(): k for k, v in CONDICION_SFC_TO_SF.items()}

    # 5. Tipo de Persona (Tabla Tipo de persona)
    PERSONA_SFC_TO_SF = {
        1: "Natural",
        2: "Jurídica",
        3: "Comunitario"
    }
    PERSONA_SF_TO_SFC = {v.lower(): k for k, v in PERSONA_SFC_TO_SF.items()}

    # ======================================================================
    # MAPEO DE ATRIBUTOS (CAMPOS)
    # ======================================================================
    MAPPING_SFC_TO_SF = {
        "codigo_queja": "Smart_Code__c",
        "fecha_creacion": "CreatedDate",
        "fecha_creación": "CreatedDate",  # Resiliencia al typo de acento de la SFC
        "nombres": "SuppliedName",
        "tipo_id_CF": "id_type__c",
        "numero_id_CF": "id_number__c",
        "telefono": "SuppliedPhone",
        "correo": "SuppliedEmail",
        "sexo": "sex__c",                 # Homologado
        "codigo_pais": "Country__c",
        "departamento_cod": "Departamento__c",
        "municipio_cod": "Ciudad__c",
        "canal_cod": "Origin",            # Homologado
        "producto_cod": "Product__c",
        "producto_nombre": "Product_Name__c",
        "macro_motivo_cod": "Categorias_COL__c",
        "texto_queja": "Description",
        "anexo_queja": "archivo_adjunto__c",
        "tutela": "Urgent_Case__c",
        "ente_control": "Ente_de_control__c",  # Homologado
        "escalamiento_DCF": "Escalamiento_DCF__c",
        "replica": "Replica__c",
        "argumento_replica": "Argumento_Replica__c",
        "desistimiento_queja": "Desistimiento__c",
        "queja_expres": "Quejas_express__c",
        "insta_recepcion": "Instancia_de_recepcion__c",
        "condicion_especial": "condicion_especial",  # Homologado
        "tipo_persona": "tipo_persona"               # Homologado
    }

    DIRECT_FIELDS = {
        "estado_cod", "producto_digital", "a_favor_de", "aceptacion_queja",
        "rectificacion_queja", "prorroga_queja", "admision", "documentacion_rta_final",
        "marcacion", "tipo_fraude", "modalidad_fraude", "monto_reclamado", "monto_reconocido"
    }

    # Mapa invertido para enviar de DB local hacia la SFC
    MAPPING_SF_TO_SFC = {v: k for k, v in MAPPING_SFC_TO_SF.items() if k != "fecha_creación"}

    # ======================================================================
    # MÉTODOS DE TRADUCCIÓN DE VALORES (LÓGICA BIDIRECCIONAL)
    # ======================================================================
    
    @classmethod
    def _translate_value_to_sf(cls, db_key: str, sfc_value: Any) -> Any:
        """
        Traduce el código numérico recibido de la SFC a su valor de texto de Salesforce.
        """
        if sfc_value is None:
            return None

        try:
            val_int = int(sfc_value)
        except (ValueError, TypeError):
            return sfc_value  # Si no es convertible a entero, se preserva tal cual

        if db_key == "sex__c":
            return cls.SEXO_SFC_TO_SF.get(val_int, "No aplica")
        elif db_key == "Origin":
            return cls.CANAL_SFC_TO_SF.get(val_int, "Internet")
        elif db_key == "Ente_de_control__c":
            return cls.ENTE_SFC_TO_SF.get(val_int, "Otros")
        elif db_key == "condicion_especial":
            return cls.CONDICION_SFC_TO_SF.get(val_int, "No aplica")
        elif db_key == "tipo_persona":
            return cls.PERSONA_SFC_TO_SF.get(val_int, "Natural")
            
        return sfc_value

    @classmethod
    def _translate_value_to_sfc(cls, sf_key: str, sf_value: Any) -> Any:
        """
        Traduce el valor textual almacenado en Salesforce a su código numérico para la SFC.
        """
        if sf_value is None:
            return None

        sf_val_str = str(sf_value).strip().lower()

        if sf_key == "sex__c":
            return cls.SEXO_SF_TO_SFC.get(sf_val_str, 10)  # Default: No aplica (10)
        elif sf_key == "Origin":
            return cls.CANAL_SF_TO_SFC.get(sf_val_str, 13)   # Default: Internet (13)
        elif sf_key == "Ente_de_control__c":
            return cls.ENTE_SF_TO_SFC.get(sf_val_str, 99)    # Default: Otros (99)
        elif sf_key == "condicion_especial":
            return cls.CONDICION_SF_TO_SFC.get(sf_val_str, 98) # Default: No aplica (98)
        elif sf_key == "tipo_persona":
            return cls.PERSONA_SF_TO_SFC.get(sf_val_str, 1)    # Default: Natural (1)

        return sf_value

    # ======================================================================
    # TRANSFORMACIONES ESTRUCTURALES
    # ======================================================================

    @classmethod
    def sfc_payload_to_db_dict(cls, sfc_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convierte un JSON de la SFC en un diccionario mapeado con claves de Salesforce 
        y con sus valores numéricos traducidos a etiquetas de texto de negocio[cite: 3, 4].
        """
        db_data = {}
        for key, value in sfc_data.items():
            if key in cls.MAPPING_SFC_TO_SF:
                db_key = cls.MAPPING_SFC_TO_SF[key]
                
                # 1. Tratamiento de Fechas
                if db_key in ("CreatedDate", "fecha_actualizacion", "fecha_cierre") and isinstance(value, str):
                    try:
                        db_data[db_key] = datetime.fromisoformat(value.replace(" ", "T"))
                    except ValueError:
                        db_data[db_key] = None
                else:
                    # 2. Traducción de Valores de negocio
                    db_data[db_key] = cls._translate_value_to_sf(db_key, value)
            elif key in cls.DIRECT_FIELDS:
                db_data[key] = value
            else:
                if key in ("tipo_entidad", "entidad_cod", "status_smart"):
                    db_data[key] = value

        return db_data

    @classmethod
    def db_entity_to_sfc_payload(cls, entity: Queja) -> Dict[str, Any]:
        """
        Toma un registro Queja (Salesforce) de la base de datos local y construye 
        el payload con nombres de campos y códigos de valores que la SFC requiere[cite: 3, 4].
        """
        sfc_data = {}
        
        for sf_field, sfc_field in cls.MAPPING_SF_TO_SFC.items():
            value = getattr(entity, sf_field, None)
            
            # 1. Tratamiento de Fechas de vuelta a string ISO
            if isinstance(value, datetime):
                sfc_data[sfc_field] = value.isoformat()
            else:
                # 2. Traducción de Valores textuales de vuelta a Códigos numéricos
                sfc_data[sfc_field] = cls._translate_value_to_sfc(sf_field, value)

        # Copia de atributos de campos directos
        for field in cls.DIRECT_FIELDS:
            value = getattr(entity, field, None)
            if value is not None:
                sfc_data[field] = value

        # Copia de metadatos de control
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