import logging
import re
import unicodedata
from datetime import datetime, date
from typing import Dict, Any, Optional
from app.core.config import settings

logger = logging.getLogger(__name__)


class SfcSalesforceMapper:

    # ======================================================================
    # ⚡ EXPRESIONES REGULARES PRECOMPILADAS (OPTIMIZACIÓN DE PERFORMANCE)
    # ======================================================================
    HTML_REGEX = re.compile(r'<[^>]*>')
    CLEAN_PHONE_DOC_REGEX = re.compile(r'[^\d+]')

    # ======================================================================
    # ⚙️ DICCIONARIOS BASE CANÓNICOS (SFC -> CRM)
    # ======================================================================
    GENERO_SFC_TO_SF = {1: "Femenino", 2: "Masculino", 3: "Trans", 4: "No binario", 10: "No Aplica"}
    
    ID_TYPE_SFC_TO_SF = {
        1: "CC", 2: "CE", 3: "RUT", 4: "DNI", 5: "PASS", 
        6: "Carné diplomático", 7: "Sociedad extranjera sin NIT", 
        8: "PEP", 9: "NUIP", 10: "PPT"
    }

    PERSONA_SFC_TO_SF = {1: "B2C", 2: "B2B"}
    LGBTIQ_SFC_TO_SF = {1: "Si", 2: "No"}

    CONDICION_SFC_TO_SF = {
        1: "Adulto mayor", 2: "Pensionado", 3: "Receptor de subsidio", 4: "Discapacidad auditiva",
        5: "Discapacidad física", 6: "Menor de edad", 7: "Indígena", 8: "Mujer embarazada",
        9: "Reinsertado", 10: "Víctima del conflicto armado", 11: "Afrocolombiano", 12: "Desplazado",
        13: "Madre cabeza de familia", 14: "Sordomudo", 15: "Discapacidad cognitiva", 16: "Discapacidad visual",
        17: "Periodista", 90: "Otra", 98: "No aplica"
    }

    ORIGIN_SFC_TO_SF = {
        1: "Aplicaciones móviles", 2: "Cajeros automáticos administrados", 3: "Cajeros automáticos no propios",
        4: "Cajeros automáticos propios", 5: "Centro de atención telefónica (Call center/Contac center)",
        6: "Asistente virtual", 7: "Corresponsales digitales propios", 8: "Corresponsales digitales tercerizados",
        9: "Corresponsales físicos propios", 10: "Corresponsales físicos tercerizados",
        11: "Corresponsales móviles propios", 12: "Corresponsales móviles tercerizados",
        13: "Internet", 14: "Oficinas", 15: "POS administrados", 16: "POS no propios",
        17: "POS propios", 18: "Sistema de acceso remoto para clientes (RAS)", 19: "Sistema de Audio Respuesta (IVR)"
    }

    ENTE_SFC_TO_SF = {1: "Procuraduría", 2: "Contraloría", 3: "Defensoría del pueblo", 4: "Personerías", 99: "Otros"}
    INSTANCIA_SFC_TO_SF = {1: "Superintendencia Financiera de Colombia", 2: "Entidad vigilada", 3: "Defensor del consumidor financiero", 9: "Otra (remisión por competencia)"}
    ADMISION_SFC_TO_SF = {1: "Queja o reclamo inadmitida y/o rechazada por el DCF", 2: "Queja o reclamo admitida por el DCF", 9: "No Aplica"}
    FAVOR_SFC_TO_SF = {1: "Favorable", 2: "Parcialmente favorable", 3: "No favorable"}
    ACEPTACION_SFC_TO_SF = {1: "Respuesta final a favor del consumidor financiero aceptadas por la entidad", 2: "Respuesta final a favor del consumidor financiero no aceptadas por la entidad"}
    
    RECTIFICACION_SFC_TO_SF = {
        1: "Queja o reclamo rectificada por la entidad vigilada antes de la decisión del DCF",
        2: "Queja o reclamo no rectificada por la entidad vigilada antes de la decisión del DCF",
        3: "Queja o reclamo rectificada por la entidad vigilada después de la decisión del DCF",
        4: "Queja o reclamo no rectificada por la entidad vigilada después de la decisión del DCF"
    }

    DESISTIMIENTO_SFC_TO_SF = {1: "Queja o reclamo desistida por el CF", 2: "Queja o reclamo no desistida por el CF"}
    TIPO_FRAUDE_SFC_TO_SF = {1: "Interno", 2: "Externo"}

    MODALIDAD_FRAUDE_SFC_TO_SF = {
        1: "Suplantación de identidad", 2: "Sim Swapping", 3: "Vulneración de cuenta o producto",
        4: "Phishing", 5: "Vishing", 6: "Smishing", 7: "Pharming", 8: "Enumeración",
        9: "Malware", 10: "Skimming", 11: "Ataque BIN", 12: "Fraude amigable", 13: "Cambiazo",
        14: "Falsificación", 15: "Pérdida de elementos (como Tarjetas, chequeras, tokens)",
        16: "Suplantación de elementos suministrados por la entidad (como QRs de pagos, corresponsales, datafonos)",
        17: "Estafa", 18: "Errores operativos de la entidad que hayan propiciado o facilitado el fraude",
        19: "Otras técnicas de ingeniería social", 90: "Otra"
    }

    PUNTO_RECEPCION_SFC_TO_SF = {1: "Web", 2: "WhatsApp", 3: "Email", 4: "Manual", 99: "Activate B2C"}

    MACRO_MOTIVO_SFC_TO_SF = {
        901: "Publicidad engañosa", 902: "Dificultad en el acceso a la información", 903: "Información o asesoría incompleta y/o errada",
        904: "Información inoportuna", 905: "Dificultad en la comunicación con la entidad", 906: "Mal trato por parte de un funcionario",
        907: "Mal trato por parte del asesor comercial o proveedor", 908: "Presunta actuación fraudulenta o no ética del personal",
        909: "Incumplimiento de los términos del contrato", 910: "Eliminada", 911: "Cotización errada",
        912: "Demora o no entrega de la cotización y/o simulación", 913: "Demora o no entrega del contrato o de la póliza",
        914: "Error o falta de claridad en las cláusulas del contrato o de la póliza", 915: "Diferencia del producto expedido con el solicitado o cotizado o simulado",
        916: "Vinculación no autorizada", 917: "Condicionamiento a la adquisición de productos o servicios", 918: "No cancelación o terminación de los productos",
        919: "Fallas en débito automático", 920: "No entrega de paz y salvo", 921: "Demora o no devolución de saldos, aportes o primas",
        922: "Presuntos timbres, sellos, adhesivos o billetes y/o monedas falsos", 923: "Negación injustificada a la apertura del producto",
        924: "Negación a la apertura de productos por condiciones de segmentos particulares de la población", 925: "No recepción de billetes y/o monedas",
        926: "No disponibilidad o fallas de los canales de atención", 927: "Obstáculo para la interposición de quejas, reclamos o peticiones",
        928: "Demora en la respuesta a quejas, reclamos o peticiones", 929: "Errores en la resolución de quejas, reclamos o peticiones.",
        930: "No resolución a quejas, peticiones y reclamos", 931: "Reporte injustificado a centrales de riesgo",
        932: "No levantamiento de reporte negativo a centrales de riesgo", 933: "Demora o no modificación de datos personales",
        934: "Actualización equivocada de datos personales", 935: "Inadecuado tratamiento de datos personales",
        936: "Información incompleta y/o errada en la ejecución", 937: "No aplicación de los protocolos especiales de atención",
        938: "Inconsistencias en los pagos a terceros", 939: "Transacción mal aplicada", 940: "Transacción no reconocida",
        941: "Cobro por transacciones en internet", 942: "Demora o no aplicación del pago", 943: "Error en la aplicación del pago",
        944: "Inconformidad por cobros de terceros", 945: "Dificultad o imposibilidad para realizar transacciones o consulta de información por el canal",
        946: "Demora en la atención o en el servicio requerido", 947: "Seguridad en canales",
        948: "Omisión o envío tardío o inoportuno de informes, extractos o reportes a los que esté obligada la entidad.",
        949: "Errores en el contenido de la información en informes, extractos o reportes.", 950: "Limitación en la expedición de certificaciones",
        951: "Inconformidad en procesos - Constitución, Modificación y Levantamiento - de garantía", 952: "Producto terminado o cancelado sin justificación",
        953: "Inconformidad por bloqueo de productos", 954: "Incrementos de tarifas no pactadas o informadas", 955: "Error en la facturación o cobro no pactado",
        956: "Modificación de condiciones en contratos", 957: "Inconsistencia en el cobro de comisiones - Descuentos injustificados",
        958: "Inconsistencia en el cobro de gastos", 959: "Inconsistencia en el cálculo y/o aplicación de impuestos",
        960: "Inoportunidad en la aplicación o cobro de comisiones o gastos bancarios", 961: "Inconsistencias en el movimiento y saldo total del producto",
        962: "Inconformidad con procesos internos de conocimiento del cliente y SARLAFT", 963: "Fallas o inoportunidad en el proceso de vinculación",
        964: "Información sujeta a reserva", 965: "Indebido deber de asesoría", 966: "Fallas en operaciones en moneda extranjera",
        967: "Diferencias en monetización", 968: "Distribución de portafolio", 969: "Remesas"
    }

    # DIVIPOLA Geográfica
    DEPT_DIVIPOLA_INV = {"11": "Bogotá D.C.", "05": "Antioquia", "08": "Atlántico"}
    MUNI_DIVIPOLA_INV = {"11001": "Bogotá D.C.", "05001": "Medellín", "08001": "Barranquilla"}

    PRODUCTO_SFC_TEXTO_TO_SF = {
        "wallet": "Wallet", "exchange": "Exchange", "transactions": "Transactions",
        "p2p": "P2P", "tarjeta digital": "Tarjeta Digital", "tarjeta fisica": "Tarjeta Fisica",
        "cuenta perfil": "Cuenta perfil", "otro": "Otro"
    }

    # ======================================================================
    # ⚙️ MÉTODOS DE NORMALIZACIÓN Y AUTO-INVERSIÓN DINÁMICA
    # ======================================================================
    @staticmethod
    def _normalize_text(text: str) -> str:
        if not text:
            return ""
        normalized = "".join(c for c in unicodedata.normalize('NFD', str(text)) if unicodedata.category(c) != 'Mn')
        return normalized.lower().strip()

    @classmethod
    def _invert_dict(cls, source_dict: Dict[int, str]) -> Dict[str, int]:
        """Invierte automáticamente un diccionario SFC -> CRM usando normalización sin tildes."""
        return {cls._normalize_text(v): k for k, v in source_dict.items()}

    # ======================================================================
    # 🔄 AUTO-INVERSIÓN AUTOMÁTICA DE CATÁLOGOS (CRM -> SFC)
    # ======================================================================
    @classmethod
    def _init_reverse_catalogs(cls):
        """Genera automáticamente todas las versiones inversas SF -> SFC en memoria."""
        cls.GENERO_SF_TO_SFC = cls._invert_dict(cls.GENERO_SFC_TO_SF)
        cls.PERSONA_SF_TO_SFC = cls._invert_dict(cls.PERSONA_SFC_TO_SF)
        cls.LGBTIQ_SF_TO_SFC = cls._invert_dict(cls.LGBTIQ_SFC_TO_SF)
        cls.CONDICION_SF_TO_SFC = cls._invert_dict(cls.CONDICION_SFC_TO_SF)
        cls.ORIGIN_SF_TO_SFC = cls._invert_dict(cls.ORIGIN_SFC_TO_SF)
        cls.ENTE_SF_TO_SFC = cls._invert_dict(cls.ENTE_SFC_TO_SF)
        cls.INSTANCIA_SF_TO_SFC = cls._invert_dict(cls.INSTANCIA_SFC_TO_SF)
        cls.ADMISION_SF_TO_SFC = cls._invert_dict(cls.ADMISION_SFC_TO_SF)
        cls.FAVOR_SF_TO_SFC = cls._invert_dict(cls.FAVOR_SFC_TO_SF)
        cls.ACEPTACION_SF_TO_SFC = cls._invert_dict(cls.ACEPTACION_SFC_TO_SF)
        cls.RECTIFICACION_SF_TO_SFC = cls._invert_dict(cls.RECTIFICACION_SFC_TO_SF)
        cls.DESISTIMIENTO_SF_TO_SFC = cls._invert_dict(cls.DESISTIMIENTO_SFC_TO_SF)
        cls.TIPO_FRAUDE_SF_TO_SFC = cls._invert_dict(cls.TIPO_FRAUDE_SFC_TO_SF)
        cls.MODALIDAD_FRAUDE_SF_TO_SFC = cls._invert_dict(cls.MODALIDAD_FRAUDE_SFC_TO_SF)
        cls.MACRO_MOTIVO_SF_TO_SFC = cls._invert_dict(cls.MACRO_MOTIVO_SFC_TO_SF)

        # Inversiones con alias/excepciones personalizadas
        cls.ID_TYPE_SF_TO_SFC = cls._invert_dict(cls.ID_TYPE_SFC_TO_SF)
        cls.ID_TYPE_SF_TO_SFC.update({"cc": 1, "ce": 2, "rut": 3, "nit": 3, "dni": 4, "pass": 5})

        cls.PUNTO_RECEPCION_SF_TO_SFC = cls._invert_dict(cls.PUNTO_RECEPCION_SFC_TO_SF)
        cls.PUNTO_RECEPCION_SF_TO_SFC.update({"activate b2c": 99, "form: change data": 99, "updatecom": 99})

        cls.DEPT_DIVIPOLA = {"bogota d.c.": "11", "bogota": "11", "antioquia": "05", "atlantico": "08"}
        cls.MUNI_DIVIPOLA = {"bogota d.c.": "11001", "bogota": "11001", "medellin": "05001", "barranquilla": "08001"}

