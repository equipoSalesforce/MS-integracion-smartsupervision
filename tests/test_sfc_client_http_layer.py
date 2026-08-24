# tests/test_sfc_client_http_layer.py
"""
Cobertura de la capa HTTP de app/integrations/sfc_client.py: los hooks de
auditoría (log_request/log_response), el decorador de reintento por
throttling (handle_sfc_throttling), la validación SSRF de 'next' URLs, y los
métodos HTTP reales de SfcClient. Hasta ahora ~35% de cobertura -- casi toda
la suite reemplaza SfcClient por un AsyncMock, así que este código (el que de
verdad habla HTTP con la SFC) casi nunca se ejercita.

Los métodos de SfcClient se prueban con httpx.MockTransport (sin red real);
el interceptor de auth se pasa como None -- SfcClient siempre pasa
`auth=self.interceptor` explícito a cada llamada, así que `auth=None`
simplemente desactiva la autenticación para esa petición sin afectar lo que
se está probando aquí (ya cubierto en test_auth_flow_interceptor.py).
"""
import json
import unittest
from unittest.mock import patch, AsyncMock

import httpx

from app.core.exceptions import SfcIntegrationException
from app.integrations.sfc_client import (
    SfcClient,
    handle_sfc_throttling,
    log_request,
    log_response,
)


def _client_con_transport(handler) -> SfcClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return SfcClient(interceptor=None, http_client=http_client)


class TestSanitizarYValidarNextUrl(unittest.TestCase):

    def setUp(self):
        self.client = SfcClient(interceptor=None, http_client=httpx.AsyncClient())
        self.base_host = httpx.URL(self.client.base_url).host

    def test_url_vacia_retorna_none(self):
        self.assertIsNone(self.client._sanitizar_y_validar_next_url(None))
        self.assertIsNone(self.client._sanitizar_y_validar_next_url("   "))

    def test_mismo_host_retorna_path_y_query(self):
        next_url = f"https://{self.base_host}/api/queja/?page=2"
        resultado = self.client._sanitizar_y_validar_next_url(next_url)
        self.assertEqual(resultado, "/api/queja/?page=2")

    def test_host_distinto_lanza_ssrf_protection_error(self):
        with self.assertRaises(SfcIntegrationException) as ctx:
            self.client._sanitizar_y_validar_next_url("https://evil.attacker.com/api/queja/?page=2")
        self.assertEqual(ctx.exception.error_type, "SSRF_PROTECTION_ERROR")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_path_relativo_sin_host_se_acepta_tal_cual(self):
        resultado = self.client._sanitizar_y_validar_next_url("/api/queja/?page=3")
        self.assertEqual(resultado, "/api/queja/?page=3")


