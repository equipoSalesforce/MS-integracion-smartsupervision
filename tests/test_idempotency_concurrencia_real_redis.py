# tests/test_idempotency_concurrencia_real_redis.py
"""
Auditoría de concurrencia (2026-08-26): IdempotencyService.verificar_o_iniciar_operacion
es la barrera central contra duplicados hacia la SFC -- su candado PROCESSING (un SET
NX sobre smart_code:operacion:payload_hash) es lo que debería garantizar que, de N
requests genuinamente simultáneos con el MISMO payload, sólo uno llegue a llamar a la
SFC. Ningún test existente lo prueba con concurrencia real: los tests de
test_idempotency_*.py llaman al método de forma secuencial (await uno, luego el otro),
lo cual nunca ejercita la condición de carrera real del SETNX bajo asyncio.gather.

También documenta, con una prueba explícita, el comportamiento que motivó el lock por
caso del hallazgo E: dos payloads DISTINTOS para el mismo Smart_Code__c generan claves
de idempotencia distintas y NO se bloquean entre sí -- ambos "ganan" el candado
PROCESSING de forma independiente. Ese es exactamente el hueco que RedisLock (en
routes_quejas.py) cierra por fuera de este servicio.
"""
import asyncio
import os
import unittest

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.idempotency_service import IdempotencyService

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
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo pruebas de concurrencia de "
    "idempotencia. Levante un Redis local (ej. `docker run --rm -p 6379:6379 "
    "redis:7-alpine`) para ejecutarlas."
)
class TestIdempotenciaConcurrenciaReal(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.idempotency_service = IdempotencyService(redis_client=self.redis)

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def test_n_requests_identicos_concurrentes_solo_uno_procede_los_demas_ven_processing(self):
        payload = {"Smart_Code__c": "SC-CONC-1", "Status": "In Progress"}
        N = 15

        resultados = await asyncio.gather(*[
            self.idempotency_service.verificar_o_iniciar_operacion(
                smart_code="SC-CONC-1", payload_dict=payload
            )
            for _ in range(N)
        ])

        es_hits = [es_hit for es_hit, _ in resultados]
        ganadores = [i for i, es_hit in enumerate(es_hits) if not es_hit]
        perdedores = [i for i, es_hit in enumerate(es_hits) if es_hit]

        self.assertEqual(len(ganadores), 1, "Exactamente un request debe proceder a hacer trabajo real")
        self.assertEqual(len(perdedores), N - 1)

        for i in perdedores:
            respuesta = resultados[i][1]
            self.assertEqual(respuesta["status"], "processing")
            self.assertTrue(respuesta["is_idempotent_hit"])

    async def test_tras_registrar_exito_del_ganador_un_nuevo_request_ve_hit_exitoso(self):
        payload = {"Smart_Code__c": "SC-CONC-2", "Status": "In Progress"}

        es_hit_inicial, _ = await self.idempotency_service.verificar_o_iniciar_operacion(
            smart_code="SC-CONC-2", payload_dict=payload
        )
        self.assertFalse(es_hit_inicial)

        await self.idempotency_service.registrar_exito(
            smart_code="SC-CONC-2", payload_dict=payload, sfc_response={"codigo_queja": "SC-CONC-2"}
        )

        es_hit_posterior, respuesta = await self.idempotency_service.verificar_o_iniciar_operacion(
            smart_code="SC-CONC-2", payload_dict=payload
        )
        self.assertTrue(es_hit_posterior)
        self.assertEqual(respuesta["status"], "success")
        self.assertTrue(respuesta["is_idempotent_hit"])

    async def test_payloads_distintos_del_mismo_smart_code_no_se_bloquean_entre_si(self):
        """
        Documenta el hueco real que cierra el lock por caso del hallazgo E: la
        idempotencia por sí sola NO serializa dos operaciones distintas del mismo
        caso -- ambas 'ganan' su propio candado PROCESSING de forma independiente,
        porque sus claves (smart_code:operacion:hash) son distintas.
        """
        payload_tramite = {"Smart_Code__c": "SC-CONC-3", "Status": "In Progress"}
        payload_cierre = {
            "Smart_Code__c": "SC-CONC-3", "Status": "Closed",
            "Favorabilidad__c": "No favorable", "Aceptacion__c": "Aceptada"
        }

        (es_hit_tramite, _), (es_hit_cierre, _) = await asyncio.gather(
            self.idempotency_service.verificar_o_iniciar_operacion(
                smart_code="SC-CONC-3", payload_dict=payload_tramite
            ),
            self.idempotency_service.verificar_o_iniciar_operacion(
                smart_code="SC-CONC-3", payload_dict=payload_cierre
            ),
        )

        self.assertFalse(es_hit_tramite, "El trámite debe proceder -- clave distinta a la del cierre")
        self.assertFalse(es_hit_cierre, "El cierre debe proceder -- clave distinta a la del trámite")


if __name__ == "__main__":
    unittest.main()
