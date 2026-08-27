# tests/test_middleware.py
"""
Cobertura de app/core/middleware.py -- sin tests previos. CorrelationIdMiddleware
se prueba end-to-end vía TestClient (necesita el ciclo real de Starlette para
ejercitar el header X-Amzn-Trace-Id de respuesta); MaxBodySizeMiddleware se
prueba como ASGI puro con receive/send falsos, sin FastAPI de por medio, para
cubrir las tres rutas de rechazo (Content-Length declarado, chunked sin
Content-Length confiable, y scope no-http que debe pasar de largo).
"""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.middleware import CorrelationIdMiddleware, MaxBodySizeMiddleware, get_correlation_id, get_aws_trace_id


class TestCorrelationIdMiddleware(unittest.TestCase):

    def setUp(self):
        self.app = FastAPI()
        self.app.add_middleware(CorrelationIdMiddleware)

        @self.app.get("/ping")
        def ping():
            return {"ok": True}

        self.client = TestClient(self.app)

    def test_propaga_aws_trace_id_en_la_respuesta_si_viene_en_la_peticion(self):
        response = self.client.get("/ping", headers={"X-Amzn-Trace-Id": "trace-abc"})
        self.assertEqual(response.headers["X-Amzn-Trace-Id"], "trace-abc")

    def test_sin_aws_trace_id_no_agrega_el_header_en_la_respuesta(self):
        response = self.client.get("/ping")
        self.assertNotIn("X-Amzn-Trace-Id", response.headers)
        self.assertIn("X-Correlation-ID", response.headers)

    def test_correlation_id_provisto_por_el_cliente_se_respeta_no_se_reemplaza(self):
        """Si el CRM manda su propio X-Correlation-ID, debe propagarse tal cual
        (no generarse uno nuevo) -- es la clave que el CRM usa para correlacionar
        sus propios logs con los nuestros."""
        response = self.client.get("/ping", headers={"X-Correlation-ID": "cid-del-crm-123"})
        self.assertEqual(response.headers["X-Correlation-ID"], "cid-del-crm-123")

    def test_sin_correlation_id_se_genera_uno_nuevo_valido(self):
        import uuid
        response = self.client.get("/ping")
        cid = response.headers["X-Correlation-ID"]
        # No debe crashear -- confirma que es un UUID válido, no un placeholder vacío.
        uuid.UUID(cid)

    def test_dos_requests_sin_correlation_id_reciben_valores_distintos(self):
        r1 = self.client.get("/ping")
        r2 = self.client.get("/ping")
        self.assertNotEqual(r1.headers["X-Correlation-ID"], r2.headers["X-Correlation-ID"])

    def test_get_correlation_id_dentro_del_handler_ve_el_mismo_valor_de_la_peticion(self):
        """El ContextVar debe reflejar el correlation_id de LA PETICIÓN ACTUAL
        dentro del propio handler -- no sólo en el header de respuesta."""
        capturado = {}

        app2 = FastAPI()
        app2.add_middleware(CorrelationIdMiddleware)

        @app2.get("/capturar")
        def capturar():
            capturado["cid"] = get_correlation_id()
            capturado["trace"] = get_aws_trace_id()
            return {"ok": True}

        client2 = TestClient(app2)
        client2.get("/capturar", headers={"X-Correlation-ID": "cid-visible-en-handler", "X-Amzn-Trace-Id": "trace-visible"})

        self.assertEqual(capturado["cid"], "cid-visible-en-handler")
        self.assertEqual(capturado["trace"], "trace-visible")

    def test_contextvar_se_libera_incluso_si_el_handler_lanza_una_excepcion(self):
        """El `finally` debe resetear el ContextVar aunque call_next propague una
        excepción -- de lo contrario, el correlation_id de una request fallida
        podría filtrarse hacia la siguiente request atendida por el mismo
        worker/tarea si algo reutiliza el ContextVar por fuera del ciclo normal."""
        app3 = FastAPI()
        app3.add_middleware(CorrelationIdMiddleware)

        @app3.get("/explota")
        def explota():
            raise RuntimeError("fallo simulado del handler")

        client3 = TestClient(app3, raise_server_exceptions=False)
        response = client3.get("/explota", headers={"X-Correlation-ID": "cid-de-la-request-fallida"})

        self.assertEqual(response.status_code, 500)
        # Tras la excepción, el contexto por defecto (fuera de cualquier request) debe
        # volver a su valor neutro -- no debe quedar "pegado" al de la request fallida.
        self.assertEqual(get_correlation_id(), "N/A")


class TestMaxBodySizeMiddlewareAsgi(unittest.IsolatedAsyncioTestCase):

    def _app_ok(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        return app

    async def _run(self, middleware, scope, messages):
        idx = {"i": 0}

        async def receive():
            msg = messages[idx["i"]]
            idx["i"] = min(idx["i"] + 1, len(messages) - 1)
            return msg

        enviados = []

        async def send(message):
            enviados.append(message)

        await middleware(scope, receive, send)
        return enviados

    async def test_scope_no_http_delega_directo_al_app_sin_envolver_receive(self):
        llamadas = {"scope_recibido": None}

        async def app_marcador(scope, receive, send):
            llamadas["scope_recibido"] = scope
            await send({"type": "lifespan.startup.complete"})

        middleware = MaxBodySizeMiddleware(app_marcador, max_body_size=1000)
        enviados = await self._run(
            middleware, {"type": "lifespan", "headers": []}, [{"type": "lifespan.startup"}]
        )

        self.assertEqual(llamadas["scope_recibido"]["type"], "lifespan")
        self.assertEqual(enviados, [{"type": "lifespan.startup.complete"}])

    async def test_content_length_excede_el_limite_rechaza_con_413_sin_leer_el_body(self):
        middleware = MaxBodySizeMiddleware(self._app_ok(), max_body_size=10)
        scope = {"type": "http", "headers": [(b"content-length", b"999")]}

        enviados = await self._run(middleware, scope, [{"type": "http.request", "body": b""}])

        self.assertEqual(enviados[0]["status"], 413)

    async def test_content_length_invalido_no_crashea_y_continua(self):
        middleware = MaxBodySizeMiddleware(self._app_ok(), max_body_size=10)
        scope = {"type": "http", "headers": [(b"content-length", b"no-es-un-numero")]}

        enviados = await self._run(
            middleware, scope, [{"type": "http.request", "body": b"ok", "more_body": False}]
        )

        self.assertEqual(enviados[0]["status"], 200)

    async def test_body_chunked_excede_el_limite_durante_la_lectura_rechaza_con_413(self):
        middleware = MaxBodySizeMiddleware(self._app_ok(), max_body_size=5)
        scope = {"type": "http", "headers": []}

        async def app_que_lee_todo(scope, receive, send):
            while True:
                msg = await receive()
                if not msg.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware.app = app_que_lee_todo

        enviados = await self._run(
            middleware, scope,
            [
                {"type": "http.request", "body": b"123456", "more_body": False},
            ],
        )

        self.assertEqual(enviados[0]["status"], 413)

    async def test_body_dentro_del_limite_no_rechaza(self):
        middleware = MaxBodySizeMiddleware(self._app_ok(), max_body_size=100)
        scope = {"type": "http", "headers": []}

        enviados = await self._run(
            middleware, scope, [{"type": "http.request", "body": b"pequeno", "more_body": False}]
        )

        self.assertEqual(enviados[0]["status"], 200)


if __name__ == "__main__":
    unittest.main()
