# tests/test_cola_redis.py
import json
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.services.queue_service import QueueService
from app.workers.scheduler import reintentar_despachos_pendientes_job
from app.core.exceptions import SfcIntegrationException
from app.api.dependencies import get_sfc_client, get_s3_client


class MockPipeline:
    """Emulador de Pipeline asíncrono transaccional de Redis para pruebas unitarias."""

    def __init__(self, redis_instance):
        self.redis = redis_instance
        self.commands = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def set(self, name, value, **kwargs):
        self.commands.append((self.redis.set, (name, value), kwargs))
        return self

    def sadd(self, name, *values):
        self.commands.append((self.redis.sadd, (name,) + values, {}))
        return self

    def srem(self, name, *values):
        self.commands.append((self.redis.srem, (name,) + values, {}))
        return self

    def zadd(self, name, mapping):
        self.commands.append((self.redis.zadd, (name, mapping), {}))
        return self

    def zrem(self, name, *values):
        self.commands.append((self.redis.zrem, (name,) + values, {}))
        return self

    def delete(self, *names):
        self.commands.append((self.redis.delete, names, {}))
        return self

    async def execute(self):
        results = []
        for func, args, kwargs in self.commands:
            res = await func(*args, **kwargs)
            results.append(res)
        self.commands.clear()
        return results


class MockAsyncRedis:
    """Emulador en memoria del cliente asíncrono de Redis para pruebas unitarias."""

    def __init__(self):
        self.counters = {}
        self.keys_store = {}
        self.sets = {}
        self.zsets = {}

    def pipeline(self, transaction=True):
        return MockPipeline(self)

    async def ping(self):
        return True

    async def incr(self, name):
        self.counters[name] = self.counters.get(name, 0) + 1
        return self.counters[name]

    async def set(self, name, value, nx=False, px=None):
        if nx and name in self.keys_store:
            return None
        self.keys_store[name] = str(value)
        return True

    async def get(self, name):
        return self.keys_store.get(name)

    async def delete(self, *names):
        count = 0
        for n in names:
            if n in self.keys_store:
                del self.keys_store[n]
                count += 1
            if n in self.sets:
                del self.sets[n]
            if n in self.zsets:
                del self.zsets[n]
        return count

    async def scard(self, name):
        return len(self.sets.get(name, set()))

    async def sadd(self, name, *values):
        if name not in self.sets:
            self.sets[name] = set()
        added = 0
        for v in values:
            str_v = str(v)
            if str_v not in self.sets[name]:
                self.sets[name].add(str_v)
                added += 1
        return added

    async def srem(self, name, *values):
        if name not in self.sets:
            return 0
        removed = 0
        for v in values:
            str_v = str(v)
            if str_v in self.sets[name]:
                self.sets[name].remove(str_v)
                removed += 1
        return removed

    async def smembers(self, name):
        return self.sets.get(name, set())

    async def sismember(self, name, value):
        return str(value) in self.sets.get(name, set())

    async def zadd(self, name, mapping):
        if name not in self.zsets:
            self.zsets[name] = {}
        for k, v in mapping.items():
            self.zsets[name][str(k)] = float(v)
        return len(mapping)

    async def zrem(self, name, *values):
        if name not in self.zsets:
            return 0
        removed = 0
        for v in values:
            str_v = str(v)
            if str_v in self.zsets[name]:
                del self.zsets[name][str_v]
                removed += 1
        return removed

    async def zrangebyscore(self, name, min_score, max_score):
        if name not in self.zsets:
            return []
        items = []
        for k, score in self.zsets[name].items():
            min_val = (
                float("-inf") if min_score in ("-inf", "-INF") else float(min_score)
            )
            max_val = (
                float("inf") if max_score in ("+inf", "+INF") else float(max_score)
            )

            if min_val <= score <= max_val:
                items.append((k, score))

        items.sort(key=lambda x: x[1])
        return [x[0] for x in items]

    async def keys(self, pattern):
        import re

        regex_pat = pattern.replace("*", ".*")
        return [k for k in self.keys_store.keys() if re.match(f"^{regex_pat}$", k)]


