# tests/test_queue_reencolar_item_fallido.py
"""
Prueba de integración contra Redis REAL de QueueService.reencolar_item_fallido
(hallazgo C2, revisión externa v5, 2026-08-25): el endpoint administrativo de
replay de la DLQ. Mismo criterio que test_queue_registrar_fallo.py -- contra
Redis real, no un mock del script Lua, porque la lógica que importa (mover
entre sets de estado, restaurar el índice smart_code->item, negarse si ya
existe un item más reciente) vive en el script mismo.
"""
import asyncio
import os
import unittest
from unittest.mock import patch, AsyncMock

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.queue_service import QueueService, QUEUE_PREFIX
from app.services.idempotency_service import IdempotencyService
from app.core.config import settings

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")


def _redis_disponible() -> bool:
    if redis_asyncio is None:
        return False
    import asyncio

    async def _check():
        client = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        try:
            await client.ping()
            return True
        except Exception:
            return False
        finally:
            await client.aclose()

    try:
        return asyncio.run(_check())
    except Exception:
        return False


_REDIS_OK = _redis_disponible()


@unittest.skipUnless(
    _REDIS_OK,
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de integración de "
    "reencolar_item_fallido. Levante un Redis local (ej. `docker run --rm -p 6379:6379 "
    "redis:7-alpine`) para ejecutarlas."
)
class TestReencolarItemFallido(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)
        self.idempotency_service = IdempotencyService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def _encolar_reclamar_y_fallar_definitivo(self, smart_code: str) -> int:
        """Lleva un item hasta FALLIDO_DEFINITIVO con QUEUE_MAX_RETRIES=1, igual que
        test_queue_registrar_fallo.py::test_alcanza_max_intentos_pasa_a_dlq_..."""
        with patch.object(settings, "QUEUE_MAX_RETRIES", 1):
            item = await self.queue_service.encolar_despacho(
                smart_code=smart_code, tipo_operacion="AUTO",
                payload_json={"Smart_Code__c": smart_code}, error_inicial="timeout inicial"
            )
            item_reclamado = await self.queue_service.reclamar_item_para_procesamiento(
                registro_id=item.id, worker_id="worker_1", lease_segundos=60
            )
            with patch(
                "app.services.queue_service.EmailAlertService.notificar_caso_fallido_definitivo",
                new_callable=AsyncMock
            ):
                await self.queue_service.registrar_fallo(
                    item=item_reclamado, error_msg="SFC caída sostenida", worker_id="worker_1"
                )
        return item.id

    async def test_reencola_item_fallido_definitivo_vuelve_a_pendiente(self):
        registro_id = await self._encolar_reclamar_y_fallar_definitivo("SC-1")

        resultado = await self.queue_service.reencolar_item_fallido(registro_id)

        self.assertTrue(resultado["success"])
        self.assertEqual(resultado["smart_code"], "SC-1")

        registros = await self.queue_service.obtener_todos_los_encolados(estado="PENDIENTE")
        self.assertEqual([r.id for r in registros], [registro_id])
        item_pendiente = registros[0]
        self.assertEqual(item_pendiente.intentos, 0)

        self.assertEqual(await self.redis.scard(f"{QUEUE_PREFIX}:status:FALLIDO_DEFINITIVO"), 0)
        self.assertEqual(await self.redis.get(f"{QUEUE_PREFIX}:index:SC-1"), str(registro_id))

    async def test_reencolar_sube_la_version_del_item(self):
        registro_id = await self._encolar_reclamar_y_fallar_definitivo("SC-2")
        raw_antes = await self.redis.get(f"{QUEUE_PREFIX}:item:{registro_id}")
        import json
        version_antes = json.loads(raw_antes)["version"]

        await self.queue_service.reencolar_item_fallido(registro_id)

        raw_despues = await self.redis.get(f"{QUEUE_PREFIX}:item:{registro_id}")
        version_despues = json.loads(raw_despues)["version"]
        self.assertGreater(version_despues, version_antes)

    async def test_reencolar_reconstruye_el_registro_de_idempotencia_como_queued(self):
        registro_id = await self._encolar_reclamar_y_fallar_definitivo("SC-3")

        await self.queue_service.reencolar_item_fallido(registro_id)

        payload = {"Smart_Code__c": "SC-3"}
        clave = self.idempotency_service._get_idempotency_key(
            "SC-3",
            IdempotencyService.infer_operation_type(payload),
            IdempotencyService.compute_payload_hash(payload)
        )
        raw = await self.redis.get(clave)
        self.assertIsNotNone(raw)
        import json
        registro = json.loads(raw)
        self.assertEqual(registro["status"], "QUEUED")
        self.assertEqual(registro["queue_item_id"], registro_id)

    async def test_reencolar_item_que_no_esta_en_fallido_definitivo_se_niega(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-4", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-4"}, error_inicial="timeout"
        )

        resultado = await self.queue_service.reencolar_item_fallido(item.id)

        self.assertFalse(resultado["success"])
        self.assertEqual(resultado["reason"], "not_failed_final")
        self.assertEqual(resultado["estado_actual"], "PENDIENTE")

    async def test_reencolar_item_actualmente_en_processing_se_niega(self):
        """
        El escenario más peligroso de dejar sin probar: un admin hace clic en
        "reencolar" justo cuando un worker YA tiene el item reclamado y lo está
        procesando en ese instante (estado PROCESSING, no PENDIENTE ni
        FALLIDO_DEFINITIVO). El mismo chequeo de estado debe rechazarlo -- si no lo
        hiciera, el reencolado pisaría un item que un worker tiene activamente en
        vuelo, con un worker real de por medio (no sólo una condición de carrera
        teórica entre dos escrituras)."""
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-8", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-8"}, error_inicial="timeout"
        )
        await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_activo", lease_segundos=60
        )

        resultado = await self.queue_service.reencolar_item_fallido(item.id)

        self.assertFalse(resultado["success"])
        self.assertEqual(resultado["reason"], "not_failed_final")
        self.assertEqual(resultado["estado_actual"], "PROCESSING")

        # El claim del worker activo no debe verse afectado por el intento rechazado.
        self.assertEqual(await self.redis.get(f"{QUEUE_PREFIX}:claim:{item.id}"), "worker_activo")

    async def test_reencolar_item_completado_se_niega(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-9", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-9"}, error_inicial="timeout"
        )
        claim = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )
        await self.queue_service.marcar_exitoso(
            item.id, worker_id="worker_1", expected_version=claim.version
        )

        resultado = await self.queue_service.reencolar_item_fallido(item.id)

        self.assertFalse(resultado["success"])
        self.assertEqual(resultado["reason"], "not_failed_final")
        self.assertEqual(resultado["estado_actual"], "EXITOSO")

    async def test_reencolar_item_inexistente_retorna_item_not_found(self):
        resultado = await self.queue_service.reencolar_item_fallido(999999)

        self.assertFalse(resultado["success"])
        self.assertEqual(resultado["reason"], "item_not_found")

    async def test_reencolar_se_niega_si_smart_code_tiene_un_item_mas_reciente(self):
        """
        El item viejo cayó a FALLIDO_DEFINITIVO (su índice smart_code se borró). Antes
        de intentar reencolarlo, llega un evento NUEVO del mismo caso -- crea un item
        distinto y toma el índice para sí. Reencolar el item viejo ahí rompería "un
        smart_code = un slot en cola"; debe negarse en vez de reactivarlo a ciegas.
        """
        registro_id_viejo = await self._encolar_reclamar_y_fallar_definitivo("SC-5")
        item_nuevo = await self.queue_service.encolar_despacho(
            smart_code="SC-5", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-5", "Status": "In Progress"}, error_inicial="timeout"
        )
        self.assertNotEqual(item_nuevo.id, registro_id_viejo)

        resultado = await self.queue_service.reencolar_item_fallido(registro_id_viejo)

        self.assertFalse(resultado["success"])
        self.assertEqual(resultado["reason"], "smart_code_tiene_item_mas_reciente")
        self.assertEqual(resultado["item_activo"], str(item_nuevo.id))
        # El item nuevo, legítimo, no se ve afectado.
        self.assertEqual(await self.redis.get(f"{QUEUE_PREFIX}:index:SC-5"), str(item_nuevo.id))

    async def test_reencolar_dos_veces_seguidas_la_segunda_se_niega(self):
        """El propio chequeo de estado (FALLIDO_DEFINITIVO) es lo que evita una doble
        aplicación -- tras el primer reencolado exitoso, el item ya está PENDIENTE."""
        registro_id = await self._encolar_reclamar_y_fallar_definitivo("SC-6")

        primero = await self.queue_service.reencolar_item_fallido(registro_id)
        segundo = await self.queue_service.reencolar_item_fallido(registro_id)

        self.assertTrue(primero["success"])
        self.assertFalse(segundo["success"])
        self.assertEqual(segundo["reason"], "not_failed_final")

    async def test_doble_click_del_admin_genuinamente_concurrente_solo_uno_gana(self):
        """
        Auditoría de concurrencia (2026-08-26): versión con asyncio.gather del test
        anterior -- un administrador que hace doble clic sobre "reencolar" dispara dos
        requests genuinamente simultáneos, no uno después del otro. El propio chequeo
        de estado dentro del script Lua (atómico en Redis) debe seguir garantizando
        que sólo uno de los dos tenga éxito, incluso bajo esta condición de carrera real.
        """
        registro_id = await self._encolar_reclamar_y_fallar_definitivo("SC-7")

        resultado_1, resultado_2 = await asyncio.gather(
            self.queue_service.reencolar_item_fallido(registro_id),
            self.queue_service.reencolar_item_fallido(registro_id),
        )

        exitos = [r for r in (resultado_1, resultado_2) if r["success"]]
        fallos = [r for r in (resultado_1, resultado_2) if not r["success"]]
        self.assertEqual(len(exitos), 1, "Exactamente uno de los dos clics debe ganar")
        self.assertEqual(len(fallos), 1)
        self.assertEqual(fallos[0]["reason"], "not_failed_final")


class TestReencolarItemFallidoSinRedis(unittest.IsolatedAsyncioTestCase):

    async def test_sin_cliente_redis_retorna_redis_no_disponible(self):
        queue_service = QueueService(redis_client=None)

        resultado = await queue_service.reencolar_item_fallido(1)

        self.assertEqual(resultado, {"success": False, "reason": "redis_no_disponible"})


if __name__ == "__main__":
    unittest.main()
