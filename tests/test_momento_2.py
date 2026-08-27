# tests/test_momento_2.py
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch
from pydantic import ValidationError

import httpx

from app.schemas.crm_payloads import Momento2QuejaCrmInput
from app.services.momento_2_sync import Momento2SincronizacionService, _es_error_queja_ya_existe_m2
from app.integrations.sfc_client import SfcClient
from app.core.exceptions import SfcIntegrationException
from app.services.email_service import EmailAlertService


class TestMomento2Pipeline(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Mocks de dependencias
        self.sfc_client_mock = MagicMock(spec=SfcClient)
        self.s3_client_mock = MagicMock()

        self.smart_code = "16551509974606"

        # Fecha reciente relativa a "hoy": CreatedDate debe caer dentro de la ventana
        # de 30 días que valida Momento2QuejaCrmInput, sin importar cuándo corra el test.
        fecha_creacion_reciente = (
            datetime.now(ZoneInfo("America/Bogota")) - timedelta(days=5)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        # Diccionario simulado de payload del CRM
        self.mock_datos_consolidados = {
            "Smart_Code__c": self.smart_code,
            "CreatedDate": fecha_creacion_reciente,
            
            "SuppliedName": "Camila Salas",
            "SC_id_type__c": "CC",
            "id_number__c": "1040011014",
            "sc_genero__c": "Femenino",
            "tipo_de_persona__c": "B2C",
            "sc_LGBTIQ__c": "No",
            "sc_Condicion_especial__c": "No aplica",
            
            # --- Datos de Contacto y Ubicación ---
            "SuppliedPhone": "3001234567",
            "SuppliedEmail": "camila@test.com",
            "direccion__c": "Calle 93 # 11-11",
            "Departamento__c": "Bogotá D.C.",
            "SC_municipio__c": "Bogotá D.C.",
            
            # --- Clasificación y Control del Caso ---
            "canal__c": "Internet",
            "punto_recepcion": "Manual",
            "Instancia_de_recepcion__c": "Entidad vigilada",
            "admision_col__c": "No Aplica",
            "Status": "New",
            
            # --- Detalles de la Queja ---
            "Description": "Prueba de queja",
            "smart_anexo_queja__c": False,
            "Tutela__c": "No",
            "Ente_de_control__c": "Otros",
            
            # --- Producto y Motivo (Tipificación) ---
            "Product__c": "Cuenta perfil",
            "smart_Producto_nombre__c": "Ahorro",
            "Categorias_COL__c": "Transacción no reconocida",
            
            "smart_escalamiento_DCF__c": "No",
            "archivos_s3": []
        }

    async def test_envio_exitoso_sin_anexos(self):
        """Prueba de despacho exitoso hacia la SFC cuando la queja no tiene archivos anexos."""
        self.sfc_client_mock.post_nueva_queja = AsyncMock(return_value={"status": "created"})
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            s3_client=self.s3_client_mock
        )
        
        payload_pydantic = Momento2QuejaCrmInput(**self.mock_datos_consolidados)
        
        resultado = await service.ejecutar_envio_momento_2(payload_pydantic)
        
        # Verificaciones
        self.assertEqual(resultado["status"], "success")
        self.sfc_client_mock.post_nueva_queja.assert_called_once()

    async def test_envio_exitoso_con_anexos(self):
        """Prueba de descarga asíncrona de S3 y transmisión concurrente a la SFC."""
        datos_con_anexos = self.mock_datos_consolidados.copy()
        # Escenario realista: el CRM manda el Case_id (su id real interno) y el
        # Smart_Code__c se deriva/prefija en el schema; el s3_key está organizado
        # por Case_id, no por el Smart_Code__c ya prefijado.
        del datos_con_anexos["Smart_Code__c"]
        datos_con_anexos["Case_id"] = self.smart_code
        datos_con_anexos["smart_anexo_queja__c"] = True
        datos_con_anexos["archivos_s3"] = [
            {
                "s3_key": f"{self.smart_code}/soporte1.pdf",
                "bucket": "mi-bucket-smartsupervision",
                "nombre_archivo": "soporte1.pdf"
            }
        ]
        
        self.sfc_client_mock.post_nueva_queja = AsyncMock(return_value={"status": "created"})
        self.sfc_client_mock.post_adjunto_queja = AsyncMock(return_value={"status": "uploaded"})
        
        # Mocking S3 de AWS
        self.s3_client_mock.head_object = MagicMock(return_value={"ContentLength": 1024})
        mock_body = MagicMock()
        mock_body.read = MagicMock(return_value=b"%PDF-1.4 Mock PDF content bytes_pdf_simulados")
        self.s3_client_mock.get_object = MagicMock(return_value={"Body": mock_body})
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            s3_client=self.s3_client_mock
        )
        
        payload_pydantic = Momento2QuejaCrmInput(**datos_con_anexos)
        
        resultado = await service.ejecutar_envio_momento_2(payload_pydantic)
        
        # Verificaciones
        self.assertEqual(resultado["status"], "success")
        self.sfc_client_mock.post_nueva_queja.assert_called_once()
        self.sfc_client_mock.post_adjunto_queja.assert_called_once()

    async def test_envio_fallido_error_api_sfc(self):
        """Verifica que si la SFC falla con una excepción no controlada, la excepción se relanza (Hallazgo 40)."""
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=Exception("SFC Timeout Connection"))
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            s3_client=self.s3_client_mock
        )
        
        payload_pydantic = Momento2QuejaCrmInput(**self.mock_datos_consolidados)
        
        with self.assertRaises(Exception) as ctx:
            await service.ejecutar_envio_momento_2(payload_pydantic)
            
        self.assertEqual(str(ctx.exception), "SFC Timeout Connection")

    async def test_envio_fallido_sfc_integration_exception(self):
        """Prueba que si la SFC lanza una SfcIntegrationException, se propaga para que la atrape el router."""
        exc = SfcIntegrationException(
            status_code=400,
            error_type="DNI_INVALIDO",
            sfc_field="numero_id_CF",
            raw_message="El número de DNI es inválido",
            crm_action="Verificar el número de identificación del cliente"
        )
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=exc)
        
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, 
            s3_client=self.s3_client_mock
        )
        
        payload_pydantic = Momento2QuejaCrmInput(**self.mock_datos_consolidados)
        
        with self.assertRaises(SfcIntegrationException):
            await service.ejecutar_envio_momento_2(payload_pydantic)

    async def test_reintento_tras_creacion_previa_se_absorbe_como_exito(self):
        """
        Nivel 2 (auditoría adversarial v10, P0-01/P0-03): reproduce el escenario de
        cierre de la sección 7 del informe -- "forzar Redis down después de SFC=200,
        un retry del mismo evento NO vuelve a duplicar en SFC". Aquí el mismo
        codigo_queja ya fue creado en un intento anterior (Redis no confirmó ese
        éxito localmente); el reintento vuelve a llamar a post_nueva_queja, la SFC
        lo rechaza porque el código YA existe (error_type=ALREADY_EXISTS), y el
        pipeline debe tratarlo como éxito idempotente -- sin crear una segunda
        queja -- en vez de propagar el error.
        """
        exc_ya_existe = SfcIntegrationException(
            status_code=400,
            error_type="ALREADY_EXISTS",
            sfc_field="codigo_queja",
            raw_message="La queja ya existe con este código",
            crm_action="No reenviar"
        )
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=exc_ya_existe)

        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock,
            s3_client=self.s3_client_mock
        )
        payload_pydantic = Momento2QuejaCrmInput(**self.mock_datos_consolidados)

        resultado = await service.ejecutar_envio_momento_2(payload_pydantic)

        self.assertEqual(resultado["status"], "success")
        self.sfc_client_mock.post_nueva_queja.assert_called_once()

    async def test_reintento_por_colision_funcional_no_se_confunde_con_ya_existe(self):
        """
        Nivel 2: contraprueba -- un rechazo por duplicidad FUNCIONAL (mismo motivo/
        producto/canal, una queja DISTINTA que coincide en esos campos) no es el
        mismo escenario que "el mismo codigo_queja ya fue creado" -- no debe
        absorberse como éxito idempotente, porque en este caso SÍ sería un error de
        negocio real que el CRM necesita conocer.
        """
        exc_colision_funcional = SfcIntegrationException(
            status_code=400,
            error_type="VALIDATION_ERROR",
            sfc_field=None,
            raw_message="Ya existe una queja con el mismo motivo y producto para este cliente. Verifique el motivo antes de continuar.",
            crm_action="Revisar duplicidad funcional"
        )
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=exc_colision_funcional)

        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock,
            s3_client=self.s3_client_mock
        )
        payload_pydantic = Momento2QuejaCrmInput(**self.mock_datos_consolidados)

        with self.assertRaises(SfcIntegrationException):
            await service.ejecutar_envio_momento_2(payload_pydantic)

    async def test_error_sfc_no_mapeado_dispara_alerta_por_correo(self):
        """Un error_type=UNKNOWN_SFC_ERROR (o is_unmapped=True) debe disparar
        notificar_error_no_mapeado antes de propagarse -- sin esto, un error real
        de la SFC que no está en la matriz curada pasa desapercibido para el
        equipo, no sólo para el CRM."""
        exc_no_mapeado = SfcIntegrationException(
            status_code=500,
            error_type="UNKNOWN_SFC_ERROR",
            sfc_field=None,
            raw_message="Fallo totalmente desconocido de la SFC",
            crm_action="Revisar logs"
        )
        self.sfc_client_mock.post_nueva_queja = AsyncMock(side_effect=exc_no_mapeado)

        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, s3_client=self.s3_client_mock
        )
        payload_pydantic = Momento2QuejaCrmInput(**self.mock_datos_consolidados)

        with patch.object(EmailAlertService, "notificar_error_no_mapeado", new_callable=AsyncMock) as mock_alerta:
            with self.assertRaises(SfcIntegrationException):
                await service.ejecutar_envio_momento_2(payload_pydantic)

        mock_alerta.assert_awaited_once()
        # El schema antepone un prefijo normativo a Smart_Code__c -- basta con
        # confirmar que el smart_code real de la queja viaja en la alerta.
        self.assertIn(self.smart_code, mock_alerta.call_args.kwargs["smart_code"])

    async def test_fallo_de_red_especifico_se_relanza_por_su_propia_rama(self):
        """httpx.RequestError/TimeoutException/ConnectionError/OSError tienen su
        propio except (distinto del genérico) que loguea con un mensaje
        específico de 'Fallo de red/conexión' -- confirma que ese branch
        realmente se alcanza y no cae en el except genérico de abajo."""
        self.sfc_client_mock.post_nueva_queja = AsyncMock(
            side_effect=httpx.ConnectTimeout("timeout conectando a la SFC")
        )
        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, s3_client=self.s3_client_mock
        )
        payload_pydantic = Momento2QuejaCrmInput(**self.mock_datos_consolidados)

        with self.assertLogs("app.services.momento_2_sync", level="ERROR") as logs:
            with self.assertRaises(httpx.ConnectTimeout):
                await service.ejecutar_envio_momento_2(payload_pydantic)

        mensajes = [r.getMessage() for r in logs.records]
        self.assertTrue(any("Fallo de red/conexión" in m for m in mensajes))

    def test_missing_smart_code(self):
        """Valida que Pydantic rechace la instanciación si faltan 'Smart_Code__c' y 'Case_id'."""
        payload_invalido = self.mock_datos_consolidados.copy()
        payload_invalido["Smart_Code__c"] = ""
        payload_invalido["Case_id"] = ""
        
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**payload_invalido)
            
        self.assertIn("Debe incluir al menos 'Case_id' o 'Smart_Code__c'", str(ctx.exception))

    def test_archivos_s3_dict_malformado_no_se_descarta_en_silencio(self):
        """
        P1-11: si el CRM manda 'archivos_s3' como un dict no vacío pero con forma
        no reconocida (sin 's3_key' ni 'nombre_archivo'), antes se descartaba en
        silencio como []. Debe rechazarse explícitamente en vez de tratarse como
        'sin adjuntos'.
        """
        payload_invalido = self.mock_datos_consolidados.copy()
        payload_invalido["archivos_s3"] = {"otro_campo": "valor_inesperado"}

        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**payload_invalido)

        self.assertIn("forma no reconocida", str(ctx.exception))

    def test_archivos_s3_dict_unico_valido_se_normaliza_a_lista(self):
        """Un único adjunto enviado como dict (no lista) sigue aceptándose normalmente."""
        payload_valido = self.mock_datos_consolidados.copy()
        payload_valido["archivos_s3"] = {"s3_key": "q/f.pdf", "nombre_archivo": "f.pdf", "bucket": "b1"}

        payload_pydantic = Momento2QuejaCrmInput(**payload_valido)

        self.assertEqual(len(payload_pydantic.archivos_s3), 1)
        self.assertEqual(payload_pydantic.archivos_s3[0].nombre_archivo, "f.pdf")

    async def test_payload_sfc_invalido_no_filtra_el_valor_rechazado_en_el_log(self):
        """
        🔴 FIX (hallazgo propio, 2026-08-27): si SfcNuevaQuejaPayload (el payload YA
        mapeado hacia la SFC) falla su propia validación Pydantic, el `except
        Exception` genérico loggeaba str(e) -- que por defecto incluye el valor
        RECHAZADO de cada campo. Con campos que sí llevan PII real (nombres,
        numero_id_CF, mapeados desde SuppliedName/id_number__c), ese valor podía ser
        el dato del cliente. Se fuerza la falla vía un campo (tipo_id_CF, int) con un
        valor centinela que Pydantic reporta tal cual en su 'input' -- y se verifica
        que ese centinela NO aparezca en el log. La excepción original SÍ se relanza
        sin envolver (los llamadores -- FastAPI, procesar_despacho_raw_json -- ya la
        manejan de forma segura); lo que se corrige es sólo el punto de logueo.
        """
        centinela_pii = "CENTINELA-PII-cliente-real-9f3a"
        payload_pydantic = Momento2QuejaCrmInput(**self.mock_datos_consolidados)

        service = Momento2SincronizacionService(
            sfc_client=self.sfc_client_mock, s3_client=self.s3_client_mock
        )
        with patch(
            "app.services.momento_2_sync.SfcSalesforceMapper.crm_entity_to_sfc_payload"
        ) as mock_mapper:
            mock_mapper.return_value = {
                "codigo_queja": self.smart_code,
                "departamento_cod": "11", "municipio_cod": "11001",
                "canal_cod": 13, "producto_cod": 207, "macro_motivo_cod": 940,
                "fecha_creacion": "2026-08-01T00:00:00",
                "nombres": "Camila Salas", "tipo_id_CF": centinela_pii,
                "numero_id_CF": "1040011014", "tipo_Persona": 1,
                "texto_queja": "Prueba", "anexo_queja": False,
                "ente_control": 99, "insta_recepcion": 1, "admision": 1,
                "codigo_pais": "170", "punto_recepcion": 1,
            }
            with self.assertLogs("app.services.momento_2_sync", level="ERROR") as logs:
                with self.assertRaises(ValidationError):
                    await service.ejecutar_envio_momento_2(payload_pydantic)

        for record in logs.records:
            self.assertNotIn(centinela_pii, record.getMessage())


