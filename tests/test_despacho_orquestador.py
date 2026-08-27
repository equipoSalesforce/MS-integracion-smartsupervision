import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.despacho_queja_orchestrator import DespachoQuejaOrquestador
from app.schemas.crm_payloads import QuejaUnificadaCrmInput
from app.core.exceptions import SfcIntegrationException


class TestDespachoQuejaOrquestadorPipeline(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Mocks para infraestructura base
        self.mock_sfc_client = MagicMock()
        self.mock_s3_client = MagicMock()

        # Instancia del orquestador a probar
        self.orquestador = DespachoQuejaOrquestador(
            sfc_client=self.mock_sfc_client,
            s3_client=self.mock_s3_client
        )

        # Mocks asíncronos con return_value por defecto
        self.orquestador.m2_service.ejecutar_envio_momento_2 = AsyncMock(
        return_value={"status": "success", "codigo_queja_sfc": "1423999000111222"}  # 👈 "status" en minúscula
        )
        self.orquestador.m3_service.ejecutar_gestion_fraude = AsyncMock(
            return_value={"status": "success", "message": "Fraude actualizado"}
        )
        self.orquestador.m3_service.ejecutar_cierre_definitivo = AsyncMock(
            return_value={"status": "success", "message": "Caso cerrado"}
        )
        self.orquestador.m3_service.ejecutar_actualizacion_tramite = AsyncMock(
            return_value={"status": "success", "message": "Tramite actualizado"}
        )

        # Fechas relativas a "hoy" para que las validaciones de ventana de 30 días
        # (CreatedDate/ClosedDate) no dependan de cuándo corra el test.
        hoy_bogota = datetime.now(ZoneInfo("America/Bogota"))
        fecha_creacion_reciente = (hoy_bogota - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        self.fecha_cierre_reciente = (hoy_bogota - timedelta(days=1)).strftime("%Y-%m-%d")

        # 🎯 Payload base canónico de CREACIÓN PURA M2 (Campos exclusivos M3 en None)
        self.base_payload_dict = {
            "Smart_Code__c": "999000111222",
            "CreatedDate": fecha_creacion_reciente,
            "Status": "New",
            "SuppliedName": "Juan Perez",
            "SC_id_type__c": "CC",
            "id_number__c": "123456789",
            "sc_genero__c": None,               # 👈 Debe ser None para M2 Puro
            "tipo_de_persona__c": "B2C",
            "sc_LGBTIQ__c": None,               # 👈 Debe ser None para M2 Puro
            "sc_Condicion_especial__c": None,   # 👈 Debe ser None para M2 Puro
            "producto_digital__c": None,        # 👈 Debe ser None para M2 Puro
            "admision_col__c": "No Aplica",
            "SuppliedPhone": "3001234567",
            "SuppliedEmail": "juan@test.com",
            "direccion__c": "Calle 123",
            "Departamento__c": "Bogotá D.C.",
            "SC_municipio__c": "Bogotá D.C.",
            "canal__c": "Internet",
            "punto_recepcion": "WhatsApp",
            "Instancia_de_recepcion__c": "Entidad vigilada",
            "Product__c": "Cuenta perfil",
            "smart_Producto_nombre__c": "Ahorro",
            "Categorias_COL__c": "Transacción no reconocida",
            "Description": "Prueba de orquestador unificado",
            "smart_anexo_queja__c": False,
            "smart_escalamiento_DCF__c": "No",
            "archivos_s3": []
        }

    # ======================================================================
    # 🟢 CASO 1: ALTA NUEVA PURA (MOMENTO 2 DIRECTO)
    # ======================================================================
    async def test_1_despacho_momento_2_creacion_pura(self):
        """Valida que una queja nueva (Status=New) sin fraude ni cierre vaya directo a Momento 2."""
        payload = QuejaUnificadaCrmInput.model_validate(self.base_payload_dict)

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        self.orquestador.m2_service.ejecutar_envio_momento_2.assert_called_once_with(payload=payload)
        self.orquestador.m3_service.ejecutar_gestion_fraude.assert_not_called()
        self.orquestador.m3_service.ejecutar_cierre_definitivo.assert_not_called()
        self.orquestador.m3_service.ejecutar_actualizacion_tramite.assert_not_called()

    # ======================================================================
    # 🟡 CASO 2: ACTUALIZACIÓN INTERMEDIA DE TRÁMITE
    # ======================================================================
    async def test_2_despacho_actualizacion_tramite_directo(self):
        """Valida que un estado intermedio ('In Progress') sin fraude/cierre llame a trámite M3."""
        tramite_dict = self.base_payload_dict.copy()
        tramite_dict.update({
            "Status": "In Progress",
            "sc_genero__c": "Masculino"  # Inyecta campo M3
        })
        payload = QuejaUnificadaCrmInput.model_validate(tramite_dict)

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        self.orquestador.m3_service.ejecutar_actualizacion_tramite.assert_called_once_with(payload=payload)
        self.orquestador.m2_service.ejecutar_envio_momento_2.assert_not_called()

    # ======================================================================
    # 🟠 CASO 3: REPORTE DE FRAUDE EN CASO EXISTENTE
    # ======================================================================
    async def test_3_despacho_solo_fraude_directo(self):
        """Valida la gestión exclusiva de Fraude cuando el caso ya existe en la SFC."""
        fraude_dict = self.base_payload_dict.copy()
        fraude_dict.update({
            "Status": "In Progress",
            "sc_genero__c": "Masculino",
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "nombre_archivo_fraude": "dictamen_fraude.pdf",
            "archivos_s3": [{"nombre_archivo": "dictamen_fraude.pdf", "s3_key": "q/f.pdf", "bucket": "b1"}]
        })
        payload = QuejaUnificadaCrmInput.model_validate(fraude_dict)

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        self.orquestador.m3_service.ejecutar_gestion_fraude.assert_called_once_with(payload=payload)
        self.orquestador.m3_service.ejecutar_cierre_definitivo.assert_not_called()

    # ======================================================================
    # 🔴 CASO 4: CIERRE DEFINITIVO EN CASO EXISTENTE
    # ======================================================================
    async def test_4_despacho_solo_cierre_directo(self):
        """Valida la gestión exclusiva de Cierre Definitivo cuando la queja existe en la SFC."""
        cierre_dict = self.base_payload_dict.copy()
        cierre_dict.update({
            "Status": "Closed",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Estimado consumidor, su reclamación ha sido resuelta no favorablemente.</p>",
            "archivos_s3": []
        })
        payload = QuejaUnificadaCrmInput.model_validate(cierre_dict)

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        self.orquestador.m3_service.ejecutar_cierre_definitivo.assert_called_once_with(payload=payload)
        self.orquestador.m3_service.ejecutar_gestion_fraude.assert_not_called()

    # ======================================================================
    # 🟣 CASO 5: SECUENCIA COMPLETA DIRECTA (FRAUDE + CIERRE)
    # ======================================================================
    async def test_5_despacho_fraude_y_cierre_simultaneo_directo(self):
        """Valida que si viene Fraude y Cierre, se ejecuten ambos sub-pasos en orden regulatorio."""
        completo_dict = self.base_payload_dict.copy()
        completo_dict.update({
            "Status": "Closed",
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "nombre_archivo_fraude": "dictamen_fraude.pdf",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Notificación final de investigación de fraude y cierre del caso.</p>",
            "archivos_s3": [
                {"nombre_archivo": "dictamen_fraude.pdf", "s3_key": "q/f.pdf", "bucket": "b1"}
            ]
        })
        payload = QuejaUnificadaCrmInput.model_validate(completo_dict)

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        self.orquestador.m3_service.ejecutar_gestion_fraude.assert_called_once_with(payload=payload)
        self.orquestador.m3_service.ejecutar_cierre_definitivo.assert_called_once_with(payload=payload)

    # ======================================================================
    # ⚡ CASO 6: AUTO-RECUPERACIÓN COMPLETA (404 -> M2 -> M3 FRAUDE -> M3 CIERRE)
    # ======================================================================
    async def test_6_auto_recuperacion_secuencia_completa_404(self):
        """
        Si llega un cierre+fraude de una queja que NO existe en la SFC (404/NOT_FOUND_ERROR),
        debe crear el caso en M2 y re-ejecutar Fraude y Cierre automáticamente.
        """
        completo_dict = self.base_payload_dict.copy()
        completo_dict.update({
            "Status": "Closed",
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "nombre_archivo_fraude": "dictamen_fraude.pdf",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Respuesta final emitida tras la secuencia de auto-recuperación.</p>",
            "archivos_s3": [
                {"nombre_archivo": "dictamen_fraude.pdf", "s3_key": "q/f.pdf", "bucket": "b1"}
            ]
        })
        payload = QuejaUnificadaCrmInput.model_validate(completo_dict)

        mock_404_error = SfcIntegrationException(
            404, "NOT_FOUND_ERROR", None, "Not found", "Queja no encontrada"
        )

        self.orquestador.m3_service.ejecutar_gestion_fraude.side_effect = [
            mock_404_error,
            {"status": "success", "message": "Fraude actualizado"}
        ]

        payload_esperado_m2 = payload.model_copy()
        payload_esperado_m2.archivos_s3 = []
        payload_esperado_m2.directorio_s3 = None

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        self.orquestador.m2_service.ejecutar_envio_momento_2.assert_called_once_with(payload=payload_esperado_m2)
        self.assertEqual(self.orquestador.m3_service.ejecutar_gestion_fraude.call_count, 2)
        self.orquestador.m3_service.ejecutar_cierre_definitivo.assert_called_once_with(payload=payload)

    # ======================================================================
    # 🛡️ CASO 7: FALLO EN AUTO-RECUPERACIÓN SI M2 RETORNA ERROR
    # ======================================================================
    async def test_7_auto_recuperacion_aborta_si_m2_falla(self):
        """Si salta el 404 pero la creación en M2 falla, aborta el proceso sin reintentar M3."""
        cierre_dict = self.base_payload_dict.copy()
        cierre_dict.update({
            "Status": "Closed",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Notificación de prueba para aborto por fallo en M2.</p>",
            "archivos_s3": []
        })
        payload = QuejaUnificadaCrmInput.model_validate(cierre_dict)

        mock_404_error = SfcIntegrationException(
            404,
            "NOT_FOUND_ERROR",
            None,
            "Not found",
            "Queja no encontrada"
        )

        self.orquestador.m3_service.ejecutar_cierre_definitivo.side_effect = mock_404_error
        self.orquestador.m2_service.ejecutar_envio_momento_2.return_value = {
            "Status": "error", "message": "Pipeline interrumpido: Timeout"
        }

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["Status"], "error")
        self.assertIn("Pipeline interrumpido", resultado["message"])
        self.orquestador.m2_service.ejecutar_envio_momento_2.assert_called_once()
        self.assertEqual(self.orquestador.m3_service.ejecutar_cierre_definitivo.call_count, 1)

    # ======================================================================
    # 🟠 CASO 8 (🟢 FIX P0-09): ERROR DE CATÁLOGO NO DEBE ACTIVAR SELF-HEALING M2
    # ======================================================================
    async def test_8_error_catalogo_no_encontrado_no_activa_self_healing(self):
        """
        Un error de catálogo/mapeo cuyo mensaje contiene 'no encontrado' (pero cuyo
        error_type NO es NOT_FOUND_ERROR y cuyo status_code NO es 404) no debe disparar
        la secuencia de auto-recuperación M2 -> M3: la queja SÍ existe en la SFC, sólo
        falló un valor de catálogo.
        """
        tramite_dict = self.base_payload_dict.copy()
        tramite_dict.update({
            "Status": "In Progress",
            "sc_genero__c": "Masculino"
        })
        payload = QuejaUnificadaCrmInput.model_validate(tramite_dict)

        error_catalogo = SfcIntegrationException(
            400,
            "VALIDATION_ERROR",
            "producto_cod",
            "El producto no encontrado en catálogo SFC",
            "Verificar catálogo"
        )
        self.orquestador.m3_service.ejecutar_actualizacion_tramite.side_effect = error_catalogo

        with self.assertRaises(SfcIntegrationException):
            await self.orquestador.procesar_despacho(payload)

        self.orquestador.m2_service.ejecutar_envio_momento_2.assert_not_called()

    # ======================================================================
    # 🟠 CASO 9 (🟢 FIX P0-08): DOCUMENTO FALTANTE NO DEBE INTERPRETARSE COMO "YA CERRADO"
    # ======================================================================
    async def test_9_documento_respuesta_final_faltante_no_se_confunde_con_ya_cerrado(self):
        """
        Un mensaje de la SFC que dice que el documento de respuesta final DEBE enviarse
        (es decir, FALTA) contiene la subcadena "respuesta final", pero significa lo
        opuesto a "ya está cerrada". No debe tratarse como éxito idempotente.
        """
        cierre_dict = self.base_payload_dict.copy()
        cierre_dict.update({
            "Status": "Closed",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Prueba de documento faltante.</p>",
            "archivos_s3": []
        })
        payload = QuejaUnificadaCrmInput.model_validate(cierre_dict)

        error_doc_faltante = SfcIntegrationException(
            400,
            "BUSINESS_RULE_ERROR",
            None,
            "El documento de respuesta final debe haber sido enviado antes de fijado en True",
            "Verificar que existe el documento de cierre"
        )
        self.orquestador.m3_service.ejecutar_cierre_definitivo.side_effect = error_doc_faltante

        with self.assertRaises(SfcIntegrationException):
            await self.orquestador.procesar_despacho(payload)

    # ======================================================================
    # 🟢 CASO 10 (P0-01 / auditoría 2026-08-13): Status=New con producto_digital__c
    # PRESENTE no debe clasificarse como M2 puro.
    # ======================================================================
    async def test_10_status_new_con_producto_digital_presente_no_es_m2_puro(self):
        """
        producto_digital__c ya no tiene default de negocio "Si" (ver
        crm_payloads.py) — su default real es None. Este test fija en el
        classifier del orquestador que, si el CRM SÍ envía un valor explícito
        para producto_digital__c junto con Status=New, el caso se trata como
        M3 (trámite/producto digital), no como alta M2 pura; y que, cuando el
        campo se omite/null, sigue clasificando como M2 puro (caso ya cubierto
        por test_1, aquí se re-afirma explícitamente el contraste).
        """
        # Variante A: producto_digital__c presente -> M3 (actualización de trámite)
        con_producto_dict = self.base_payload_dict.copy()
        con_producto_dict["producto_digital__c"] = "Si"
        payload_con_producto = QuejaUnificadaCrmInput.model_validate(con_producto_dict)

        await self.orquestador.procesar_despacho(payload_con_producto)

        self.orquestador.m2_service.ejecutar_envio_momento_2.assert_not_called()
        self.orquestador.m3_service.ejecutar_actualizacion_tramite.assert_called_once()

        # Variante B (control): producto_digital__c ausente/None -> M2 puro
        sin_producto_dict = self.base_payload_dict.copy()
        sin_producto_dict["producto_digital__c"] = None
        payload_sin_producto = QuejaUnificadaCrmInput.model_validate(sin_producto_dict)

        await self.orquestador.procesar_despacho(payload_sin_producto)

        self.orquestador.m2_service.ejecutar_envio_momento_2.assert_called_once()

    # ======================================================================
    # 🟢 Nivel 2 (auditoría adversarial v10, P0-01/P0-03): CASO 11 y 12
    # ======================================================================
    # Reproduce el escenario exacto de la sección 7 del informe: "Forzar Redis
    # down después de SFC=200. Un retry del mismo evento NO vuelve a SFC" -- o si
    # vuelve, la SFC debe rechazarlo como ya-hecho y el pipeline debe absorberlo
    # como éxito idempotente, no como un segundo efecto real.

    async def test_11_reintento_de_cierre_ya_aplicado_se_absorbe_como_exito(self):
        """
        El caso ya quedó cerrado en la SFC en el intento anterior (Redis no
        confirmó ese éxito localmente antes de que expirara el candado de
        idempotencia). El reintento vuelve a llamar a ejecutar_cierre_definitivo;
        la SFC lo rechaza porque el caso YA está cerrado -- el orquestador debe
        devolver éxito sintético, no propagar el error ni reintentar de nuevo.
        """
        cierre_dict = self.base_payload_dict.copy()
        cierre_dict.update({
            "Status": "Closed",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Reintento de un cierre ya aplicado.</p>",
            "archivos_s3": []
        })
        payload = QuejaUnificadaCrmInput.model_validate(cierre_dict)

        exc_ya_cerrada = SfcIntegrationException(
            400, "BUSINESS_RULE_ERROR", None,
            "La queja se encuentra con estado cerrado y no admite nuevas actualizaciones",
            "No reenviar"
        )
        self.orquestador.m3_service.ejecutar_cierre_definitivo.side_effect = exc_ya_cerrada

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        self.assertIn("ya se encuentra cerrado", resultado["message"])
        self.orquestador.m3_service.ejecutar_cierre_definitivo.assert_called_once()

    async def test_12_reintento_con_mensaje_sfc_no_mapeado_no_se_absorbe(self):
        """
        Contraprueba deliberada de la fragilidad del mecanismo (documentada en la
        discusión de Nivel 2): _es_error_caso_ya_cerrado() sólo reconoce 5 frases
        textuales curadas. Si la SFC rechaza el reintento porque el caso ya está
        cerrado pero con una redacción que NO coincide exactamente con ninguna de
        esas frases, el orquestador NO lo reconoce como éxito idempotente y
        propaga la excepción -- un caso genuinamente ya cerrado terminaría
        reportándose como fallo. Esta es la brecha real: la protección depende de
        que la SFC nunca cambie/varíe cómo redacta ese rechazo específico.
        """
        cierre_dict = self.base_payload_dict.copy()
        cierre_dict.update({
            "Status": "Closed",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Reintento con mensaje no mapeado.</p>",
            "archivos_s3": []
        })
        payload = QuejaUnificadaCrmInput.model_validate(cierre_dict)

        exc_no_mapeada = SfcIntegrationException(
            409, "CONFLICT", None,
            "Esta operación no puede completarse porque el caso ya fue finalizado previamente",
            "Revisar estado del caso"
        )
        self.orquestador.m3_service.ejecutar_cierre_definitivo.side_effect = exc_no_mapeada

        with self.assertRaises(SfcIntegrationException):
            await self.orquestador.procesar_despacho(payload)

    async def test_12b_tramite_sobre_caso_ya_cerrado_se_absorbe_como_noop(self):
        """
        Hallazgo E (revisión externa v5): sin serialización por caso, un trámite
        simple puede llegarle a la SFC después de que otro request para el mismo
        Smart_Code__c ya cerró el caso (una carrera entre dos requests, o
        simplemente un trámite tardío). Antes esto propagaba el rechazo de la SFC
        como error real al CRM -- a diferencia de cierre y fraude, que ya
        absorbían este mismo escenario como éxito idempotente.

        🔴 FIX (hallazgo de revisión externa, 2026-08-26, ronda 4 -- W5/V7/X7): a
        diferencia del cierre, el trámite NUNCA se aplicó -- la SFC lo rechazó por
        completo. Reportar "success" le afirma al CRM que esos campos quedaron
        sincronizados cuando en realidad no se tocó nada. Debe ser "noop": no es
        un error (nada que reintentar), pero tampoco fue una escritura exitosa.
        """
        tramite_dict = self.base_payload_dict.copy()
        tramite_dict.update({
            "Status": "In Progress",
            "sc_genero__c": "Masculino"
        })
        payload = QuejaUnificadaCrmInput.model_validate(tramite_dict)

        exc_ya_cerrada = SfcIntegrationException(
            400, "BUSINESS_RULE_ERROR", None,
            "La queja se encuentra con estado cerrado y no admite nuevas actualizaciones",
            "No reenviar"
        )
        self.orquestador.m3_service.ejecutar_actualizacion_tramite.side_effect = exc_ya_cerrada

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "noop")
        self.assertIn("ya se encuentra cerrado", resultado["message"])
        self.assertIn("no se aplicó", resultado["message"])
        self.orquestador.m3_service.ejecutar_actualizacion_tramite.assert_called_once()

    async def test_12c_tramite_con_mensaje_sfc_no_mapeado_no_se_absorbe(self):
        """Misma contraprueba que test_12 pero para el paso de trámite: un rechazo
        de "ya cerrado" con una redacción que no coincide con las frases curadas
        se sigue propagando como error real."""
        tramite_dict = self.base_payload_dict.copy()
        tramite_dict.update({
            "Status": "In Progress",
            "sc_genero__c": "Masculino"
        })
        payload = QuejaUnificadaCrmInput.model_validate(tramite_dict)

        exc_no_mapeada = SfcIntegrationException(
            409, "CONFLICT", None,
            "Esta operación no puede completarse porque el caso ya fue finalizado previamente",
            "Revisar estado del caso"
        )
        self.orquestador.m3_service.ejecutar_actualizacion_tramite.side_effect = exc_no_mapeada

        with self.assertRaises(SfcIntegrationException):
            await self.orquestador.procesar_despacho(payload)

    async def test_13_procesar_despacho_raw_json_rehidrata_y_despacha(self):
        """procesar_despacho_raw_json es el punto de entrada que usa el scheduler al
        reintentar un item de la cola Redis (dict crudo, no un QuejaUnificadaCrmInput)."""
        resultado = await self.orquestador.procesar_despacho_raw_json(self.base_payload_dict)

        self.assertEqual(resultado["status"], "success")
        self.orquestador.m2_service.ejecutar_envio_momento_2.assert_called_once()

    async def test_14_procesar_despacho_raw_json_payload_corrupto_lanza_error_infra(self):
        """Un ítem de la cola Redis con datos corruptos/incompletos debe fallar como
        error de infraestructura (REDIS_PAYLOAD_INVALIDO), no como 400 de validación
        de negocio -- el problema es la cola, no lo que envió el CRM."""
        payload_corrupto = {"Smart_Code__c": "SC-1"}  # Faltan campos obligatorios.

        with self.assertRaises(SfcIntegrationException) as ctx:
            await self.orquestador.procesar_despacho_raw_json(payload_corrupto)

        self.assertEqual(ctx.exception.error_type, "REDIS_PAYLOAD_INVALIDO")
        self.assertEqual(ctx.exception.status_code, 500)

    async def test_15_directorio_s3_encontrado_asigna_archivos_dinamicamente(self):
        """Si el payload trae sólo 'directorio_s3' (sin 'archivos_s3'), el orquestador
        debe listar el directorio en S3 y poblar archivos_s3 dinámicamente."""
        tramite_dict = self.base_payload_dict.copy()
        tramite_dict.update({"directorio_s3": "caso/SC-1/", "archivos_s3": []})
        payload = QuejaUnificadaCrmInput.model_validate(tramite_dict)

        self.orquestador.m3_service.s3_service = MagicMock()
        self.orquestador.m3_service.s3_service.listar_archivos_en_directorio = AsyncMock(
            return_value=[{"nombre_archivo": "soporte.pdf", "s3_key": "caso/SC-1/soporte.pdf", "bucket": "b1"}]
        )

        await self.orquestador.procesar_despacho(payload)

        self.assertEqual(len(payload.archivos_s3), 1)
        self.assertEqual(payload.archivos_s3[0].nombre_archivo, "soporte.pdf")

    async def test_16_directorio_s3_vacio_no_asigna_archivos_y_continua(self):
        tramite_dict = self.base_payload_dict.copy()
        tramite_dict.update({"directorio_s3": "caso/SC-vacio/", "archivos_s3": []})
        payload = QuejaUnificadaCrmInput.model_validate(tramite_dict)

        self.orquestador.m3_service.s3_service = MagicMock()
        self.orquestador.m3_service.s3_service.listar_archivos_en_directorio = AsyncMock(return_value=[])

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(payload.archivos_s3, [])
        self.assertEqual(resultado["status"], "success")

    async def test_17_fraude_sin_archivos_tras_listar_directorio_vacio_lanza_400(self):
        """El caso pasó la validación de Pydantic porque traía 'directorio_s3' (fraude
        con directorio en vez de archivos_s3 explícitos), pero al listar el directorio
        en S3 no se encontró ningún archivo -- el orquestador debe rechazar el envío
        en vez de transmitir un fraude sin soporte documental."""
        fraude_dict = self.base_payload_dict.copy()
        fraude_dict.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "directorio_s3": "caso/SC-fraude-sin-soporte/",
            "archivos_s3": [],
        })
        payload = QuejaUnificadaCrmInput.model_validate(fraude_dict)

        self.orquestador.m3_service.s3_service = MagicMock()
        self.orquestador.m3_service.s3_service.listar_archivos_en_directorio = AsyncMock(return_value=[])

        with self.assertRaises(SfcIntegrationException) as ctx:
            await self.orquestador.procesar_despacho(payload)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.sfc_field, "archivos_s3")

    async def test_18_fraude_ya_cerrado_se_absorbe_y_continua_con_cierre(self):
        """Si el sub-paso de Fraude falla porque el caso ya cuenta con respuesta final
        (ya cerrado) y el payload también trae Cierre, el orquestador debe omitir esa
        falla intermedia y proceder con el Cierre en vez de propagar el error."""
        completo_dict = self.base_payload_dict.copy()
        completo_dict.update({
            "Status": "Closed",
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "nombre_archivo_fraude": "dictamen_fraude.pdf",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Cierre tras fraude ya reportado.</p>",
            "archivos_s3": [
                {"nombre_archivo": "dictamen_fraude.pdf", "s3_key": "q/f.pdf", "bucket": "b1"}
            ],
        })
        payload = QuejaUnificadaCrmInput.model_validate(completo_dict)

        self.orquestador.m3_service.ejecutar_gestion_fraude.side_effect = SfcIntegrationException(
            409, "CONFLICT", None, "La queja ya cuenta con un documento de respuesta final", "N/A"
        )

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        self.orquestador.m3_service.ejecutar_cierre_definitivo.assert_called_once()

    async def test_19_fraude_retorna_error_de_negocio_sin_lanzar_no_ejecuta_cierre(self):
        """Si el sub-paso de Fraude retorna un dict {'status': 'error'} (no una
        excepción), el pipeline debe cortar ahí y no seguir con el Cierre."""
        completo_dict = self.base_payload_dict.copy()
        completo_dict.update({
            "Status": "Closed",
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "nombre_archivo_fraude": "dictamen_fraude.pdf",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Fraude con error de negocio.</p>",
            "archivos_s3": [
                {"nombre_archivo": "dictamen_fraude.pdf", "s3_key": "q/f.pdf", "bucket": "b1"}
            ],
        })
        payload = QuejaUnificadaCrmInput.model_validate(completo_dict)

        self.orquestador.m3_service.ejecutar_gestion_fraude = AsyncMock(
            return_value={"status": "error", "message": "Catálogo de fraude inválido"}
        )

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "error")
        self.orquestador.m3_service.ejecutar_cierre_definitivo.assert_not_called()


