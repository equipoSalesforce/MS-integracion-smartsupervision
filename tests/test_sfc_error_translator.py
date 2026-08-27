# tests/test_sfc_error_translator.py
import asyncio
import unittest
from unittest.mock import patch, AsyncMock

from app.core.exceptions import SfcErrorTranslator, SfcIntegrationException


class TestCoincide(unittest.TestCase):
    """
    Cobertura de SfcErrorTranslator._coincide -- hallazgo de revisión, 2026-08-26.
    Las reglas de UN SOLO TOKEN sin espacios ('codigo_queja', '556240') deben
    anclarse a límites de palabra: si no, matchean como falso positivo cuando
    aparecen incrustadas dentro de un identificador más largo que el cliente
    controla (ej. un Smart_Code__c que contenga esos dígitos en medio de su parte
    numérica). Las frases de varias palabras (con espacio) no se anclan -- ya son
    suficientemente específicas por su longitud.
    """

    def test_token_incrustado_en_identificador_mas_largo_no_matchea(self):
        self.assertFalse(SfcErrorTranslator._coincide("556240", "1286seq556240abc"))

    def test_token_rodeado_de_no_alfanumericos_si_matchea(self):
        self.assertTrue(SfcErrorTranslator._coincide("556240", "codigo 556240)"))
        self.assertTrue(SfcErrorTranslator._coincide("556240", "(556240)"))

    def test_token_al_inicio_o_final_del_texto_si_matchea(self):
        self.assertTrue(SfcErrorTranslator._coincide("556240", "556240"))
        self.assertTrue(SfcErrorTranslator._coincide("codigo_queja", "codigo_queja"))

    def test_token_con_guion_bajo_incrustado_no_matchea(self):
        self.assertFalse(SfcErrorTranslator._coincide("codigo_queja", "xcodigo_quejay"))

    def test_frase_con_espacios_no_se_ancla(self):
        # Comportamiento sin cambios para frases largas -- siguen usando substring plano,
        # incluso "incrustadas" dentro de una oración más grande (ya son lo bastante
        # específicas por su longitud).
        self.assertTrue(SfcErrorTranslator._coincide("ya existe", "el anexo ya existe registrado"))

    def test_token_incrustado_con_guion_no_matchea(self):
        """
        🔴 FIX (hallazgo de revisión externa, 2026-08-26, ronda 4): el límite
        [a-zA-Z0-9] no cubre el alfabeto real de Smart_Code__c
        (^[a-zA-Z0-9_-]{1,30}$). Reproducido: 'SC-556240-01' seguía matcheando
        '556240' porque el '-' contaba como límite válido -- ahora no."""
        self.assertFalse(SfcErrorTranslator._coincide("556240", "sc-556240-01"))

    def test_token_incrustado_con_guion_bajo_no_matchea(self):
        self.assertFalse(SfcErrorTranslator._coincide("556240", "sc_556240_01"))