class TestHandleSfcThrottling(unittest.IsolatedAsyncioTestCase):

    async def test_reintenta_ante_429_y_termina_en_exito(self):
        llamadas = {"n": 0}

        @handle_sfc_throttling
        async def flaky():
            llamadas["n"] += 1
            if llamadas["n"] == 1:
                raise SfcIntegrationException(
                    status_code=429, error_type="THROTTLED_ERROR",
                    sfc_field=None, raw_message="rate limited", crm_action="retry"
                )
            return "ok"

        with patch("app.integrations.sfc_client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            resultado = await flaky()

        self.assertEqual(resultado, "ok")
        self.assertEqual(llamadas["n"], 2)
        mock_sleep.assert_awaited_once()

    async def test_agota_reintentos_y_relanza(self):
        llamadas = {"n": 0}

        @handle_sfc_throttling
        async def siempre_regulado():
            llamadas["n"] += 1
            raise SfcIntegrationException(
                status_code=429, error_type="THROTTLED_ERROR",
                sfc_field=None, raw_message="rate limited", crm_action="retry"
            )

        with patch("app.integrations.sfc_client.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException):
                await siempre_regulado()

        # settings.SFC_MINI_RETRY_ATTEMPTS por defecto es 2 -- 1 intento inicial + 2 reintentos = 3 llamadas.
        self.assertEqual(llamadas["n"], 3)

    async def test_error_no_throttled_no_reintenta(self):
        llamadas = {"n": 0}

        @handle_sfc_throttling
        async def error_de_validacion():
            llamadas["n"] += 1
            raise SfcIntegrationException(
                status_code=400, error_type="CRM_PAYLOAD_VALIDATION_ERROR",
                sfc_field="numero_id_CF", raw_message="campo inválido", crm_action="corregir"
            )

        with self.assertRaises(SfcIntegrationException):
            await error_de_validacion()

        self.assertEqual(llamadas["n"], 1)


class TestSfcClientMetodosHttp(unittest.IsolatedAsyncioTestCase):

    async def test_fetch_quejas_pagina_exito(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": [], "next": None})

        client = _client_con_transport(handler)
        resultado = await client.fetch_quejas_pagina()
        self.assertEqual(resultado, {"results": [], "next": None})
        await client.close()

    async def test_fetch_quejas_pagina_error_http_se_traduce(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "Internal error"})

        client = _client_con_transport(handler)
        with patch("app.core.exceptions.SfcErrorTranslator.obtener_matriz_errores", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException) as ctx:
                await client.fetch_quejas_pagina()
        self.assertEqual(ctx.exception.status_code, 500)
        await client.close()

    async def test_error_de_red_se_traduce_a_503(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = _client_con_transport(handler)
        with patch("app.core.exceptions.SfcErrorTranslator.obtener_matriz_errores", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.email_service.EmailAlertService.notificar_error_no_mapeado", new_callable=AsyncMock):
            with self.assertRaises(SfcIntegrationException) as ctx:
                await client.fetch_quejas_pagina()
        self.assertEqual(ctx.exception.status_code, 503)
        await client.close()

    async def test_post_nueva_queja_envia_payload_y_parsea_respuesta(self):
        capturado = {}

        def handler(request: httpx.Request) -> httpx.Response:
            capturado["body"] = json.loads(request.content)
            capturado["url"] = str(request.url)
            return httpx.Response(201, json={"codigo_queja": "SC-1"})

        client = _client_con_transport(handler)
        resultado = await client.post_nueva_queja({"codigo_queja": "SC-1", "estado_cod": 1})

        self.assertEqual(resultado, {"codigo_queja": "SC-1"})
        self.assertEqual(capturado["body"], {"codigo_queja": "SC-1", "estado_cod": 1})
        await client.close()

    async def test_post_adjunto_queja_trunca_nombre_largo_y_envia_multipart(self):
        capturado = {}

        def handler(request: httpx.Request) -> httpx.Response:
            capturado["content_type"] = request.headers.get("content-type", "")
            capturado["extensions"] = request.extensions.get("sfc_signature_fields")
            return httpx.Response(200, json={"status": "ok"})

        client = _client_con_transport(handler)
        nombre_muy_largo = ("a" * 200) + ".pdf"
        resultado = await client.post_adjunto_queja(
            sfc_codigo_queja="SC-1", file_data=b"%PDF-1.4 contenido", file_type="pdf", file_name=nombre_muy_largo
        )

        self.assertEqual(resultado, {"status": "ok"})
        self.assertTrue(capturado["content_type"].startswith("multipart/form-data"))
        self.assertEqual(capturado["extensions"], {"codigo_queja": "SC-1", "type": "pdf"})
        await client.close()

    async def test_put_actualizar_queja_usa_patch_y_url_con_codigo(self):
        capturado = {}

        def handler(request: httpx.Request) -> httpx.Response:
            capturado["method"] = request.method
            capturado["url"] = str(request.url)
            return httpx.Response(200, json={"status": "closed"})

        client = _client_con_transport(handler)
        resultado = await client.put_actualizar_queja("SC-1", {"estado_cod": 4})

        self.assertEqual(resultado, {"status": "closed"})
        self.assertEqual(capturado["method"], "PATCH")
        self.assertTrue(capturado["url"].endswith("/SC-1/"))
        await client.close()


class TestLogRequestResponse(unittest.IsolatedAsyncioTestCase):

    async def test_log_request_json_body_no_crashea(self):
        request = httpx.Request(
            "POST", "https://sfc.test/api/queja/",
            content=json.dumps({"a": 1}).encode("utf-8"),
        )
        await log_request(request)  # No debe lanzar; sólo se verifica ausencia de crash.

    async def test_log_request_archivo_se_omite_sin_crashear(self):
        request = httpx.Request(
            "POST", "https://sfc.test/api/storage/",
            content=b"binario",
        )
        await log_request(request)

    async def test_log_request_body_no_json_no_crashea(self):
        request = httpx.Request("POST", "https://sfc.test/api/queja/", content=b"no-es-json")
        await log_request(request)

    async def test_log_response_json_no_crashea(self):
        request = httpx.Request("GET", "https://sfc.test/api/queja/")
        response = httpx.Response(200, json={"a": 1}, request=request)
        await log_response(response)

    async def test_log_response_no_json_usa_texto_plano_sin_crashear(self):
        request = httpx.Request("GET", "https://sfc.test/api/queja/")
        response = httpx.Response(200, content=b"<html>error</html>", request=request)
        await log_response(response)

    async def test_log_response_pdf_se_omite_sin_crashear(self):
        request = httpx.Request("GET", "https://sfc.test/api/storage/algo.pdf")
        response = httpx.Response(
            200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}, request=request
        )
        await log_response(response)


if __name__ == "__main__":
    unittest.main()
