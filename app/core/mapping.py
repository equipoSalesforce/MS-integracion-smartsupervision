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
    # ⚙️ DICCIONARIOS DE EQUIVALENCIAS EXACTAS (SEGÚN MATRIZ CRM ↔ SFC)
    # ======================================================================
    
    # sc_genero__c (Account)
    GENERO_SFC_TO_SF = {1: "Femenino", 2: "Masculino", 3: "Trans", 4: "No binario", 10: "No Aplica"}
    GENERO_SF_TO_SFC = {v.lower(): k for k, v in GENERO_SFC_TO_SF.items()}

    # SC_id_type__c (Account)
    # SC_id_type__c (Account)
    ID_TYPE_SFC_TO_SF = {
        1: "CC", 
        2: "CE", 
        3: "RUT", 
        4: "DNI",
        5: "PASS", 
        6: "Carné diplomático", 
        7: "Sociedad extranjera sin NIT", 
        8: "PEP",
        9: "NUIP", 
        10: "PPT"
    }
    
    ID_TYPE_SF_TO_SFC = {
        "cc": 1, 
        "ce": 2, 
        "rut": 3, 
        "nit": 3,
        "dni": 4,
        "pass": 5, 
        "carne diplomatico": 6, 
        "sociedad extranjera sin nit": 7, 
        "pep": 8,
        "nuip": 9, 
        "ppt": 10
    }

    # tipo_de_persona__c (Case)
    PERSONA_SFC_TO_SF = {1: "B2C", 2: "B2B"}
    PERSONA_SF_TO_SFC = {v.lower(): k for k, v in PERSONA_SFC_TO_SF.items()}

    # sc_LGBTIQ__c (Case)
    LGBTIQ_SFC_TO_SF = {1: "Si", 2: "No"}
    LGBTIQ_SF_TO_SFC = {v.lower(): k for k, v in LGBTIQ_SFC_TO_SF.items()}

    # sc_Condicion_especial__c (Case)
    CONDICION_SFC_TO_SF = {
        1: "Adulto mayor", 2: "Pensionado", 3: "Receptor de subsidio", 4: "Discapacidad auditiva",
        5: "Discapacidad física", 6: "Menor de edad", 7: "Indígena", 8: "Mujer embarazada",
        9: "Reinsertado", 10: "Víctima del conflicto armado", 11: "Afrocolombiano", 12: "Desplazado",
        13: "Madre cabeza de familia", 14: "Sordomudo", 15: "Discapacidad cognitiva", 16: "Discapacidad visual",
        17: "Periodista", 90: "Otra", 98: "No aplica"
    }
    CONDICION_SF_TO_SFC = {
        "adulto mayor": 1, "pensionado": 2, "receptor de subsidio": 3, "discapacidad auditiva": 4,
        "discapacidad fisica": 5, "menor de edad": 6, "indigena": 7, "mujer embarazada": 8,
        "reinsertado": 9, "victima del conflicto armado": 10, "afrocolombiano": 11, "desplazado": 12,
        "madre cabeza de familia": 13, "sordomudo": 14, "discapacidad cognitiva": 15, "discapacidad visual": 16,
        "periodista": 17, "otra": 90, "no aplica": 98
    }

    # canal__c / canal_cod (Case)
    ORIGIN_SFC_TO_SF = {
        1: "Aplicaciones móviles", 2: "Cajeros automáticos administrados", 3: "Cajeros automáticos no propios",
        4: "Cajeros automáticos propios", 5: "Centro de atención telefónica (Call center/Contac center)",
        6: "Asistente virtual", 7: "Corresponsales digitales propios", 8: "Corresponsales digitales tercerizados",
        9: "Corresponsales físicos propios", 10: "Corresponsales físicos tercerizados",
        11: "Corresponsales móviles propios", 12: "Corresponsales móviles tercerizados",
        13: "Internet", 14: "Oficinas", 15: "POS administrados", 16: "POS no propios",
        17: "POS propios", 18: "Sistema de acceso remoto para clientes (RAS)", 19: "Sistema de Audio Respuesta (IVR)"
    }
    ORIGIN_SF_TO_SFC = {
        "aplicaciones moviles": 1, "cajeros automaticos administrados": 2, "cajeros automaticos no propios": 3,
        "cajeros automaticos propios": 4, "centro de atencion telefonica (call center/contac center)": 5,
        "asistente virtual": 6, "corresponsales digitales propios": 7, "corresponsales digitales tercerizados": 8,
        "corresponsales fisicos propios": 9, "corresponsales fisicos tercerizados": 10,
        "corresponsales moviles propios": 11, "corresponsales moviles tercerizados": 12,
        "internet": 13, "oficinas": 14, "pos administrados": 15, "pos no propios": 16,
        "pos propios": 17, "sistema de acceso remoto para clientes (ras)": 18, "sistema de audio respuesta (ivr)": 19
    }

    # Ente_de_control__c (Case)
    ENTE_SFC_TO_SF = {1: "Procuraduría", 2: "Contraloría", 3: "Defensoría del pueblo", 4: "Personerías", 99: "Otros"}
    ENTE_SF_TO_SFC = {v.lower(): k for k, v in ENTE_SFC_TO_SF.items()}

    # Instancia_de_recepcion__c (Case)
    INSTANCIA_SFC_TO_SF = {1: "Superintendencia Financiera de Colombia", 2: "Entidad vigilada", 3: "Defensor del consumidor financiero", 9: "Otra (remisión por competencia)"}
    INSTANCIA_SF_TO_SFC = {v.lower(): k for k, v in INSTANCIA_SFC_TO_SF.items()}

    # admision_col__c (Case)
    ADMISION_SFC_TO_SF = {1: "Queja o reclamo inadmitida y/o rechazada por el DCF", 2: "Queja o reclamo admitida por el DCF", 9: "No Aplica"}
    ADMISION_SF_TO_SFC = {v.lower(): k for k, v in ADMISION_SFC_TO_SF.items()}

    # Favorabilidad__c / a_favor_de (Case)
    FAVOR_SFC_TO_SF = {1: "Favorable", 2: "Parcialmente favrable", 3: "No favorable"}
    FAVOR_SF_TO_SFC = {v.lower(): k for k, v in FAVOR_SFC_TO_SF.items()}

    # Aceptacion__c / aceptacion_queja (Case)
    ACEPTACION_SFC_TO_SF = {1: "Respuesta final a favor del consumidor financiero aceptadas por la entidad", 2: "Respuesta final a favor del consumidor financiero no aceptadas por la entidad"}
    ACEPTACION_SF_TO_SFC = {v.lower(): k for k, v in ACEPTACION_SFC_TO_SF.items()}

    # Rectificacion__c / rectificacion_queja (Case)
    RECTIFICACION_SFC_TO_SF = {
        1: "Queja o reclamo rectificada por la entidad vigilada antes de la decisión del DCF",
        2: "Queja o reclamo no rectificada por la entidad vigilada antes de la decisión del DCF",
        3: "Queja o reclamo rectificada por la entidad vigilada después de la decisión del DCF",
        4: "Queja o reclamo no rectificada por la entidad vigilada después de la decisión del DCF"
    }
    RECTIFICACION_SF_TO_SFC = {v.lower(): k for k, v in RECTIFICACION_SFC_TO_SF.items()}

    # Desistimiento__c / desistimiento_queja (Case)
    DESISTIMIENTO_SFC_TO_SF = {1: "Queja o reclamo desistida por el CF", 2: "Queja o reclamo no desistida por el CF"}
    DESISTIMIENTO_SF_TO_SFC = {v.lower(): k for k, v in DESISTIMIENTO_SFC_TO_SF.items()}

    # Tipo_Fraude__c (Case)
    TIPO_FRAUDE_SFC_TO_SF = {1: "Interno", 2: "Externo"}
    TIPO_FRAUDE_SF_TO_SFC = {v.lower(): k for k, v in TIPO_FRAUDE_SFC_TO_SF.items()}

    # Modalidad_Fraude__c (Case)
    MODALIDAD_FRAUDE_SFC_TO_SF = {
        1: "Suplantación de identidad", 2: "Sim Swapping", 3: "Vulneración de cuenta o producto",
        4: "Phishing", 5: "Vishing", 6: "Smishing", 7: "Pharming", 8: "Enumeración",
        9: "Malware", 10: "Skimming", 11: "Ataque BIN", 12: "Fraude amigable", 13: "Cambiazo",
        14: "Falsificación", 15: "Pérdida de elementos (como Tarjetas, chequeras, tokens)",
        16: "Suplantación de elementos suministrados por la entidad (como QRs de pagos, corresponsales, datafonos)",
        17: "Estafa", 18: "Errores operativos de la entidad que hayan propiciado o facilitado el fraude",
        19: "Otras técnicas de ingeniería social", 90: "Otra"
    }
    MODALIDAD_FRAUDE_SF_TO_SFC = {
        "suplantacion de identidad": 1, "sim swapping": 2, "vulneracion de cuenta o producto": 3,
        "phishing": 4, "vishing": 5, "smishing": 6, "pharming": 7, "enumeracion": 8,
        "malware": 9, "skimming": 10, "ataque bin": 11, "fraude amigable": 12, "cambiazo": 13,
        "falsificacion": 14, "perdida de elementos (como tarjetas, chequeras, tokens)": 15,
        "suplantacion de elementos suministrados por la entidad (como qrs de pagos, corresponsales, datafonos)": 16,
        "estafa": 17, "errores operativos de la entidad que hayan propiciado o facilitado el fraude": 18,
        "otras tecnicas de ingenieria social": 19, "otra": 90
    }

    # punto_recepcion (Case)
    PUNTO_RECEPCION_SFC_TO_SF = {1: "Web", 2: "WhatsApp", 3: "Email", 4: "Manual", 99: "Activate B2C"}
    PUNTO_RECEPCION_SF_TO_SFC = {
        "web": 1, "whatsapp": 2, "email": 3, "manual": 4, 
        "activate b2c": 99, "form: change data": 99, "updatecom": 99
    }

    #TODO: ampliar a más municipios de ser necesario
    # DIVIPOLA Geográfica Excluyente
    # DIVIPOLA Geográfica Excluyente (Corregido: claves fijas sin tildes)
    DEPT_DIVIPOLA_INV = {"11": "Bogotá D.C.", "05": "Antioquia", "08": "Atlántico"}
    DEPT_DIVIPOLA = {"bogota d.c.": "11", "bogota": "11", "antioquia": "05", "atlantico": "08"}

    MUNI_DIVIPOLA_INV = {"11001": "Bogotá D.C.", "05001": "Medellín", "08001": "Barranquilla"}
    MUNI_DIVIPOLA = {"bogota d.c.": "11001", "bogota": "11001", "medellin": "05001", "barranquilla": "08001"}
    
    # Enrutador inverso para el Momento 1 basado en producto_nombre
    PRODUCTO_SFC_TEXTO_TO_SF = {
        "wallet": "Wallet",
        "exchange": "Exchange",
        "transactions": "Transactions",
        "p2p": "P2P",
        "tarjeta digital": "Tarjeta Digital",
        "tarjeta fisica": "Tarjeta Fisica",
        "cuenta perfil": "Cuenta perfil",
        "otro": "Otro"
    }
    
    # macro_motivo_cod / Categorias_COL__c (Case)
    MACRO_MOTIVO_SFC_TO_SF = {
        901: "Publicidad engañosa",
        902: "Dificultad en el acceso a la información",
        903: "Información o asesoría incompleta y/o errada",
        904: "Información inoportuna",
        905: "Dificultad en la comunicación con la entidad",
        906: "Mal trato por parte de un funcionario",
        907: "Mal trato por parte del asesor comercial o proveedor",
        908: "Presunta actuación fraudulenta o no ética del personal",
        909: "Incumplimiento de los términos del contrato",
        910: "Eliminada",
        911: "Cotización errada",
        912: "Demora o no entrega de la cotización y/o simulación",
        913: "Demora o no entrega del contrato o de la póliza",
        914: "Error o falta de claridad en las cláusulas del contrato o de la póliza",
        915: "Diferencia del producto expedido con el solicitado o cotizado o simulado",
        916: "Vinculación no autorizada",
        917: "Condicionamiento a la adquisición de productos o servicios",
        918: "No cancelación o terminación de los productos",
        919: "Fallas en débito automático",
        920: "No entrega de paz y salvo",
        921: "Demora o no devolución de saldos, aportes o primas",
        922: "Presuntos timbres, sellos, adhesivos o billetes y/o monedas falsos",
        923: "Negación injustificada a la apertura del producto",
        924: "Negación a la apertura de productos por condiciones de segmentos particulares de la población",
        925: "No recepción de billetes y/o monedas",
        926: "No disponibilidad o fallas de los canales de atención",
        927: "Obstáculo para la interposición de quejas, reclamos o peticiones",
        928: "Demora en la respuesta a quejas, reclamos o peticiones",
        929: "Errores en la resolución de quejas, reclamos o peticiones.",
        930: "No resolución a quejas, peticiones y reclamos",
        931: "Reporte injustificado a centrales de riesgo",
        932: "No levantamiento de reporte negativo a centrales de riesgo",
        933: "Demora o no modificación de datos personales",
        934: "Actualización equivocada de datos personales",
        935: "Inadecuado tratamiento de datos personales",
        936: "Información incompleta y/o errada en la ejecución",
        937: "No aplicación de los protocolos especiales de atención",
        938: "Inconsistencias en los pagos a terceros",
        939: "Transacción mal aplicada",
        940: "Transacción no reconocida",
        941: "Cobro por transacciones en internet",
        942: "Demora o no aplicación del pago",
        943: "Error en la aplicación del pago",
        944: "Inconformidad por cobros de terceros",
        945: "Dificultad o imposibilidad para realizar transacciones o consulta de información por el canal",
        946: "Demora en la atención o en el servicio requerido",
        947: "Seguridad en canales",
        948: "Omisión o envío tardío o inoportuno de informes, extractos o reportes a los que esté obligada la entidad.",
        949: "Errores en el contenido de la información en informes, extractos o reportes.",
        950: "Limitación en la expedición de certificaciones",
        951: "Inconformidad en procesos - Constitución, Modificación y Levantamiento - de garantía",
        952: "Producto terminado o cancelado sin justificación",
        953: "Inconformidad por bloqueo de productos",
        954: "Incrementos de tarifas no pactadas o informadas",
        955: "Error en la facturación o cobro no pactado",
        956: "Modificación de condiciones en contratos",
        957: "Inconsistencia en el cobro de comisiones - Descuentos injustificados",
        958: "Inconsistencia en el cobro de gastos",
        959: "Inconsistencia en el cálculo y/o aplicación de impuestos",
        960: "Inoportunidad en la aplicación o cobro de comisiones o gastos bancarios",
        961: "Inconsistencias en el movimiento y saldo total del producto",
        962: "Inconformidad con procesos internos de conocimiento del cliente y SARLAFT",
        963: "Fallas o inoportunidad en el proceso de vinculación",
        964: "Información sujeta a reserva",
        965: "Indebido deber de asesoría",
        966: "Fallas en operaciones en moneda extranjera",
        967: "Diferencias en monetización",
        968: "Distribución de portafolio",
        969: "Remesas"
    }

    MACRO_MOTIVO_SF_TO_SFC = {
        "publicidad enganosa": 901,
        "dificultad en el acceso a la informacion": 902,
        "informacion o asesoria incompleta y/o errada": 903,
        "informacion inoportuna": 904,
        "dificultad en la comunicacion con la entidad": 905,
        "mal trato por parte de un funcionario": 906,
        "mal trato por parte del asesor comercial o proveedor": 907,
        "presunta actuacion fraudulenta o no etica del personal": 908,
        "incumplimiento de los terminos del contrato": 909,
        "eliminada": 910,
        "cotizacion errada": 911,
        "demora o no entrega de la cotizacion y/o simulacion": 912,
        "demora o no entrega del contrato o de la poliza": 913,
        "error o falta de claridad en las clausulas del contrato o de la poliza": 914,
        "diferencia del producto expedido con el solicitado o cotizado o simulado": 915,
        "vinculacion no autorizada": 916,
        "condicionamiento a la adquisicion de productos o servicios": 917,
        "no cancelacion o terminacion de los productos": 918,
        "fallas en debito automatico": 919,
        "no entrega de paz y salvo": 920,
        "demora o no devolucion de saldos, aportes o primas": 921,
        "presuntos timbres, sellos, adhesivos o billetes y/o monedas falsos": 922,
        "negacion injustificada a la apertura del producto": 923,
        "negacion a la apertura de productos por condiciones de segmentos particulares de la poblacion": 924,
        "no recepcion de billetes y/o monedas": 925,
        "no disponibilidad o fallas de los canales de atencion": 926,
        "obstaculo para la interposicion de quejas, reclamos o peticiones": 927,
        "demora en la respuesta a quejas, reclamos o peticiones": 928,
        "errores en la resolucion de quejas, reclamos o peticiones.": 929,
        "no resolucion a quejas, peticiones y reclamos": 930,
        "reporte injustificado a centrales de riesgo": 931,
        "no levantamiento de reporte negativo a centrales de riesgo": 932,
        "demora o no modificacion de datos personales": 933,
        "actualizacion equivocada de datos personales": 934,
        "inadecuado tratamiento de datos personales": 935,
        "informacion incompleta y/o errada en la ejecucion": 936,
        "no aplicacion de los protocolos especiales de atencion": 937,
        "inconsistencias en los pagos a terceros": 938,
        "transaccion mal aplicada": 939,
        "transaccion no reconocida": 940,
        "cobro por transacciones en internet": 941,
        "demora o no aplicacion del pago": 942,
        "error en la aplicacion del pago": 943,
        "inconformidad por cobros de terceros": 944,
        "dificultad o imposibilidad para realizar transacciones o consulta de informacion por el canal": 945,
        "demora en la atencion o en el servicio requerido": 946,
        "seguridad en canales": 947,
        "omision o envio tardio o inoportuno de informes, extractos o reportes a los que este obligada la entidad.": 948,
        "errores en el contenido de la informacion en informes, extractos o reportes.": 949,
        "limitacion en la expedicion de certificaciones": 950,
        "inconformidad en procesos - constitucion, modificacion y levantamiento - de garantia": 951,
        "producto terminado o cancelado sin justificacion": 952,
        "inconformidad por bloqueo de productos": 953,
        "incrementos de tarifas no pactadas o informadas": 954,
        "error en la facturacion o cobro no pactado": 955,
        "modificacion de condiciones en contratos": 956,
        "inconsistencia en el cobro de comisiones - descuentos injustificados": 957,
        "inconsistencia en el cobro de gastos": 958,
        "inconsistencia en el calculo y/o aplicacion de impuestos": 959,
        "inoportunidad en la aplicacion o cobro de comisiones o gastos bancarios": 960,
        "inconsistencias en el movimiento y saldo total del producto": 961,
        "inconformidad con procesos internos de conocimiento del cliente y sarlaft": 962,
        "fallas o inoportunidad en el proceso de vinculacion": 963,
        "informacion sujeta a reserva": 964,
        "indebido deber de asesoria": 965,
        "fallas en operaciones en moneda extranjera": 966,
        "diferencias en monetizacion": 967,
        "distribucion de portafolio": 968,
        "remesas": 969
    }

    # ======================================================================
    # 📑 MATRICES CANÓNICAS DE ENRUTAMIENTO DE ATRIBUTOS
    # ======================================================================
    MAPPING_MOMENTO_1_SFC_TO_CRM = {
        "codigo_queja": "Smart_Code__c",
        "fecha_creacion": "CreatedDate",
        "nombres": "SuppliedName",
        "numero_id_CF": "id_number__c",
        "correo": "SuppliedEmail",
        "telefono": "SuppliedPhone",
        "direccion": "direccion__c",
        "departamento_cod": "Departamento__c",
        "municipio_cod": "SC_municipio__c",
        "texto_queja": "Description",
        "anexo_queja": "smart_anexo_queja__c",
        "tipo_id_CF": "SC_id_type__c",
        "tipo_persona": "tipo_de_persona__c",
        "sexo": "sc_genero__c",
        "lgbtiq": "sc_LGBTIQ__c",
        "canal_cod": "canal__c",
        "condicion_especial": "sc_Condicion_especial__c",
        "producto_cod": "Product__c",
        "macro_motivo_cod": "Categorias_COL__c",
        "tutela": "Tutela__c",
        "ente_control": "Ente_de_control__c",
        "desistimiento_queja": "Desistimiento__c",
        "queja_expres": "Quejas_express__c",
        "codigo_pais": "codigo_pais__c",
        "producto_nombre": "smart_Producto_nombre__c",
        "escalamiento_DCF": "escalamiento_DCF__c",
        "replica": "replica__c",
        "argumento_replica": "argumento_replica__c"
    }

    MAPPING_CRM_TO_SFC_MASTER = {
        "Smart_Code__c": "codigo_queja",
        "CreatedDate": "fecha_creación",
        "SuppliedName": "nombres",
        "id_number__c": "numero_id_CF",
        "SuppliedEmail": "correo",
        "SuppliedPhone": "telefono",
        "direccion__c": "direccion",
        "Departamento__c": "departamento_cod",
        "SC_municipio__c": "municipio_cod",
        "Description": "texto_queja",
        "smart_anexo_queja__c": "anexo_queja",
        "LastModifiedDate": "fecha_actualizacion",
        "sinRespuestaFinal__c": "documentacion_rta_final",
        "ClosedDate": "fecha_cierre",
        "card_amount__c": "monto_reclamado",
        "Total_Devuelto_por_Desconocimiento__c": "monto_reconocido",
        "SC_id_type__c": "tipo_id_CF",
        "tipo_de_persona__c": "tipo_Persona",
        "sc_LGBTIQ__c": "lgbtiq",
        "sc_Condicion_especial__c": "condicion_especial",
        "canal__c": "canal_cod",
        "Product__c": "producto_cod",
        "Categorias_COL__c": "macro_motivo_cod",
        "Ente_de_control__c": "ente_control",
        "Instancia_de_recepcion__c": "insta_recepcion",
        "admision_col__c": "admision",
        "Status": "estado_cod",
        "Producto_digital__c": "producto_digital",
        "Favorabilidad__c": "a_favor_de",
        "Aceptacion__c": "aceptacion_queja",
        "Rectificacion__c": "rectificacion_queja",
        "Desistimiento__c": "desistimiento_queja",
        "Prorroga__c": "prorroga_queja",
        "Tutela__c": "tutela",
        "Quejas_express__c": "queja_expres",
        "Tipo_Fraude__c": "tipo_fraude",
        "Modalidad_Fraude__c": "modalidad_fraude",
        "marcacion__c": "marcacion",
        "punto_recepcion": "punto_recepcion"
    }

    # ======================================================================
    # ⚙️ MÉTODOS DE NORMALIZACIÓN INTERNA
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
            if field_name == "Smart_Code__c" and "Smart_Code__c" not in entity:
                return entity.get("CaseNumber")
            return entity.get(field_name)
        if field_name == "Smart_Code__c" and not hasattr(entity, "Smart_Code__c"):
            return getattr(entity, "CaseNumber", None)
        return getattr(entity, field_name, None)

    # ======================================================================
    # 🔁 FUNCIONES DE TRADUCCIÓN DE VALORES (TRADUCTORES)
    # ======================================================================
    @classmethod
    def _translate_value_to_crm(cls, sfc_key: str, sfc_value: Any) -> Any:
        """[SFC ➔ CRM] Convierte los códigos numéricos de la SFC a picklists legibles."""
        if sfc_value is None: return None
        val_int = int(sfc_value) if str(sfc_value).isdigit() else sfc_value

        if sfc_key == "sexo": return cls.GENERO_SFC_TO_SF.get(val_int, "No Aplica")
        elif sfc_key == "tipo_id_CF": return cls.ID_TYPE_SFC_TO_SF.get(val_int, "Cedula de ciudadanía")
        elif sfc_key == "tipo_persona": return cls.PERSONA_SFC_TO_SF.get(val_int, "B2C")
        elif sfc_key in ("lgbtiq", "sc_LGBTIQ__c"): return cls.LGBTIQ_SFC_TO_SF.get(val_int, "No")
        elif sfc_key == "condicion_especial": return cls.CONDICION_SFC_TO_SF.get(val_int, "No aplica")
        elif sfc_key == "canal_cod": return cls.ORIGIN_SFC_TO_SF.get(val_int, "Internet")
        elif sfc_key == "ente_control": return cls.ENTE_SFC_TO_SF.get(val_int, "Otros")
        elif sfc_key == "insta_recepcion": return cls.INSTANCIA_SFC_TO_SF.get(val_int, "Entidad vigilada")
        elif sfc_key == "admision": return cls.ADMISION_SFC_TO_SF.get(val_int, "No Aplica")
        elif sfc_key == "a_favor_de": return cls.FAVOR_SFC_TO_SF.get(val_int, "No favorable")
        elif sfc_key == "aceptacion_queja": return cls.ACEPTACION_SFC_TO_SF.get(val_int, "Respuesta final a favor del consumidor financiero no aceptadas por la entidad")
        elif sfc_key == "rectificacion_queja": return cls.RECTIFICACION_SFC_TO_SF.get(val_int, "Queja o reclamo no rectificada por la entidad vigilada antes de la decisión del DCF")
        elif sfc_key == "desistimiento_queja": return cls.DESISTIMIENTO_SFC_TO_SF.get(val_int, "Queja o reclamo no desistida por el CF")
        elif sfc_key == "tipo_fraude": return cls.TIPO_FRAUDE_SFC_TO_SF.get(val_int, "Externo")
        elif sfc_key == "modalidad_fraude": return cls.MODALIDAD_FRAUDE_SFC_TO_SF.get(val_int, "Otra")
        elif sfc_key == "punto_recepcion": return cls.PUNTO_RECEPCION_SFC_TO_SF.get(val_int, "Manual")
        elif sfc_key == "departamento_cod": return cls.DEPT_DIVIPOLA_INV.get(str(sfc_value), sfc_value)
        elif sfc_key == "municipio_cod": return cls.MUNI_DIVIPOLA_INV.get(str(sfc_value), sfc_value)
        elif sfc_key == "estado_cod": return val_int # Retorna el número para mapear en Status posterior
        elif sfc_key in ("tutela", "queja_expres", "escalamiento_DCF", "replica", "producto_digital", "prorroga_queja"):
            # Si el catálogo SFC de booleanos usa 1=Si / 2=No, lo transformamos
            if val_int == 2 or sfc_value == False: return "No"
            return "Si"
        elif sfc_key in ("macro_motivo_cod", "Categorias_COL__c"): return cls.MACRO_MOTIVO_SFC_TO_SF.get(val_int, "Transacción no reconocida")
        
        return sfc_value

    @classmethod
    def _translate_value_to_sfc(cls, sf_key: str, sf_value: Any) -> Any:
        """[CRM ➔ SFC] Convierte las strings/picklists de Salesforce a códigos numéricos SFC."""
        if sf_value is None: return None
        
        # Procesamiento tolerante y unificado de picklists dicotómicas del CRM ("Si"/"No") o booleanos
        if sf_key in ("sinRespuestaFinal__c", "Aceptacion__c", "Prorroga__c", "Rectificacion__c", "Tutela__c", "Quejas_express__c"):
            v_clean = str(sf_value).lower().strip()
            if v_clean in ("si", "sí", "true", "1"): return 1
            if v_clean in ("no", "false", "2"): return 2
            return 1 if bool(sf_value) else 2

        normalized = cls._normalize_text(str(sf_value))

        if sf_key == "sc_genero__c": return cls.GENERO_SF_TO_SFC.get(normalized, 10)
        elif sf_key == "SC_id_type__c": return cls.ID_TYPE_SF_TO_SFC.get(normalized, 1)
        elif sf_key == "tipo_de_persona__c": return cls.PERSONA_SF_TO_SFC.get(normalized, 1)
        elif sf_key == "sc_LGBTIQ__c": return cls.LGBTIQ_SF_TO_SFC.get(normalized, 2)
        elif sf_key == "sc_Condicion_especial__c": return cls.CONDICION_SF_TO_SFC.get(normalized, 98)
        elif sf_key == "canal__c": return cls.ORIGIN_SF_TO_SFC.get(normalized, 13)
        elif sf_key == "Ente_de_control__c": return cls.ENTE_SF_TO_SFC.get(normalized, 99)
        elif sf_key == "Instancia_de_recepcion__c": return cls.INSTANCIA_SF_TO_SFC.get(normalized, 2)
        elif sf_key == "admision_col__c": return cls.ADMISION_SF_TO_SFC.get(normalized, 9)
        elif sf_key == "Favorabilidad__c": return cls.FAVOR_SF_TO_SFC.get(normalized, 3)
        elif sf_key == "Desistimiento__c": return cls.DESISTIMIENTO_SF_TO_SFC.get(normalized, 2)
        elif sf_key == "Tipo_Fraude__c": return cls.TIPO_FRAUDE_SF_TO_SFC.get(normalized, 2)
        elif sf_key == "Modalidad_Fraude__c": return cls.MODALIDAD_FRAUDE_SF_TO_SFC.get(normalized, 90)
        elif sf_key == "punto_recepcion": return cls.PUNTO_RECEPCION_SF_TO_SFC.get(normalized, 4)
        elif sf_key == "Departamento__c": return cls.DEPT_DIVIPOLA.get(normalized, sf_value)
        elif sf_key == "SC_municipio__c": return cls.MUNI_DIVIPOLA.get(normalized, sf_value)
        elif sf_key == "Categorias_COL__c": return cls.MACRO_MOTIVO_SF_TO_SFC.get(normalized, 940) # Fallback canónico de transacciones
        # 🎯 REGLA COMODÍN EXCEL: Todos los productos digitales mapean estrictamente a 207 (Cuenta perfil)
        elif sf_key == "Product__c":
            if normalized in ("wallet", "exchange", "transactions", "p2p", "tarjeta digital", "tarjeta fisica", "cuenta perfil", "otro"):
                return 207
            return int(sf_value) if str(sf_value).isdigit() else 207
            
        elif sf_key == "Status":
            # 1: New, 2: In progress, 3: Stand by, 4: Closed
            if normalized in ("new", "nuevo"): return 1
            if normalized in ("in progress", "en progreso"): return 2
            if normalized in ("stand by", "espera"): return 3
            if normalized in ("closed", "cerrado", "resolved"): return 4
            return 2

        elif sf_key == "Producto_digital__c":
            return 1 if normalized in ("si", "sí", "true") else 2

        elif sf_key == "Description": 
            return cls._strip_html(str(sf_value))[:4500].strip()
        elif sf_key in ("id_number__c", "SuppliedPhone"):
            return re.sub(r'[^\d+]', '', str(sf_value))[:15]
            
        return sf_value

    # ======================================================================
    # 🔀 FUNCIONES PÚBLICAS OPERATIVAS (MOTORES DE PIPELINE)
    # ======================================================================
    @classmethod
    def sfc_payload_to_db_dict(cls, sfc_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        [MOMENTO 1] (SFC ➔ CRM)
        Toma el JSON de la SFC y lo transforma a la estructura exacta que espera Salesforce.
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
                elif sfc_key == "producto_cod":
                    sfc_prod_nombre = sfc_data.get("producto_nombre", "")
                    normalized_prod = cls._normalize_text(str(sfc_prod_nombre))
                    
                    # Buscamos la equivalencia exacta en nuestro catálogo de texto
                    crm_data[crm_key] = cls.PRODUCTO_SFC_TEXTO_TO_SF.get(
                        normalized_prod, 
                        "Cuenta perfil"  # Fallback seguro por si viene vacío o no catalogado
                    )
                else:
                    crm_data[crm_key] = cls._translate_value_to_crm(sfc_key, value)
        return crm_data

    @classmethod
    def crm_entity_to_sfc_payload(cls, entity: Any) -> Dict[str, Any]:
        """
        [MOMENTOS 2 Y 3] (CRM ➔ SFC)
        Toma la entidad o JSON de Salesforce y calcula el payload canónico empaquetado para la SFC.
        """
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

        # Inyección tipográfica de seguridad exigida por la SFC para variaciones de payloads
        if "tipo_Persona" in sfc_data:
            sfc_data["tipo_persona"] = sfc_data["tipo_Persona"]

        return sfc_data