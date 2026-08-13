# tests/test_throttling_classification.py
import unittest
from unittest.mock import AsyncMock
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


if __name__ == "__main__":
    unittest.main()