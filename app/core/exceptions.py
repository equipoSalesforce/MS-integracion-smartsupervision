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
    async def obtener_matriz_errores(cls) -> List[Dict[str, str]]:
        """
        Retorna la matriz en RAM. Si la caché expiró o está vacía, realiza una
        petición HTTP GET saliente hacia Google Sheets.
        """
        ahora = time.time()

        # 1. Si ya está cargada en RAM y no ha vencido el TTL (10 min), usar RAM
        if cls.MATRIZ_ERRORES_TEXTO and (ahora - cls.ULTIMA_ACTUALIZACION) < cls.CACHE_TTL_SEGUNDOS:
            return cls.MATRIZ_ERRORES_TEXTO

        url_sheets = getattr(settings, "GOOGLE_SHEETS_MATRIX_URL", None)

        # 2. Consultar Google Sheets
        if url_sheets:
            try:
                logger.info("🔄 [SfcErrorTranslator] Sincronizando matriz de errores desde Google Sheets...")
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.get(url_sheets, follow_redirects=True)
                    
                    if settings.ENVIRONMENT == "local":
                        # 🎯 FIX: Imprimir response.text en lugar de forzar .json()
                        logger.info(f"Retornado por el script (Status {response.status_code}):\n {response.text[:300]}")
                    
                    if response.status_code == 200:
                        try:
                            data = response.json()
                            if isinstance(data, list) and len(data) > 0:
                                cls.MATRIZ_ERRORES_TEXTO = data
                                cls.ULTIMA_ACTUALIZACION = ahora
                                logger.info(
                                    f"✅ [SfcErrorTranslator] Matriz actualizada desde Google Sheets: "
                                    f"{len(data)} reglas cargadas."
                                )
                                return cls.MATRIZ_ERRORES_TEXTO
                        except json.JSONDecodeError:
                            logger.warning(
                                "⚠️ [SfcErrorTranslator] La respuesta de Google Sheets no es un JSON válido. "
                                "Verifica que el despliegue del Apps Script tenga acceso para 'Cualquiera'."
                            )
                    else:
                        logger.warning(
                            f"⚠️ [SfcErrorTranslator] Google Apps Script devolvió HTTP {response.status_code}."
                        )
            except Exception as e:
                logger.warning(
                    f"⚠️ [SfcErrorTranslator] Falló la sincronización con Google Sheets: {e}. Usando respaldo local."
                )

        # 3. Fallback: Cargar JSON local
        if not cls.MATRIZ_ERRORES_TEXTO:
            cls.cargar_matriz_local()

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
        """Extrae el campo (sfc_field) y el mensaje legible (raw_message) del JSON de respuesta."""
        sfc_field = None
        raw_message = response_text

        try:
            data = json.loads(response_text)
            if not isinstance(data, dict):
                return sfc_field, raw_message

            msg_obj = data.get("message")

            if isinstance(msg_obj, dict):
                for key, value in msg_obj.items():
                    sfc_field = key
                    raw_message = str(value[0]) if isinstance(value, list) and value else str(value)
                    break
            elif isinstance(msg_obj, str):
                raw_message = msg_obj
            elif "detail" in data and data["detail"] != "Error en API":
                raw_message = str(data["detail"])
            else:
                for key, value in data.items():
                    if key in ("status_code", "detail"):
                        continue
                    sfc_field = key
                    raw_message = str(value[0]) if isinstance(value, list) and value else str(value)
                    break
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