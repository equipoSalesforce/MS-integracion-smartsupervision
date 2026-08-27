# tests/test_throttling_classification.py
import unittest
from unittest.mock import AsyncMock, patch
from app.integrations.sfc_client import handle_sfc_throttling
from app.core.exceptions import SfcIntegrationException


class TestThrottlingClassification(unittest.IsolatedAsyncioTestCase):

    async def test_throttling_429_ejecuta_reintento_local(self):
        """Verifica que respuestas 429 activen la lógica de reintento local."""
        mock_func = AsyncMock()
        # Primer intento lanza 429, segundo intento retorna éxito
        mock_func.side_effect = [
            SfcIntegrationException(
                status_code=429,
                error_type="THROTTLED_ERROR",
                sfc_field=None,
                raw_message="Quota exceeded",
                crm_action="Espere unos segundos e intente de nuevo."
            ),
            {"status": "success"}
        ]

        decorated = handle_sfc_throttling(mock_func)
        res = await decorated()

        self.assertEqual(res, {"status": "success"})
        self.assertEqual(mock_func.call_count, 2)

    async def test_infraestructura_error_500_se_eleva_sin_mini_reintento(self):
        """Verifica que INFRASTRUCTURE_ERROR / 500 NO active el bucle de throttling y se eleve de inmediato."""
        mock_func = AsyncMock()
        mock_func.side_effect = SfcIntegrationException(
            status_code=500,
            error_type="INFRASTRUCTURE_ERROR",
            sfc_field=None,
            raw_message="Internal Server Error / Connection refused",
            crm_action="Reintente la operación más tarde."
        )

        decorated = handle_sfc_throttling(mock_func)

        with self.assertRaises(SfcIntegrationException) as ctx:
            await decorated()

        # Debe llamarse una sola vez y re-elevar la excepción de infraestructura sin reintentar
        self.assertEqual(mock_func.call_count, 1)
        self.assertEqual(ctx.exception.status_code, 500)


class TestThrottlingClassificationSubcadenaEmbebida(unittest.IsolatedAsyncioTestCase):
    """
    🔴 FIX (hallazgo propio, 2026-08-27): _es_respuesta_throttled comparaba
    "throttled"/"quota"/"resource_exhausted" contra raw_message SIN ancla -- el
    mismo defecto que la revisión externa v8 encontró (y se corrigió) en
    SfcErrorTranslator._coincide (§X2/Y2) y en s3_service._manejar_duplicado_o_
    cerrado (§X3), pero en un archivo que esas rondas no revisaron. raw_message
    puede reflejar un identificador del request (ver §X1/Y1: la SFC repite
    codigo_queja en sus mensajes de error de "does not exist"), así que un error
    de NEGOCIO real cuyo identificador contenga una de esas palabras embebida se
    confundía con una respuesta de throttling -- disparando mini-reintentos y
    delay sobre un fallo que iba a repetirse idéntico.
    """

    async def test_error_de_negocio_con_quota_embebido_en_identificador_no_se_confunde_con_throttling(self):
        mock_func = AsyncMock()
        mock_func.side_effect = SfcIntegrationException(
            status_code=400,
            error_type="ALREADY_EXISTS",
            sfc_field="codigo_queja",
            raw_message="Object with codigo_queja=SC-QUOTA123-01 does not exist.",
            crm_action="Revisar el código de queja."
        )

        decorated = handle_sfc_throttling(mock_func)

        with self.assertRaises(SfcIntegrationException) as ctx:
            await decorated()

        self.assertEqual(mock_func.call_count, 1, "No debe reintentar: es un error de negocio, no throttling.")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_error_de_negocio_con_throttled_embebido_no_se_confunde_con_throttling(self):
        mock_func = AsyncMock()
        mock_func.side_effect = SfcIntegrationException(
            status_code=400,
            error_type="VALIDATION_ERROR",
            sfc_field="canal_cod",
            raw_message="Object with canal_cod=THROTTLED_99 does not exist.",
            crm_action="Revisar el catálogo de canal."
        )

        decorated = handle_sfc_throttling(mock_func)

        with self.assertRaises(SfcIntegrationException):
            await decorated()

        self.assertEqual(mock_func.call_count, 1, "No debe reintentar: es un error de negocio, no throttling.")

    async def test_error_de_negocio_con_resource_exhausted_embebido_no_se_confunde_con_throttling(self):
        mock_func = AsyncMock()
        mock_func.side_effect = SfcIntegrationException(
            status_code=400,
            error_type="VALIDATION_ERROR",
            sfc_field="producto_cod",
            raw_message="Object with producto_cod=RESOURCE_EXHAUSTED_1 does not exist.",
            crm_action="Revisar el catálogo de producto."
        )

        decorated = handle_sfc_throttling(mock_func)

        with self.assertRaises(SfcIntegrationException):
            await decorated()

        self.assertEqual(mock_func.call_count, 1, "No debe reintentar: es un error de negocio, no throttling.")

    async def test_mensaje_real_de_quota_como_palabra_completa_sigue_activando_el_reintento(self):
        """Contraprueba: el ancla no debe romper la detección real de throttling."""
        mock_func = AsyncMock()
        mock_func.side_effect = [
            SfcIntegrationException(
                status_code=400,
                error_type=None,
                sfc_field=None,
                raw_message="Daily quota exceeded, please retry later.",
                crm_action="Espere unos segundos e intente de nuevo."
            ),
            {"status": "success"}
        ]

        decorated = handle_sfc_throttling(mock_func)
        res = await decorated()

        self.assertEqual(res, {"status": "success"})
        self.assertEqual(mock_func.call_count, 2)


