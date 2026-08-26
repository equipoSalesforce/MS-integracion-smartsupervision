# tests/test_auth_flow_interceptor.py
"""
Cobertura de SfcAuthManager.async_auth_flow / _preparar_headers_y_firma
(app/core/auth.py) -- el interceptor real de httpx.Auth que inyecta
Authorization/X-SFC-Signature en cada petición a la SFC y maneja el reintento
post-401. Hasta ahora, ~0% de cobertura de líneas: los tests de auth
existentes (test_auth_and_signatures.py, test_auth_concurrency.py) prueban
get_valid_token() y las estrategias de firma por separado, pero ninguno
invoca el generador async_auth_flow() como lo hace httpx en producción.

Se invoca el generador directamente (sin httpx.Client/MockTransport) para
tener control total sobre el request construido, sin que httpx inyecte
headers automáticos (ej. content-type) que interfieran con lo que estamos
probando.
"""
import json
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

import httpx

from app.core.auth import SfcAuthManager
from app.core.security.signatures import SfcSignatureContext, UrlSignatureStrategy, PayloadSignatureStrategy, FileTransferSignatureStrategy

SECRET_KEY = "test-secret-key-para-firmas"


class TestAsyncAuthFlow(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.sig_ctx = SfcSignatureContext(secret_key=SECRET_KEY)
        self.manager = SfcAuthManager(signature_context=self.sig_ctx, http_client=httpx.AsyncClient())
        # Aísla el flujo de la coordinación distribuida en Redis -- no es lo que se prueba aquí.
        self._redis_patch = patch("app.core.auth.get_redis_client", return_value=None)
        self._redis_patch.start()

    async def asyncTearDown(self):
        self._redis_patch.stop()
        await self.manager.client.aclose()

    async def test_login_path_bypassa_headers_y_no_pide_token(self):
        request = httpx.Request("POST", "https://sfc.test/api/login")
        with patch.object(self.manager, "get_valid_token", new_callable=AsyncMock) as mock_token:
            gen = self.manager.async_auth_flow(request)
            sent = await gen.__anext__()
            with self.assertRaises(StopAsyncIteration):
                await gen.asend(httpx.Response(200, request=sent))

        mock_token.assert_not_called()
        self.assertNotIn("Authorization", sent.headers)
        self.assertNotIn("X-SFC-Signature", sent.headers)

    async def test_get_inyecta_headers_y_firma_la_url_completa(self):
        request = httpx.Request("GET", "https://sfc.test/api/queja/?estado=1")
        with patch.object(self.manager, "get_valid_token", new_callable=AsyncMock, return_value="token-abc"):
            gen = self.manager.async_auth_flow(request)
            sent = await gen.__anext__()
            with self.assertRaises(StopAsyncIteration):
                await gen.asend(httpx.Response(200, request=sent))

        self.assertEqual(sent.headers["Authorization"], "Bearer token-abc")
        self.assertEqual(sent.headers["Cache-Control"], "no-cache")
        self.assertEqual(sent.headers["Accept"], "application/json")
        self.assertEqual(sent.headers["accept-language"], "es")

        firma_esperada = UrlSignatureStrategy(SECRET_KEY).sign(str(sent.url))
        self.assertEqual(sent.headers["X-SFC-Signature"], firma_esperada)

    async def test_accept_language_existente_no_se_sobreescribe(self):
        request = httpx.Request("GET", "https://sfc.test/api/queja/", headers={"accept-language": "en"})
        with patch.object(self.manager, "get_valid_token", new_callable=AsyncMock, return_value="token-abc"):
            gen = self.manager.async_auth_flow(request)
            sent = await gen.__anext__()
            with self.assertRaises(StopAsyncIteration):
                await gen.asend(httpx.Response(200, request=sent))

        self.assertEqual(sent.headers["accept-language"], "en")

    async def test_post_json_fuerza_content_type_y_firma_el_payload(self):
        body = {"codigo_queja": "SC-1", "estado_cod": 4}
        request = httpx.Request("POST", "https://sfc.test/api/queja/", content=json.dumps(body).encode("utf-8"))
        with patch.object(self.manager, "get_valid_token", new_callable=AsyncMock, return_value="token-abc"):
            gen = self.manager.async_auth_flow(request)
            sent = await gen.__anext__()
            with self.assertRaises(StopAsyncIteration):
                await gen.asend(httpx.Response(200, request=sent))

        self.assertEqual(sent.headers["content-type"], "application/json")
        firma_esperada = PayloadSignatureStrategy(SECRET_KEY).sign(body)
        self.assertEqual(sent.headers["X-SFC-Signature"], firma_esperada)

    async def test_firma_no_es_byte_exacta_sobre_el_body_realmente_enviado(self):
        """
        Auditoría del flujo de despacho/reintentos (2026-08-26): el test de arriba
        construye el Request con `content=json.dumps(body).encode()` -- separadores
        de Python por defecto (con espacios: ', ' / ': ') -- así que el round-trip
        json.loads/json.dumps que hace _preparar_headers_y_firma reproduce
        exactamente esos mismos bytes por casualidad. Pero sfc_client.py, en la vida
        real, SIEMPRE construye sus requests con `client.post(url, json=payload, ...)`
        (ver post_nueva_queja, put_actualizar_queja, etc.) -- y httpx serializa `json=`
        con separadores COMPACTOS (',' / ':', sin espacios, ver httpx/_content.py). Ese
        es el request que este test reproduce.

        Confirmado (no un bug): la firma que este cliente calcula y envía en
        X-SFC-Signature no es un HMAC byte-exacto sobre `request.content` -- es un
        HMAC sobre una re-serialización con espacios de ese mismo contenido, EXACTAMENTE
        como especifica el script de referencia que la propia SFC entrega a cada
        entidad vigilada (`docs/SignatureGenerator_comment (1).txt`:
        `json.dumps(data, ensure_ascii=False)`, sin fijar `separators`). Confirmado
        también contra el ambiente QA real de la SFC.

        Este test fija el comportamiento CORRECTO como contrato explícito, para que un
        cambio futuro que "corrija" los separadores de PayloadSignatureStrategy para
        que coincidan con los bytes reales de httpx (un cambio que parecería
        obviamente correcto sin este contexto) no rompa la integración real con la
        SFC -- ya se intentó y produjo el mismo error de firma inválida del lado SFC.
        """
        body = {"Smart_Code__c": "12345", "Description": "Reclamo por transacción no reconocida"}
        # Igual que sfc_client.py de verdad: json= (no content=), para que httpx
        # serialice con SUS separadores compactos, no los de Python por defecto.
        request = httpx.Request("POST", "https://sfc.test/api/queja/", json=body)

        bytes_reales_enviados = request.content
        self.assertNotIn(b", ", bytes_reales_enviados, "httpx debe seguir serializando compacto (sin espacios)")

        with patch.object(self.manager, "get_valid_token", new_callable=AsyncMock, return_value="token-abc"):
            gen = self.manager.async_auth_flow(request)
            sent = await gen.__anext__()
            with self.assertRaises(StopAsyncIteration):
                await gen.asend(httpx.Response(200, request=sent))

        firma_calculada = sent.headers["X-SFC-Signature"]
        firma_byte_exacta_sobre_lo_enviado = UrlSignatureStrategy(SECRET_KEY).sign(
            bytes_reales_enviados.decode("utf-8")
        )  # reutiliza el mismo HMAC-SHA256-upper, sólo cambia qué string se firma

        self.assertNotEqual(
            firma_calculada, firma_byte_exacta_sobre_lo_enviado,
            "Si esto empieza a fallar, alguien cambió la firma para que sea byte-exacta "
            "sobre el body real -- coordinar con el equipo de la SFC antes de desplegarlo."
        )
        # Lo que SÍ es verdad hoy: la firma corresponde al round-trip json.loads/dumps.
        firma_esperada_hoy = PayloadSignatureStrategy(SECRET_KEY).sign(json.loads(bytes_reales_enviados))
        self.assertEqual(firma_calculada, firma_esperada_hoy)

    async def test_multipart_usa_extensions_para_firma_y_no_fuerza_json(self):
        campos_firma = {"codigo_queja": "SC-1", "type": "pdf"}
        request = httpx.Request(
            "POST", "https://sfc.test/api/storage/",
            headers={"content-type": "multipart/form-data; boundary=xyz"},
            content=b"--xyz--",
            extensions={"sfc_signature_fields": campos_firma},
        )
        with patch.object(self.manager, "get_valid_token", new_callable=AsyncMock, return_value="token-abc"):
            gen = self.manager.async_auth_flow(request)
            sent = await gen.__anext__()
            with self.assertRaises(StopAsyncIteration):
                await gen.asend(httpx.Response(200, request=sent))

        self.assertTrue(sent.headers["content-type"].startswith("multipart/form-data"))
        firma_esperada = FileTransferSignatureStrategy(SECRET_KEY).sign(campos_firma)
        self.assertEqual(sent.headers["X-SFC-Signature"], firma_esperada)

    async def test_signature_ya_provista_no_se_recalcula(self):
        request = httpx.Request(
            "POST", "https://sfc.test/api/queja/",
            headers={"X-SFC-Signature": "FIRMA-YA-CALCULADA"},
            content=b'{"a": 1}',
        )
        with patch.object(self.manager, "get_valid_token", new_callable=AsyncMock, return_value="token-abc"), \
             patch.object(self.sig_ctx, "get_signature") as mock_get_sig:
            gen = self.manager.async_auth_flow(request)
            sent = await gen.__anext__()
            with self.assertRaises(StopAsyncIteration):
                await gen.asend(httpx.Response(200, request=sent))

        self.assertEqual(sent.headers["X-SFC-Signature"], "FIRMA-YA-CALCULADA")
        mock_get_sig.assert_not_called()

    async def test_401_reintenta_con_token_y_firma_renovados(self):
        request = httpx.Request("GET", "https://sfc.test/api/queja/")
        with patch.object(self.manager, "get_valid_token", new_callable=AsyncMock, return_value="token-viejo"), \
             patch.object(self.manager, "_get_valid_token_unlocked", new_callable=AsyncMock, return_value="token-nuevo"):
            gen = self.manager.async_auth_flow(request)
            primer_envio = await gen.__anext__()
            self.assertEqual(primer_envio.headers["Authorization"], "Bearer token-viejo")

            respuesta_401 = httpx.Response(401, request=primer_envio)
            segundo_envio = await gen.asend(respuesta_401)

            self.assertEqual(segundo_envio.headers["Authorization"], "Bearer token-nuevo")
            firma_esperada = UrlSignatureStrategy(SECRET_KEY).sign(str(segundo_envio.url))
            self.assertEqual(segundo_envio.headers["X-SFC-Signature"], firma_esperada)

            with self.assertRaises(StopAsyncIteration):
                await gen.asend(httpx.Response(200, request=segundo_envio))

    async def test_401_con_recuperacion_fallida_no_reintenta_ni_relanza(self):
        """Si _get_valid_token_unlocked falla durante la recuperación post-401, se loguea
        y el generador termina sin volver a producir una petición (httpx conserva el 401 original)."""
        request = httpx.Request("GET", "https://sfc.test/api/queja/")
        with patch.object(self.manager, "get_valid_token", new_callable=AsyncMock, return_value="token-viejo"), \
             patch.object(self.manager, "_get_valid_token_unlocked", new_callable=AsyncMock, side_effect=RuntimeError("SFC login caído")):
            gen = self.manager.async_auth_flow(request)
            primer_envio = await gen.__anext__()

            respuesta_401 = httpx.Response(401, request=primer_envio)
            with self.assertRaises(StopAsyncIteration):
                await gen.asend(respuesta_401)

    async def test_reintento_post_401_con_adjunto_multipart_reenvia_el_archivo_completo(self):
        """
        Verificación del hallazgo de revisión externa (2026-08-25, §16): la
        hipótesis era que un reintento post-401 sobre una subida multipart
        (post_adjunto_queja) podía reenviar el archivo VACÍO -- porque el stream
        del archivo ya habría sido consumido durante el primer envío, y el único
        `seek(0)` en post_adjunto_queja ocurre ANTES de entrar al flujo de auth
        (no se repite para el reintento).

        Verificado CONTRA EL PIPELINE REAL (httpx.AsyncClient + MockTransport +
        SfcAuthManager, sin mockear el envío) con httpx==0.28.1 (versión pineada
        en requirements.txt): NO es un bug. httpx.FileField.render_data() hace su
        PROPIO `self.file.seek(0)` en cada iteración del stream (ver
        httpx/_multipart.py) -- se ejecuta de nuevo en el segundo envío,
        independientemente de cualquier seek() de nuestro lado. El archivo se
        retransmite completo en el reintento.

        Se deja este test como regresión: si una futura actualización de httpx
        cambiara ese comportamiento (o el proyecto migrara a otra librería HTTP),
        esta prueba lo detectaría.
        """
        contenido_real = b"contenido real del PDF adjunto de prueba, no vacio"
        tmp_file = tempfile.SpooledTemporaryFile(max_size=5 * 1024 * 1024)
        tmp_file.write(contenido_real)
        tmp_file.seek(0)

        cuerpos_recibidos = []

        async def handler(request: httpx.Request) -> httpx.Response:
            cuerpo = await request.aread()
            cuerpos_recibidos.append(cuerpo)
            if len(cuerpos_recibidos) == 1:
                return httpx.Response(401, request=request)
            return httpx.Response(200, json={"status": "ok"}, request=request)

        transport = httpx.MockTransport(handler)
        campos_firma = {"codigo_queja": "SC-1", "type": "pdf"}

        async with httpx.AsyncClient(transport=transport) as client:
            with patch.object(self.manager, "get_valid_token", new_callable=AsyncMock, return_value="token-viejo"), \
                 patch.object(self.manager, "_get_valid_token_unlocked", new_callable=AsyncMock, return_value="token-nuevo"):
                response = await client.post(
                    "https://sfc.test/api/storage/",
                    data=campos_firma,
                    files={"file": ("soporte.pdf", tmp_file, "application/pdf")},
                    auth=self.manager,
                    extensions={"sfc_signature_fields": campos_firma}
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(cuerpos_recibidos), 2, "Debe haber un primer envío (401) y un reintento (200)")
        self.assertIn(contenido_real, cuerpos_recibidos[0], "El primer envío debe llevar el archivo completo")
        self.assertIn(
            contenido_real, cuerpos_recibidos[1],
            "El REINTENTO post-401 debe llevar el archivo completo, no vacío -- si esto falla, "
            "httpx dejó de re-seekear el file-like object entre envíos y §16 pasó a ser un bug real."
        )

    async def test_solo_200_no_dispara_recuperacion(self):
        request = httpx.Request("GET", "https://sfc.test/api/queja/")
        with patch.object(self.manager, "get_valid_token", new_callable=AsyncMock, return_value="token-abc"), \
             patch.object(self.manager, "_get_valid_token_unlocked", new_callable=AsyncMock) as mock_unlocked:
            gen = self.manager.async_auth_flow(request)
            sent = await gen.__anext__()
            with self.assertRaises(StopAsyncIteration):
                await gen.asend(httpx.Response(200, request=sent))

        mock_unlocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
