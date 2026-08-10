import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, date
from typing import Dict, Any, Optional, Set, List
from zoneinfo import ZoneInfo
import httpx

from app.core.config import settings
from app.schemas.sfc_payloads import SfcNuevaQuejaPayload, SfcActualizarQuejaPayload

logger = logging.getLogger(__name__)


class SfcSalesforceMapper:
    HTML_REGEX = re.compile(r'<[^>]*>')
    CLEAN_PHONE_DOC_REGEX = re.compile(r'[^\d+]')

    # 🎯 CACHÉ EN MEMORIA RAM (TTL: 10 Minutos)
    CATALOGOS: Dict[str, Dict[str, str]] = {}
    INVERSE_CATALOGS: Dict[str, Dict[str, Any]] = {}
    
    # Mapeos dinámicos de nombres de campos
    MAPPING_MOMENTO_1_SFC_TO_CRM: Dict[str, str] = {}
    MAPPING_MOMENTO_4_SFC_TO_CRM: Dict[str, str] = {}

    ULTIMA_ACTUALIZACION: float = 0
    CACHE_TTL_SEGUNDOS: int = 600

    DEPT_DIVIPOLA_INV: Dict[str, str] = {}  
    MUNI_DIVIPOLA_INV: Dict[str, str] = {}  
    DEPT_DIVIPOLA: Dict[str, str] = {}      
    MUNI_DIVIPOLA: Dict[str, str] = {}      
    
    PRODUCTO_SFC_TEXTO_TO_SF = {
        "wallet": "Wallet", "exchange": "Exchange", "transactions": "Transactions",
        "p2p": "P2P", "tarjeta digital": "Tarjeta Digital", "tarjeta fisica": "Tarjeta Fisica",
        "cuenta perfil": "Cuenta perfil", "otro": "Otro"
    }

    # Respaldo por defecto para Mapeos de Campos
    DEFAULT_MAPPING_M1 = {
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

    DEFAULT_MAPPING_M4 = {
        "numero_id_CF": "id_number__c", "tipo_id_CF": "SC_id_type__c",
        "nombre": "FirstName", "nombres": "FirstName", "Nombres": "FirstName",
        "apellido": "LastName", "apellidos": "LastName", "Apellidos": "LastName",
        "fecha_nacimiento": "fecha_nacimiento__c", "correo": "SuppliedEmail",
        "Correo": "SuppliedEmail", "telefono": "SuppliedPhone",
        "Teléfono": "SuppliedPhone", "Telefono": "SuppliedPhone",
        "razon_social": "company_name__c", "direccion": "direccion__c",
        "Dirección": "direccion__c", "Direccion": "direccion__c",
        "departamento_cod": "Departamento__c", "municipio_cod": "SC_municipio__c",
    }

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Remueve tildes, signos de puntuación y convierte a minúsculas limpias."""
        if not text:
            return ""
        normalized = "".join(c for c in unicodedata.normalize('NFD', str(text)) if unicodedata.category(c) != 'Mn')
        normalized = re.sub(r'[^\w\s]', '', normalized)
        return normalized.lower().strip()

    @classmethod
    async def _obtener_google_access_token(cls) -> Optional[str]:
        """Obtiene token de acceso vía Google OAuth 2.0."""
        client_id = getattr(settings, "GOOGLE_CLIENT_ID", None)
        client_secret = getattr(settings, "GOOGLE_CLIENT_SECRET", None)
        refresh_token = getattr(settings, "GOOGLE_REFRESH_TOKEN", None)

        if not all([client_id, client_secret, refresh_token]):
            return None

        url_oauth = "https://oauth2.googleapis.com/token"
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post(url_oauth, data=payload)
                if res.status_code == 200:
                    return res.json().get("access_token")
        except Exception as e:
            logger.warning(f"⚠️ [SfcSalesforceMapper] Error solicitando Access Token a Google: {e}")

        return None

    @classmethod
    async def obtener_catalogos_y_mapeos(cls) -> None:
        """
        Sincroniza Catálogos y Mapeos consultando las pestañas individuales de Google Sheets en un solo lote (batchGet).
        """
        ahora = time.time()
        if cls.CATALOGOS and cls.ULTIMA_ACTUALIZACION > 0 and (ahora - cls.ULTIMA_ACTUALIZACION) < cls.CACHE_TTL_SEGUNDOS:
            return

        spreadsheet_id = getattr(settings, "GOOGLE_CATALOGS_SPREADSHEET_ID", None)

        if spreadsheet_id:
            try:
                logger.info("🔄 [SfcSalesforceMapper] Sincronizando pestañas de Google Sheets...")
                access_token = await cls._obtener_google_access_token()

                if access_token:
                    headers = {"Authorization": f"Bearer {access_token}"}
                    
                    # 1. Obtener lista de títulos de todas las pestañas existentes en el libro
                    url_meta = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}?fields=sheets.properties.title"
                    
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        res_meta = await client.get(url_meta, headers=headers)
                        
                        if res_meta.status_code == 200:
                            sheet_titles = [
                                s["properties"]["title"] 
                                for s in res_meta.json().get("sheets", []) 
                                if "properties" in s and "title" in s["properties"]
                            ]

                            # 2. Consultar todas las pestañas en una única llamada batchGet
                            params = [("ranges", f"'{title}'!A:C") for title in sheet_titles]
                            url_batch = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchGet"
                            
                            res_batch = await client.get(url_batch, headers=headers, params=params)
                            if res_batch.status_code == 200:
                                value_ranges = res_batch.json().get("valueRanges", [])
                                
                                nuevos_catalogos: Dict[str, Dict[str, str]] = {}
                                m1_map, m4_map = {}, {}

                                for vr in value_ranges:
                                    range_str = vr.get("range", "")
                                    tab_title = range_str.split("!")[0].replace("'", "").strip()
                                    rows = vr.get("values", [])[1:] # Omitir fila de encabezados

                                    # Caso Pestaña de Mapeo de Nombres de Campos
                                    if tab_title.lower() == "mapeo_campos":
                                        for row in rows:
                                            if len(row) >= 3 and row[0] and row[1] and row[2]:
                                                momento = str(row[0]).strip().upper()
                                                campo_sfc = str(row[1]).strip()
                                                campo_crm = str(row[2]).strip()
                                                
                                                if "MOMENTO_1" in momento or "M1" in momento:
                                                    m1_map[campo_sfc] = campo_crm
                                                elif "MOMENTO_4" in momento or "M4" in momento:
                                                    m4_map[campo_sfc] = campo_crm
                                    else:
                                        # Caso Pestaña de Catálogo por Valor (ej: genero, tipo_id, canal, etc.)
                                        cat_key = tab_title.lower()
                                        cat_dict = {}
                                        for row in rows:
                                            if len(row) >= 2 and row[0] and row[1]:
                                                code = str(row[0]).strip()
                                                val = str(row[1]).strip()
                                                if cat_key == "producto":
                                                    logger.info(f"El codigo de producto es: {code} y el valor en el crm es: {val}")
                                                cat_dict[code] = val
                                                
                                        
                                        if cat_dict:
                                            nuevos_catalogos[cat_key] = cat_dict

                                if nuevos_catalogos:
                                    cls.CATALOGOS = nuevos_catalogos
                                    cls._construir_indices_inversos()
                                    cls.MAPPING_MOMENTO_1_SFC_TO_CRM = m1_map or cls.DEFAULT_MAPPING_M1
                                    cls.MAPPING_MOMENTO_4_SFC_TO_CRM = m4_map or cls.DEFAULT_MAPPING_M4
                                    cls.ULTIMA_ACTUALIZACION = ahora
                                    logger.info(
                                        f"✅ [SfcSalesforceMapper] {len(nuevos_catalogos)} catálogos y mapeos "
                                        f"cargados desde pestañas de Google Sheets."
                                    )
                                    return
            except Exception as e:
                logger.warning(f"⚠️ [SfcSalesforceMapper] Falló sincronización por pestañas: {e}. Cargando respaldo local.")

        if not cls.CATALOGOS:
            cls.cargar_catalogos_local()

    @classmethod
    def _construir_indices_inversos(cls):
        """Genera diccionarios inversos optimizados para búsquedas CRM -> SFC."""
        if "producto" not in cls.CATALOGOS or not cls.CATALOGOS["producto"]:
            cls.CATALOGOS["producto"] = {
                f"207_{idx}": nombre
                for idx, nombre in enumerate(cls.PRODUCTO_SFC_TEXTO_TO_SF.values(), 1)
            }

        cls.INVERSE_CATALOGS = {}
        for cat_key, cat_dict in cls.CATALOGOS.items():
            cat_inverse = {}
            for k, v in cat_dict.items():
                val_to_store = int(k) if str(k).isdigit() else str(k)
                cat_inverse[cls._normalize_text(v)] = val_to_store
                cat_inverse[str(k)] = val_to_store 
            cls.INVERSE_CATALOGS[cat_key] = cat_inverse
        
        if "tipo_id" in cls.INVERSE_CATALOGS:
            cls.INVERSE_CATALOGS["tipo_id"].update({
                "cc": 1, "ce": 2, "rut": 3, "nit": 3, "dni": 4, "pass": 5, "passport": 5, "pasaporte": 5
            })
        if "punto_recepcion" in cls.INVERSE_CATALOGS:
            cls.INVERSE_CATALOGS["punto_recepcion"].update({
                "activate b2c": 99, "form: change data": 99, "updatecom": 99, "manual": 1, "internet": 2
            })

    @classmethod
    def cargar_catalogos_local(cls, force: bool = False):
        """Carga el respaldo local desde catalogos_sfc_crm.json."""
        if cls.CATALOGOS and cls.INVERSE_CATALOGS and not force:
            return

        ruta = os.path.join(os.path.dirname(__file__), "resources/catalogos_sfc_crm.json")
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                cls.CATALOGOS = json.load(f)
            
            cls._construir_indices_inversos()
            cls.MAPPING_MOMENTO_1_SFC_TO_CRM = cls.DEFAULT_MAPPING_M1
            cls.MAPPING_MOMENTO_4_SFC_TO_CRM = cls.DEFAULT_MAPPING_M4
            cls.cargar_divipola(force=force)
            cls.ULTIMA_ACTUALIZACION = 0

            logger.info("📂 [SfcSalesforceMapper] Respaldo local de catálogos cargado en RAM.")
        except Exception as e:
            logger.error(f"❌ Error al cargar catalogos_sfc_crm.json: {e}")

    @classmethod
    def cargar_divipola(cls, force: bool = False):
        """Carga la codificación DIVIPOLA de departamentos y municipios."""
        if cls.DEPT_DIVIPOLA and cls.MUNI_DIVIPOLA and not force:
            return

        ruta = os.path.join(os.path.dirname(__file__), "resources/divipola_sfc_crm.json")
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                data = json.load(f)

            cls.DEPT_DIVIPOLA_INV = data.get("departamentos", {})
            cls.MUNI_DIVIPOLA_INV = data.get("municipios", {})

            cls.DEPT_DIVIPOLA = {cls._normalize_text(v): k for k, v in cls.DEPT_DIVIPOLA_INV.items()}
            cls.MUNI_DIVIPOLA = {cls._normalize_text(v): k for k, v in cls.MUNI_DIVIPOLA_INV.items()}

            cls.DEPT_DIVIPOLA["bogota"] = "11"
            cls.DEPT_DIVIPOLA["bogota dc"] = "11"
            cls.MUNI_DIVIPOLA["bogota"] = "11001"
            cls.MUNI_DIVIPOLA["bogota dc"] = "11001"
        except Exception as e:
            logger.error(f"❌ Error al cargar divipola_sfc_crm.json: {e}")

    @classmethod
    def get_crm_allowed_values(cls, catalog_key: str) -> Set[str]:
        if not cls.CATALOGOS:
            cls.cargar_catalogos_local()
            
        allowed = set(cls.CATALOGOS.get(catalog_key, {}).values())
        if catalog_key == "tipo_id":
            allowed.update(["NIT", "N.I.T.", "R.U.T."])
        return allowed

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
        if not text:
            return ""
        return cls.HTML_REGEX.sub('', str(text)).strip()

    @classmethod
    def _translate_value_to_crm(cls, sfc_key: str, sfc_value: Any) -> Any:
        if sfc_value is None: 
            return None
        str_key = str(sfc_value).strip()
        
        if sfc_key == "codigo_pais":
            cat_paises = cls.CATALOGOS.get("codigo_pais", {})
            return cat_paises.get(str_key, "Colombia")
        
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
            cat_dict = cls.CATALOGOS.get(cat_key, {})

            if str_key in cat_dict:
                return cat_dict[str_key]

            if cat_key == "macro_motivo" and str_key.isdigit():
                offset_key = str(int(str_key) + 900)
                if offset_key in cat_dict:
                    return cat_dict[offset_key]

            return default_val

        if sfc_key in ("departamento_cod", "Departamento__c"):
            return cls.DEPT_DIVIPOLA_INV.get(str_key, str(sfc_value))
        if sfc_key in ("municipio_cod", "SC_municipio__c"):
            return cls.MUNI_DIVIPOLA_INV.get(str_key, str(sfc_value))

        if sfc_key in ("tutela", "queja_expres", "escalamiento_DCF", "replica", "producto_digital"):
            val_int = int(sfc_value) if str_key.isdigit() else sfc_value
            return "No" if (val_int == 2 or sfc_value is False) else "Si"

        return sfc_value

    @classmethod
    def _translate_value_to_sfc(cls, sf_key: str, sf_value: Any) -> Any:
        if sf_value is None: 
            return None
        
        if sf_key == "codigo_pais__c":
            normalized_country = cls._normalize_text(str(sf_value))
            cat_inverse_pais = cls.INVERSE_CATALOGS.get("codigo_pais", {})
            return str(cat_inverse_pais.get(normalized_country, "170"))

        if sf_key in ("Tutela__c", "Quejas_express__c"):
            v_clean = str(sf_value).lower().strip()
            if v_clean in ("si", "sí", "true", "1"): return 1
            if v_clean in ("no", "false", "2"): return 2
            return 2

        if sf_key == "sinRespuestaFinal__c":
            v_clean = str(sf_value).lower().strip()
            return v_clean in ("si", "sí", "true", "1")

        # 🎯 PRODUCTO SFC: Retorno directo ANTES de evaluar la matriz genérica
        if sf_key == "Product__c":
            return 207

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
            "Favorabilidad__c": ("favorabilidad", None),
            "Desistimiento__c": ("desistimiento", 2),
            "tipo_fraude__c": ("tipo_fraude", 2),
            "Tipo_Fraude__c": ("tipo_fraude", 2),
            "modalidad_fraude__c": ("modalidad_fraude", 90),
            "Modalidad_Fraude__c": ("modalidad_fraude", 90),
            "punto_recepcion": ("punto_recepcion", 1),
            "Categorias_COL__c": ("macro_motivo", 940),
            "Aceptacion__c": ("aceptacion", None),
            "Rectificacion__c": ("rectificacion", 2),
        }

        if sf_key in sf_to_cat:
            cat_key, default_val = sf_to_cat[sf_key]
            return cls.INVERSE_CATALOGS.get(cat_key, {}).get(normalized, default_val)

        if sf_key == "Departamento__c": return cls.DEPT_DIVIPOLA.get(normalized, str(sf_value))
        if sf_key == "SC_municipio__c": return cls.MUNI_DIVIPOLA.get(normalized, str(sf_value))

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
        mapping_m1 = cls.MAPPING_MOMENTO_1_SFC_TO_CRM or cls.DEFAULT_MAPPING_M1

        for sfc_key, value in sfc_data.items():
            if sfc_key in mapping_m1:
                crm_key = mapping_m1[sfc_key]
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
        
        tipo_id_raw = str(sfc_data.get("tipo_id_CF", "")).strip()
        tipo_persona_raw = str(sfc_data.get("tipo_persona", "")).strip()

        if tipo_id_raw == "3":
            crm_data["SC_id_type__c"] = "NIT" if tipo_persona_raw == "2" else "RUT"
        
        return crm_data

    @classmethod
    def sfc_user_payload_to_db_dict(cls, sfc_data: Dict[str, Any]) -> Dict[str, Any]:
        crm_data = {}
        if not isinstance(sfc_data, dict):
            return crm_data

        mapping_m4 = cls.MAPPING_MOMENTO_4_SFC_TO_CRM or cls.DEFAULT_MAPPING_M4

        for sfc_key, value in sfc_data.items():
            if value is None:
                continue

            crm_key = mapping_m4.get(sfc_key) or mapping_m4.get(str(sfc_key).lower())

            if crm_key:
                if sfc_key == "fecha_nacimiento" and isinstance(value, str) and value.strip():
                    try:
                        clean_date = value.replace(" ", "T")
                        crm_data[crm_key] = datetime.fromisoformat(clean_date).isoformat()
                    except ValueError:
                        crm_data[crm_key] = value
                else:
                    crm_data[crm_key] = cls._translate_value_to_crm(sfc_key, value)

        first_name = crm_data.get("FirstName", "")
        last_name = crm_data.get("LastName", "")
        if first_name or last_name:
            crm_data["SuppliedName"] = f"{first_name} {last_name}".strip()

        return crm_data

    @classmethod
    def _safe_int(cls, value: Any, default: Optional[int] = None) -> Optional[int]:
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    @classmethod
    def crm_entity_to_sfc_momento2_payload(cls, entity: Any) -> Dict[str, Any]:
        prefix = f"{settings.SFC_TIPO_ENTIDAD}{settings.SFC_ENTIDAD_COD}"
        raw_code = cls._get_sf_field_value(entity, "Smart_Code__c") or ""
        codigo_queja = f"{prefix}{raw_code}" if raw_code and not str(raw_code).startswith(prefix) else raw_code

        created_raw = str(cls._get_sf_field_value(entity, "CreatedDate") or datetime.now(ZoneInfo("America/Bogota")).isoformat())
        fecha_iso = created_raw

        if "T" in created_raw or "-" in created_raw:
            try:
                clean_date = created_raw.split(".")[0] if "." in created_raw else created_raw
                dt_val = datetime.fromisoformat(clean_date) if "T" in clean_date else datetime.strptime(clean_date, "%Y-%m-%d")
                fecha_iso = dt_val.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                pass

        payload_obj = SfcNuevaQuejaPayload(
            codigo_queja=str(codigo_queja),
            departamento_cod=str(cls._translate_value_to_sfc("Departamento__c", cls._get_sf_field_value(entity, "Departamento__c")) or "11"),
            municipio_cod=str(cls._translate_value_to_sfc("SC_municipio__c", cls._get_sf_field_value(entity, "SC_municipio__c")) or "11001"),
            canal_cod=int(cls._translate_value_to_sfc("canal__c", cls._get_sf_field_value(entity, "canal__c")) or 13),
            producto_cod=int(cls._translate_value_to_sfc("Product__c", cls._get_sf_field_value(entity, "Product__c")) or 207),
            macro_motivo_cod=int(cls._translate_value_to_sfc("Categorias_COL__c", cls._get_sf_field_value(entity, "Categorias_COL__c")) or 940),
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
        codigo_queja = f"{prefix}{raw_code}" if raw_code and not str(raw_code).startswith(prefix) else raw_code

        fecha_act = datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m-%dT%H:%M:%S")

        closed_date_raw = cls._get_sf_field_value(entity, "ClosedDate")
        fecha_cierre_val = None

        if closed_date_raw:
            hora_actual = datetime.now(ZoneInfo("America/Bogota")).strftime("%H:%M:%S")
            if isinstance(closed_date_raw, datetime):
                fecha_cierre_val = closed_date_raw.strftime("%Y-%m-%dT%H:%M:%S")
            elif isinstance(closed_date_raw, date):
                fecha_cierre_val = f"{closed_date_raw.isoformat()}T{hora_actual}"
            elif isinstance(closed_date_raw, str) and closed_date_raw.strip():
                clean_str = closed_date_raw.strip()
                fecha_cierre_val = clean_str if "T" in clean_str else f"{clean_str.split()[0]}T{hora_actual}"
                
        status_val = cls._get_sf_field_value(entity, "Status")
        estado_cod_val = cls._translate_value_to_sfc("Status", status_val) or 2

        if estado_cod_val == 4:
            fav_val = cls._translate_value_to_sfc("Favorabilidad__c", cls._get_sf_field_value(entity, "Favorabilidad__c"))
            acep_val = cls._translate_value_to_sfc("Aceptacion__c", cls._get_sf_field_value(entity, "Aceptacion__c"))
            if fav_val is None or acep_val is None:
                raise ValueError("No es posible construir el payload de Cierre (Estado 4) sin valores válidos en 'Favorabilidad__c' y 'Aceptacion__c'.")

        doc_rta_final = cls._get_sf_field_value(entity, "sinRespuestaFinal__c")

        payload_obj = SfcActualizarQuejaPayload(
            codigo_queja=str(codigo_queja),
            sexo=cls._safe_int(cls._translate_value_to_sfc("sc_genero__c", cls._get_sf_field_value(entity, "sc_genero__c"))),
            lgbtiq=cls._safe_int(cls._translate_value_to_sfc("sc_LGBTIQ__c", cls._get_sf_field_value(entity, "sc_LGBTIQ__c"))),
            condicion_especial=cls._safe_int(cls._translate_value_to_sfc("sc_Condicion_especial__c", cls._get_sf_field_value(entity, "sc_Condicion_especial__c"))),
            canal_cod=cls._safe_int(cls._translate_value_to_sfc("canal__c", cls._get_sf_field_value(entity, "canal__c")), default=13),
            producto_cod=cls._safe_int(cls._translate_value_to_sfc("Product__c", cls._get_sf_field_value(entity, "Product__c")), default=207),
            macro_motivo_cod=cls._safe_int(cls._translate_value_to_sfc("Categorias_COL__c", cls._get_sf_field_value(entity, "Categorias_COL__c")), default=940),
            estado_cod=cls._safe_int(estado_cod_val, default=2),
            fecha_actualizacion=fecha_act,
            producto_digital=cls._safe_int(cls._translate_value_to_sfc("producto_digital__c", cls._get_sf_field_value(entity, "producto_digital__c"))),
            admision=cls._safe_int(cls._translate_value_to_sfc("admision_col__c", cls._get_sf_field_value(entity, "admision_col__c"))),
            desistimiento_queja=2,
            anexo_queja=bool(cls._get_sf_field_value(entity, "smart_anexo_queja__c") or False),
            tutela=cls._safe_int(cls._translate_value_to_sfc("Tutela__c", cls._get_sf_field_value(entity, "Tutela__c"))),
            ente_control=cls._safe_int(cls._translate_value_to_sfc("Ente_de_control__c", cls._get_sf_field_value(entity, "Ente_de_control__c"))),
            queja_expres=cls._safe_int(cls._translate_value_to_sfc("Quejas_express__c", cls._get_sf_field_value(entity, "Quejas_express__c")), 2),
            a_favor_de=cls._safe_int(cls._translate_value_to_sfc("Favorabilidad__c", cls._get_sf_field_value(entity, "Favorabilidad__c"))),
            aceptacion_queja=cls._safe_int(cls._translate_value_to_sfc("Aceptacion__c", cls._get_sf_field_value(entity, "Aceptacion__c"))),
            rectificacion_queja=cls._safe_int(cls._translate_value_to_sfc("Rectificacion__c", cls._get_sf_field_value(entity, "Rectificacion__c"))),
            prorroga_queja=cls._safe_int(cls._translate_value_to_sfc("Prorroga__c", cls._get_sf_field_value(entity, "Prorroga__c"))),
            documentacion_rta_final=bool(doc_rta_final) if doc_rta_final is not None else False,
            fecha_cierre=fecha_cierre_val,
            marcacion=cls._safe_int(cls._translate_value_to_sfc("marcacion__c", cls._get_sf_field_value(entity, "marcacion__c"))),
            tipo_fraude=cls._safe_int(cls._translate_value_to_sfc("tipo_fraude__c", cls._get_sf_field_value(entity, "tipo_fraude__c"))),
            modalidad_fraude=cls._safe_int(cls._translate_value_to_sfc("modalidad_fraude__c", cls._get_sf_field_value(entity, "modalidad_fraude__c"))),
            monto_reclamado=cls._get_sf_field_value(entity, "card_amount__c"),
            monto_reconocido=cls._get_sf_field_value(entity, "Total_Devuelto_por_Desconocimiento__c")
        )

        return payload_obj.model_dump()

    @classmethod
    def crm_entity_to_sfc_payload(cls, entity: Any, momento: int = 2) -> Dict[str, Any]:
        if momento == 3:
            return cls.crm_entity_to_sfc_momento3_payload(entity)
        return cls.crm_entity_to_sfc_momento2_payload(entity)


# ⚡ Cargar respaldo local inicial al importar el módulo
SfcSalesforceMapper.cargar_catalogos_local()

# Aliases de compatibilidad
sfc_payload_to_db_dict = SfcSalesforceMapper.sfc_payload_to_db_dict
sfc_user_payload_to_db_dict = SfcSalesforceMapper.sfc_user_payload_to_db_dict
crm_entity_to_sfc_payload = SfcSalesforceMapper.crm_entity_to_sfc_payload
crm_entity_to_sfc_momento2_payload = SfcSalesforceMapper.crm_entity_to_sfc_momento2_payload
crm_entity_to_sfc_momento3_payload = SfcSalesforceMapper.crm_entity_to_sfc_momento3_payload