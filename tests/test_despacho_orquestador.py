import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock

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


if __name__ == "__main__":
    unittest.main()