class TestColaRedis(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.mock_redis = MockAsyncRedis()
        self.s3_client_mock = MagicMock()
        app.dependency_overrides[get_s3_client] = lambda: self.s3_client_mock

        self.client = TestClient(app)
        self.client.headers.update({"X-API-Key": settings.CRM_API_KEY})

        self.smart_code_esperado = (
            f"{settings.SFC_TIPO_ENTIDAD}{settings.SFC_ENTIDAD_COD}999888777666"
        )

        self.payload_crm_test = {
            "Case_id": "999888777666",
            "Status": "New",
            "SuppliedName": "Prueba Contingencia Cola Redis",
            "SC_id_type__c": "CC",
            "id_number__c": "123456789",
            "sc_genero__c": "Femenino",
            "tipo_de_persona__c": "B2C",
            "direccion__c": "Calle 123",
            "Departamento__c": "Bogotá D.C.",
            "SC_municipio__c": "Bogotá D.C.",
            "canal__c": "Internet",
            "punto_recepcion": "Manual",
            "Product__c": "Cuenta perfil",
            "Categorias_COL__c": "Transacción no reconocida",
            "Description": "Test de encolado automático en Redis",
            "smart_escalamiento_DCF__c": "No",
            "archivos_s3": [],
        }

    async def asyncTearDown(self):
        app.dependency_overrides.clear()

    async def test_1_encolar_despacho_y_contar(self):
        """Verifica que el encolado en Redis registre los sets e incrementos correctamente."""
        service = QueueService(self.mock_redis)

        item = await service.encolar_despacho(
            smart_code="1286TEST001",
            tipo_operacion="AUTO",
            payload_json=self.payload_crm_test,
            error_inicial="HTTP 502 Bad Gateway",
        )

        self.assertEqual(item.id, 1)
        self.assertEqual(item.smart_code, "1286TEST001")
        self.assertEqual(item.estado, "PENDIENTE")

        count = await service.contar_pendientes()
        self.assertEqual(count, 1)

    async def test_2_marcar_exitoso_y_transicion_estado(self):
        """Verifica el movimiento de sets cuando un caso pasa a EXITOSO."""
        service = QueueService(self.mock_redis)

        item = await service.encolar_despacho(
            smart_code="1286TEST002",
            tipo_operacion="AUTO",
            payload_json=self.payload_crm_test,
            error_inicial="HTTP 502 Bad Gateway",
        )

        await service.marcar_exitoso(item.id)

        count_pendientes = await service.contar_pendientes()
        self.assertEqual(count_pendientes, 0)

        todos_exitosos = await service.obtener_todos_los_encolados(estado="EXITOSO")
        self.assertEqual(len(todos_exitosos), 1)
        self.assertEqual(todos_exitosos[0].id, item.id)

    async def test_3_registrar_fallo_y_max_intentos(self):
        """Verifica la lógica de retiros y cambio a FALLIDO_DEFINITIVO al exceder reintentos."""
        service = QueueService(self.mock_redis)

        item = await service.encolar_despacho(
            smart_code="1286TEST003",
            tipo_operacion="AUTO",
            payload_json=self.payload_crm_test,
            error_inicial="Error 1",
        )

        # Forzar límite máximo de intentos
        for i in range(settings.QUEUE_MAX_RETRIES):
            await service.registrar_fallo(item.id, f"Error {i+2}")

        fallidos = await service.obtener_todos_los_encolados(
            estado="FALLIDO_DEFINITIVO"
        )
        self.assertEqual(len(fallidos), 1)
        self.assertEqual(fallidos[0].estado, "FALLIDO_DEFINITIVO")


if __name__ == "__main__":
    unittest.main()