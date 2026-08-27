# tests/test_scheduler_ciclo_completo_real_redis.py
"""
Cobertura de integración de punta a punta para
`scheduler.py::reintentar_despachos_pendientes_job` -- la función que APScheduler
invoca literalmente en producción cada `QUEUE_RETRY_INTERVAL_MINUTES`. Hasta ahora,
esta función sólo tenía dos tipos de cobertura, ninguno de punta a punta:

  1. `test_scheduler_retry_job.py` / `test_email_triggers.py`: llaman a la función
     REAL, pero con `QueueService` completamente MOCKEADO -- prueban lógica de
     decisión (qué hacer ante cada resultado posible), no el flujo real contra
     Redis.
  2. `test_scheduler_worker_ignora_despacho_lock.py` / `test_queue_sla_reintentos_lease.py`:
     usan Redis real, pero llaman piezas INTERNAS sueltas
     (`_reclamar_y_procesar_si_lock_disponible`, métodos individuales de
     `QueueService`) -- nunca el loop completo que orquesta un ciclo entero.

Ningún test existente había ejecutado `reintentar_despachos_pendientes_job()`
completa, con Redis real detrás de TODA la cola (scripts Lua reales, no un mock
de `eval`), en un solo ciclo con más de un item a la vez. Ese es exactamente el
tipo de bug que un test de integración real atrapa y que las dos categorías de
arriba, por separado, no pueden: algo mal secuenciado en cómo el loop externo
itera sobre el batch, en el umbral de fallas de infraestructura consecutivas, o
en la coordinación de locks entre dos corridas concurrentes del propio job.

Se mockean únicamente los límites externos reales: `DespachoQuejaOrquestador`
(equivalente a la llamada real a la SFC/S3), `CrmWebhookService` (el webhook al
CRM) y `EmailAlertService` (para no disparar correos reales) -- exactamente el
mismo criterio que ya usa `test_routes_quejas_despacho_lock_real_redis.py` para
el camino síncrono.
"""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.queue_service import QueueService, QUEUE_PREFIX, DESPACHO_LOCK_PREFIX
from app.core.constants import SmartStatus
from app.core.config import settings
from app.core.exceptions import SfcIntegrationException
from app.core.distributed_lock import RedisLock
from app.services.email_service import EmailAlertService
from app.services.crm_webhook_service import CrmWebhookService
from app.workers.scheduler import reintentar_despachos_pendientes_job

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")


