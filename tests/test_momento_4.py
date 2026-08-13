# tests/test_momento_4.py
import unittest
from unittest.mock import AsyncMock
from app.services.momento_4_sync import UserSync


class TestMomento4Service(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.sfc_client_mock = AsyncMock()
        self.user_sync = UserSync(sfc_client=self.sfc_client_mock)

    async def test_sincronizar_usuarios_paginado_exitoso(self):
        """Valida la descarga paginada y el mapeo en memoria de usuarios."""
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

        resultado = await self.user_sync.sincronizar_usuarios()

        # 🟢 FIX: Verificación sobre diccionario estructurado
        self.assertEqual(resultado["status"], "success")
        self.assertEqual(resultado["total_exitosos"], 2)
        
        usuarios = resultado["usuarios"]
        self.assertEqual(len(usuarios), 2)
        self.assertEqual(usuarios[0]["id_number__c"], "1018222333")
        self.assertEqual(usuarios[0]["SuppliedName"], "Juan Pérez")
        self.assertEqual(usuarios[1]["id_number__c"], "1020444555")
        self.assertEqual(self.sfc_client_mock.fetch_usuarios_pagina.call_count, 2)

    async def test_confirmar_recepcion_ack_usuarios_en_lotes(self):
        """Valida que un lote grande (>100 IDs) se divida en chunks de máximo 100 para la SFC."""
        ids_usuarios = [f"ID_CF_{i}" for i in range(250)]
        self.sfc_client_mock.send_user_ack_batch.return_value = {"Response": {"message": "ID CF actualizado"}}

        resultado = await self.user_sync.confirmar_recepcion_ack_usuarios(numeros_id_cf=ids_usuarios)

        self.assertEqual(resultado["status"], "success")
        self.assertEqual(resultado["confirmados"], 250)
        self.assertEqual(self.sfc_client_mock.send_user_ack_batch.call_count, 3)

    async def test_confirmar_recepcion_ack_usuarios_vacio(self):
        """Valida el comportamiento defensivo al enviar una lista vacía de IDs."""
        resultado = await self.user_sync.confirmar_recepcion_ack_usuarios(numeros_id_cf=[])

        self.assertEqual(resultado["status"], "warning")
        self.assertEqual(resultado["confirmados"], 0)
        self.sfc_client_mock.send_user_ack_batch.assert_not_called()

    async def test_confirmar_recepcion_ack_usuarios_falla_maneja_error_sin_crash(self):
        """Valida que si falla un lote en el ACK, se capture el error y se retorne la trazabilidad sin crash (Hallazgo 18)."""
        ids_usuarios = ["1018111222"]
        self.sfc_client_mock.send_user_ack_batch.side_effect = Exception("Error 500 SFC")

        # 🟢 FIX: Se aserta la resiliencia en lugar de exigir re-lanzamiento de excepción
        resultado = await self.user_sync.confirmar_recepcion_ack_usuarios(numeros_id_cf=ids_usuarios)

        self.assertEqual(resultado["status"], "error")
        self.assertEqual(resultado["confirmados"], 0)
        self.assertEqual(resultado["ids_error"], ids_usuarios)


if __name__ == "__main__":
    unittest.main()