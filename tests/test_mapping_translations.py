# tests/test_mapping_translations.py
"""
Cobertura de ramas de app/core/mapping.py no cubiertas por test_mapper.py
(que sólo prueba el camino feliz de crm_entity_to_sfc_payload/
sfc_payload_to_db_dict contra un dict): el path de entidad-como-objeto
(_get_sf_field_value_from_object, nunca ejercitado porque el fixture de
test_mapper.py siempre es un dict), el hard-fail de _lookup_or_fail para
catálogos regulatorios sin default, el soft-fail con default aguas abajo,
los casos especiales de traducción CRM->SFC, y las ramas de error de
sfc_payload_to_db_dict/sfc_user_payload_to_db_dict/crm_entity_to_sfc_momento3_payload.

No depende de Redis ni de red -- SfcSalesforceMapper.CATALOGOS se carga
localmente desde los JSON del repo (cargar_catalogos_local), igual que en
producción cuando Google Sheets no está disponible.
"""
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from app.core.exceptions import SfcIntegrationException
from app.core.mapping import SfcSalesforceMapper


class _MapperTestCase(unittest.TestCase):

    def setUp(self):
        self.patcher = patch("app.core.mapping.settings")
        self.mock_settings = self.patcher.start()
        self.mock_settings.SFC_TIPO_ENTIDAD = 1
        self.mock_settings.SFC_ENTIDAD_COD = "423"
        if not SfcSalesforceMapper.CATALOGOS:
            SfcSalesforceMapper.cargar_catalogos_local()

    def tearDown(self):
        self.patcher.stop()


class TestGetSfFieldValueFromObject(_MapperTestCase):
    """El fixture de test_mapper.py siempre pasa un dict -- crm_entity_to_sfc_payload
    también acepta un objeto con atributos (p.ej. un SObject de Salesforce), camino
    que hasta ahora nunca se ejercitaba."""

    def test_momento_2_con_entidad_como_objeto_de_atributos(self):
        entidad = SimpleNamespace(
            Smart_Code__c="16551509974606",
            CreatedDate="2026-07-16T12:00:00",
            SuppliedName="Camila Salas",
            id_number__c="1040011014",
            Departamento__c="Bogotá D.C.",
            SC_municipio__c="Bogotá D.C.",
            Description="Prueba de queja vía objeto.",
            smart_anexo_queja__c=True,
            canal__c="Internet",
            Ente_de_control__c="Otros",
            tipo_de_persona__c="B2C",
            Status="New",
        )

        resultado = SfcSalesforceMapper.crm_entity_to_sfc_payload(entidad, momento=2)

        self.assertEqual(resultado["nombres"], "Camila Salas")
        self.assertEqual(resultado["numero_id_CF"], "1040011014")
        self.assertEqual(resultado["texto_queja"], "Prueba de queja vía objeto.")

    def test_atributo_ausente_retorna_none(self):
        entidad = SimpleNamespace(SuppliedName="Solo Nombre")
        self.assertIsNone(SfcSalesforceMapper._get_sf_field_value(entidad, "campo_inexistente"))

    def test_smart_code_busca_alias_case_id_en_objeto(self):
        entidad = SimpleNamespace(Case_id="CASE-0001")
        self.assertEqual(SfcSalesforceMapper._get_sf_field_value(entidad, "Smart_Code__c"), "CASE-0001")


class TestLookupOrFailHardFail(_MapperTestCase):

    def test_valor_no_reconocido_en_catalogo_regulatorio_lanza_excepcion(self):
        with self.assertRaises(SfcIntegrationException) as ctx:
            SfcSalesforceMapper._translate_value_to_sfc("sc_genero__c", "Un Valor Que No Existe En El Catalogo")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.sfc_field, "sc_genero__c")


class TestTraducirViaCatalogoSfcSoftFail(_MapperTestCase):

    def test_valor_no_reconocido_en_catalogo_con_default_aguas_abajo_retorna_none(self):
        # Ente_de_control__c está en sf_to_cat_soft -- un valor no reconocido no
        # rechaza el caso, retorna None (el default de negocio se aplica más arriba).
        resultado = SfcSalesforceMapper._translate_value_to_sfc("Ente_de_control__c", "Valor Inventado XYZ")
        self.assertIsNone(resultado)


class TestTraducirValorEspecialASfc(_MapperTestCase):

    def test_tutela_valor_no_reconocido_usa_default_no(self):
        self.assertEqual(SfcSalesforceMapper._traducir_valor_especial_a_sfc("Tutela__c", "tal vez"), 2)

    def test_sin_respuesta_final_si_retorna_true(self):
        self.assertTrue(SfcSalesforceMapper._traducir_valor_especial_a_sfc("sinRespuestaFinal__c", "Si"))

    def test_sin_respuesta_final_no_retorna_false(self):
        self.assertFalse(SfcSalesforceMapper._traducir_valor_especial_a_sfc("sinRespuestaFinal__c", "No"))


class TestSafeInt(_MapperTestCase):

    def test_valor_no_convertible_retorna_default(self):
        self.assertEqual(SfcSalesforceMapper._safe_int("no-es-un-numero", default=99), 99)

    def test_none_retorna_default(self):
        self.assertIsNone(SfcSalesforceMapper._safe_int(None))


