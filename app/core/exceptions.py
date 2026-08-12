import asyncio
import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple
import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class SfcIntegrationException(Exception):
    """Excepción personalizada para controlar fallas devueltas por la SFC."""

    def __init__(
        self,
        status_code: int,
        error_type: str,
        sfc_field: Optional[str],
        raw_message: str,
        crm_action: str,
    ):
        self.status_code = status_code
        self.error_type = error_type
        self.sfc_field = sfc_field
        self.raw_message = raw_message
        self.crm_action = crm_action
        super().__init__(f"[{error_type}] {raw_message}")


class SfcErrorTranslator:
    # Matriz cargada dinámicamente en RAM desde Google Sheets con fallback a errores_sfc.json
    MATRIZ_ERRORES_TEXTO: List[Dict[str, str]] = []
    ULTIMA_ACTUALIZACION: float = 0
    CACHE_TTL_SEGUNDOS: int = 600  # 10 Minutos en RAM

    @classmethod
    async def _obtener_google_access_token(cls) -> Optional[str]:
        """
        Intercambia el Refresh Token por un Access Token válido de Google.
        """
        client_id = getattr(settings, "GOOGLE_CLIENT_ID", None)
        client_secret = getattr(settings, "GOOGLE_CLIENT_SECRET", None)
        refresh_token = getattr(settings, "GOOGLE_REFRESH_TOKEN", None)

        if not all([client_id, client_secret, refresh_token]):
            logger.warning(
                "⚠️ [SfcErrorTranslator] Faltan credenciales de Google OAuth en settings. "
                "No se agregará header de autorización."
            )
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
                    data = res.json()
                    return data.get("access_token")
                else:
                    logger.warning(
                        f"⚠️ [SfcErrorTranslator] Falló renovación de token Google OAuth: HTTP {res.status_code}"
                    )
        except Exception as e:
            logger.warning(f"⚠️ [SfcErrorTranslator] Error solicitando Access Token a Google: {e}")

        return None

    @classmethod
    async def obtener_matriz_errores(cls) -> List[Dict[str, str]]:
        """
        Retorna la matriz en RAM. Si la caché expiró o está vacía, realiza una
        petición HTTP GET a la API v4 de Google Sheets usando OAuth 2.0.
        """
        ahora = time.time()

        # 1. Si ya está cargada en RAM y no ha vencido el TTL (10 min), usar RAM
        if cls.MATRIZ_ERRORES_TEXTO and cls.ULTIMA_ACTUALIZACION > 0 and (ahora - cls.ULTIMA_ACTUALIZACION) < cls.CACHE_TTL_SEGUNDOS:
            return cls.MATRIZ_ERRORES_TEXTO

        spreadsheet_id = getattr(settings, "GOOGLE_SPREADSHEET_ID", None)
        sheet_range = getattr(settings, "GOOGLE_SHEET_RANGE", "Hoja1!A:C")

        # 2. Consultar la API oficial v4 de Google Sheets
        if spreadsheet_id:
            try:
                logger.info("🔄 [SfcErrorTranslator] Sincronizando matriz mediante Google Sheets API v4...")

                access_token = await cls._obtener_google_access_token()
                if not access_token:
                    raise ValueError("No se pudo obtener el Access Token de Google OAuth.")

                headers = {"Authorization": f"Bearer {access_token}"}
                url_api_v4 = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{sheet_range}"

                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.get(url_api_v4, headers=headers)

                    if response.status_code == 200:
                        data = response.json()
                        rows = data.get("values", [])

                        reglas = []
                        for row in rows[1:]:
                            if not row or not row[0]:
                                continue
                            reglas.append({
                                "subcadena": str(row[0]).strip(),
                                "tipo": str(row[1]).strip() if len(row) > 1 else "UNKNOWN_SFC_ERROR",
                                "accion": str(row[2]).strip() if len(row) > 2 else "Revisar logs del payload."
                            })

                        if reglas:
                            cls.MATRIZ_ERRORES_TEXTO = reglas
                            cls.ULTIMA_ACTUALIZACION = ahora
                            logger.info(
                                f"✅ [SfcErrorTranslator] Matriz actualizada desde Google Sheets API v4: "
                                f"{len(reglas)} reglas cargadas."
                            )
                            return cls.MATRIZ_ERRORES_TEXTO
                    else:
                        logger.warning(
                            f"⚠️ [SfcErrorTranslator] Google Sheets API devolvió HTTP {response.status_code}: {response.text}"
                        )
            except Exception as e:
                logger.warning(
                    f"⚠️ [SfcErrorTranslator] Falló la sincronización con Google Sheets API v4: {e}. Usando datos vigentes/local."
                )

        # 3. Fallback: Cargar JSON local si la RAM está totalmente vacía
        if not cls.MATRIZ_ERRORES_TEXTO:
            cls.cargar_matriz_local()

        # 🟢 ACTUALIZAR TTL: Refrescar marca de tiempo tras fallback para no reintentar Google Sheets en cada petición
        cls.ULTIMA_ACTUALIZACION = ahora

        return cls.MATRIZ_ERRORES_TEXTO

    @classmethod
    def cargar_matriz_local(cls) -> None:
        """Carga el respaldo local desde errores_sfc.json."""
        ruta_archivo = os.path.join(os.path.dirname(__file__), "resources/errores_sfc.json")
        try:
            with open(ruta_archivo, "r", encoding="utf-8") as f:
                cls.MATRIZ_ERRORES_TEXTO = json.load(f)
            cls.ULTIMA_ACTUALIZACION = time.time()
            logger.info(
                f"📂 [SfcErrorTranslator] Respaldo local cargado en RAM con {len(cls.MATRIZ_ERRORES_TEXTO)} reglas."
            )
        except Exception as e:
            logger.error(f"❌ [SfcErrorTranslator] Error al cargar errores_sfc.json local: {e}")
            cls.MATRIZ_ERRORES_TEXTO = []

    @classmethod
    def _extraer_informacion_error(cls, response_text: str) -> Tuple[Optional[str], str]:
        """Extrae los campos (sfc_field) y los mensajes legibles (raw_message) del JSON de respuesta."""
        sfc_field = None
        raw_message = response_text

        try:
            data = json.loads(response_text)
            if not isinstance(data, dict):
                return sfc_field, raw_message

            msg_obj = data.get("message")

            if isinstance(msg_obj, dict):
                fields_list = []
                messages_list = []
                for key, value in msg_obj.items():
                    val_str = str(value[0]) if isinstance(value, list) and value else str(value)
                    fields_list.append(key)
                    messages_list.append(f"{key}: {val_str}")
                
                sfc_field = ", ".join(fields_list) if fields_list else None
                raw_message = " | ".join(messages_list) if messages_list else response_text

            elif isinstance(msg_obj, str):
                raw_message = msg_obj
            elif "detail" in data and data["detail"] != "Error en API":
                raw_message = str(data["detail"])
            else:
                fields_list = []
                messages_list = []
                for key, value in data.items():
                    if key in ("status_code", "detail"):
                        continue
                    val_str = str(value[0]) if isinstance(value, list) and value else str(value)
                    fields_list.append(key)
                    messages_list.append(f"{key}: {val_str}")
                
                if fields_list:
                    sfc_field = ", ".join(fields_list)
                    raw_message = " | ".join(messages_list)
        except (json.JSONDecodeError, TypeError):
            pass

        return sfc_field, raw_message

    @classmethod
    def _notificar_desconocido_async(
        cls, status_code: int, raw_message: str, sfc_field: Optional[str]
    ) -> None:
        """Dispara la notificación por correo de forma segura sin romper la ejecución si no hay Event Loop."""
        try:
            from app.services.email_service import EmailAlertService

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    EmailAlertService.notificar_error_no_mapeado(
                        status_code=status_code,
                        raw_message=raw_message,
                        sfc_field=sfc_field,
                    )
                )
            except RuntimeError:
                logger.warning(
                    "⚠️ [SfcErrorTranslator] No se detectó un event loop activo para disparar la alerta de correo."
                )
        except Exception as mail_err:
            logger.warning(f"⚠️ [SfcErrorTranslator] Error al intentar notificar por correo: {mail_err}")

    @classmethod
    async def procesar_y_lanzar(cls, status_code: int, response_text: str) -> None:
        """Analiza el body devuelto por la SFC y lanza SfcIntegrationException con el error traducido."""
        # 🎯 1. Cargar matriz (de Google Sheets con fallback local)
        matriz = await cls.obtener_matriz_errores()

        # 2. Extraer campo y mensaje relevante
        sfc_field, raw_message = cls._extraer_informacion_error(response_text)

        error_type = "UNKNOWN_SFC_ERROR"
        crm_action = "Error no mapeado por la SFC. Por favor revisar los logs del payload."

        # 3. Búsqueda de coincidencia en la matriz en RAM
        response_text_lower = response_text.lower()
        raw_message_lower = raw_message.lower()
        sfc_field_lower = (sfc_field or "").lower()

        encontrado = False
        for regla in matriz:
            subcadena = regla.get("subcadena", "").lower()
            if not subcadena:
                continue

            if (
                subcadena in raw_message_lower
                or subcadena in sfc_field_lower
                or subcadena in response_text_lower
            ):
                error_type = regla.get("tipo", "UNKNOWN_SFC_ERROR")
                crm_action = regla.get("accion", crm_action)
                encontrado = True
                break

        # 4. Notificar si no se halló en la matriz
        if not encontrado or error_type == "UNKNOWN_SFC_ERROR":
            cls._notificar_desconocido_async(status_code, raw_message, sfc_field)

        # 5. Lanzar la excepción controlada
        raise SfcIntegrationException(
            status_code=status_code,
            error_type=error_type,
            sfc_field=sfc_field,
            raw_message=raw_message,
            crm_action=crm_action,
        )