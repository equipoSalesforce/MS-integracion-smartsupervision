# app/core/mapping.py
import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, date
from typing import Dict, Any, Optional, Set, Tuple
from zoneinfo import ZoneInfo
import httpx

from app.core.config import settings
from app.core.exceptions import SfcIntegrationException
from app.schemas.sfc_payloads import SfcNuevaQuejaPayload, SfcActualizarQuejaPayload

logger = logging.getLogger(__name__)

# Sentinela para los helpers de traducción campo-por-campo: distingue "esta llave no
# aplica a este catálogo, seguir probando" de "sí aplica y el valor resuelto es None".
_NO_MATCH = object()


class SfcSalesforceMapper:
    HTML_REGEX = re.compile(r'<[^>]*>')
    CLEAN_PHONE_DOC_REGEX = re.compile(r'[^\d+]')

    # 🎯 CACHÉ EN MEMORIA RAM (TTL: 10 Minutos)
    CATALOGOS: Dict[str, Dict[str, str]] = {}
    INVERSE_CATALOGS: Dict[str, Dict[str, Any]] = {}
    
    # Mapeos dinámicos de nombres de campos
    MAPPING_MOMENTO_1_SFC_TO_CRM: Dict[str, str] = {}
    MAPPING_MOMENTO_4_SFC_TO_CRM: Dict[str, str] = {}

    # ULTIMA_ACTUALIZACION marca el último INTENTO de refresh (éxito o fallo) — se usa
    # para el backoff/TTL del fast-path, así un fallo no provoca reintentos inmediatos
    # contra Google Sheets. ULTIMO_EXITO_TIMESTAMP marca el último ÉXITO real — se usa
    # exclusivamente para calcular la antigüedad de la caché y la alerta de 24h
    # (🟢 FIX P1-04: antes ambos propósitos compartían una sola variable, que se
    # reescribía también en cada fallo, y la alerta de stale nunca llegaba a dispararse).
    ULTIMA_ACTUALIZACION: float = 0
    ULTIMO_EXITO_TIMESTAMP: float = 0
    CACHE_TTL_SEGUNDOS: int = 600
    MAX_STALE_TTL_SEGUNDOS: int = 86400  # 🟢 FIX HALLAZGO 49: Umbral máximo de obsolescencia (24 Horas)

    # 🟢 FIX HALLAZGO 48: Candado de refresco Single-Flight para evitar estampidas contra Google Sheets API
    _REFRESH_LOCK: Optional[asyncio.Lock] = None
    
    

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

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._REFRESH_LOCK is None:
            cls._REFRESH_LOCK = asyncio.Lock()
        return cls._REFRESH_LOCK

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Remueve tildes, signos de puntuación y convierte a minúsculas limpias."""
        if not text:
            return ""
        normalized = "".join(c for c in unicodedata.normalize('NFD', str(text)) if unicodedata.category(c) != 'Mn')
        normalized = re.sub(r'[^\w\s]', '', normalized)
        return normalized.lower().strip()

    @classmethod
    async def _obtener_google_access_token(cls, client: Optional[httpx.AsyncClient] = None) -> Optional[str]:
        """Obtiene token de acceso vía Google OAuth 2.0 reutilizando cliente o con timeout extendido."""
        client_id = settings.GOOGLE_CLIENT_ID
        client_secret = settings.GOOGLE_CLIENT_SECRET
        refresh_token = settings.GOOGLE_REFRESH_TOKEN

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
            if client is not None:
                res = await client.post(url_oauth, data=payload)
            else:
                async with httpx.AsyncClient(timeout=8.0) as http_client:
                    res = await http_client.post(url_oauth, data=payload)

            if res.status_code == 200:
                return res.json().get("access_token")
            else:
                logger.warning(f"⚠️ [SfcSalesforceMapper] Google OAuth respondió HTTP {res.status_code}")
        except Exception as e:
            logger.warning(f"⚠️ [SfcSalesforceMapper] Error solicitando Access Token a Google: {e}")

        return None

    @staticmethod
    def _parsear_value_ranges(value_ranges: list) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str], Dict[str, str]]:
        """Parsea la respuesta de batchGet de Sheets en (catalogos, mapeo_momento_1, mapeo_momento_4)."""
        nuevos_catalogos: Dict[str, Dict[str, str]] = {}
        m1_map: Dict[str, str] = {}
        m4_map: Dict[str, str] = {}

        for vr in value_ranges:
            range_str = vr.get("range", "")
            tab_title = range_str.split("!")[0].replace("'", "").strip()
            rows = vr.get("values", [])[1:]

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
                cat_key = tab_title.lower()
                cat_dict = {}
                for row in rows:
                    if len(row) >= 2 and row[0] and row[1]:
                        code = str(row[0]).strip()
                        val = str(row[1]).strip()
                        cat_dict[code] = val

                if cat_dict:
                    nuevos_catalogos[cat_key] = cat_dict

        return nuevos_catalogos, m1_map, m4_map

    @classmethod
    async def _manejar_falla_sincronizacion_sheets(cls, e: Exception, ahora: float) -> None:
        # 🟢 FIX P1-04: la antigüedad se calcula sobre ULTIMO_EXITO_TIMESTAMP (el
        # último éxito real), no sobre ULTIMA_ACTUALIZACION (que se actualiza en
        # cada intento, exitoso o no, para el backoff del fast-path). Antes ambos
        # propósitos compartían la misma variable y la alerta de 24h nunca podía
        # dispararse: cada fallo "rejuvenecía" la antigüedad reportada.
        edad_segundos = (ahora - cls.ULTIMO_EXITO_TIMESTAMP) if cls.ULTIMO_EXITO_TIMESTAMP > 0 else 0
        edad_horas = edad_segundos / 3600.0

        logger.warning(
            f"⚠️ [SfcSalesforceMapper] Falló la sincronización con Google Sheets: {e}. "
            f"Antigüedad de la caché en RAM: {edad_horas:.1f} horas ({int(edad_segundos)}s)."
        )

        if cls.ULTIMO_EXITO_TIMESTAMP > 0 and edad_segundos > cls.MAX_STALE_TTL_SEGUNDOS:
            logger.critical(
                f"🚨 [SfcSalesforceMapper] ALERTA CRÍTICA: La caché de catálogos en RAM tiene {edad_horas:.1f}h "
                f"de antigüedad (supera el umbral máximo de {cls.MAX_STALE_TTL_SEGUNDOS // 3600}h)."
            )
            try:
                from app.services.email_service import EmailAlertService
                asyncio.create_task(
                    EmailAlertService.notificar_catalogo_stale(
                        nombre_componente="SfcSalesforceMapper (Catálogos)",
                        edad_horas=edad_horas,
                        error_msg=str(e)
                    )
                )
            except Exception as alert_err:
                logger.warning(f"No se pudo disparar la alerta por catálogo stale: {alert_err}")

    @classmethod
    async def _refrescar_catalogos_desde_sheets(
        cls, http_client: Optional[httpx.AsyncClient], spreadsheet_id: str, ahora: float
    ) -> bool:
        """Intenta refrescar CATALOGOS/mapeos desde Google Sheets. Si retorna True, ya dejó todo el estado actualizado."""
        try:
            access_token = await cls._obtener_google_access_token(client=http_client)
            if not access_token:
                return False

            headers = {"Authorization": f"Bearer {access_token}"}
            url_meta = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}?fields=sheets.properties.title"

            async def _fetch_sheets(client_to_use: httpx.AsyncClient):
                res_meta = await client_to_use.get(url_meta, headers=headers)
                if res_meta.status_code != 200:
                    return None

                sheet_titles = [
                    s["properties"]["title"]
                    for s in res_meta.json().get("sheets", [])
                    if "properties" in s and "title" in s["properties"]
                ]

                params = [("ranges", f"'{title}'!A:C") for title in sheet_titles]
                url_batch = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchGet"
                return await client_to_use.get(url_batch, headers=headers, params=params)

            if http_client is not None:
                res_batch = await _fetch_sheets(http_client)
            else:
                async with httpx.AsyncClient(timeout=10.0) as local_client:
                    res_batch = await _fetch_sheets(local_client)

            if not res_batch or res_batch.status_code != 200:
                return False

            nuevos_catalogos, m1_map, m4_map = cls._parsear_value_ranges(res_batch.json().get("valueRanges", []))

            if not nuevos_catalogos:
                return False

            cls.CATALOGOS = nuevos_catalogos
            cls._construir_indices_inversos()
            cls.MAPPING_MOMENTO_1_SFC_TO_CRM = m1_map or cls.DEFAULT_MAPPING_M1
            cls.MAPPING_MOMENTO_4_SFC_TO_CRM = m4_map or cls.DEFAULT_MAPPING_M4
            cls.ULTIMA_ACTUALIZACION = ahora
            cls.ULTIMO_EXITO_TIMESTAMP = ahora  # 🟢 FIX P1-04
            logger.info(
                f"✅ [SfcSalesforceMapper] {len(nuevos_catalogos)} catálogos y mapeos "
                f"cargados desde pestañas de Google Sheets."
            )
            return True
        except Exception as e:
            await cls._manejar_falla_sincronizacion_sheets(e, ahora)
            return False

    @classmethod
    async def obtener_catalogos_y_mapeos(cls, http_client: Optional[httpx.AsyncClient] = None) -> None:
        """
        Sincroniza Catálogos y Mapeos consultando las pestañas individuales de Google Sheets en un solo lote (batchGet).
        Implementa el patrón Single-Flight para evitar estampidas de peticiones concurrentes.
        """
        ahora = time.time()
        # 1. Fast Path: Validación de caché fresca en RAM sin bloqueo
        if cls.CATALOGOS and cls.ULTIMA_ACTUALIZACION > 0 and (ahora - cls.ULTIMA_ACTUALIZACION) < cls.CACHE_TTL_SEGUNDOS:
            return

        # 2. Bloqueo Single-Flight: Solo una petición concurrente refresca la caché
        async with cls._get_lock():
            ahora = time.time()
            # Double-check locking: si otra corrutina ya actualizó la caché mientras esperábamos el candado
            if cls.CATALOGOS and cls.ULTIMA_ACTUALIZACION > 0 and (ahora - cls.ULTIMA_ACTUALIZACION) < cls.CACHE_TTL_SEGUNDOS:
                return

            logger.info("🔄 [SfcSalesforceMapper] Intentando conectar con catalogo de mapeo en Google Sheets...")

            spreadsheet_id = settings.GOOGLE_CATALOGS_SPREADSHEET_ID

            if spreadsheet_id:
                if await cls._refrescar_catalogos_desde_sheets(http_client, spreadsheet_id, ahora):
                    return
            else:
                logger.info("ℹ️ [SfcSalesforceMapper] GOOGLE_CATALOGS_SPREADSHEET_ID no está configurado. Usando respaldo local.")

            if not cls.CATALOGOS:
                cls.cargar_catalogos_local()

            # ULTIMA_ACTUALIZACION se actualiza siempre (éxito o fallo): es la marca de
            # "último intento", usada para el backoff del fast-path de arriba. La
            # antigüedad real (ULTIMO_EXITO_TIMESTAMP) sólo se tocó arriba en el punto de
            # éxito genuino.
            cls.ULTIMA_ACTUALIZACION = ahora

    # 🔴 FIX (hallazgo de revisión, 2026-08-26): refrescar_catalogos_job (antes en
    # scheduler.py) sólo corría en el proceso con RUN_SCHEDULER activo -- en
    # producción, únicamente el worker (scripts/render_task_def.py fija
    # RUN_SCHEDULER="False" incondicionalmente para el servicio API, sin importar
    # ninguna variable de entorno). Los catálogos en RAM de cada réplica de la API
    # quedaban congelados en lo que cargó al arrancar, sin refresco nunca más,
    # mientras el worker sí refrescaba cada CACHE_TTL_SEGUNDOS -- exactamente la
    # divergencia que el hallazgo C1 debía cerrar, pero nunca llegó a la API real.
    #
    # Este refresco es deliberadamente INDEPENDIENTE del scheduler/RUN_SCHEDULER y
    # SIN lock distribuido cross-proceso: CATALOGOS es un atributo de clase que vive
    # en la memoria de CADA proceso por separado (no hay un solo "CATALOGOS" global
    # compartido) -- cada proceso (cada réplica de la API, y el worker) necesita
    # refrescar el suyo propio. Un lock de single-flight cross-proceso (como el que
    # sí tiene sentido para reintentar_despachos_pendientes_job, que muta estado
    # REALMENTE compartido en Redis) dejaría a todos los procesos MENOS el que gana
    # el lock sin refrescar nunca -- exactamente el bug que este fix corrige, no algo
    # a repetir aquí. `obtener_catalogos_y_mapeos` ya tiene su propia protección
    # process-local (TTL fast-path + asyncio.Lock single-flight) contra llamadas
    # redundantes DENTRO de un mismo proceso; eso es suficiente.
    _refresh_task: Optional[asyncio.Task] = None

    @classmethod
    async def iniciar_refresco_periodico(cls, http_client: Optional[httpx.AsyncClient] = None) -> None:
        """Arranca el loop de refresco periódico en segundo plano. Llamar una vez por
        proceso, en el arranque (main.py/worker.py), sin condicionarlo a RUN_SCHEDULER."""
        if cls._refresh_task is not None and not cls._refresh_task.done():
            return
        cls._refresh_task = asyncio.create_task(cls._loop_refresco_periodico(http_client))

    @classmethod
    async def _loop_refresco_periodico(cls, http_client: Optional[httpx.AsyncClient] = None) -> None:
        while True:
            await asyncio.sleep(cls.CACHE_TTL_SEGUNDOS)
            try:
                await cls.obtener_catalogos_y_mapeos(http_client=http_client)
            except Exception as e:
                logger.warning(f"⚠️ [SfcSalesforceMapper] Error en el loop de refresco periódico de catálogos: {e}")

    @classmethod
    async def detener_refresco_periodico(cls) -> None:
        if cls._refresh_task is None:
            return
        cls._refresh_task.cancel()
        try:
            await cls._refresh_task
        except asyncio.CancelledError:
            pass
        cls._refresh_task = None

    @classmethod
    def _construir_indices_inversos(cls):
        """
        Genera diccionarios inversos optimizados para búsquedas CRM -> SFC.
        Garantiza aislamiento atómico durante la construcción en RAM para prevenir
        condiciones de carrera con peticiones HTTP concurrentes.
        """
        if "producto" not in cls.CATALOGOS or not cls.CATALOGOS["producto"]:
            cls.CATALOGOS["producto"] = {
                f"207_{idx}": nombre
                for idx, nombre in enumerate(cls.PRODUCTO_SFC_TEXTO_TO_SF.values(), 1)
            }

        nuevos_indices_inversos: Dict[str, Dict[str, Any]] = {}

        for cat_key, cat_dict in cls.CATALOGOS.items():
            cat_inverse = {}
            for k, v in cat_dict.items():
                val_to_store = int(k) if str(k).isdigit() else str(k)
                cat_inverse[cls._normalize_text(v)] = val_to_store
                cat_inverse[str(k)] = val_to_store 
            nuevos_indices_inversos[cat_key] = cat_inverse
        
        if "tipo_id" in nuevos_indices_inversos:
            nuevos_indices_inversos["tipo_id"].update({
                "cc": 1, "ce": 2, "rut": 3, "nit": 3, "dni": 4, "pass": 5, "passport": 5, "pasaporte": 5
            })
        if "punto_recepcion" in nuevos_indices_inversos:
            nuevos_indices_inversos["punto_recepcion"].update({
                "activate b2c": 99, "form: change data": 99, "updatecom": 99, "manual": 1, "internet": 2
            })

        cls.INVERSE_CATALOGS = nuevos_indices_inversos

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
    def _get_sf_field_value_from_dict(cls, entity: Dict[str, Any], field_name: str) -> Any:
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

    @classmethod
    def _get_sf_field_value_from_object(cls, entity: Any, field_name: str) -> Any:
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
    def _get_sf_field_value(cls, entity: Any, field_name: str) -> Any:
        if isinstance(entity, dict):
            return cls._get_sf_field_value_from_dict(entity, field_name)
        return cls._get_sf_field_value_from_object(entity, field_name)

    @classmethod
    def _strip_html(cls, text: str) -> str:
        if not text:
            return ""
        return cls.HTML_REGEX.sub('', str(text)).strip()

    @classmethod
    def _traducir_via_catalogo_crm(cls, sfc_key: str, str_key: str) -> Any:
        """
        Resuelve sfc_key contra CATALOGOS por mapeo directo de llave (con offset especial
        para macro_motivo). Retorna _NO_MATCH si sfc_key no pertenece a este grupo (ver
        _translate_value_to_crm para el resto de campos).
        """
        key_to_cat = {
            "sexo": "genero",
            "tipo_id_CF": "tipo_id",
            "tipo_persona": "persona",
            "lgbtiq": "lgbtiq",
            "sc_LGBTIQ__c": "lgbtiq",
            "condicion_especial": "condicion_especial",
            "canal_cod": "canal",
            "ente_control": "ente_control",
            "insta_recepcion": "instancia_recepcion",
            "admision": "admision",
            "a_favor_de": "favorabilidad",
            "aceptacion_queja": "aceptacion",
            "rectificacion_queja": "rectificacion",
            "desistimiento_queja": "desistimiento",
            "tipo_fraude": "tipo_fraude",
            "modalidad_fraude": "modalidad_fraude",
            "punto_recepcion": "punto_recepcion",
            "macro_motivo_cod": "macro_motivo",
            "Categorias_COL__c": "macro_motivo"
        }

        if sfc_key not in key_to_cat:
            return _NO_MATCH

        cat_key = key_to_cat[sfc_key]
        cat_dict = cls.CATALOGOS.get(cat_key, {})

        if str_key in cat_dict:
            return cat_dict[str_key]

        if cat_key == "macro_motivo" and str_key.isdigit():
            offset_key = str(int(str_key) + 900)
            if offset_key in cat_dict:
                return cat_dict[offset_key]

        logger.warning(f"⚠️ [Mapping M1] Código no reconocido en catálogo '{cat_key}' para la llave '{sfc_key}': '{str_key}'")
        return str_key

    @classmethod
    def _translate_value_to_crm(cls, sfc_key: str, sfc_value: Any) -> Any:
        if sfc_value is None:
            return None

        str_key = str(sfc_value).strip()
        if not str_key:
            return None

        if sfc_key == "codigo_pais":
            cat_paises = cls.CATALOGOS.get("codigo_pais", {})
            if str_key in cat_paises:
                return cat_paises[str_key]
            logger.warning(f"⚠️ [Mapping M1] Código de país no mapeado recibido de SFC: '{str_key}'")
            return str_key

        resultado_catalogo = cls._traducir_via_catalogo_crm(sfc_key, str_key)
        if resultado_catalogo is not _NO_MATCH:
            return resultado_catalogo

        if sfc_key in ("departamento_cod", "Departamento__c"):
            return cls.DEPT_DIVIPOLA_INV.get(str_key, str(sfc_value))
        if sfc_key in ("municipio_cod", "SC_municipio__c"):
            return cls.MUNI_DIVIPOLA_INV.get(str_key, str(sfc_value))

        if sfc_key in ("tutela", "queja_expres", "escalamiento_DCF", "replica", "producto_digital"):
            val_int = int(sfc_value) if str_key.isdigit() else sfc_value
            return "No" if (val_int == 2 or sfc_value is False) else "Si"

        return sfc_value

    @classmethod
    def _lookup_or_fail(cls, source_dict: Dict[str, Any], normalized_value: str, sf_key: str, raw_value: Any, catalog_name: str) -> Any:
        """
        Traduce un valor CRM->SFC contra un catálogo/índice ya normalizado. Si el valor
        no existe en el catálogo, rechaza el caso en lugar de inventar un dato regulatorio
        plausible (HALLAZGO 23 de la auditoría 2026-08-13).
        """
        if normalized_value in source_dict:
            return source_dict[normalized_value]

        logger.error(
            f"🚫 [Mapping SFC] Valor no reconocido para '{sf_key}' en catálogo '{catalog_name}': "
            f"'{raw_value}'. Se rechaza el envío a la SFC en lugar de usar un default plausible."
        )
        raise SfcIntegrationException(
            status_code=400,
            error_type="CRM_PAYLOAD_VALIDATION_ERROR",
            sfc_field=sf_key,
            raw_message=f"El valor '{raw_value}' de '{sf_key}' no existe en el catálogo '{catalog_name}'.",
            crm_action=(
                f"Verifique el valor de '{sf_key}' en el CRM o actualice el catálogo de mapeo "
                f"'{catalog_name}' antes de reintentar."
            ),
        )


    @classmethod
    def _traducir_valor_especial_a_sfc(cls, sf_key: str, sf_value: Any) -> Any:
        """
        Casos de _translate_value_to_sfc que no siguen el patrón normalize+catálogo (booleanos
        de texto, constantes). Retorna _NO_MATCH si sf_key no pertenece a este grupo.
        """
        if sf_key == "codigo_pais__c":
            normalized_country = cls._normalize_text(str(sf_value))
            cat_inverse_pais = cls.INVERSE_CATALOGS.get("codigo_pais", {})
            return str(cls._lookup_or_fail(cat_inverse_pais, normalized_country, sf_key, sf_value, "codigo_pais"))

        if sf_key in ("Tutela__c", "Quejas_express__c"):
            v_clean = str(sf_value).lower().strip()
            if v_clean in ("si", "sí", "true", "1"): return 1
            if v_clean in ("no", "false", "2"): return 2
            return 2

        if sf_key == "sinRespuestaFinal__c":
            v_clean = str(sf_value).lower().strip()
            return v_clean in ("si", "sí", "true", "1")

        if sf_key == "Product__c":
            return 207

        return _NO_MATCH

    @classmethod
    def _traducir_via_catalogo_sfc(cls, sf_key: str, sf_value: Any, normalized: str) -> Any:
        """
        Resuelve sf_key contra INVERSE_CATALOGS/DIVIPOLA (hard-fail o soft-fail según el campo).
        Retorna _NO_MATCH si sf_key no pertenece a ninguno de estos catálogos (ver
        _translate_value_to_sfc para el resto de campos).
        """
        # Campos regulatorios sin una capa de default explícita aguas abajo: un valor no
        # reconocido en el catálogo rechaza el caso (HALLAZGO 23) en vez de inventar un dato.
        sf_to_cat = {
            "sc_genero__c": "genero",
            "SC_id_type__c": "tipo_id",
            "tipo_de_persona__c": "persona",
            "sc_LGBTIQ__c": "lgbtiq",
            "canal__c": "canal",
            "Instancia_de_recepcion__c": "instancia_recepcion",
            "Desistimiento__c": "desistimiento",
            "tipo_fraude__c": "tipo_fraude",
            "Tipo_Fraude__c": "tipo_fraude",
            "modalidad_fraude__c": "modalidad_fraude",
            "Modalidad_Fraude__c": "modalidad_fraude",
            "punto_recepcion": "punto_recepcion",
            "Categorias_COL__c": "macro_motivo",
            "Rectificacion__c": "rectificacion",
        }

        # Campos que YA cuentan con una capa de default deliberada aguas abajo:
        # - Favorabilidad__c / Aceptacion__c: sólo son exigidos por
        #   crm_entity_to_sfc_momento3_payload cuando estado_cod == 4 (Cierre); en cualquier
        #   otro caso un valor ausente/no reconocido es legítimamente None.
        # - sc_Condicion_especial__c / Ente_de_control__c / admision_col__c: Momento 3
        #   (momento_3_sync.py, diccionario `sfc_defaults`) y Momento 2
        #   (crm_entity_to_sfc_momento2_payload, operador `or`) ya rellenan un default de
        #   negocio explícito si el mapeo no resuelve el valor. Forzar un hard-fail aquí
        #   duplicaría/contradiría esa capa existente en vez de reemplazarla.
        # Se registra la advertencia igualmente para mantener visibilidad (antes no existía).
        sf_to_cat_soft = {
            "sc_Condicion_especial__c": "condicion_especial",
            "Ente_de_control__c": "ente_control",
            "admision_col__c": "admision",
            "Favorabilidad__c": "favorabilidad",
            "Aceptacion__c": "aceptacion",
        }

        if sf_key in sf_to_cat:
            cat_key = sf_to_cat[sf_key]
            return cls._lookup_or_fail(cls.INVERSE_CATALOGS.get(cat_key, {}), normalized, sf_key, sf_value, cat_key)

        if sf_key in sf_to_cat_soft:
            cat_key = sf_to_cat_soft[sf_key]
            cat_dict = cls.INVERSE_CATALOGS.get(cat_key, {})
            if normalized in cat_dict:
                return cat_dict[normalized]
            logger.warning(
                f"⚠️ [Mapping SFC] Valor no reconocido para '{sf_key}' en catálogo '{cat_key}': "
                f"'{sf_value}'. Se usará None (el default de negocio se aplica aguas abajo)."
            )
            return None

        if sf_key == "Departamento__c":
            return cls._lookup_or_fail(cls.DEPT_DIVIPOLA, normalized, sf_key, sf_value, "DIVIPOLA departamento")
        if sf_key == "SC_municipio__c":
            return cls._lookup_or_fail(cls.MUNI_DIVIPOLA, normalized, sf_key, sf_value, "DIVIPOLA municipio")

        return _NO_MATCH

    @classmethod
    def _translate_value_to_sfc(cls, sf_key: str, sf_value: Any) -> Any:
        if sf_value is None:
            return None

        resultado_especial = cls._traducir_valor_especial_a_sfc(sf_key, sf_value)
        if resultado_especial is not _NO_MATCH:
            return resultado_especial

        normalized = cls._normalize_text(str(sf_value))

        resultado_catalogo = cls._traducir_via_catalogo_sfc(sf_key, sf_value, normalized)
        if resultado_catalogo is not _NO_MATCH:
            return resultado_catalogo

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
        if not isinstance(sfc_data, dict):
            raise ValueError("El payload de usuario recibido de la SFC debe ser un objeto/diccionario válido.")
    
        num_id = sfc_data.get("numero_id_CF") or sfc_data.get("numero_id") or sfc_data.get("id_number__c")
        if not num_id or not str(num_id).strip():
            raise ValueError("Campo obligatorio 'numero_id_CF' ausente o vacío en el registro de usuario de la SFC.")
    
        crm_data = {}
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

SfcSalesforceMapper.cargar_catalogos_local()

sfc_payload_to_db_dict = SfcSalesforceMapper.sfc_payload_to_db_dict
sfc_user_payload_to_db_dict = SfcSalesforceMapper.sfc_user_payload_to_db_dict
crm_entity_to_sfc_payload = SfcSalesforceMapper.crm_entity_to_sfc_payload
crm_entity_to_sfc_momento2_payload = SfcSalesforceMapper.crm_entity_to_sfc_momento2_payload
crm_entity_to_sfc_momento3_payload = SfcSalesforceMapper.crm_entity_to_sfc_momento3_payload