class TestSfcPayloadToDbDictRamasDeError(_MapperTestCase):

    def test_fecha_creacion_con_formato_invalido_se_usa_tal_cual(self):
        resultado = SfcSalesforceMapper.sfc_payload_to_db_dict({"fecha_creacion": "no-es-una-fecha-valida"})
        self.assertEqual(resultado["CreatedDate"], "no-es-una-fecha-valida")

    def test_tipo_id_3_y_tipo_persona_2_resuelve_a_nit(self):
        resultado = SfcSalesforceMapper.sfc_payload_to_db_dict({"tipo_id_CF": "3", "tipo_persona": "2"})
        self.assertEqual(resultado["SC_id_type__c"], "NIT")

    def test_tipo_id_3_y_tipo_persona_distinto_de_2_resuelve_a_rut(self):
        resultado = SfcSalesforceMapper.sfc_payload_to_db_dict({"tipo_id_CF": "3", "tipo_persona": "1"})
        self.assertEqual(resultado["SC_id_type__c"], "RUT")


class TestSfcUserPayloadToDbDict(_MapperTestCase):

    def test_payload_no_dict_lanza_value_error(self):
        with self.assertRaises(ValueError):
            SfcSalesforceMapper.sfc_user_payload_to_db_dict(["no", "es", "un", "dict"])

    def test_sin_numero_id_cf_lanza_value_error(self):
        with self.assertRaises(ValueError):
            SfcSalesforceMapper.sfc_user_payload_to_db_dict({"nombres": "Camila"})

    def test_valores_none_se_omiten(self):
        resultado = SfcSalesforceMapper.sfc_user_payload_to_db_dict({
            "numero_id_CF": "123", "correo": None,
        })
        self.assertNotIn("SuppliedEmail", resultado)

    def test_fecha_nacimiento_valida_se_normaliza_a_iso(self):
        resultado = SfcSalesforceMapper.sfc_user_payload_to_db_dict({
            "numero_id_CF": "123", "fecha_nacimiento": "1995-04-12",
        })
        self.assertTrue(resultado.get("fecha_nacimiento__c", "").startswith("1995-04-12"))

    def test_fecha_nacimiento_invalida_se_usa_tal_cual(self):
        resultado = SfcSalesforceMapper.sfc_user_payload_to_db_dict({
            "numero_id_CF": "123", "fecha_nacimiento": "fecha-invalida",
        })
        self.assertEqual(resultado.get("fecha_nacimiento__c"), "fecha-invalida")


class TestCrmEntityToSfcMomento3Payload(_MapperTestCase):

    def _entidad_base(self, **overrides):
        base = dict(
            Smart_Code__c="16551509974606",
            Status="In progress",
        )
        base.update(overrides)
        return base

    def test_closed_date_como_objeto_date_puro(self):
        resultado = SfcSalesforceMapper.crm_entity_to_sfc_payload(
            self._entidad_base(ClosedDate=date(2026, 7, 16)), momento=3
        )
        self.assertTrue(resultado["fecha_cierre"].startswith("2026-07-16T"))

    def test_closed_date_como_datetime(self):
        resultado = SfcSalesforceMapper.crm_entity_to_sfc_payload(
            self._entidad_base(ClosedDate=datetime(2026, 7, 16, 10, 30, 0)), momento=3
        )
        self.assertEqual(resultado["fecha_cierre"], "2026-07-16T10:30:00")

    def test_cierre_sin_favorabilidad_ni_aceptacion_lanza_value_error(self):
        with self.assertRaises(ValueError):
            SfcSalesforceMapper.crm_entity_to_sfc_payload(self._entidad_base(Status="Closed"), momento=3)

    def test_cierre_con_favorabilidad_y_aceptacion_resuelve_estado_4(self):
        resultado = SfcSalesforceMapper.crm_entity_to_sfc_payload(
            self._entidad_base(
                Status="Closed",
                Favorabilidad__c="Favorable",
                Aceptacion__c="Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            ),
            momento=3,
        )
        self.assertEqual(resultado["estado_cod"], 4)


class TestFalloDeRespaldoLocalEsCritico(unittest.TestCase):
    """
    🔴 FIX (hallazgo propio, 2026-08-27): a diferencia de una falla de Google Sheets
    (que sí dispara EmailAlertService.notificar_catalogo_stale si la caché envejece),
    si el respaldo LOCAL (catalogos_sfc_crm.json / divipola_sfc_crm.json, empaquetado
    en la imagen) también falla al cargar, CATALOGOS/DEPT_DIVIPOLA quedan vacíos para
    siempre sin ninguna alerta -- cada queja subsiguiente se rechaza como un 400
    CRM_PAYLOAD_VALIDATION_ERROR ordinario, disfrazando una caída total del servicio.
    Se sube a logger.critical (antes era logger.error) para que sea visible de
    inmediato en CloudWatch en vez de mezclarse con errores de negocio ordinarios."""

    def test_fallo_cargando_catalogos_local_se_loggea_como_critical(self):
        with patch("app.core.mapping.open", side_effect=OSError("disco corrupto")), \
             self.assertLogs("app.core.mapping", level="CRITICAL") as logs:
            SfcSalesforceMapper.cargar_catalogos_local(force=True)

        self.assertTrue(any("catalogos_sfc_crm.json" in msg for msg in logs.output))

    def test_fallo_cargando_divipola_local_se_loggea_como_critical(self):
        with patch("app.core.mapping.open", side_effect=OSError("disco corrupto")), \
             self.assertLogs("app.core.mapping", level="CRITICAL") as logs:
            SfcSalesforceMapper.cargar_divipola(force=True)

        self.assertTrue(any("divipola_sfc_crm.json" in msg for msg in logs.output))


if __name__ == "__main__":
    unittest.main()
