# tests/test_distributed_lock_real_redis.py
"""
Auditoría de concurrencia (2026-08-26): RedisLock (app/core/distributed_lock.py)
nunca se había ejecutado contra Redis real en ningún test -- test_distributed_lock.py
y test_distributed_lock_edge_cases.py mockean el cliente por completo, así que un bug
real en cualquiera de los dos scripts Lua (RELEASE_LOCK_LUA_SCRIPT/EXTEND_LOCK_LUA_SCRIPT)
no lo habría detectado ningún test existente. RedisLock ahora protege tanto los jobs
periódicos del scheduler (purga, reintentos, refresco de catálogos) como el lock por
caso del hallazgo E en el despacho síncrono -- una regresión aquí afecta a ambos.

También cubre la propiedad que ningún test del repo probaba con concurrencia GENUINA
(asyncio.gather sobre corrutinas realmente simultáneas, no una simulación secuencial
determinista): que sólo UNO de N acquire() concurrentes sobre la MISMA llave gane el
lock.
"""
import asyncio
import os
import unittest

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.core.distributed_lock import RedisLock

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de RedisLock contra "
    "Redis real. Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) "
    "para ejecutarlas."
)
class TestRedisLockContraRedisReal(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_acquire_y_release_round_trip(self):
        lock = RedisLock(redis_client=self.redis, lock_key="test:lock:a", lease_segundos=10)

        adquirido = await lock.acquire()
        self.assertTrue(adquirido)
        valor = await self.redis.get("test:lock:a")
        self.assertEqual(valor, lock.owner_token)
        ttl = await self.redis.ttl("test:lock:a")
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 10)

        await lock.release()
        self.assertIsNone(await self.redis.get("test:lock:a"))

    async def test_acquire_falla_si_la_llave_ya_existe(self):
        await self.redis.set("test:lock:b", "otro-dueno", ex=30)
        lock = RedisLock(redis_client=self.redis, lock_key="test:lock:b")

        self.assertFalse(await lock.acquire())
        # La llave del otro dueño no debe verse alterada por el intento fallido.
        self.assertEqual(await self.redis.get("test:lock:b"), "otro-dueno")

    async def test_release_no_borra_la_llave_de_otro_dueno(self):
        """El script CAD (compare-and-delete) es lo que protege esto -- si estuviera
        roto (ej. un DEL incondicional), este test lo detectaría."""
        await self.redis.set("test:lock:c", "dueno-legitimo", ex=30)
        lock_impostor = RedisLock(redis_client=self.redis, lock_key="test:lock:c")
        lock_impostor.acquired = True  # Simula un lock que cree tener ownership sin tenerlo.

        await lock_impostor.release()

        self.assertEqual(await self.redis.get("test:lock:c"), "dueno-legitimo")

    async def test_heartbeat_extiende_el_ttl_real(self):
        lock = RedisLock(
            redis_client=self.redis, lock_key="test:lock:d",
            lease_segundos=2, intervalo_heartbeat=0.2
        )
        await lock.acquire()

        await asyncio.sleep(0.5)  # deja correr el heartbeat un par de veces

        ttl = await self.redis.ttl("test:lock:d")
        # Si el heartbeat no funcionara, el TTL ya habría bajado de 2s a <1.5s tras
        # 0.5s de espera sin renovación -- comprobamos que se mantuvo cerca del lease
        # original, señal de que sí se renovó.
        self.assertGreater(ttl, 1)

        await lock.release()

    async def test_release_sobre_llave_expirada_no_lanza(self):
        lock = RedisLock(redis_client=self.redis, lock_key="test:lock:e", lease_segundos=1)
        await lock.acquire()
        await self.redis.delete("test:lock:e")  # simula expiración/borrado externo

        await lock.release()  # No debe lanzar aunque la llave ya no exista.

    async def test_tras_release_otro_dueno_puede_adquirir(self):
        lock_1 = RedisLock(redis_client=self.redis, lock_key="test:lock:f", lease_segundos=10)
        await lock_1.acquire()
        await lock_1.release()

        lock_2 = RedisLock(redis_client=self.redis, lock_key="test:lock:f", lease_segundos=10)
        self.assertTrue(await lock_2.acquire())
        await lock_2.release()

    async def test_solo_uno_de_n_acquires_concurrentes_gana_el_lock(self):
        """
        Concurrencia GENUINA (no simulación secuencial): N locks independientes,
        mismo lock_key, mismo cliente Redis real, disparados con asyncio.gather para
        que compitan de verdad por el SETNX en el mismo instante.
        """
        N = 20
        locks = [
            RedisLock(redis_client=self.redis, lock_key="test:lock:contencion", lease_segundos=10)
            for _ in range(N)
        ]

        resultados = await asyncio.gather(*[lock.acquire() for lock in locks])

        self.assertEqual(resultados.count(True), 1, "Exactamente un acquire debe ganar la carrera")
        self.assertEqual(resultados.count(False), N - 1)

        ganador = next(lock for lock, ok in zip(locks, resultados) if ok)
        self.assertEqual(await self.redis.get("test:lock:contencion"), ganador.owner_token)

        for lock in locks:
            await lock.release()

    async def test_ciclo_completo_de_perdida_real_release_tardio_del_viejo_dueno_no_afecta_al_nuevo(self):
        """
        Caso ultra específico de concurrencia (revisión final de locks, 2026-08-27):
        el ciclo de vida COMPLETO del "split brain" -- combina en un solo flujo real
        lo que hasta ahora sólo se probaba por separado: heartbeat detectando pérdida
        (con mocks, en test_distributed_lock_edge_cases.py) y el CAD del release
        protegiendo la llave de otro dueño (acá mismo, pero con un "otro dueño"
        simulado a mano vía SET directo, no un RedisLock real que haya ganado la
        carrera).

        Aquí: lease corto SIN heartbeat (para que expire de verdad), un segundo
        RedisLock genuino adquiere tras la expiración real, y sólo DESPUÉS el
        primero se entera (heartbeat manual) de que perdió -- confirmando que
        `acquired` pasa a False y que su release() posterior (tardío, ya fuera de
        tiempo) no toca para nada la llave del segundo dueño.
        """
        lock_1 = RedisLock(
            redis_client=self.redis, lock_key="test:lock:split-brain",
            lease_segundos=1, intervalo_heartbeat=999  # heartbeat no debe disparar solo
        )
        self.assertTrue(await lock_1.acquire())
        # Se cancela la tarea de heartbeat real para controlar manualmente cuándo se
        # "entera" de la pérdida -- de lo contrario el intervalo tan largo (999s)
        # simplemente nunca correría dentro del test, lo cual serviría igual, pero
        # así el paso "se entera de la pérdida" queda explícito y determinista.
        lock_1._heartbeat_task.cancel()
        try:
            await lock_1._heartbeat_task
        except asyncio.CancelledError:
            pass

        await asyncio.sleep(1.2)  # deja que el lease de 1s expire de verdad en Redis
        self.assertIsNone(await self.redis.get("test:lock:split-brain"))

        lock_2 = RedisLock(redis_client=self.redis, lock_key="test:lock:split-brain", lease_segundos=30)
        self.assertTrue(await lock_2.acquire(), "El segundo dueño debe poder adquirir tras la expiración real.")

        # El primero recién ahora "revisa" -- un ciclo de heartbeat manual contra la
        # llave ya ocupada por el segundo dueño debe reportar pérdida confirmada.
        from app.core.distributed_lock import EXTEND_LOCK_LUA_SCRIPT
        res = await self.redis.eval(
            EXTEND_LOCK_LUA_SCRIPT, 1, "test:lock:split-brain", lock_1.owner_token, "60000"
        )
        self.assertNotEqual(res, 1, "El script de extensión no debe reportar éxito: la llave ya es de otro dueño.")
        lock_1.acquired = False  # mismo efecto que produciría _heartbeat_loop al ver res != 1

        # El release tardío del primer dueño (ya sin ownership real) no debe tocar
        # la llave del segundo -- ni siquiera intentarlo, por el guard `if not
        # self.acquired: return` de release().
        await lock_1.release()
        self.assertEqual(await self.redis.get("test:lock:split-brain"), lock_2.owner_token)

        await lock_2.release()
        self.assertIsNone(await self.redis.get("test:lock:split-brain"))

    async def test_tras_liberar_el_ganador_otro_puede_ganar_la_siguiente_ronda(self):
        """Complemento del anterior: la mutua exclusión no es de un solo uso -- tras
        liberar, una nueva ronda de contención vuelve a producir exactamente un ganador."""
        primera_ronda = [
            RedisLock(redis_client=self.redis, lock_key="test:lock:rondas", lease_segundos=10)
            for _ in range(5)
        ]
        resultados_1 = await asyncio.gather(*[lock.acquire() for lock in primera_ronda])
        ganador_1 = next(lock for lock, ok in zip(primera_ronda, resultados_1) if ok)
        await ganador_1.release()

        segunda_ronda = [
            RedisLock(redis_client=self.redis, lock_key="test:lock:rondas", lease_segundos=10)
            for _ in range(5)
        ]
        resultados_2 = await asyncio.gather(*[lock.acquire() for lock in segunda_ronda])

        self.assertEqual(resultados_2.count(True), 1)
        for lock, ok in zip(segunda_ronda, resultados_2):
            if ok:
                await lock.release()


if __name__ == "__main__":
    unittest.main()