def _redis_disponible() -> bool:
    if redis_asyncio is None:
        return False

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo el ciclo completo de "
    "integración del scheduler. Levante un Redis local (ej. `docker run --rm "
    "-p 6379:6379 redis:7-alpine`) para ejecutarlas."
)
class TestCicloCompletoSchedulerRealRedis(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.queue_service = QueueService(redis_client=self.redis)

        for target, value in [
            ("app.workers.scheduler.get_redis_client", self.redis),
            ("app.workers.scheduler.get_sfc_client", MagicMock()),
            ("app.workers.scheduler.get_s3_client", MagicMock()),
        ]:
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Alertas por correo: nunca deben intentar enviar correos reales en tests.
        for metodo in (
            "notificar_casos_vencimiento_sla", "notificar_recuperacion_sfc",
            "notificar_falla_infraestructura", "notificar_caso_fallido_definitivo",
            "notificar_umbral_cola",
        ):
            patcher = patch.object(EmailAlertService, metodo, new_callable=AsyncMock)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def _forzar_disponible_de_inmediato(self, item_id: int, score: float) -> None:
        """Fuerza a un item recién encolado (proximo_reintento_at en el futuro,
        por diseño de encolar_despacho) a quedar disponible YA para el próximo
        ciclo -- mismo patrón que test_queue_sla_reintentos_lease.py."""
        await self.redis.zadd(f"{QUEUE_PREFIX}:pending_zset", {str(item_id): score})

    async def _estado_item(self, item_id: int) -> dict:
        raw = await self.redis.get(f"{QUEUE_PREFIX}:item:{item_id}")
        import json
        return json.loads(raw) if raw else {}

    async def test_lote_con_exito_y_fallo_definitivo_se_procesa_completo_en_una_sola_corrida(self):
        """
        Dos items en la cola, procesados en UNA sola llamada real al job: uno
        termina exitoso de punta a punta (SFC + webhook CRM + marcado COMPLETED
        en Redis real), el otro es rechazado por la SFC con un error de NEGOCIO
        (no transitorio) y, con QUEUE_MAX_RETRIES=2, cae a FALLIDO_DEFINITIVO en
        el mismo ciclo -- sin que uno interfiera con el otro. (`intentos` arranca
        en 1 -- representa el intento síncrono inicial que ya falló y disparó el
        encolado -- así que max_retries=2 es el mínimo que deja lugar para UN
        reintento real vía el scheduler antes de agotarse.)
        """
        with patch.object(settings, "QUEUE_MAX_RETRIES", 2):
            item_a = await self.queue_service.encolar_despacho(
                smart_code="SC-OK-1", tipo_operacion="AUTO",
                payload_json={"Smart_Code__c": "SC-OK-1", "Status": "In Progress"},
                error_inicial="timeout inicial"
            )
            item_b = await self.queue_service.encolar_despacho(
                smart_code="SC-FAIL-1", tipo_operacion="AUTO",
                payload_json={"Smart_Code__c": "SC-FAIL-1", "Status": "In Progress"},
                error_inicial="timeout inicial"
            )
            await self._forzar_disponible_de_inmediato(item_a.id, 0)
            await self._forzar_disponible_de_inmediato(item_b.id, 1)

            async def _procesar(payload_dict, **_kwargs):
                if payload_dict.get("Smart_Code__c") == "SC-FAIL-1":
                    raise SfcIntegrationException(
                        400, "VALIDATION_ERROR", "numero_id_CF",
                        "Dato inválido", "Corrija el payload"
                    )
                return {"status": "success", "message": "OK"}

            with patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrq, \
                 patch.object(CrmWebhookService, "notificar_resolucion_contingencia",
                              new_callable=AsyncMock, return_value=(True, None)) as mock_webhook:
                MockOrq.return_value.procesar_despacho_raw_json = AsyncMock(side_effect=_procesar)

                await reintentar_despachos_pendientes_job()

                self.assertEqual(MockOrq.return_value.procesar_despacho_raw_json.await_count, 2)

        # Item A: éxito de punta a punta, marcado en el set real de EXITOSO.
        self.assertTrue(await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.COMPLETED.value}", str(item_a.id)))
        mock_webhook.assert_awaited_once()

        # Item B: rechazo de negocio, QUEUE_MAX_RETRIES=1 -> cae a DLQ en el mismo ciclo.
        self.assertTrue(await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.FAILED_FINAL.value}", str(item_b.id)))

        # El índice de cada operación se liberó -- confirma que MARK_SUCCESS/
        # REGISTRAR_FALLO corrieron de verdad contra Redis, no un mock.
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:index:SC-OK-1:M3_UPDATE"))
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:index:SC-FAIL-1:M3_UPDATE"))

    async def test_fallas_de_infraestructura_consecutivas_difieren_el_resto_del_lote_sin_tocarlo(self):
        """
        5 items pendientes; los primeros 3 fallan con un error TRANSITORIO de la
        SFC (503) -- al alcanzar el umbral de 3 fallas consecutivas
        (UMBRAL_FALLAS_INFRA_CONSECUTIVAS_PARA_DIFERIR), el resto del lote (items
        4 y 5) debe DIFERIRSE sin siquiera intentarse: nunca deben llegar al
        orquestador, y sus `intentos` no deben incrementarse -- a diferencia de
        los 3 primeros, que sí consumieron un intento real.
        """
        with patch.object(settings, "QUEUE_MAX_RETRIES", 5):
            items = []
            for i in range(1, 6):
                item = await self.queue_service.encolar_despacho(
                    smart_code=f"SC-INFRA-{i}", tipo_operacion="AUTO",
                    payload_json={"Smart_Code__c": f"SC-INFRA-{i}", "Status": "In Progress"},
                    error_inicial="timeout inicial"
                )
                await self._forzar_disponible_de_inmediato(item.id, float(i))
                items.append(item)

            llamados = []

            async def _siempre_falla_infra(payload_dict, **_kwargs):
                llamados.append(payload_dict.get("Smart_Code__c"))
                raise SfcIntegrationException(503, "SFC_DOWN", None, "SFC caída", "Reintentar")

            with patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrq:
                MockOrq.return_value.procesar_despacho_raw_json = AsyncMock(side_effect=_siempre_falla_infra)

                await reintentar_despachos_pendientes_job()

        # Sólo los primeros 3 (orden de score ascendente) llegaron al orquestador.
        self.assertEqual(llamados, ["SC-INFRA-1", "SC-INFRA-2", "SC-INFRA-3"])

        for i in (1, 2, 3):
            data = await self._estado_item(items[i - 1].id)
            self.assertEqual(data["intentos"], 2, f"SC-INFRA-{i} debió consumir un intento real")
            self.assertEqual(data["estado"], SmartStatus.PENDING.value)

        for i in (4, 5):
            data = await self._estado_item(items[i - 1].id)
            self.assertEqual(data["intentos"], 1, f"SC-INFRA-{i} NO debió tocarse -- se difirió sin intentar")
            self.assertEqual(data["estado"], SmartStatus.PENDING.value)

    async def test_lock_de_despacho_sincrono_ocupado_deja_el_item_intacto_sin_reclamar(self):
        """
        Un despacho síncrono está en curso para el mismo Smart_Code__c (lock real
        tomado, no simulado) cuando corre el ciclo del scheduler -- el item debe
        quedar PENDIENTE, sin reclamar, sin tocar sus intentos, y el orquestador
        del worker no debe invocarse. Prueba la coordinación cruzada del hallazgo
        E a través del job REAL, no sólo de su helper interno.
        """
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-LOCK-SYNC", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-LOCK-SYNC", "Status": "In Progress"},
            error_inicial="timeout inicial"
        )
        await self._forzar_disponible_de_inmediato(item.id, 0)

        despacho_lock_sincrono = RedisLock(
            redis_client=self.redis,
            lock_key=f"{DESPACHO_LOCK_PREFIX}:lock:SC-LOCK-SYNC",
            lease_segundos=180
        )
        self.assertTrue(await despacho_lock_sincrono.acquire())

        try:
            with patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrq:
                MockOrq.return_value.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})

                await reintentar_despachos_pendientes_job()

                MockOrq.return_value.procesar_despacho_raw_json.assert_not_awaited()
        finally:
            await despacho_lock_sincrono.release()

        data = await self._estado_item(item.id)
        self.assertEqual(data["estado"], SmartStatus.PENDING.value)
        self.assertEqual(data["intentos"], 1)
        self.assertIsNone(await self.redis.get(f"{QUEUE_PREFIX}:claim:{item.id}"))

    async def test_dos_corridas_concurrentes_del_job_solo_una_procesa(self):
        """
        Dos invocaciones genuinamente concurrentes de
        reintentar_despachos_pendientes_job() (el job_lock global, SCHEDULER_LOCK_PREFIX)
        -- sólo una debe ejecutar el ciclo; la otra debe encontrar el lock
        ocupado y retornar de inmediato sin tocar nada. Nunca antes probado bajo
        concurrencia real: los tests existentes de este job siempre lo invocan
        una vez a la vez.
        """
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-JOBLOCK-1", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-JOBLOCK-1", "Status": "In Progress"},
            error_inicial="timeout inicial"
        )
        await self._forzar_disponible_de_inmediato(item.id, 0)

        async def _procesar_lento(payload_dict, **_kwargs):
            await asyncio.sleep(0.3)  # ensancha la ventana de la carrera
            return {"status": "success"}

        with patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrq, \
             patch.object(CrmWebhookService, "notificar_resolucion_contingencia",
                          new_callable=AsyncMock, return_value=(True, None)):
            MockOrq.return_value.procesar_despacho_raw_json = AsyncMock(side_effect=_procesar_lento)

            await asyncio.gather(
                reintentar_despachos_pendientes_job(),
                reintentar_despachos_pendientes_job(),
            )

            self.assertEqual(
                MockOrq.return_value.procesar_despacho_raw_json.await_count, 1,
                "El job_lock global debe impedir que dos corridas concurrentes procesen el mismo item dos veces."
            )

        self.assertTrue(await self.redis.sismember(f"{QUEUE_PREFIX}:status:{SmartStatus.COMPLETED.value}", str(item.id)))

    async def test_autorrecuperacion_se_notifica_solo_cuando_la_cola_termina_vacia_tras_exito(self):
        """3. Notificación de Autorrecuperación -- integrada de punta a punta:
        sólo debe dispararse cuando ALGO se despachó con éxito en este ciclo Y la
        cola quedó completamente vacía al terminar (no antes, no si sigue
        habiendo pendientes)."""
        item = await self.queue_service.encolar_despacho(
            smart_code="SC-RECOVERY-1", tipo_operacion="AUTO",
            payload_json={"Smart_Code__c": "SC-RECOVERY-1", "Status": "In Progress"},
            error_inicial="timeout inicial"
        )
        await self._forzar_disponible_de_inmediato(item.id, 0)

        with patch("app.workers.scheduler.DespachoQuejaOrquestador") as MockOrq, \
             patch.object(CrmWebhookService, "notificar_resolucion_contingencia",
                          new_callable=AsyncMock, return_value=(True, None)):
            MockOrq.return_value.procesar_despacho_raw_json = AsyncMock(return_value={"status": "success"})

            await reintentar_despachos_pendientes_job()

        EmailAlertService.notificar_recuperacion_sfc.assert_awaited_once_with(total_despachados=1)


if __name__ == "__main__":
    unittest.main()