# Inicialización al cargar el módulo
SfcSalesforceMapper._init_reverse_catalogs()


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
    if not text:
        return ""
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
    if sfc_value is None:
        return None
    val_int = int(sfc_value) if str(sfc_value).isdigit() else sfc_value
    str_val = str(sfc_value)

    # Mapeo usando Dispatcher de Catálogos Directos
    catalog_map = {
        "sexo": (cls.GENERO_SFC_TO_SF, "No Aplica"),
        "tipo_id_CF": (cls.ID_TYPE_SFC_TO_SF, "Cedula de ciudadanía"),
        "tipo_persona": (cls.PERSONA_SFC_TO_SF, "B2C"),
            "lgbtiq": (cls.LGBTIQ_SFC_TO_SF, "No"),
            "sc_LGBTIQ__c": (cls.LGBTIQ_SFC_TO_SF, "No"),
            "condicion_especial": (cls.CONDICION_SFC_TO_SF, "No aplica"),
            "canal_cod": (cls.ORIGIN_SFC_TO_SF, "Internet"),
            "ente_control": (cls.ENTE_SFC_TO_SF, "Otros"),
            "insta_recepcion": (cls.INSTANCIA_SFC_TO_SF, "Entidad vigilada"),
            "admision": (cls.ADMISION_SFC_TO_SF, "No Aplica"),
            "a_favor_de": (cls.FAVOR_SFC_TO_SF, "No favorable"),
            "aceptacion_queja": (cls.ACEPTACION_SFC_TO_SF, "Respuesta final a favor del consumidor financiero no aceptadas por la entidad"),
            "rectificacion_queja": (cls.RECTIFICACION_SFC_TO_SF, "Queja o reclamo no rectificada por la entidad vigilada antes de la decisión del DCF"),
            "desistimiento_queja": (cls.DESISTIMIENTO_SFC_TO_SF, "Queja o reclamo no desistida por el CF"),
            "tipo_fraude": (cls.TIPO_FRAUDE_SFC_TO_SF, "Externo"),
            "modalidad_fraude": (cls.MODALIDAD_FRAUDE_SFC_TO_SF, "Otra"),
            "punto_recepcion": (cls.PUNTO_RECEPCION_SFC_TO_SF, "Manual"),
            "departamento_cod": (cls.DEPT_DIVIPOLA_INV, sfc_value),
            "municipio_cod": (cls.MUNI_DIVIPOLA_INV, sfc_value),
            "macro_motivo_cod": (cls.MACRO_MOTIVO_SFC_TO_SF, "Transacción no reconocida"),
            "Categorias_COL__c": (cls.MACRO_MOTIVO_SFC_TO_SF, "Transacción no reconocida"),
        }

    if sfc_key in catalog_map:
        source_dict, default_val = catalog_map[sfc_key]
        # Búsqueda tolerante: prueba primero por entero y si no coincide, prueba por string
        if val_int in source_dict:
            return source_dict[val_int]
        if str_val in source_dict:
            return source_dict[str_val]
        return default_val

    if sfc_key in ("tutela", "queja_expres", "escalamiento_DCF", "replica", "producto_digital", "prorroga_queja"):
        return "No" if (val_int == 2 or sfc_value is False) else "Si"

    return sfc_value        


