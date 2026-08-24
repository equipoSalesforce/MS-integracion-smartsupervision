# tests/test_s3_service.py
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from botocore.exceptions import ClientError
from app.services.s3_service import S3StorageService
from app.core.exceptions import SfcIntegrationException


class _StubRedisHash:
    """Emula únicamente las operaciones de HASH usadas por el checkpoint de idempotencia
    (HSET/HKEYS/EXPIRE/DELETE) para probar P0-10 sin depender de un Redis real."""

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


class TestS3ServiceErrorHandling(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_boto_client = MagicMock()
        self.service = S3StorageService(s3_client=self.mock_boto_client)

    async def test_listar_archivos_access_denied_raises_exception(self):
        """
        Verifica que si S3 responde con AccessDenied (403) al listar un directorio,
        NO devuelva lista vacía [] sino que eleve un SfcIntegrationException 500.
        """
        client_error = ClientError(
            error_response={"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            operation_name="ListObjectsV2"
        )
        paginator_mock = MagicMock()
        paginator_mock.paginate.side_effect = client_error
        self.mock_boto_client.get_paginator.return_value = paginator_mock

        with self.assertRaises(SfcIntegrationException) as ctx:
            await self.service.listar_archivos_en_directorio(prefix="quejas/123")

        exc = ctx.exception
        self.assertEqual(exc.status_code, 500)
        self.assertEqual(exc.error_type, "S3_LIST_ERROR")

    async def test_obtener_stream_access_denied_raises_exception(self):
        """
        Verifica que AccessDenied en head_object lance S3_ACCESS_DENIED (500)
        en lugar de S3_FILE_NOT_FOUND (404).
        """
        client_error = ClientError(
            error_response={"Error": {"Code": "AccessDenied", "Message": "Forbidden"}},
            operation_name="HeadObject"
        )
        self.mock_boto_client.head_object.side_effect = client_error

        with self.assertRaises(SfcIntegrationException) as ctx:
            await self.service.obtener_stream_archivo(s3_key="quejas/123/doc.pdf")

        exc = ctx.exception
        self.assertEqual(exc.status_code, 500)
        self.assertEqual(exc.error_type, "S3_ACCESS_DENIED")

    async def test_s3_key_de_otro_caso_es_rechazada(self):
        """
        Auditoría 2026-08-13, item 19 / P1-10: intentar leer una s3_key que no
        pertenece al case_id que se está procesando debe rechazarse (403), sin
        siquiera llegar a consultar S3 (protección de ownership antes del I/O).
        """
        with self.assertRaises(SfcIntegrationException) as ctx:
            await self.service.obtener_stream_archivo(
                s3_key="quejas/OTRO-CASO-999/doc.pdf",
                case_id_esperado="CASO-ESPERADO-123"
            )

        exc = ctx.exception
        self.assertEqual(exc.status_code, 403)
        self.assertEqual(exc.error_type, "S3_KEY_OWNERSHIP_MISMATCH")
        self.mock_boto_client.head_object.assert_not_called()

    async def test_s3_key_del_caso_correcto_no_se_rechaza_por_ownership(self):
        """Control: una key que SÍ contiene el case_id esperado no debe activar el rechazo."""
        self.mock_boto_client.head_object.return_value = {"ContentLength": 1024}
        mock_body = MagicMock()
        mock_body.read = MagicMock(return_value=b"%PDF-1.4 contenido")
        self.mock_boto_client.get_object.return_value = {"Body": mock_body}

        # No debe lanzar SfcIntegrationException por ownership.
        await self.service.obtener_stream_archivo(
            s3_key="quejas/CASO-ESPERADO-123/doc.pdf",
            case_id_esperado="CASO-ESPERADO-123"
        )

        self.mock_boto_client.head_object.assert_called_once()


class TestS3ServiceCheckpointArchivos(unittest.IsolatedAsyncioTestCase):
    """🟢 FIX P0-10: un reintento del lote no debe volver a subir archivos ya
    confirmados por la SFC en un intento previo."""

    def setUp(self):
        self.service = S3StorageService(s3_client=MagicMock())
        self.service.obtener_stream_archivo = AsyncMock(return_value=b"contenido")
        self.service.validar_integridad_archivo = MagicMock()

        self.stub_redis = _StubRedisHash()
        self.patcher = patch("app.services.s3_service.get_redis_client", return_value=self.stub_redis)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        self.archivos = [
            {"nombre_archivo": f"doc{i}.pdf", "s3_key": f"caso/X/doc{i}.pdf", "bytes": b"x"}
            for i in range(1, 6)
        ]

    async def test_retry_no_reenvia_archivos_ya_confirmados(self):
        """M2 con 5 adjuntos y fallo en el adjunto 4: el retry del lote completo no debe
        volver a subir los que ya tuvieron éxito (escenario exacto de la auditoría)."""
        sfc_client = MagicMock()

        async def post_falla_doc4(sfc_codigo_queja, file_data, file_type, file_name):
            if file_name == "doc4.pdf":
                raise SfcIntegrationException(500, "SFC_INTERNAL_ERROR", None, "Error inesperado", "reintentar")
            return {"id": file_name}

        sfc_client.post_adjunto_queja = post_falla_doc4

        with self.assertRaises(SfcIntegrationException):
            await self.service.transferir_lote_s3_a_sfc(
                sfc_client=sfc_client, sfc_codigo_queja="CASO-X", adjuntos_crm=self.archivos
            )

        llamados_reintento = []

        async def post_ok(sfc_codigo_queja, file_data, file_type, file_name):
            llamados_reintento.append(file_name)
            return {"id": file_name}

        sfc_client.post_adjunto_queja = post_ok

        resultado = await self.service.transferir_lote_s3_a_sfc(
            sfc_client=sfc_client, sfc_codigo_queja="CASO-X", adjuntos_crm=self.archivos
        )

        # Sólo el archivo que realmente había fallado debe volver a llegar a la SFC.
        self.assertEqual(llamados_reintento, ["doc4.pdf"])

        estados = {r["file_name"]: r["status"] for r in resultado}
        self.assertEqual(estados["doc4.pdf"], "OK")
        for nombre in ("doc1.pdf", "doc2.pdf", "doc3.pdf", "doc5.pdf"):
            self.assertEqual(estados[nombre], "ALREADY_CONFIRMED_CHECKPOINT")

    async def test_sfc_rechaza_adjunto_duplicado_se_absorbe_como_exito_y_marca_checkpoint(self):
        """
        Nivel 2 (auditoría adversarial v10, P0-01/P0-03): reproduce el escenario
        exacto que el informe usa como ejemplo -- un reintento (disparado porque
        Redis no pudo confirmar el envío anterior) re-sube un adjunto que la SFC YA
        recibió. La SFC debe rechazarlo como duplicado (error_type=DUPLICATE_FILE,
        o alguna de las frases de errores_sfc.json) y el código debe absorberlo
        como éxito idempotente -- sin propagar la excepción y SIN crear un segundo
        registro del mismo archivo -- en vez de que el reintento cuente como un
        fallo real o (peor) como un envío nuevo.
        """
        sfc_client = MagicMock()
        sfc_client.post_adjunto_queja = AsyncMock(
            side_effect=SfcIntegrationException(
                400, "DUPLICATE_FILE", None,
                "El archivo ya existe para esta queja (código 556240)",
                "No reenviar"
            )
        )
        archivo = [{"nombre_archivo": "respuesta_final.pdf", "s3_key": "caso/Y/respuesta_final.pdf", "bytes": b"x"}]

        resultado = await self.service.transferir_lote_s3_a_sfc(
            sfc_client=sfc_client, sfc_codigo_queja="CASO-Y", adjuntos_crm=archivo
        )

        self.assertEqual(resultado[0]["status"], "DUPLICATE_OMITTED")
        # El checkpoint debe quedar marcado también en este camino -- un tercer
        # reintento del mismo lote ya ni siquiera debe volver a golpear a la SFC.
        # La identidad incluye un hash del contenido inline (bytes) para que un
        # reintento con contenido DISTINTO no se confunda con este mismo checkpoint
        # -- ver test_s3_checkpoint_content_aware.py -- así que se verifica el
        # prefijo en vez de la key exacta.
        completados = await self.stub_redis.hkeys("{sfc:idempotency}:file_checkpoint:CASO-Y")
        self.assertEqual(len(completados), 1)
        self.assertTrue(completados[0].startswith("caso/Y/respuesta_final.pdf:"))

    async def test_sfc_rechaza_por_mensaje_no_mapeado_no_se_absorbe(self):
        """
        Nivel 2: contraprueba deliberada de la fragilidad del mecanismo -- si la SFC
        rechaza el reenvío con un mensaje que en la práctica significa lo mismo
        ("duplicado"/"ya procesado") pero con una redacción que NO coincide con
        ninguna de las subcadenas ya mapeadas (ni DUPLICATE_FILE, ni 'ya existe',
        ni '556240', ni 'ya cuenta con un documento', ni 'se encuentra cerrada'),
        el código NO lo reconoce como éxito idempotente y propaga la excepción.
        Documenta el riesgo real: esta protección depende de que la SFC siga
        fraseando sus rechazos exactamente como hoy están mapeados.
        """
        sfc_client = MagicMock()
        sfc_client.post_adjunto_queja = AsyncMock(
            side_effect=SfcIntegrationException(
                409, "CONFLICT", None,
                "El recurso ya fue procesado anteriormente por el sistema",
                "Verificar estado del caso"
            )
        )
        archivo = [{"nombre_archivo": "respuesta_final.pdf", "s3_key": "caso/Z/respuesta_final.pdf", "bytes": b"x"}]

        with self.assertRaises(SfcIntegrationException):
            await self.service.transferir_lote_s3_a_sfc(
                sfc_client=sfc_client, sfc_codigo_queja="CASO-Z", adjuntos_crm=archivo
            )


if __name__ == "__main__":
    unittest.main()