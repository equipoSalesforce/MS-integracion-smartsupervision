from datetime import datetime
from typing import Dict, Any
from app.models.quejas import Queja

class SfcSalesforceMapper:
    # Diccionario de Equivalencia: Clave SFC -> Clave Salesforce
    MAPPING_SFC_TO_SF = {
        "codigo_queja": "Smart_Code__c",
        "fecha_creacion": "CreatedDate",
        "fecha_creación": "CreatedDate",  # Resiliencia al typo con acento del API de la SFC
        "nombres": "SuppliedName",
        "tipo_id_CF": "id_type__c",
        "numero_id_CF": "id_number__c",
        "telefono": "SuppliedPhone",
        "correo": "SuppliedEmail",
        "sexo": "sex__c",
        "codigo_pais": "Country__c",
        "departamento_cod": "Departamento__c",
        "municipio_cod": "Ciudad__c",
        "canal_cod": "Origin",
        "producto_cod": "Product__c",
        "producto_nombre": "Product_Name__c",
        "macro_motivo_cod": "Categorias_COL__c",
        "texto_queja": "Description",
        "anexo_queja": "archivo_adjunto__c",
        "tutela": "Urgent_Case__c",
        "ente_control": "Ente_de_control__c",
        "escalamiento_DCF": "Escalamiento_DCF__c",
        "replica": "Replica__c",
        "argumento_replica": "Argumento_Replica__c",
        "desistimiento_queja": "Desistimiento__c",
        "queja_expres": "Quejas_express__c",
        "insta_recepcion": "Instancia_de_recepcion__c"
    }

    # Campos planos de Momento 3 que no requieren alias específicos
    DIRECT_FIELDS = {
        "estado_cod", "producto_digital", "a_favor_de", "aceptacion_queja",
        "rectificacion_queja", "prorroga_queja", "admision", "documentacion_rta_final",
        "marcacion", "tipo_fraude", "modalidad_fraude", "monto_reclamado", "monto_reconocido"
    }

    # Diccionario invertido para serializar de Salesforce hacia la SFC (omitiendo duplicados con acento)
    MAPPING_SF_TO_SFC = {v: k for k, v in MAPPING_SFC_TO_SF.items() if k != "fecha_creación"}

    @classmethod
    def sfc_payload_to_db_dict(cls, sfc_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convierte un JSON de la SFC en un diccionario compatible con las columnas de Salesforce.
        """
        db_data = {}
        for key, value in sfc_data.items():
            if key in cls.MAPPING_SFC_TO_SF:
                db_key = cls.MAPPING_SFC_TO_SF[key]
                
                # Conversión segura y unificada de strings de fecha a DateTime
                if db_key in ("CreatedDate", "fecha_actualizacion", "fecha_cierre") and isinstance(value, str):
                    try:
                        db_data[db_key] = datetime.fromisoformat(value.replace(" ", "T"))
                    except ValueError:
                        db_data[db_key] = None
                else:
                    db_data[db_key] = value
            elif key in cls.DIRECT_FIELDS:
                db_data[key] = value
            else:
                # Conservar metadatos locales auxiliares de control
                if key in ("tipo_entidad", "entidad_cod", "status_smart"):
                    db_data[key] = value

        return db_data

    @classmethod
    def db_entity_to_sfc_payload(cls, entity: Queja) -> Dict[str, Any]:
        """
        Toma una entidad local Queja de Salesforce y reconstruye el JSON que la SFC espera.
        """
        sfc_data = {}
        
        for sf_field, sfc_field in cls.MAPPING_SF_TO_SFC.items():
            value = getattr(entity, sf_field, None)
            
            # Serializamos marcas de tiempo de vuelta a formato de texto ISO compatible con SFC
            if isinstance(value, datetime):
                sfc_data[sfc_field] = value.isoformat()
            else:
                sfc_data[sfc_field] = value

        # Sumamos los atributos directos
        for field in cls.DIRECT_FIELDS:
            value = getattr(entity, field, None)
            if value is not None:
                sfc_data[field] = value

        # Sumamos la metadata de control
        for metadata in ("tipo_entidad", "entidad_cod"):
            val = getattr(entity, metadata, None)
            if val is not None:
                sfc_data[metadata] = val

        return sfc_data

    @classmethod
    def create_entity_from_sfc(cls, sfc_data: Dict[str, Any], status_smart: str) -> Queja:
        """
        Factoría para instanciar directamente un modelo Queja mapeado para persistir en la DB.
        """
        db_dict = cls.sfc_payload_to_db_dict(sfc_data)
        db_dict["status_smart"] = status_smart
        return Queja(**db_dict)