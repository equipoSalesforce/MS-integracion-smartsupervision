# tests/test_queue_service_edge_cases.py
"""
Cobertura de ramas de QueueService no cubiertas por los demás
tests/test_queue_*.py (que se centran en las transiciones "felices" contra
Redis real): los guard-clauses de "sin Redis", las razones de error no
mapeadas explícitamente de cada script Lua (que deben relanzar en vez de
tragarse), los fallbacks para clientes de Redis sin sscan_iter/scan_iter,
y los manejadores de excepción de bajo nivel. Se usa Redis mockeado
(determinista) en vez de Redis real porque el objetivo es forzar
condiciones que un Redis real no produce a demanda (timeouts, respuestas
con "reason" arbitrario, ausencia de comandos).
"""
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.services.queue_service import QueueService, ColaItemRedis, QUEUE_PREFIX


def _item(**overrides) -> ColaItemRedis:
    data = {
        "id": 1, "smart_code": "SC-1", "intentos": 0, "max_intentos": 10,
        "version": 1, "payload_json": {"a": 1}, "correlation_id": "cid-1",
    }
    data.update(overrides)
    return ColaItemRedis(data)


class TestGuardsSinRedis(unittest.IsolatedAsyncioTestCase):

    def test_crear_pipeline_compatible_retorna_none_sin_redis(self):
        queue_service = QueueService(redis_client=None)
        self.assertIsNone(queue_service._crear_pipeline_compatible())

    async def test_contar_pendientes_retorna_cero_sin_redis(self):
        queue_service = QueueService(redis_client=None)
        self.assertEqual(await queue_service.contar_pendientes(), 0)

    async def test_encolar_despacho_sin_redis_lanza(self):
        queue_service = QueueService(redis_client=None)
        with self.assertRaises(RuntimeError):
            await queue_service.encolar_despacho(
                smart_code="SC-1", tipo_operacion="AUTO", payload_json={}, error_inicial="timeout"
            )

    async def test_reclamar_item_sin_redis_retorna_none(self):
        queue_service = QueueService(redis_client=None)
        self.assertIsNone(await queue_service.reclamar_item_para_procesamiento(registro_id=1, worker_id="w"))

    async def test_marcar_exitoso_sin_redis_lanza(self):
        queue_service = QueueService(redis_client=None)
        with self.assertRaises(RuntimeError):
            await queue_service.marcar_exitoso(registro_id=1, worker_id="w", expected_version=1)


class TestEncolarDespachoErrorDeRedis(unittest.IsolatedAsyncioTestCase):

    async def test_error_en_eval_se_loguea_y_se_relanza(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("redis caido"))
        queue_service = QueueService(redis_client=mock_redis)

        with self.assertRaises(ConnectionError):
            await queue_service.encolar_despacho(
                smart_code="SC-1", tipo_operacion="AUTO", payload_json={"a": 1}, error_inicial="timeout"
            )


