import asyncio
import json
import os
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

class SfcIntegrationException(Exception):
    """Excepción personalizada para controlar fallas devueltas por la SFC."""
    def __init__(self, status_code: int, error_type: str, sfc_field: Optional[str], raw_message: str, crm_action: str):
        self.status_code = status_code
        self.error_type = error_type
        self.sfc_field = sfc_field
        self.raw_message = raw_message
        self.crm_action = crm_action
        super().__init__(raw_message)

class SfcErrorTranslator:
    # Matriz cargada dinámicamente en memoria RAM desde errores_sfc.json
    MATRIZ_ERRORES_TEXTO: List[Dict[str, str]] = []

    @classmethod
    def cargar_matriz_errores(cls):
        """Carga los mapeos desde errores_sfc.json hacia la RAM."""
        ruta_archivo = os.path.join(os.path.dirname(__file__), "errores_sfc.json")
        try:
            with open(ruta_archivo, "r", encoding="utf-8") as f:
                cls.MATRIZ_ERRORES_TEXTO = json.load(f)
            logger.info(f"✅ [SfcErrorTranslator] Cargados {len(cls.MATRIZ_ERRORES_TEXTO)} patrones de errores desde JSON.")
        except Exception as e:
            logger.error(f"❌ [SfcErrorTranslator] Error al cargar errores_sfc.json: {str(e)}")
            cls.MATRIZ_ERRORES_TEXTO = []

    @classmethod
    def procesar_y_lanzar(cls, status_code: int, response_text: str):
        """Analiza el body devuelto por la SFC (JSON o String) y lanza SfcIntegrationException."""
        if not cls.MATRIZ_ERRORES_TEXTO:
            cls.cargar_matriz_errores()

        sfc_field = None
        raw_message = response_text
        error_type = "UNKNOWN_SFC_ERROR"
        crm_action = "Error no mapeado por la SFC. Por favor revisar los logs del payload."

        # 1. Intentar parsear si la SFC respondió con un JSON estructurado
        try:
            data = json.loads(response_text)
            if isinstance(data, dict):
                # 🎯 FIX: La SFC empaqueta los errores detallados dentro de "message": {"campo": ["detalle"]}
                msg_obj = data.get("message")
                
                if isinstance(msg_obj, dict):
                    for key, value in msg_obj.items():
                        sfc_field = key
                        if isinstance(value, list) and len(value) > 0:
                            raw_message = str(value[0])
                        else:
                            raw_message = str(value)
                        break
                elif isinstance(msg_obj, str):
                    raw_message = msg_obj
                elif "detail" in data and data["detail"] != "Error en API":
                    raw_message = str(data["detail"])
                else:
                    # Fallback para estructuras directas tipo {"nombres": ["Campo obligatorio"]}
                    for key, value in data.items():
                        if key in ("status_code", "detail"):
                            continue
                        sfc_field = key
                        if isinstance(value, list) and len(value) > 0:
                            raw_message = str(value[0])
                        else:
                            raw_message = str(value)
                        break
        except Exception:
            # Si es texto plano (ej. fallas 502/503/timeout), se conserva en raw_message
            pass

        # 2. Buscar coincidencias en nuestra matriz en RAM
        response_text_lower = response_text.lower()
        raw_message_lower = raw_message.lower()
        sfc_field_lower = (sfc_field or "").lower()

        encontrado = False
        for regla in cls.MATRIZ_ERRORES_TEXTO:
            subcadena = regla.get("subcadena", "").lower()
            if not subcadena:
                continue

            # 🎯 Coincide si está en el mensaje extraído, en el campo o en el cuerpo JSON crudo
            if (subcadena in raw_message_lower 
                or subcadena in sfc_field_lower 
                or subcadena in response_text_lower):
                
                error_type = regla.get("tipo", "UNKNOWN_SFC_ERROR")
                crm_action = regla.get("accion", crm_action)
                encontrado = True
                break

        if not encontrado or error_type == "UNKNOWN_SFC_ERROR":
            try:
                from app.services.email_service import EmailAlertService
                
                # Disparo de la notificación al correo dev
                asyncio.create_task(
                    EmailAlertService.notificar_error_no_mapeado(
                        status_code=status_code,
                        raw_message=raw_message,
                        sfc_field=sfc_field
                    )
                )
            except Exception as mail_err:
                logger.warning(f"⚠️ No se pudo enviar el correo de error no mapeado: {str(mail_err)}")
        
        # 3. Lanzar la excepción controlada con el error_type ya traducido
        raise SfcIntegrationException(
            status_code=status_code,
            error_type=error_type,
            sfc_field=sfc_field,
            raw_message=raw_message,
            crm_action=crm_action
        )