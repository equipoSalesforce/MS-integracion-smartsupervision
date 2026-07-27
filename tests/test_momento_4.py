import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, Any

from app.services.momento_4_sync import UserSync
from app.core.exceptions import SfcIntegrationException


class TestMomento4Service(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.sfc_client_mock = AsyncMock()
        self.user_sync = UserSync(sfc_client=self.sfc_client_mock)

    async def test_sincronizar_usuarios_paginado_exitoso(self):
        """Valida la descarga paginada y el mapeo en memoria de usuarios."""
        # Simulamos 2 páginas de la SFC
        pagina_1 = {
            "Response": {
                "count": 2,
                "next": "https://sfc.gov.co/api/usuarios/info/?page=2",
                "results": [
                    {
                        "numero_id_CF": "1018222333",
                        "tipo_id_CF": 1,
                        "nombre": "Juan",
                        "apellido": "Pérez",
                        "correo": "juan.perez@example.com"
                    }
                ]
            }
        }
        pagina_2 = {
            "Response": {
                "count": 2,
                "next": None,
                "results": [
                    {
                        "numero_id_CF": "1020444555",
                        "tipo_id_CF": 2,
                        "nombre": "Maria",
                        "apellido": "Gomez",
                        "correo": "maria.gomez@example.com"
                    }
                ]
            }
        }

        self.sfc_client_mock.fetch_usuarios_pagina.side_effect = [pagina_1, pagina_2]

        # Ejecución
        resultado = await self.user_sync.sincronizar_usuarios()

        # Aserciones
        self.assertEqual(len(resultado), 2)
        self.assertEqual(resultado[0]["id_number__c"], "1018222333")
        self.assertEqual(resultado[0]["SuppliedName"], "Juan Pérez")
        self.assertEqual(resultado[1]["id_number__c"], "1020444555")
        self.assertEqual(self.sfc_client_mock.fetch_usuarios_pagina.call_count, 2)

    async def test_confirmar_recepcion_ack_usuarios_en_lotes(self):
        """Valida que un lote grande (>100 IDs) se divida en chunks de máximo 100 para la SFC."""
        # Generamos 250 IDs de usuario
        ids_usuarios = [f"ID_CF_{i}" for i in range(250)]
        self.sfc_client_mock.send_user_ack_batch.return_value = {"Response": {"message": "ID CF actualizado"}}

        resultado = await self.user_sync.confirmar_recepcion_ack_usuarios(numeros_id_cf=ids_usuarios)

        # Aserciones
        self.assertEqual(resultado["status"], "success")
        self.assertEqual(resultado["confirmados"], 250)
        # Se deben haber realizado 3 llamadas HTTP POST (100, 100 y 50)
        self.assertEqual(self.sfc_client_mock.send_user_ack_batch.call_count, 3)

    async def test_confirmar_recepcion_ack_usuarios_vacio(self):
        """Valida el comportamiento defensivo al enviar una lista vacía de IDs."""
        resultado = await self.user_sync.confirmar_recepcion_ack_usuarios(numeros_id_cf=[])

        self.assertEqual(resultado["status"], "warning")
        self.assertEqual(resultado["confirmados"], 0)
        self.sfc_client_mock.send_user_ack_batch.assert_not_called()

    async def test_confirmar_recepcion_ack_usuarios_falla_relanza_excepcion(self):
        """Valida que si falla un lote en el ACK, se registre el log y se relance la excepción."""
        ids_usuarios = ["1018111222"]
        self.sfc_client_mock.send_user_ack_batch.side_effect = Exception("Error 500 SFC")

        with self.assertRaises(Exception):
            await self.user_sync.confirmar_recepcion_ack_usuarios(numeros_id_cf=ids_usuarios)


if __name__ == "__main__":
    unittest.main()