class TestEsErrorQuejaYaExisteM2(unittest.TestCase):
    """
    Cobertura directa de _es_error_queja_ya_existe_m2 -- hasta ahora sólo
    probada indirectamente vía SfcIntegrationException/pipeline completo,
    nunca aislada para sus ramas específicas.
    """

    def test_error_type_already_exists_tiene_prioridad_sobre_frase_de_colision_funcional(self):
        """
        🔴 Caso que el propio fix (2026-08-25) documenta como motivo del reordenamiento:
        un error_type='ALREADY_EXISTS' real cuyo raw_message CONTIENE 'already_exist'
        (la subcadena que dispara la rama de colisión funcional, paso 2) debe seguir
        tolerándose como éxito idempotente -- la señal estructurada gana siempre,
        sin importar qué texto libre traiga el mensaje.
        """
        self.assertTrue(
            _es_error_queja_ya_existe_m2(
                "Object already_exist in database", error_type="ALREADY_EXISTS"
            )
        )

    def test_frase_mismo_motivo_sin_error_type_no_se_tolera(self):
        self.assertFalse(
            _es_error_queja_ya_existe_m2("Ya existe una queja con el mismo motivo", error_type=None)
        )

    def test_frase_mismo_producto_no_se_tolera(self):
        self.assertFalse(
            _es_error_queja_ya_existe_m2("Colisión: mismo producto para este cliente", error_type="VALIDATION_ERROR")
        )

    def test_keyword_already_exists_en_ingles_si_se_tolera(self):
        self.assertTrue(_es_error_queja_ya_existe_m2("Queja already exists", error_type=None))

    def test_keyword_registrado_en_la_sfc_si_se_tolera(self):
        self.assertTrue(_es_error_queja_ya_existe_m2("El caso ya está registrado en la SFC", error_type=None))

    def test_mensaje_sin_ninguna_senal_no_se_tolera(self):
        self.assertFalse(_es_error_queja_ya_existe_m2("Error de validación de otro campo", error_type="VALIDATION_ERROR"))

    def test_mensaje_vacio_o_none_no_se_tolera(self):
        self.assertFalse(_es_error_queja_ya_existe_m2("", error_type=None))
        self.assertFalse(_es_error_queja_ya_existe_m2(None, error_type=None))


if __name__ == "__main__":
    unittest.main()