class TestMarcarSfcCompletadoRazonNoMapeada(unittest.IsolatedAsyncioTestCase):

    async def test_razon_desconocida_lanza_runtime_error(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(return_value=json.dumps({"success": False, "reason": "algo_inesperado"}))
        queue_service = QueueService(redis_client=mock_redis)

        with self.assertRaises(RuntimeError):
            await queue_service.marcar_sfc_completado(
                1, worker_id="w", expected_version=1, smart_code="SC-1", payload_dict={"a": 1},
                max_intentos_persistencia=1
            )


class TestReclamarItemParaProcesamiento(unittest.IsolatedAsyncioTestCase):

    async def test_claim_no_exitoso_retorna_none(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(return_value=json.dumps({"claimed": False}))
        queue_service = QueueService(redis_client=mock_redis)

        resultado = await queue_service.reclamar_item_para_procesamiento(registro_id=1, worker_id="w")

        self.assertIsNone(resultado)

    async def test_error_de_redis_se_loguea_y_retorna_none(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("redis caido"))
        queue_service = QueueService(redis_client=mock_redis)

        resultado = await queue_service.reclamar_item_para_procesamiento(registro_id=1, worker_id="w")

        self.assertIsNone(resultado)


class TestMarcarExitosoRamasDeError(unittest.IsolatedAsyncioTestCase):

    async def test_item_not_found_retorna_not_found(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(return_value=json.dumps({"success": False, "reason": "item_not_found"}))
        queue_service = QueueService(redis_client=mock_redis)

        resultado = await queue_service.marcar_exitoso(registro_id=1, worker_id="w", expected_version=1)

        self.assertEqual(resultado, "not_found")

    async def test_razon_desconocida_lanza_runtime_error(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(return_value=json.dumps({"success": False, "reason": "algo_raro"}))
        queue_service = QueueService(redis_client=mock_redis)

        with self.assertRaises(RuntimeError):
            await queue_service.marcar_exitoso(registro_id=1, worker_id="w", expected_version=1)

    async def test_error_de_redis_se_loguea_y_se_relanza(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("redis caido"))
        queue_service = QueueService(redis_client=mock_redis)

        with self.assertRaises(ConnectionError):
            await queue_service.marcar_exitoso(registro_id=1, worker_id="w", expected_version=1)


class TestRegistrarFalloRamasDeError(unittest.IsolatedAsyncioTestCase):

    async def test_sin_redis_retorna_not_found(self):
        queue_service = QueueService(redis_client=None)
        resultado = await queue_service.registrar_fallo(item=_item(), error_msg="timeout", worker_id="w")
        self.assertEqual(resultado, "not_found")

    async def test_razon_no_mapeada_retorna_not_found(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(return_value=json.dumps({"success": False, "reason": "item_not_found"}))
        queue_service = QueueService(redis_client=mock_redis)

        resultado = await queue_service.registrar_fallo(item=_item(), error_msg="timeout", worker_id="w")

        self.assertEqual(resultado, "not_found")

    async def test_error_de_redis_se_loguea_y_retorna_not_found_sin_lanzar(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("redis caido"))
        queue_service = QueueService(redis_client=mock_redis)

        resultado = await queue_service.registrar_fallo(item=_item(), error_msg="timeout", worker_id="w")

        self.assertEqual(resultado, "not_found")


class TestObtenerTodosLosEncoladosFallbacks(unittest.IsolatedAsyncioTestCase):

    async def test_sin_redis_retorna_lista_vacia(self):
        queue_service = QueueService(redis_client=None)
        self.assertEqual(await queue_service.obtener_todos_los_encolados(), [])

    async def test_con_estado_usa_smembers_si_no_hay_sscan_iter(self):
        # spec=["smembers", "get"] -- sin sscan_iter, hasattr(...) da False y fuerza el fallback.
        mock_redis = MagicMock(spec=["smembers", "get"])
        mock_redis.smembers = AsyncMock(return_value={"1"})
        mock_redis.get = AsyncMock(return_value=json.dumps({"id": 1, "smart_code": "SC-1", "created_at": "2026-01-01T00:00:00"}))
        queue_service = QueueService(redis_client=mock_redis)

        registros = await queue_service.obtener_todos_los_encolados(estado="pending")

        self.assertEqual([r.smart_code for r in registros], ["SC-1"])
        mock_redis.smembers.assert_awaited_once_with(f"{QUEUE_PREFIX}:status:PENDING")

    async def test_sin_estado_usa_smembers_si_no_hay_sscan_iter(self):
        """
        🟡 FIX (hallazgo C3, auditoría adversarial 2026-08-25): sin filtro de estado,
        ya no se hace scan_iter sobre TODO el keyspace de items -- se unen los 3 sets
        de estado reales (PENDIENTE, EXITOSO, FALLIDO_DEFINITIVO), que entre los tres
        cubren cualquier item sin tocar el keyspace completo.
        """
        mock_redis = MagicMock(spec=["smembers", "get"])
        mock_redis.smembers = AsyncMock(side_effect=[{"1"}, {"2"}, set()])
        mock_redis.get = AsyncMock(side_effect=[
            json.dumps({"id": 1, "smart_code": "SC-1", "created_at": "2026-01-01T00:00:00"}),
            json.dumps({"id": 2, "smart_code": "SC-2", "created_at": "2026-01-02T00:00:00"}),
        ])
        queue_service = QueueService(redis_client=mock_redis)

        registros = await queue_service.obtener_todos_los_encolados()

        self.assertEqual({r.smart_code for r in registros}, {"SC-1", "SC-2"})
        mock_redis.smembers.assert_any_await(f"{QUEUE_PREFIX}:status:PENDIENTE")
        mock_redis.smembers.assert_any_await(f"{QUEUE_PREFIX}:status:EXITOSO")
        mock_redis.smembers.assert_any_await(f"{QUEUE_PREFIX}:status:FALLIDO_DEFINITIVO")

    async def test_error_al_listar_se_loguea_y_retorna_lista_vacia(self):
        mock_redis = MagicMock(spec=["smembers"])
        mock_redis.smembers = AsyncMock(side_effect=ConnectionError("redis caido"))
        queue_service = QueueService(redis_client=mock_redis)

        self.assertEqual(await queue_service.obtener_todos_los_encolados(), [])


class TestPurgarRegistrosAntiguos(unittest.IsolatedAsyncioTestCase):

    async def test_sin_redis_retorna_cero(self):
        queue_service = QueueService(redis_client=None)
        self.assertEqual(await queue_service.purgar_registros_antiguos(), 0)

    async def test_error_durante_la_purga_se_loguea_y_retorna_cero(self):
        mock_redis = MagicMock(spec=["sscan_iter"])

        async def _sscan_iter_falla(*args, **kwargs):
            raise ConnectionError("redis caido")
            yield  # pragma: no cover - hace de este un generador async

        mock_redis.sscan_iter = _sscan_iter_falla
        queue_service = QueueService(redis_client=mock_redis)

        self.assertEqual(await queue_service.purgar_registros_antiguos(), 0)


class TestDiferirPendientesErrorPorItem(unittest.IsolatedAsyncioTestCase):

    async def test_error_en_un_item_no_interrumpe_el_resto(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("redis caido"))
        queue_service = QueueService(redis_client=mock_redis)

        modificados = await queue_service.diferir_pendientes_por_caida_sfc(registro_ids=[1, 2])

        self.assertEqual(modificados, 0)


class TestExtenderLeaseItemErrorDeRedis(unittest.IsolatedAsyncioTestCase):

    async def test_error_de_redis_retorna_false(self):
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("redis caido"))
        queue_service = QueueService(redis_client=mock_redis)

        self.assertFalse(await queue_service.extender_lease_item(registro_id=1, worker_id="w"))


if __name__ == "__main__":
    unittest.main()