class TestLimpiarCheckpointSiCierreExitoso(unittest.IsolatedAsyncioTestCase):
    """
    🟡 FIX (hallazgo de revisión externa, 2026-08-25, §6): limpiar_checkpoint_archivos
    (IdempotencyService) existía sin ningún caller en app/ -- el checkpoint de
    adjuntos por caso vivía hasta su TTL fijo de 30 días aunque el caso ya hubiera
    cerrado, bloqueando el reenvío de una corrección al mismo s3_key si el caso se
    reabría dentro de esa ventana. Se conecta: se libera apenas el CIERRE del caso
    se confirma exitoso -- no antes (un tramite/fraude sin cierre puede tener más
    pasos de M3 por delante para el mismo caso).
    """

    def setUp(self):
        self.mock_sfc_client = MagicMock()
        self.mock_s3_client = MagicMock()
        self.orquestador = DespachoQuejaOrquestador(
            sfc_client=self.mock_sfc_client, s3_client=self.mock_s3_client
        )
        self.orquestador.m2_service.ejecutar_envio_momento_2 = AsyncMock(
            return_value={"status": "success", "codigo_queja_sfc": "1423999000111222"}
        )
        self.orquestador.m3_service.ejecutar_gestion_fraude = AsyncMock(
            return_value={"status": "success", "message": "Fraude actualizado"}
        )
        self.orquestador.m3_service.ejecutar_cierre_definitivo = AsyncMock(
            return_value={"status": "success", "message": "Caso cerrado"}
        )
        self.orquestador.m3_service.ejecutar_actualizacion_tramite = AsyncMock(
            return_value={"status": "success", "message": "Tramite actualizado"}
        )

        hoy_bogota = datetime.now(ZoneInfo("America/Bogota"))
        fecha_creacion_reciente = (hoy_bogota - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        self.fecha_cierre_reciente = (hoy_bogota - timedelta(days=1)).strftime("%Y-%m-%d")

        self.base_payload_dict = {
            "Smart_Code__c": "999000111222",
            "CreatedDate": fecha_creacion_reciente,
            "Status": "In Progress",
            "SuppliedName": "Juan Perez",
            "SC_id_type__c": "CC",
            "id_number__c": "123456789",
            "sc_genero__c": "Masculino",
            "tipo_de_persona__c": "B2C",
            "sc_LGBTIQ__c": None,
            "sc_Condicion_especial__c": None,
            "producto_digital__c": None,
            "admision_col__c": "No Aplica",
            "SuppliedPhone": "3001234567",
            "SuppliedEmail": "juan@test.com",
            "direccion__c": "Calle 123",
            "Departamento__c": "Bogotá D.C.",
            "SC_municipio__c": "Bogotá D.C.",
            "canal__c": "Internet",
            "punto_recepcion": "WhatsApp",
            "Instancia_de_recepcion__c": "Entidad vigilada",
            "Product__c": "Cuenta perfil",
            "smart_Producto_nombre__c": "Ahorro",
            "Categorias_COL__c": "Transacción no reconocida",
            "Description": "Prueba de limpieza de checkpoint",
            "smart_anexo_queja__c": False,
            "smart_escalamiento_DCF__c": "No",
            "archivos_s3": []
        }

    async def test_cierre_exitoso_limpia_el_checkpoint(self):
        cierre_dict = self.base_payload_dict.copy()
        cierre_dict.update({
            "Status": "Closed",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Cierre.</p>",
        })
        payload = QuejaUnificadaCrmInput.model_validate(cierre_dict)

        with patch(
            "app.services.despacho_queja_orchestrator.IdempotencyService"
        ) as MockIdempotencyService:
            instancia = MockIdempotencyService.return_value
            instancia.limpiar_checkpoint_archivos = AsyncMock()

            resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        instancia.limpiar_checkpoint_archivos.assert_awaited_once_with(payload.Smart_Code__c)

    async def test_limpiar_checkpoint_en_exito_false_no_limpia_pese_a_cierre_exitoso(self):
        """
        🔴 FIX (hallazgo N2, revisión externa v5, 2026-08-25): el camino del worker
        (scheduler.py) pasa limpiar_checkpoint_en_exito=False porque todavía le falta
        persistir SFC_DONE de forma durable después de esto -- el orquestador NO debe
        limpiar el checkpoint por su cuenta en ese caso, sin importar que el cierre
        haya sido exitoso.
        """
        cierre_dict = self.base_payload_dict.copy()
        cierre_dict.update({
            "Status": "Closed",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Cierre.</p>",
        })
        payload = QuejaUnificadaCrmInput.model_validate(cierre_dict)

        with patch(
            "app.services.despacho_queja_orchestrator.IdempotencyService"
        ) as MockIdempotencyService:
            instancia = MockIdempotencyService.return_value
            instancia.limpiar_checkpoint_archivos = AsyncMock()

            resultado = await self.orquestador.procesar_despacho(payload, limpiar_checkpoint_en_exito=False)

        self.assertEqual(resultado["status"], "success")
        instancia.limpiar_checkpoint_archivos.assert_not_awaited()

    async def test_procesar_despacho_raw_json_propaga_limpiar_checkpoint_en_exito(self):
        """procesar_despacho_raw_json (el punto de entrada que usa el scheduler) debe
        propagar el flag hacia procesar_despacho, no perderlo en la rehidratación."""
        cierre_dict = self.base_payload_dict.copy()
        cierre_dict.update({
            "Status": "Closed",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Cierre.</p>",
        })

        with patch(
            "app.services.despacho_queja_orchestrator.IdempotencyService"
        ) as MockIdempotencyService:
            instancia = MockIdempotencyService.return_value
            instancia.limpiar_checkpoint_archivos = AsyncMock()

            resultado = await self.orquestador.procesar_despacho_raw_json(
                cierre_dict, limpiar_checkpoint_en_exito=False
            )

        self.assertEqual(resultado["status"], "success")
        instancia.limpiar_checkpoint_archivos.assert_not_awaited()

    async def test_tramite_sin_cierre_no_limpia_el_checkpoint(self):
        payload = QuejaUnificadaCrmInput.model_validate(self.base_payload_dict)

        with patch(
            "app.services.despacho_queja_orchestrator.IdempotencyService"
        ) as MockIdempotencyService:
            instancia = MockIdempotencyService.return_value
            instancia.limpiar_checkpoint_archivos = AsyncMock()

            resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        instancia.limpiar_checkpoint_archivos.assert_not_awaited()

    async def test_fraude_sin_cierre_no_limpia_el_checkpoint(self):
        fraude_dict = self.base_payload_dict.copy()
        fraude_dict.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "nombre_archivo_fraude": "dictamen_fraude.pdf",
            "archivos_s3": [{"nombre_archivo": "dictamen_fraude.pdf", "s3_key": "q/f.pdf", "bucket": "b1"}]
        })
        payload = QuejaUnificadaCrmInput.model_validate(fraude_dict)

        with patch(
            "app.services.despacho_queja_orchestrator.IdempotencyService"
        ) as MockIdempotencyService:
            instancia = MockIdempotencyService.return_value
            instancia.limpiar_checkpoint_archivos = AsyncMock()

            resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        instancia.limpiar_checkpoint_archivos.assert_not_awaited()

    async def test_fraude_y_cierre_limpia_el_checkpoint(self):
        completo_dict = self.base_payload_dict.copy()
        completo_dict.update({
            "Status": "Closed",
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "nombre_archivo_fraude": "dictamen_fraude.pdf",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Fraude y cierre.</p>",
            "archivos_s3": [{"nombre_archivo": "dictamen_fraude.pdf", "s3_key": "q/f.pdf", "bucket": "b1"}]
        })
        payload = QuejaUnificadaCrmInput.model_validate(completo_dict)

        with patch(
            "app.services.despacho_queja_orchestrator.IdempotencyService"
        ) as MockIdempotencyService:
            instancia = MockIdempotencyService.return_value
            instancia.limpiar_checkpoint_archivos = AsyncMock()

            resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        instancia.limpiar_checkpoint_archivos.assert_awaited_once_with(payload.Smart_Code__c)

    async def test_fallo_al_limpiar_checkpoint_no_afecta_la_respuesta_exitosa(self):
        """Best-effort: un fallo al limpiar el checkpoint no debe convertir un cierre
        ya exitoso en un error -- el peor caso es que el checkpoint sigue vivo hasta
        su propio TTL, no una pérdida de datos."""
        cierre_dict = self.base_payload_dict.copy()
        cierre_dict.update({
            "Status": "Closed",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Cierre.</p>",
        })
        payload = QuejaUnificadaCrmInput.model_validate(cierre_dict)

        with patch(
            "app.services.despacho_queja_orchestrator.IdempotencyService"
        ) as MockIdempotencyService:
            instancia = MockIdempotencyService.return_value
            instancia.limpiar_checkpoint_archivos = AsyncMock(side_effect=ConnectionError("redis caido"))

            resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")

    async def test_cierre_ya_aplicado_previamente_tambien_limpia_el_checkpoint(self):
        """El camino de 'ya cerrada' (SFC rechaza porque el caso ya cuenta con
        respuesta final) también cuenta como cierre exitoso -- debe limpiar el
        checkpoint igual que un cierre normal."""
        from app.core.exceptions import SfcIntegrationException

        cierre_dict = self.base_payload_dict.copy()
        cierre_dict.update({
            "Status": "Closed",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Cierre ya aplicado.</p>",
        })
        payload = QuejaUnificadaCrmInput.model_validate(cierre_dict)

        self.orquestador.m3_service.ejecutar_cierre_definitivo = AsyncMock(
            side_effect=SfcIntegrationException(
                400, "BUSINESS_RULE_ERROR", None,
                "La queja ya cuenta con un documento de respuesta final",
                "Ya está cerrada"
            )
        )

        with patch(
            "app.services.despacho_queja_orchestrator.IdempotencyService"
        ) as MockIdempotencyService:
            instancia = MockIdempotencyService.return_value
            instancia.limpiar_checkpoint_archivos = AsyncMock()

            resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        instancia.limpiar_checkpoint_archivos.assert_awaited_once_with(payload.Smart_Code__c)


if __name__ == "__main__":
    unittest.main()