@classmethod
def _translate_value_to_sfc(cls, sf_key: str, sf_value: Any) -> Any:
    """[CRM ➔ SFC] Convierte las strings/picklists de Salesforce a códigos numéricos SFC."""
    if sf_value is None:
        return None

    # Normalización para picklists dicotómicas (Si/No)
    if sf_key in ("sinRespuestaFinal__c", "Aceptacion__c", "Prorroga__c", "Rectificacion__c", "Tutela__c", "Quejas_express__c"):
        v_clean = str(sf_value).lower().strip()
        if v_clean in ("si", "sí", "true", "1"):
            return 1
        if v_clean in ("no", "false", "2"):
            return 2
        return 1 if bool(sf_value) else 2

    normalized = cls._normalize_text(str(sf_value))

    # Tabla Dispatcher para conversión SF -> SFC
    catalog_map = {
        "sc_genero__c": (cls.GENERO_SF_TO_SFC, 10),
        "SC_id_type__c": (cls.ID_TYPE_SF_TO_SFC, 1),
        "tipo_de_persona__c": (cls.PERSONA_SF_TO_SFC, 1),
        "sc_LGBTIQ__c": (cls.LGBTIQ_SF_TO_SFC, 2),
        "sc_Condicion_especial__c": (cls.CONDICION_SF_TO_SFC, 98),
        "canal__c": (cls.ORIGIN_SF_TO_SFC, 13),
        "Ente_de_control__c": (cls.ENTE_SF_TO_SFC, 99),
        "Instancia_de_recepcion__c": (cls.INSTANCIA_SF_TO_SFC, 2),
        "admision_col__c": (cls.ADMISION_SF_TO_SFC, 9),
        "Favorabilidad__c": (cls.FAVOR_SF_TO_SFC, 3),
        "Desistimiento__c": (cls.DESISTIMIENTO_SF_TO_SFC, 2),
        "Tipo_Fraude__c": (cls.TIPO_FRAUDE_SF_TO_SFC, 2),
        "Modalidad_Fraude__c": (cls.MODALIDAD_FRAUDE_SF_TO_SFC, 90),
        "punto_recepcion": (cls.PUNTO_RECEPCION_SF_TO_SFC, 4),
        "Departamento__c": (cls.DEPT_DIVIPOLA, sf_value),
        "SC_municipio__c": (cls.MUNI_DIVIPOLA, sf_value),
        "Categorias_COL__c": (cls.MACRO_MOTIVO_SF_TO_SFC, 940),
    }

    if sf_key in catalog_map:
        source_dict, default_val = catalog_map[sf_key]
        return source_dict.get(normalized, default_val)

    if sf_key == "Product__c":
        return 207  # Regla comodín: Todos los productos digitales mapean a 207 (Cuenta perfil)

    if sf_key == "Status":
        status_map = {"new": 1, "nuevo": 1, "in progress": 2, "en progreso": 2, "stand by": 2, "espera": 2, "closed": 4, "cerrado": 4, "resolved": 4}
        return status_map.get(normalized, 2)

    if sf_key == "Producto_digital__c":
        return 1 if normalized in ("si", "sí", "true") else 2

    if sf_key == "Description":
        return cls._strip_html(str(sf_value))[:4500].strip()

    if sf_key in ("id_number__c", "SuppliedPhone"):
        return cls.CLEAN_PHONE_DOC_REGEX.sub('', str(sf_value))[:15]

    return sf_value


