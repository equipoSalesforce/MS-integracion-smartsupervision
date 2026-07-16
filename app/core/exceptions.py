# app/core/exceptions.py
import json
from typing import Dict, Any, Optional

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
    # Matriz de traducción basada en subcadenas para mapear los mensajes oficiales de la SFC
    MATRIZ_ERRORES_TEXTO = [
        ("Clave primaria \"0\" inválida", "VALIDATION_ERROR", "Verificar que el dato o ID de la cuenta asociada este diligenciado con los valores correctos."),
        ("No existe el objeto", "VALIDATION_ERROR", "Cambiar la categoría o código en la pestaña SSV y guardar."),
        ("no corresponde al departamento asignado", "VALIDATION_ERROR", "Verificar el municipio y departamento de la cuenta asociada."),
        ("no tenga más de 50 caracteres", "VALIDATION_ERROR", "Verificar que el nombre de la cuenta asociada no tenga más de 50 caracteres."),
        ("Este campo no puede ser nulo", "VALIDATION_ERROR", "Verificar que el campo requerido en la cuenta asociada este diligenciado."),
        ("no tenga más de 14 caracteres", "VALIDATION_ERROR", "Verificar que el documento de identidad no supere los 14 caracteres."),
        ("no tenga más de 1000 caracteres", "VALIDATION_ERROR", "Verificar que la descripción o texto no supere los caracteres permitidos."),
        ("Compruebe el código o ack de la queja", "STATUS_ERROR", "Marcar el Smart status en la pestaña SSV en none y guardar."),
        ("documento de respuesta final debe haber sido enviado", "BUSINESS_RULE_ERROR", "Verificar que existe el documento de cierre y que tenga el prefijo/afijo RESP_FINAL_SFC."),
        ("fijado en True", "BUSINESS_RULE_ERROR", "Verificar que existe el documento de cierre y que tenga el prefijo/afijo RESP_FINAL_SFC."),
        ("ya existe una Queja radicada para la entidad con el mismo motivo", "ALREADY_EXISTS", "Validar si corresponde al mismo caso. Si son diferentes, cambiar levemente el canal o motivo para diferenciarlo."),
        ("Debido a que el estado enviado de la Queja es diferente de (4) Cerrado", "BUSINESS_RULE_ERROR", "No hacer nada en el microservicio. El caso ya se encuentra cerrado en la SFC."),
        ("La Queja se encuentra con estado Cerrado", "BUSINESS_RULE_ERROR", "No es posible actualizar metadatos generales; solo se permiten operaciones de réplica o desistimiento."),
        ("No se puede actualizar el anexo debido a que la Queja se encuentra cerrada", "BUSINESS_RULE_ERROR", "Operación rechazada por la SFC debido a que el radicado ya está en estado de cierre."),
        ("debe ser mayor que la fecha de creación", "VALIDATION_ERROR", "Corregir las fechas ingresadas en el CRM y guardar nuevamente en estado resolved."),
        ("La fecha debe ser diferente a la fecha ya registrada", "VALIDATION_ERROR", "Forzar un cambio de minutos o fecha diferente e intentar guardar de nuevo."),
        ("La denuncia tiene una fecha de cierre de más de dos meses", "BUSINESS_RULE_ERROR", "La SFC bloqueó la edición por inactividad de más de 2 meses en estado cerrado."),
        ("El anexo ya existe", "DUPLICATE_FILE", "El archivo ya fue cargado previamente en la SFC de forma exitosa. Guardar nuevamente."),
        ("El documento ya existe", "DUPLICATE_FILE", "El archivo ya fue cargado previamente en la SFC de forma exitosa. Guardar nuevamente."),
        # Fallas de infraestructura o timeouts[cite: 4]
        ("no está disponible por el momento", "INFRASTRUCTURE_ERROR", "Servidor de la SFC caído o en mantenimiento. Volver a intentar más tarde."),
        ("Error inesperado", "SFC_INTERNAL_ERROR", "Fallo crítico interno en el servidor de la SFC. Volver a guardar más tarde."),
        ("upstream request timeout", "TIMEOUT_ERROR", "La SFC tardó demasiado en responder (Timeout). Reintentar la operación."),
        ("Token expiró", "AUTH_ERROR", "Sesión expirada en la SFC. El microservicio renovará los tokens automáticamente, reintente en un momento.")
    ]

    @classmethod
    def procesar_y_lanzar(cls, status_code: int, response_text: str):
        """Analiza el body devuelto por la SFC (JSON o String) y lanza SfcIntegrationException[cite: 4]."""
        sfc_field = None
        raw_message = response_text
        error_type = "UNKNOWN_SFC_ERROR"
        crm_action = "Error no mapeado por la SFC. Por favor revisar los logs del payload."

        # 1. Intentar parsear si la SFC respondió con un JSON estructurado de campos[cite: 4]
        try:
            data = json.loads(response_text)
            if isinstance(data, dict):
                # Caso especial: JSON con detail directo[cite: 4]
                if "detail" in data:
                    raw_message = str(data["detail"])
                # Caso común: {"campo": ["mensaje de error"]}[cite: 4]
                else:
                    for key, value in data.items():
                        sfc_field = key
                        if isinstance(value, list) and len(value) > 0:
                            raw_message = str(value[0])
                        else:
                            raw_message = str(value)
                        break
        except Exception:
            # Si no es JSON, es un texto plano (infraestructura/timeout)[cite: 4]
            pass

        # 2. Buscar coincidencias en nuestra matriz semántica[cite: 4]
        for subcadena, tipo, accion in cls.MATRIZ_ERRORES_TEXTO:
            if subcadena.lower() in raw_message.lower() or (sfc_field and subcadena.lower() in sfc_field.lower()):
                error_type = tipo
                crm_action = accion
                break

        # 3. Lanzar la excepción controlada con el diagnóstico listo
        raise SfcIntegrationException(
            status_code=status_code,
            error_type=error_type,
            sfc_field=sfc_field,
            raw_message=raw_message,
            crm_action=crm_action
        )