class TestThrottlingClassificationMetricaEmf(unittest.IsolatedAsyncioTestCase):
    """Métrica EMF SSV/ThrottlingSfc (propuesta de observabilidad CX)."""

    async def test_throttling_con_reintento_disponible_emite_metrica_resultado_retried(self):
        llamadas = {"n": 0}

        async def funcion_sfc(*args, **kwargs):
            llamadas["n"] += 1
            if llamadas["n"] == 1:
                raise SfcIntegrationException(
                    status_code=429, error_type="THROTTLED_ERROR", sfc_field=None,
                    raw_message="Quota exceeded", crm_action="Espere unos segundos e intente de nuevo."
                )
            return {"status": "success"}

        with patch("app.integrations.sfc_client.emit_emf_metric") as mock_emit, \
                patch("app.integrations.sfc_client.asyncio.sleep", new=AsyncMock()):
            decorated = handle_sfc_throttling(funcion_sfc)
            await decorated()

        llamadas_ssv = [c for c in mock_emit.call_args_list if c.kwargs["namespace"] == "SSV/ThrottlingSfc"]
        self.assertEqual(len(llamadas_ssv), 1)
        self.assertEqual(llamadas_ssv[0].kwargs["dimensions"]["resultado"], "retried")
        self.assertEqual(llamadas_ssv[0].kwargs["dimensions"]["endpoint"], "funcion_sfc")

    async def test_throttling_reintentos_agotados_emite_metrica_resultado_exhausted(self):
        async def funcion_sfc(*args, **kwargs):
            raise SfcIntegrationException(
                status_code=429, error_type="THROTTLED_ERROR", sfc_field=None,
                raw_message="Quota exceeded", crm_action="Espere unos segundos e intente de nuevo."
            )

        with patch("app.integrations.sfc_client.emit_emf_metric") as mock_emit, \
                patch("app.integrations.sfc_client.settings.SFC_MINI_RETRY_ATTEMPTS", 0):
            decorated = handle_sfc_throttling(funcion_sfc)
            with self.assertRaises(SfcIntegrationException):
                await decorated()

        llamadas_ssv = [c for c in mock_emit.call_args_list if c.kwargs["namespace"] == "SSV/ThrottlingSfc"]
        self.assertEqual(len(llamadas_ssv), 1)
        self.assertEqual(llamadas_ssv[0].kwargs["dimensions"]["resultado"], "exhausted")

    async def test_error_de_infraestructura_no_emite_metrica_de_throttling(self):
        async def funcion_sfc(*args, **kwargs):
            raise SfcIntegrationException(
                status_code=500, error_type="INFRASTRUCTURE_ERROR", sfc_field=None,
                raw_message="Internal Server Error", crm_action="Reintente la operación más tarde."
            )

        with patch("app.integrations.sfc_client.emit_emf_metric") as mock_emit:
            decorated = handle_sfc_throttling(funcion_sfc)
            with self.assertRaises(SfcIntegrationException):
                await decorated()

        llamadas_ssv = [c for c in mock_emit.call_args_list if c.kwargs["namespace"] == "SSV/ThrottlingSfc"]
        self.assertEqual(llamadas_ssv, [])


if __name__ == "__main__":
    unittest.main()