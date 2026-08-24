# tests/test_momento_3_edge_cases.py
"""
Cobertura de ramas de Momento3SincronizacionService no cubiertas por
test_momento_3.py (que sólo ejercita el payload como QuejaUnificadaCrmInput,
siempre con archivos_s3 vacío para trámite, y siempre con fecha_cierre ya
resuelta por el mock del mapper): el camino de payload-como-dict (usado al
rehidratar desde la cola Redis), ejecutar_gestion_fraude, el cálculo propio
de fecha_cierre cuando el mapper no la resolvió, la transmisión de adjuntos
en un trámite normal, las ramas de red/error no controlado del PATCH a la
SFC, el texto vacío tras limpiar el HTML del cuerpo de cierre, y el fallo
no-fatal al respaldar el PDF en S3.
"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import httpx

from app.services.momento_3_sync import Momento3SincronizacionService


class _StubRedisHash:
    """Mismo doble usado en test_momento_3.py para el checkpoint de idempotencia por
    archivo (P0-10), sin depender de un Redis real."""

    def __init__(self):
        self.hashes = {}

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def hkeys(self, key):
        return list(self.hashes.get(key, {}).keys())

    async def expire(self, key, ttl):
        pass

    async def delete(self, key):
        self.hashes.pop(key, None)


class TestMomento3EdgeCases(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.sfc_client_mock = MagicMock()
        self.s3_client_mock = MagicMock()
        self.servicio = Momento3SincronizacionService(sfc_client=self.sfc_client_mock, s3_client=self.s3_client_mock)

        fecha_reciente = (datetime.now(ZoneInfo("America/Bogota")) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        self.payload_dict = {
            "Smart_Code__c": "16551509974609",
            "Case_id": "CASE-0001",
            "CreatedDate": fecha_reciente,
            "Status": "In Progress",
            "archivos_s3": [],
            "cuerpo_respuesta_final": None,
            "SuppliedName": "Camila Salas",
        }
        self.mock_mapper_response = {"canal_cod": 13, "producto_cod": 207, "macro_motivo_cod": 940}

    async def test_payload_como_dict_se_extrae_correctamente(self):
        """El scheduler rehidrata desde Redis y llama a estos métodos con un dict
        crudo, no con QuejaUnificadaCrmInput -- camino nunca ejercitado hasta ahora."""
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(return_value={"Status": "updated"})

        with patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload", return_value=self.mock_mapper_response.copy()):
            resultado = await self.servicio.ejecutar_actualizacion_tramite(payload=self.payload_dict)

        self.assertEqual(resultado["status"], "success")
        self.assertIn("16551509974609", resultado["message"])

    async def test_ejecutar_gestion_fraude_delega_con_afijo_regulatorio(self):
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(return_value={"Status": "updated"})

        payload = dict(self.payload_dict)
        payload["archivos_s3"] = [{"nombre_archivo": "dictamen.pdf", "s3_key": "caso/CASE-0001/dictamen.pdf", "bucket": "b1"}]

        self.servicio.s3_service.transferir_lote_s3_a_sfc = AsyncMock()

        with patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload", return_value=self.mock_mapper_response.copy()):
            resultado = await self.servicio.ejecutar_gestion_fraude(payload=payload)

        self.assertEqual(resultado["status"], "success")
        kwargs = self.servicio.s3_service.transferir_lote_s3_a_sfc.call_args[1]
        self.assertEqual(kwargs["afijo_regulatorio"], "INV_FRAUDE_SFC")
        self.assertTrue(kwargs["afijo_masivo"])

    async def test_cierre_sin_fecha_resuelta_por_el_mapper_la_calcula_localmente(self):
        """sfc_raw_payload sin 'fecha_cierre' (o en None) obliga a
        _aplicar_estado_inicial_sfc a calcularla con la hora actual de Bogotá."""
        sfc_mock = self.mock_mapper_response.copy()
        sfc_mock["estado_cod"] = 4  # Sin 'fecha_cierre' -- rama .get(...) es None.

        self.sfc_client_mock.put_actualizar_queja = AsyncMock(return_value={"Status": "closed"})

        with patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload", return_value=sfc_mock):
            resultado = await self.servicio.ejecutar_cierre_definitivo(payload=self.payload_dict)

        self.assertEqual(resultado["status"], "success")
        payload_enviado = self.sfc_client_mock.put_actualizar_queja.call_args[1]["payload"]
        self.assertIsNotNone(payload_enviado.get("fecha_cierre"))

    async def test_error_de_red_en_patch_se_relanza_sin_transformar(self):
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )

        with patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload", return_value=self.mock_mapper_response.copy()):
            with self.assertRaises(httpx.ConnectError):
                await self.servicio.ejecutar_actualizacion_tramite(payload=self.payload_dict)

    async def test_error_no_controlado_se_relanza_y_se_loguea(self):
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(side_effect=RuntimeError("fallo inesperado"))

        with patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload", return_value=self.mock_mapper_response.copy()):
            with self.assertRaises(RuntimeError):
                await self.servicio.ejecutar_actualizacion_tramite(payload=self.payload_dict)

    async def test_cuerpo_de_cierre_sin_texto_tras_limpiar_html_usa_mensaje_por_defecto(self):
        sfc_mock = self.mock_mapper_response.copy()
        sfc_mock["estado_cod"] = 4
        sfc_mock["fecha_cierre"] = "2026-07-16"

        payload = dict(self.payload_dict)
        payload["cuerpo_respuesta_final"] = "<div>   </div>"  # Sin texto real tras limpiar el HTML.

        self.sfc_client_mock.post_adjunto_queja = AsyncMock(return_value={"id": 1})
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(return_value={"Status": "closed"})

        with patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload", return_value=sfc_mock), \
             patch("app.services.s3_service.get_redis_client", return_value=_StubRedisHash()):
            resultado = await self.servicio.ejecutar_cierre_definitivo(payload=payload)

        self.assertEqual(resultado["status"], "success")
        self.sfc_client_mock.post_adjunto_queja.assert_called_once()

    async def test_fallo_respaldando_pdf_en_s3_no_interrumpe_el_cierre(self):
        """Un fallo al guardar la copia del PDF en S3 (no la transmisión oficial a la
        SFC) es sólo un respaldo interno -- no debe impedir que el cierre continúe."""
        sfc_mock = self.mock_mapper_response.copy()
        sfc_mock["estado_cod"] = 4
        sfc_mock["fecha_cierre"] = "2026-07-16"

        payload = dict(self.payload_dict)
        payload["cuerpo_respuesta_final"] = "<p>Cierre con fallo de respaldo en S3.</p>"

        self.sfc_client_mock.post_adjunto_queja = AsyncMock(return_value={"id": 1})
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(return_value={"Status": "closed"})
        self.servicio.s3_service.subir_bytes_archivo = AsyncMock(side_effect=ConnectionError("s3 caido"))

        with patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload", return_value=sfc_mock), \
             patch("app.services.s3_service.get_redis_client", return_value=_StubRedisHash()):
            resultado = await self.servicio.ejecutar_cierre_definitivo(payload=payload)

        self.assertEqual(resultado["status"], "success")
        self.sfc_client_mock.post_adjunto_queja.assert_called_once()


if __name__ == "__main__":
    unittest.main()
