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
        # 1. Validación de claves primarias y campos específicos del formulario (SFC Fields)
        ("tipo_id_CF", "VALIDATION_ERROR", "Verificar que el tipo DNI de la cuenta asociada este diligenciado o con los valores correctos."),
        ("departamento_cod", "VALIDATION_ERROR", "Verificar el departamento asociado a la cuenta."),
        ("canal_cod", "VALIDATION_ERROR", "Verificar que el \"canal\" en la pestaña SSV este seleccionado o tenga un valor correcto."),
        ("macro_motivo_cod", "VALIDATION_ERROR", "Cambiar la categoria COL en la pestaña SSV y guardar."),
        ("municipio_cod", "VALIDATION_ERROR", "Verificar el municipio de la cuenta asociada."),
        
        # 2. Longitudes cruzadas y validaciones condicionales de datos del Consumidor
        ("no tenga más de 14 caracteres", "VALIDATION_ERROR", "Verificar que el documento de identidad de la cuenta asociada no tenga mas de 14 caracteres."),
        ("numero_id_CF", "VALIDATION_ERROR", "Verificar que Numero de DNI de la cuenta asociada este diligenciado."),
        ("no tenga más de 50 caracteres", "VALIDATION_ERROR", "Verificar que el nombre de la cuenta asociada no tenga mas de 50 caracteres."),
        ("Este campo no puede ser nulo", "VALIDATION_ERROR", "Verificar que el nombre de la cuenta asociada este diligenciado."),
        ("nombres", "VALIDATION_ERROR", "Verificar que el nombre de la cuenta asociada este diligenciado."),
        ("no tenga más de 1000 caracteres", "VALIDATION_ERROR", "Verificar que la descripcion no supere los caracteres permitidos."),
        ("texto_queja", "VALIDATION_ERROR", "Verificar que la descripcion no supere los caracteres permitidos."),
        
        # 3. Estados Smart, Restricciones del Momento 3 y Reglas de Cierre
        ("Compruebe el código o ack de la queja", "STATUS_ERROR", "Marcar el Smart status en la pestaña SSV en none y guardar."),
        ("documento de respuesta final debe haber sido enviado", "BUSINESS_RULE_ERROR", "Verificar que existe el documento de cierre y que tenga el nombre RESP_FINAL_SFC."),
        ("fijado en True", "BUSINESS_RULE_ERROR", "Verificar que existe el documento de cierre y que tenga el nombre RESP_FINAL_SFC."),
        ("ya cuenta con un documento de respuesta final", "BUSINESS_RULE_ERROR", "No hacer nada porque ya esta cerrada en SSV."),
        ("La Queja se encuentra con estado Cerrado", "BUSINESS_RULE_ERROR", "No hacer nada porque ya esta cerrada en SSV."),
        ("No se puede actualizar el anexo debido a que la Queja se encuentra cerrada", "BUSINESS_RULE_ERROR", "No hacer nada porque ya esta cerrada en SSV."),
        ("estado_cod", "BUSINESS_RULE_ERROR", "Verificar el estado del caso."),
        
        # 4. Control de Fechas, Vigencias y Archivos Duplicados
        ("debe ser mayor que la fecha de creación", "VALIDATION_ERROR", "Guardar nuevamente en resolved."),
        ("La fecha debe ser diferente a la fecha ya registrada", "VALIDATION_ERROR", "Guardar nuevamente en resolved."),
        ("La denuncia tiene una fecha de cierre de más de dos meses", "BUSINESS_RULE_ERROR", "Guardar nuevamente en resolved."),
        ("fecha_actualizacion", "VALIDATION_ERROR", "Guardar nuevamente en resolved."),
        ("fecha_cierre", "VALIDATION_ERROR", "Guardar nuevamente en resolved."),
        ("El anexo ya existe", "DUPLICATE_FILE", "Guardar nuevamente en resolved."),
        ("El documento ya existe", "DUPLICATE_FILE", "Guardar nuevamente en resolved."),
        ("file", "DUPLICATE_FILE", "Guardar nuevamente en resolved."),
        
        # 5. Casos Especiales de Radicación Existente y Fallas de Infraestructura Base
        ("ya existe una Queja radicada para la entidad con el mismo motivo", "ALREADY_EXISTS", "Validar que el caso que referencian en el mensaje donde estan los signos de interrogación no corresponda a la misma información del caso actual. Si son casos diferentes cambiar el canal para que se entienda que son casos diferentes y volver a guardar."),
        ("no está disponible por el momento", "INFRASTRUCTURE_ERROR", "Volver a guardar."),
        ("Error inesperado", "SFC_INTERNAL_ERROR", "Volver a guardar."),
        ("upstream request timeout", "TIMEOUT_ERROR", "Volver a guardar."),
        ("Token expiró", "AUTH_ERROR", "Volver a guardar.")
    ]

    @classmethod
    def procesar_y_lanzar(cls, status_code: int, response_text: str):
        """Analiza el body devuelto por la SFC (JSON o String) y lanza SfcIntegrationException."""
        sfc_field = None
        raw_message = response_text
        error_type = "UNKNOWN_SFC_ERROR"
        crm_action = "Error no mapeado por la SFC. Por favor revisar los logs del payload."

        # 1. Intentar parsear si la SFC respondió con un JSON de campos estructurado
        try:
            data = json.loads(response_text)
            if isinstance(data, dict):
                # Caso especial: JSON con detail directo o error 404 de recurso
                if "detail" in data:
                    raw_message = str(data["detail"])
                # Caso común: {"campo": ["mensaje de error"]}
                else:
                    for key, value in data.items():
                        sfc_field = key
                        if isinstance(value, list) and len(value) > 0:
                            raw_message = str(value[0])
                        else:
                            raw_message = str(value)
                        break
        except Exception:
            # Si no es JSON, es un texto plano (infraestructura/timeout)
            pass

        # 2. Buscar coincidencias en nuestra matriz semántica
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