class TestExtraerInformacionError(unittest.TestCase):
    """
    Cobertura de SfcErrorTranslator._extraer_informacion_error: hasta ahora sin ningún
    test directo (0% de cobertura de líneas) a pesar de ser el parser que interpreta
    CUALQUIER body de error crudo devuelto por la SFC.
    """

    def test_message_string_simple(self):
        sfc_field, raw_message = SfcErrorTranslator._extraer_informacion_error(
            '{"message": "La queja se encuentra con estado cerrado"}'
        )
        self.assertIsNone(sfc_field)
        self.assertEqual(raw_message, "La queja se encuentra con estado cerrado")

    def test_message_dict_arma_sfc_field_y_raw_message(self):
        sfc_field, raw_message = SfcErrorTranslator._extraer_informacion_error(
            '{"message": {"numero_id_CF": ["Este campo es requerido."]}}'
        )
        self.assertEqual(sfc_field, "numero_id_CF")
        self.assertEqual(raw_message, "numero_id_CF: Este campo es requerido.")

    def test_message_dict_multiples_campos(self):
        sfc_field, raw_message = SfcErrorTranslator._extraer_informacion_error(
            '{"message": {"campo_a": ["error a"], "campo_b": ["error b"]}}'
        )
        self.assertEqual(sfc_field, "campo_a, campo_b")
        self.assertEqual(raw_message, "campo_a: error a | campo_b: error b")

    def test_detail_distinto_de_error_en_api_se_usa_como_raw_message(self):
        sfc_field, raw_message = SfcErrorTranslator._extraer_informacion_error(
            '{"detail": "Authentication credentials were not provided."}'
        )
        self.assertIsNone(sfc_field)
        self.assertEqual(raw_message, "Authentication credentials were not provided.")

    def test_detail_generico_error_apiexception_cae_a_fallback_generico(self):
        """
        🔴 FIX (hallazgo de revisión externa, 2026-08-26, ronda 4): el literal
        genérico comparado era 'Error en API', un valor que no aparece en ningún
        response real de la SFC (verificado contra la colección Postman oficial).
        El placeholder genérico real es 'Error APIException' -- se corrige el
        literal comparado."""
        response_text = '{"detail": "Error APIException"}'
        sfc_field, raw_message = SfcErrorTranslator._extraer_informacion_error(response_text)
        # Único campo presente ('detail') está excluido del fallback -> no hay fields_list,
        # raw_message queda igual al texto crudo original.
        self.assertIsNone(sfc_field)
        self.assertEqual(raw_message, response_text)

    def test_messages_plural_arma_sfc_field_y_raw_message_igual_que_message(self):
        """
        🔴 FIX (hallazgo de revisión externa, 2026-08-26, ronda 4): el envoltorio de
        error ESTÁNDAR real de la SFC (colección Postman oficial, la forma que
        cubre la mayoría de los endpoints) usa 'messages' en PLURAL, no 'message'.
        Reproducido con el ejemplo real de la colección:
        {"status_code": 400, "messages": {"codigo_queja": [...]}, "detail": "Error APIException"}
        -- antes de este fix, sfc_field quedaba en None y raw_message en la
        constante genérica "Error APIException", perdiendo toda la información
        estructurada del error."""
        sfc_field, raw_message = SfcErrorTranslator._extraer_informacion_error(
            '{"status_code": 400, "messages": {"codigo_queja": '
            '["Object with codigo_queja=111635888992248094 does not exist."]}, '
            '"detail": "Error APIException"}'
        )
        self.assertEqual(sfc_field, "codigo_queja")
        self.assertEqual(
            raw_message, "codigo_queja: Object with codigo_queja=111635888992248094 does not exist."
        )

    def test_message_singular_tiene_prioridad_sobre_messages_plural_si_ambos_estan(self):
        """Caso borde -- si por alguna razón ambos vinieran presentes, 'message'
        (singular) conserva la prioridad que ya tenía antes de este fix."""
        sfc_field, raw_message = SfcErrorTranslator._extraer_informacion_error(
            '{"message": {"campo_singular": ["error singular"]}, '
            '"messages": {"campo_plural": ["error plural"]}}'
        )
        self.assertEqual(sfc_field, "campo_singular")

    def test_dict_sin_message_ni_detail_usa_fallback_de_campos_sueltos(self):
        sfc_field, raw_message = SfcErrorTranslator._extraer_informacion_error(
            '{"numero_id_CF": ["ya existe"], "codigo_queja": "12345"}'
        )
        self.assertEqual(sfc_field, "numero_id_CF, codigo_queja")
        self.assertEqual(raw_message, "numero_id_CF: ya existe | codigo_queja: 12345")

    def test_json_no_es_dict_retorna_texto_crudo(self):
        response_text = "[1, 2, 3]"
        sfc_field, raw_message = SfcErrorTranslator._extraer_informacion_error(response_text)
        self.assertIsNone(sfc_field)
        self.assertEqual(raw_message, response_text)

    def test_texto_no_json_no_crashea_y_retorna_texto_crudo(self):
        response_text = "<html>502 Bad Gateway</html>"
        sfc_field, raw_message = SfcErrorTranslator._extraer_informacion_error(response_text)
        self.assertIsNone(sfc_field)
        self.assertEqual(raw_message, response_text)


