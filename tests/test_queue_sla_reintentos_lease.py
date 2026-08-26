# tests/test_queue_sla_reintentos_lease.py
"""
Prueba de integración contra Redis REAL de QueueService.obtener_casos_vencidos_sla,
obtener_pendientes_para_reintento, diferir_pendientes_por_caida_sfc y
extender_lease_item -- las piezas que alimentan cada ciclo del scheduler
(reintentar_despachos_pendientes_job) y la alerta de SLA. Antes sin cobertura
directa.

Mismo criterio que el resto de tests de cola: contra Redis real, no un mock
del script/estructura de claves.
"""
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.queue_service import QueueService, QUEUE_PREFIX
from app.core.config import settings
from app.core.constants import SmartStatus

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de integración de cola. "
    "Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) para ejecutarlas."
)
class TestQueueSlaReintentosLease(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    # ------------------------------------------------------------------
    # obtener_casos_vencidos_sla
    # ------------------------------------------------------------------

    async def test_sla_incluye_solo_pendientes_mas_antiguos_que_el_limite(self):
        item_viejo = await self.queue_service.encolar_despacho(
            smart_code="SC-VIEJO", tipo_operacion="AUTO", payload_json={"a": 1}, error_inicial="timeout"
        )
        await self.queue_service.encolar_despacho(
            smart_code="SC-RECIENTE", tipo_operacion="AUTO", payload_json={"a": 1}, error_inicial="timeout"
        )
        # Envejece artificialmente el primero: 20h atrás en el created_zset.
        hace_20h = (datetime.now(ZoneInfo("America/Bogota")) - timedelta(hours=20)).timestamp()
        await self.redis.zadd(f"{QUEUE_PREFIX}:created_zset", {str(item_viejo.id): hace_20h})

        vencidos = await self.queue_service.obtener_casos_vencidos_sla(horas_limite=12)

        codigos = {c["smart_code"] for c in vencidos}
        self.assertIn("SC-VIEJO", codigos)
        self.assertNotIn("SC-RECIENTE", codigos)

    async def test_sla_excluye_items_que_ya_no_estan_pendientes(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-COMPLETADO", tipo_operacion="AUTO", payload_json={"a": 1}, error_inicial="timeout"
        )
        hace_20h = (datetime.now(ZoneInfo("America/Bogota")) - timedelta(hours=20)).timestamp()
        await self.redis.zadd(f"{QUEUE_PREFIX}:created_zset", {str(item.id): hace_20h})
        # Ya no está pendiente (se reclamó y completó en otro ciclo).
        await self.redis.srem(f"{QUEUE_PREFIX}:status:{SmartStatus.PENDING.value}", str(item.id))

        vencidos = await self.queue_service.obtener_casos_vencidos_sla(horas_limite=12)

        self.assertEqual(vencidos, [])

    async def test_sla_sin_redis_retorna_lista_vacia(self):
        queue_service = QueueService(redis_client=None)
        self.assertEqual(await queue_service.obtener_casos_vencidos_sla(), [])

    # ------------------------------------------------------------------
    # obtener_pendientes_para_reintento
    # ------------------------------------------------------------------

    async def test_pendientes_incluye_items_vencidos_dentro_del_limite_de_intentos(self):
        with patch.object(settings, "QUEUE_MAX_RETRIES", 5):
            item = await self.queue_service.encolar_despacho(
                smart_code="SC-DUE", tipo_operacion="AUTO", payload_json={"a": 1}, error_inicial="timeout"
            )
            # Fuerza que ya esté "vencido" para reintento (encolar_despacho lo agenda a futuro).
            await self.redis.zadd(f"{QUEUE_PREFIX}:pending_zset", {str(item.id): 0})

            pendientes = await self.queue_service.obtener_pendientes_para_reintento()

        self.assertEqual([p.smart_code for p in pendientes], ["SC-DUE"])

    async def test_pendientes_excluye_items_que_agotaron_max_intentos(self):
        with patch.object(settings, "QUEUE_MAX_RETRIES", 1):
            item = await self.queue_service.encolar_despacho(
                smart_code="SC-AGOTADO", tipo_operacion="AUTO", payload_json={"a": 1}, error_inicial="timeout"
            )
            await self.redis.zadd(f"{QUEUE_PREFIX}:pending_zset", {str(item.id): 0})
            # intentos=1 (el que deja encolar_despacho) ya no es < max_intentos=1.

            pendientes = await self.queue_service.obtener_pendientes_para_reintento()

        self.assertEqual(pendientes, [])

    async def test_pendientes_excluye_items_aun_no_vencidos(self):
        with patch.object(settings, "QUEUE_MAX_RETRIES", 5):
            await self.queue_service.encolar_despacho(
                smart_code="SC-FUTURO", tipo_operacion="AUTO", payload_json={"a": 1}, error_inicial="timeout"
            )
            # Sin forzar el score del pending_zset: encolar_despacho lo agenda a futuro por defecto.
            pendientes = await self.queue_service.obtener_pendientes_para_reintento()

        self.assertEqual(pendientes, [])

    async def test_pendientes_sin_redis_retorna_lista_vacia(self):
        queue_service = QueueService(redis_client=None)
        self.assertEqual(await queue_service.obtener_pendientes_para_reintento(), [])

    # ------------------------------------------------------------------
    # diferir_pendientes_por_caida_sfc
    # ------------------------------------------------------------------

    async def test_diferir_actualiza_proximo_reintento_y_limpia_el_claim(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-DIFERIR", tipo_operacion="AUTO", payload_json={"a": 1}, error_inicial="timeout"
        )
        await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )

        modificados = await self.queue_service.diferir_pendientes_por_caida_sfc(
            registro_ids=[item.id], minutos_delay=30
        )

        self.assertEqual(modificados, 1)
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:claim:{item.id}"))
        raw_item = await self.redis.get(f"{QUEUE_PREFIX}:item:{item.id}")
        data = json.loads(raw_item)
        self.assertIn("pospuesto automáticamente", data["ultimo_error"])

    async def test_diferir_es_atomico_y_no_pisa_un_campo_actualizado_por_otra_escritura(self):
        """
        Regresión de un hallazgo de code review (2026-08-24): antes, diferir hacía
        GET + mutar en Python + SET como pasos separados (no atómico) -- si un
        evento nuevo del mismo smart_code sobrescribía el item entre el GET y el
        SET de este método, el SET final pisaba esa sobrescritura con el snapshot
        viejo (payload_json/version/sfc_completado/intentos volvían al contenido
        ANTERIOR). Ahora es un único script Lua (GET+mutar+SET atómico en Redis),
        así que el SET final siempre parte del estado MÁS RECIENTE, sin ventana
        para que otro cliente se intercale. Se prueba marcando sfc_completado=true
        (vía marcar_sfc_completado, una escritura real e independiente) y
        verificando que diferir preserve ese valor -- no lo resetea ni lo pisa con
        nada, sólo toca ultimo_error/updated_at/proximo_reintento_at.
        """
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-DIFERIR-ATOMICO", tipo_operacion="AUTO", payload_json={"a": 1}, error_inicial="timeout"
        )
        reclamado = await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )
        await self.queue_service.marcar_sfc_completado(
            reclamado.id, worker_id="worker_1", expected_version=reclamado.version,
            smart_code="SC-DIFERIR-ATOMICO", payload_dict=reclamado.payload_json,
            sfc_response={"status": "success"}
        )

        modificados = await self.queue_service.diferir_pendientes_por_caida_sfc(
            registro_ids=[item.id], minutos_delay=30
        )

        self.assertEqual(modificados, 1)
        raw_item = await self.redis.get(f"{QUEUE_PREFIX}:item:{item.id}")
        data = json.loads(raw_item)
        self.assertTrue(data["sfc_completado"])
        # 🔴 FIX (hallazgo N4, revisión externa v5, 2026-08-25): sfc_response ahora se
        # guarda como string JSON opaco dentro del item (ver MARK_SFC_DONE_LUA_SCRIPT),
        # no como objeto anidado -- se decodifica una vez más para comparar el valor.
        self.assertEqual(json.loads(data["sfc_response"]), {"status": "success"})
        self.assertIn("pospuesto automáticamente", data["ultimo_error"])

    async def test_diferir_ignora_ids_inexistentes_sin_lanzar(self):
        modificados = await self.queue_service.diferir_pendientes_por_caida_sfc(registro_ids=[999999])
        self.assertEqual(modificados, 0)

    async def test_diferir_lista_vacia_no_toca_redis(self):
        self.assertEqual(await self.queue_service.diferir_pendientes_por_caida_sfc(registro_ids=[]), 0)

    async def test_diferir_sin_redis_retorna_cero(self):
        queue_service = QueueService(redis_client=None)
        self.assertEqual(await queue_service.diferir_pendientes_por_caida_sfc(registro_ids=[1]), 0)

    # ------------------------------------------------------------------
    # extender_lease_item
    # ------------------------------------------------------------------

    async def test_extender_lease_exitoso_para_el_dueno_del_claim(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-LEASE", tipo_operacion="AUTO", payload_json={"a": 1}, error_inicial="timeout"
        )
        await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )

        extendido = await self.queue_service.extender_lease_item(
            registro_id=item.id, worker_id="worker_1", lease_segundos=120
        )

        self.assertTrue(extendido)

    async def test_extender_lease_falla_para_worker_sin_ownership(self):
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-LEASE-2", tipo_operacion="AUTO", payload_json={"a": 1}, error_inicial="timeout"
        )
        await self.queue_service.reclamar_item_para_procesamiento(
            registro_id=item.id, worker_id="worker_1", lease_segundos=60
        )

        extendido = await self.queue_service.extender_lease_item(
            registro_id=item.id, worker_id="worker_intruso", lease_segundos=120
        )

        self.assertFalse(extendido)

    async def test_extender_lease_sin_redis_retorna_false(self):
        queue_service = QueueService(redis_client=None)
        self.assertFalse(await queue_service.extender_lease_item(registro_id=1, worker_id="w"))


if __name__ == "__main__":
    unittest.main()
