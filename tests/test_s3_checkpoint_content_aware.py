# tests/test_s3_checkpoint_content_aware.py
"""
Regresión de un hallazgo de code review (2026-08-24), encontrado durante la
auditoría de bugs similares al de sfc_completado en queue_service.py: el
checkpoint por archivo de S3StorageService (_procesar_envio_s3_a_sfc) usaba
la s3_key como única identidad para decidir si un adjunto ya fue confirmado.

Para el PDF de respuesta final que genera momento_3_sync.py
(_generar_y_enviar_pdf_respuesta_final), esa s3_key es DETERMINISTA -- función
sólo del case_id, no del contenido. Si un reintento regeneraba el PDF con
contenido distinto (ej. un evento nuevo cambió cuerpo_respuesta_final antes de
que terminara un reintento anterior del mismo cierre), el checkpoint lo veía
como "ya confirmado" y post_adjunto_queja NUNCA se llamaba con el contenido
nuevo -- la SFC se quedaba con el PDF viejo pese a que el cierre reportaba
éxito.

El fix incorpora un hash del contenido a la identidad del checkpoint SÓLO
para adjuntos con bytes inline (hoy, sólo este PDF generado) -- los adjuntos
referenciados por una s3_key real subida por el CRM no cambian de
comportamiento, ver test_momento_3.py::test_cierre_pdf_ok_patch_falla_retry_no_duplica_el_documento
(mismo contenido en el retry -> mismo hash -> se sigue deduplicando).

Se prueba contra Redis real: el checkpoint vive en Redis vía IdempotencyService.
"""
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import redis.asyncio as redis_asyncio
except ImportError:
    redis_asyncio = None

from app.services.s3_service import S3StorageService

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

_PDF_ORIGINAL = b"%PDF-1.4 CONTENIDO ORIGINAL DE LA RESPUESTA FINAL"
_PDF_NUEVO = b"%PDF-1.4 CONTENIDO NUEVO Y DISTINTO DE LA RESPUESTA FINAL"


@unittest.skipUnless(
    _REDIS_OK,
    f"Redis no disponible en {TEST_REDIS_URL} — omitiendo la regresión del checkpoint content-aware. "
    "Levante un Redis local (ej. `docker run --rm -p 6379:6379 redis:7-alpine`) para ejecutarla."
)
class TestCheckpointDistingueContenidoDistintoBajoLaMismaKey(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.redis = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
        await self.redis.flushdb()
        self.s3_service = S3StorageService(s3_client=None, http_client=None)
        self.sfc_client_mock = MagicMock()
        self.sfc_client_mock.post_adjunto_queja = AsyncMock(return_value={"id": 1})
        self.s3_key_deterministico = "caso/CASE-CHK-1/Respuesta_Final_CASE-CHK-1_RESP_FINAL_SFC.pdf"

    async def asyncTearDown(self):
        await self.redis.flushdb()
        await self.redis.aclose()

    async def _enviar(self, contenido: bytes):
        with patch("app.services.s3_service.get_redis_client", return_value=self.redis):
            return await self.s3_service.transferir_lote_s3_a_sfc(
                sfc_client=self.sfc_client_mock,
                sfc_codigo_queja="1423CASE-CHK-1",
                adjuntos_crm=[{
                    "nombre_archivo": "Respuesta_Final_CASE-CHK-1_RESP_FINAL_SFC.pdf",
                    "s3_key": self.s3_key_deterministico,
                    "bytes": contenido,
                }],
                case_id="CASE-CHK-1",
            )

    async def test_mismo_contenido_en_retry_se_deduplica(self):
        await self._enviar(_PDF_ORIGINAL)
        resultado_retry = await self._enviar(_PDF_ORIGINAL)

        self.assertEqual(resultado_retry[0]["status"], "ALREADY_CONFIRMED_CHECKPOINT")
        self.sfc_client_mock.post_adjunto_queja.assert_awaited_once()

    async def test_contenido_distinto_bajo_la_misma_s3_key_si_se_retransmite(self):
        await self._enviar(_PDF_ORIGINAL)
        resultado_nuevo = await self._enviar(_PDF_NUEVO)

        self.assertEqual(resultado_nuevo[0]["status"], "OK")
        self.assertEqual(self.sfc_client_mock.post_adjunto_queja.await_count, 2)


if __name__ == "__main__":
    unittest.main()