class TestProcesarYLanzar(unittest.IsolatedAsyncioTestCase):
    """
    Cobertura de SfcErrorTranslator.procesar_y_lanzar: el motor central que clasifica
    cualquier error de la SFC contra MATRIZ_ERRORES_TEXTO y decide error_type/crm_action
    -- de ahí depende el self-healing, la detección de "caso ya cerrado" y si se alerta
    al equipo de desarrollo. Hasta ahora, 0% de cobertura de líneas: toda la suite
    construye SfcIntegrationException a mano en vez de pasar por este traductor real.
    """

    def _mockear_matriz(self, reglas):
        return patch.object(
            SfcErrorTranslator, "obtener_matriz_errores",
            new_callable=AsyncMock, return_value=reglas
        )

    async def test_regla_matcheada_por_raw_message_no_notifica(self):
        reglas = [{"subcadena": "ya se encuentra con estado cerrado", "tipo": "CASO_YA_CERRADO", "accion": "Ignorar, ya está cerrado."}]
        with self._mockear_matriz(reglas), \
             patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock) as mock_alert:
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(
                    400, '{"message": "La queja ya se encuentra con estado cerrado"}'
                )
            exc = ctx.exception
            self.assertEqual(exc.error_type, "CASO_YA_CERRADO")
            self.assertEqual(exc.crm_action, "Ignorar, ya está cerrado.")
            self.assertEqual(exc.status_code, 400)
            await asyncio.sleep(0)
            mock_alert.assert_not_called()

    async def test_regla_matcheada_por_sfc_field(self):
        # La subcadena sólo aparece en el nombre del campo (sfc_field), no en el mensaje.
        reglas = [{"subcadena": "numero_id_cf", "tipo": "CAMPO_REQUERIDO", "accion": "Complete el campo."}]
        with self._mockear_matriz(reglas), \
             patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(
                    400, '{"message": {"numero_id_CF": ["Este campo es requerido."]}}'
                )
            self.assertEqual(ctx.exception.error_type, "CAMPO_REQUERIDO")

    async def test_regla_matcheada_solo_en_response_text_crudo(self):
        # La subcadena no está en raw_message extraído ni en sfc_field, sólo en el JSON crudo.
        reglas = [{"subcadena": "traceback", "tipo": "ERROR_INTERNO_SFC", "accion": "Reintentar más tarde."}]
        response_text = '{"detail": "Error en API", "debug": "traceback (most recent call last)"}'
        with self._mockear_matriz(reglas), \
             patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(500, response_text)
            self.assertEqual(ctx.exception.error_type, "ERROR_INTERNO_SFC")

    async def test_ninguna_regla_matchea_usa_unknown_sfc_error_y_notifica(self):
        reglas = [{"subcadena": "ya se encuentra con estado cerrado", "tipo": "CASO_YA_CERRADO", "accion": "Ignorar."}]
        with self._mockear_matriz(reglas), \
             patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock) as mock_alert:
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(500, '{"message": "Fallo totalmente desconocido"}')
            exc = ctx.exception
            self.assertEqual(exc.error_type, "UNKNOWN_SFC_ERROR")
            self.assertEqual(exc.status_code, 500)
            # _notificar_desconocido_async dispara la tarea de alerta en segundo plano
            # (asyncio.create_task) -- se cede el loop una vez para que corra.
            await asyncio.sleep(0)
            mock_alert.assert_called_once_with(
                status_code=500,
                raw_message="Fallo totalmente desconocido",
                sfc_field=None,
            )

    async def test_matriz_vacia_tambien_es_unknown_sfc_error(self):
        with self._mockear_matriz([]), \
             patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock) as mock_alert:
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(500, "Internal Server Error")
            self.assertEqual(ctx.exception.error_type, "UNKNOWN_SFC_ERROR")
            await asyncio.sleep(0)
            mock_alert.assert_called_once()

    async def test_not_found_tiene_prioridad_sobre_coincidencia_por_nombre_de_campo(self):
        """
        🔴 FIX (hallazgo de revisión, 2026-08-26): reproduce el caso real -- una
        regla genérica basada en el NOMBRE del campo ('codigo_queja'->ALREADY_EXISTS)
        aparece ANTES en la matriz que la regla NOT_FOUND_ERROR ('does not exist'),
        igual que en errores_sfc.json real. 'codigo_queja' es el nombre del campo en
        CUALQUIER error de la SFC sobre una queja -- incluida la respuesta real de
        "Add File" cuando el caso NO existe todavía
        ({"codigo_queja": ["Object with codigo_queja=X does not exist."]}). Sin
        prioridad explícita, la coincidencia por nombre de campo gana por orden y
        deja el self-healing M2->M3 permanentemente inalcanzable para esta forma de
        error real de la SFC.
        """
        reglas = [
            {"subcadena": "codigo_queja", "tipo": "ALREADY_EXISTS", "accion": "El código ya existe."},
            {"subcadena": "does not exist", "tipo": "NOT_FOUND_ERROR", "accion": "Autorrecuperar (self-healing)."},
        ]
        with self._mockear_matriz(reglas), \
             patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(
                    400, '{"codigo_queja": ["Object with codigo_queja=111635888992248094 does not exist."]}'
                )
            self.assertEqual(ctx.exception.error_type, "NOT_FOUND_ERROR")

    async def test_not_found_no_reclasifica_error_de_validacion_de_otro_campo(self):
        """
        🔴 Regresión propia (encontrada y corregida el mismo día): la primera
        versión de este fix le daba prioridad a NOT_FOUND_ERROR sobre TODA la
        matriz, sin restringirlo a 'codigo_queja' -- reclasificaba también errores
        de VALIDACIÓN genuinos de otros campos catálogo (departamento_cod,
        municipio_cod, etc.) que la SFC también puede reportar con la misma frase
        "does not exist" (mismo patrón SlugRelatedField de Django REST Framework)
        para SUS PROPIOS valores inválidos -- sin relación alguna con que el CASO
        no exista. Ese tipo de mensaje debe seguir respetando el orden real de la
        matriz (donde 'departamento_cod' ya mapea correctamente a VALIDATION_ERROR,
        el tipo correcto para un valor de catálogo inválido).
        """
        reglas = [
            {"subcadena": "departamento_cod", "tipo": "VALIDATION_ERROR", "accion": "Verificar el departamento."},
            {"subcadena": "does not exist", "tipo": "NOT_FOUND_ERROR", "accion": "Autorrecuperar (self-healing)."},
        ]
        with self._mockear_matriz(reglas), \
             patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(
                    400, '{"departamento_cod": ["Object with departamento_cod=999 does not exist."]}'
                )
            self.assertEqual(ctx.exception.error_type, "VALIDATION_ERROR")

    async def test_already_exists_genuino_sigue_funcionando_con_not_found_en_la_matriz(self):
        """Contraprueba: un mensaje de 'ya existe' genuino (sin ninguna de las frases
        NOT_FOUND) debe seguir clasificando ALREADY_EXISTS aunque la matriz también
        tenga reglas NOT_FOUND_ERROR -- la prioridad no debe generar falsos negativos
        para el caso verdadero."""
        reglas = [
            {"subcadena": "does not exist", "tipo": "NOT_FOUND_ERROR", "accion": "Autorrecuperar."},
            {"subcadena": "codigo_queja", "tipo": "ALREADY_EXISTS", "accion": "El código ya existe."},
        ]
        with self._mockear_matriz(reglas), \
             patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(
                    400, '{"codigo_queja": ["queja with this codigo queja already exists."]}'
                )
            self.assertEqual(ctx.exception.error_type, "ALREADY_EXISTS")

    async def test_regla_con_tipo_unknown_sfc_error_explicito_tambien_notifica(self):
        """
        🟢 Caso límite (exceptions.py:320): incluso si una regla SÍ matchea pero su
        'tipo' configurado en la matriz es literalmente "UNKNOWN_SFC_ERROR", igual se
        dispara la alerta -- el catálogo no debería usar ese tipo como respuesta real,
        así que si ocurre, sigue tratándose como no clasificado.
        """
        reglas = [{"subcadena": "algo raro", "tipo": "UNKNOWN_SFC_ERROR", "accion": "Revisar logs."}]
        with self._mockear_matriz(reglas), \
             patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock) as mock_alert:
            with self.assertRaises(SfcIntegrationException):
                await SfcErrorTranslator.procesar_y_lanzar(400, '{"message": "algo raro pasó"}')
            await asyncio.sleep(0)
            mock_alert.assert_called_once()


