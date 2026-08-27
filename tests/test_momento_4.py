# tests/test_momento_4.py
import unittest
from unittest.mock import AsyncMock, patch
from app.core.config import settings
from app.services.email_service import EmailAlertService
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

    async def test_usuario_duplicado_por_numero_id_cf_entre_paginas_se_deduplica(self):
        """Hallazgo 50: el mismo numero_id_CF puede repetirse entre páginas
        (paginación por cursor 'next', el backlog cambia entre fetch y fetch) --
        debe deduplicarse, no entregarse dos veces al CRM."""
        usuario = {
            "numero_id_CF": "1018222333", "tipo_id_CF": 1,
            "nombre": "Juan", "apellido": "Pérez", "correo": "juan.perez@example.com"
        }
        pagina_1 = {"Response": {"count": 2, "next": "https://x?page=2", "results": [usuario]}}
        pagina_2 = {"Response": {"count": 2, "next": None, "results": [dict(usuario)]}}
        self.sfc_client_mock.fetch_usuarios_pagina.side_effect = [pagina_1, pagina_2]

        resultado = await self.user_sync.sincronizar_usuarios()

        self.assertEqual(resultado["total_exitosos"], 1)
        self.assertEqual(resultado["total_procesados"], 2)
        self.assertEqual(len(resultado["usuarios"]), 1)

    async def test_registro_no_es_dict_se_reporta_como_fallido_sin_crashear(self):
        """Un elemento de 'results' que no es un dict (SFC devolvió algo
        inesperado) debe caer en failed_items -- no debe tumbar el resto del
        lote ni la sincronización completa."""
        pagina = {
            "Response": {
                "count": 2, "next": None,
                "results": [
                    "esto-no-es-un-dict",
                    {"numero_id_CF": "1018222333", "tipo_id_CF": 1, "nombre": "Juan", "apellido": "Pérez", "correo": "j@x.com"},
                ]
            }
        }
        self.sfc_client_mock.fetch_usuarios_pagina.side_effect = [pagina]

        resultado = await self.user_sync.sincronizar_usuarios()

        self.assertEqual(resultado["status"], "partial")
        self.assertEqual(resultado["total_exitosos"], 1)
        self.assertEqual(resultado["total_fallidos"], 1)
        self.assertEqual(resultado["failed_items"][0]["numero_id_CF"], "DESCONOCIDO")

    async def test_mapeo_devuelve_vacio_o_sin_id_number_se_reporta_como_fallido(self):
        """Si SfcSalesforceMapper.sfc_user_payload_to_db_dict falla en silencio
        (retorna {} o algo sin 'id_number__c'), debe tratarse como fallo
        explícito, no colarse como un usuario válido pero vacío."""
        from unittest.mock import patch as _patch
        from app.core.mapping import SfcSalesforceMapper

        pagina = {
            "Response": {
                "count": 1, "next": None,
                "results": [{"numero_id_CF": "1018222333", "tipo_id_CF": 1, "nombre": "Juan"}]
            }
        }
        self.sfc_client_mock.fetch_usuarios_pagina.side_effect = [pagina]

        with _patch.object(SfcSalesforceMapper, "sfc_user_payload_to_db_dict", return_value={}):
            resultado = await self.user_sync.sincronizar_usuarios()

        self.assertEqual(resultado["status"], "error")
        self.assertEqual(resultado["total_exitosos"], 0)
        self.assertEqual(resultado["total_fallidos"], 1)
        self.assertEqual(resultado["failed_items"][0]["numero_id_CF"], "1018222333")

    async def test_todos_los_usuarios_fallan_status_error_no_partial(self):
        """Cuando NINGÚN usuario se procesa con éxito, el status debe ser
        'error' (no 'partial', que implicaría que algo sí se aprovechó)."""
        pagina = {
            "Response": {"count": 2, "next": None, "results": ["no-es-dict", 12345]}
        }
        self.sfc_client_mock.fetch_usuarios_pagina.side_effect = [pagina]

        resultado = await self.user_sync.sincronizar_usuarios()

        self.assertEqual(resultado["status"], "error")
        self.assertEqual(resultado["total_exitosos"], 0)
        self.assertEqual(resultado["total_fallidos"], 2)

    async def test_extraer_lista_usuarios_objeto_unico_sin_envoltorio_results(self):
        """La SFC puede devolver un único registro de usuario SIN envolverlo en
        'results' (forma vista en producción para ciertos endpoints) --
        _extraer_lista_usuarios debe reconocerlo por la presencia de
        'numero_id_CF' y tratarlo como una lista de un elemento."""
        pagina = {"Response": {"numero_id_CF": "1018222333", "tipo_id_CF": 1, "nombre": "Juan", "apellido": "P", "correo": "j@x.com"}}
        self.sfc_client_mock.fetch_usuarios_pagina.side_effect = [pagina]

        resultado = await self.user_sync.sincronizar_usuarios()

        self.assertEqual(resultado["total_exitosos"], 1)

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

    async def test_confirmar_recepcion_ack_usuarios_parcial_por_respuesta_de_la_sfc(self):
        """La SFC puede aceptar unos IDs y reportar otros como error en el CUERPO
        de la respuesta (numero_id_CF_error), sin lanzar ninguna excepción --
        status debe ser 'partial', distinto del camino de excepción ya probado."""
        self.sfc_client_mock.send_user_ack_batch.return_value = {
            "Response": {"numero_id_CF_error": ["1018111222"]}
        }

        resultado = await self.user_sync.confirmar_recepcion_ack_usuarios(
            numeros_id_cf=["1018111222", "1018333444"]
        )

        self.assertEqual(resultado["status"], "partial")
        self.assertEqual(resultado["confirmados"], 1)
        self.assertEqual(resultado["ids_error"], ["1018111222"])
        self.assertEqual(resultado["ids_procesados"], ["1018333444"])

    async def test_confirmar_recepcion_ack_usuarios_deduplica_ids_repetidos_y_vacios(self):
        """Hallazgo 50: IDs repetidos o vacíos en la solicitud del CRM se
        deduplican/limpian antes de llamar a la SFC -- no se envían de más."""
        self.sfc_client_mock.send_user_ack_batch.return_value = {"Response": {"numero_id_CF_error": []}}

        resultado = await self.user_sync.confirmar_recepcion_ack_usuarios(
            numeros_id_cf=["1018111222", "1018111222", "  ", "", "1018333444"]
        )

        self.assertEqual(resultado["confirmados"], 2)
        self.sfc_client_mock.send_user_ack_batch.assert_called_once_with(["1018111222", "1018333444"])

    async def test_confirmar_recepcion_ack_usuarios_todos_vacios_tras_limpieza(self):
        """Si tras deduplicar/limpiar no queda ningún ID válido, debe devolver
        'warning' sin llegar a llamar a la SFC -- no un lote vacío."""
        resultado = await self.user_sync.confirmar_recepcion_ack_usuarios(
            numeros_id_cf=["", "   ", ""]
        )

        self.assertEqual(resultado["status"], "warning")
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

    async def test_paginacion_se_corta_al_alcanzar_el_limite_de_paginas(self):
        """
        P1-12: si el enlace 'next' de la SFC nunca se agota, el ciclo de
        paginación de M4 debe cortarse por límite de páginas en vez de correr
        indefinidamente, marcar el resultado como parcial y alertar por correo.
        """
        def _pagina_infinita(url=None):
            return {
                "Response": {
                    "count": 1,
                    "next": "https://sfc.gov.co/api/usuarios/info/?page=siguiente",
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

        self.sfc_client_mock.fetch_usuarios_pagina.side_effect = _pagina_infinita

        with patch.object(settings, "SFC_SYNC_MAX_PAGINAS", 3), \
             patch.object(EmailAlertService, "notificar_falla_infraestructura", new_callable=AsyncMock) as mock_alerta:
            resultado = await self.user_sync.sincronizar_usuarios()

        self.assertEqual(self.sfc_client_mock.fetch_usuarios_pagina.call_count, 3)
        self.assertEqual(resultado["status"], "partial")
        self.assertTrue(resultado["paginacion_incompleta"])
        mock_alerta.assert_called_once()


if __name__ == "__main__":
    unittest.main()