@classmethod
def sfc_payload_to_db_dict(cls, sfc_data: Dict[str, Any]) -> Dict[str, Any]:
    """[MOMENTO 1] (SFC ➔ CRM) Transforma el JSON de la SFC a la estructura del CRM."""
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


@classmethod
def crm_entity_to_sfc_payload(cls, entity: Any) -> Dict[str, Any]:
    """[MOMENTOS 2 Y 3] (CRM ➔ SFC) Calcula el payload canónico para la SFC."""
    sfc_data = {
        "codigo_pais": "COL",
        "punto_recepcion": 1,
        "tipo_entidad": settings.SFC_TIPO_ENTIDAD,
        "entidad_cod": settings.SFC_ENTIDAD_COD
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
            except ValueError:
                sfc_data[sfc_field] = value
        else:
            sfc_data[sfc_field] = cls._translate_value_to_sfc(sf_field, value)

    if "tipo_Persona" in sfc_data:
        sfc_data["tipo_persona"] = sfc_data["tipo_Persona"]

    return sfc_data


# Asociamos las funciones estáticas a la clase para exponer la API pública esperada por el orquestador
SfcSalesforceMapper._strip_html = _strip_html
SfcSalesforceMapper._get_sf_field_value = _get_sf_field_value
SfcSalesforceMapper._translate_value_to_crm = _translate_value_to_crm
SfcSalesforceMapper._translate_value_to_sfc = _translate_value_to_sfc
SfcSalesforceMapper.sfc_payload_to_db_dict = sfc_payload_to_db_dict
SfcSalesforceMapper.crm_entity_to_sfc_payload = crm_entity_to_sfc_payload