class TestProcesarYLanzarContraMatrizLocalReal(unittest.IsolatedAsyncioTestCase):
    """
    A diferencia de TestProcesarYLanzar (matriz mockeada a medida), esta clase
    carga la matriz REAL de errores_sfc.json -- el respaldo local que usa
    producción cuando Google Sheets no está disponible/configurado, y que
    comparte el mismo orden de reglas que la fuente primaria (Google Sheets).
    Ancla el comportamiento contra los 46 registros reales, no una matriz de
    prueba simplificada que podría no reproducir la colisión real.
    """

    def setUp(self):
        self._matriz_original = SfcErrorTranslator.MATRIZ_ERRORES_TEXTO
        SfcErrorTranslator.cargar_matriz_local()

    def tearDown(self):
        SfcErrorTranslator.MATRIZ_ERRORES_TEXTO = self._matriz_original

    async def test_respuesta_real_add_file_queja_no_existe_clasifica_not_found(self):
        """Body textual exacto de la colección Postman oficial de la SFC: 400 Add
        File cuando el codigo_queja referenciado no existe todavía."""
        with patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(
                    400, '{"codigo_queja": ["Object with codigo_queja=111635888992248094 does not exist."]}'
                )
        self.assertEqual(ctx.exception.error_type, "NOT_FOUND_ERROR")

    async def test_respuesta_real_ya_existe_queja_sigue_clasificando_already_exists(self):
        with patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(
                    400, '{"message": "Ya existe queja con este codigo queja"}'
                )
        self.assertEqual(ctx.exception.error_type, "ALREADY_EXISTS")

    async def test_respuesta_real_departamento_cod_invalido_sigue_clasificando_validation_error(self):
        """
        🔴 Regresión propia (encontrada y corregida el mismo día): reproduce contra
        la matriz LOCAL REAL (no una matriz de prueba simplificada) el mismo patrón
        DRF SlugRelatedField ("Object with X=Y does not exist.") pero para
        departamento_cod -- un valor de catálogo inválido, sin ninguna relación con
        que el caso no exista. Debe seguir clasificando VALIDATION_ERROR.
        """
        with patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(
                    400, '{"departamento_cod": ["Object with departamento_cod=999 does not exist."]}'
                )
        self.assertEqual(ctx.exception.error_type, "VALIDATION_ERROR")

    async def test_556240_incrustado_en_smart_code_no_se_clasifica_duplicate_file(self):
        """
        🔴 Vulnerabilidad reportada (auditoría adversarial v7, 2026-08-26):
        '556240'->DUPLICATE_FILE es una regla de un solo token (código corto), sin
        ancla -- coincide con CUALQUIER texto que la contenga como substring,
        incluido un Smart_Code__c que la contenga incrustada en su parte numérica
        (`^[a-zA-Z0-9_-]{1,30}$` lo permite, sea por azar o a propósito). Reproducido:
        un error genérico de la SFC sin ninguna relación con archivos, sobre un caso
        cuyo identificador contiene esos dígitos, se clasificaba DUPLICATE_FILE --
        _manejar_duplicado_o_cerrado lo habría absorbido como éxito, marcando el
        checkpoint como entregado para un archivo que nunca se transmitió.
        """
        with patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(
                    400, '{"detail": "Unexpected server condition XYZ-1286SEQ556240ABC"}'
                )
        self.assertNotEqual(ctx.exception.error_type, "DUPLICATE_FILE")

    async def test_556240_genuino_sigue_clasificando_duplicate_file(self):
        """Contraprueba: el código 556240 genuino (rodeado de no-alfanuméricos, el
        formato real documentado por la SFC) debe seguir funcionando."""
        with patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException) as ctx:
                await SfcErrorTranslator.procesar_y_lanzar(
                    400, '{"detail": "El archivo ya existe para esta queja (codigo 556240)"}'
                )
        self.assertEqual(ctx.exception.error_type, "DUPLICATE_FILE")


if __name__ == "__main__":
    unittest.main()
