import unittest
from unittest.mock import AsyncMock, MagicMock
from datetime import date

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

        # Mocks asíncronos para aislar las llamadas de servicios
        self.orquestador.m2_service.ejecutar_envio_momento_2 = AsyncMock()
        self.orquestador.m3_service.ejecutar_gestion_fraude = AsyncMock()
        self.orquestador.m3_service.ejecutar_cierre_definitivo = AsyncMock()
        self.orquestador.m3_service.ejecutar_actualizacion_tramite = AsyncMock()

        # Payload base canónico válido
        self.base_payload_dict = {
            "Smart_Code__c": "999000111222",
            "CreatedDate": "2026-07-21T10:00:00",
            "Status": "New",
            "SuppliedName": "Juan Perez",
            "SC_id_type__c": "CC",
            "id_number__c": "123456789",
            "sc_genero__c": "No Aplica",
            "tipo_de_persona__c": "B2C",
            "sc_LGBTIQ__c": "No",
            "sc_Condicion_especial__c": "No aplica",
            "direccion__c": "Calle 123",
            "Departamento__c": "Bogotá D.C.",
            "SC_municipio__c": "Bogotá D.C.",
            "canal__c": "Internet",
            "punto_recepcion": "WhatsApp",
            "Instancia_de_recepcion__c": "Entidad vigilada",
            "Product__c": "Cuenta perfil",
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
        self.orquestador.m2_service.ejecutar_envio_momento_2.return_value = {
            "status": "success", "codigo_queja_sfc": "1423999000111222"
        }

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
        tramite_dict["Status"] = "In Progress"
        payload = QuejaUnificadaCrmInput.model_validate(tramite_dict)

        self.orquestador.m3_service.ejecutar_actualizacion_tramite.return_value = {
            "status": "success", "message": "Tramite actualizado"
        }

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
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "nombre_archivo_fraude": "dictamen_fraude.pdf",
            "archivos_s3": [{"nombre_archivo": "dictamen_fraude.pdf", "s3_key": "q/f.pdf", "bucket": "b1"}]
        })
        payload = QuejaUnificadaCrmInput.model_validate(fraude_dict)

        self.orquestador.m3_service.ejecutar_gestion_fraude.return_value = {
            "status": "success", "message": "Fraude actualizado"
        }

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
            "ClosedDate": "2026-07-22",
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Estimado consumidor, su reclamación ha sido resuelta no favorablemente.</p>",
            "archivos_s3": []
        })
        payload = QuejaUnificadaCrmInput.model_validate(cierre_dict)

        self.orquestador.m3_service.ejecutar_cierre_definitivo.return_value = {
            "status": "success", "message": "Caso cerrado"
        }

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
            "nombre_archivo_fraude": "dictamen_fraude.pdf",
            "ClosedDate": "2026-07-22",
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Notificación final de investigación de fraude y cierre del caso.</p>",
            "archivos_s3": [
                {"nombre_archivo": "dictamen_fraude.pdf", "s3_key": "q/f.pdf", "bucket": "b1"}
            ]
        })
        payload = QuejaUnificadaCrmInput.model_validate(completo_dict)

        self.orquestador.m3_service.ejecutar_gestion_fraude.return_value = {"status": "success"}
        self.orquestador.m3_service.ejecutar_cierre_definitivo.return_value = {"status": "success"}

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        self.orquestador.m3_service.ejecutar_gestion_fraude.assert_called_once_with(payload=payload)
        self.orquestador.m3_service.ejecutar_cierre_definitivo.assert_called_once_with(payload=payload)

    # ======================================================================
    # ⚡ CASO 6: AUTO-RECUPERACIÓN COMPLETA (404 -> M2 -> M3 FRAUDE -> M3 CIERRE)
    # ======================================================================
    async def test_6_auto_recuperacion_secuencia_completa_404(self):
        """
        🔥 TEST PRINCIPAL DE SELF-HEALING:
        Si llega un cierre+fraude de una queja que NO existe en la SFC (404/NOT_FOUND_ERROR),
        debe crear el caso en M2 y re-ejecutar Fraude y Cierre automáticamente.
        """
        completo_dict = self.base_payload_dict.copy()
        completo_dict.update({
            "Status": "Closed",
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "nombre_archivo_fraude": "dictamen_fraude.pdf",
            "ClosedDate": "2026-07-22",
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
            "cuerpo_respuesta_final": "<p>Respuesta final emitida tras la secuencia de auto-recuperación.</p>",
            "archivos_s3": [
                {"nombre_archivo": "dictamen_fraude.pdf", "s3_key": "q/f.pdf", "bucket": "b1"}
            ]
        })
        payload = QuejaUnificadaCrmInput.model_validate(completo_dict)

        mock_404_error = SfcIntegrationException(
            404,                 # status_code
            "NOT_FOUND_ERROR",   # error_type
            None,                # sfc_field
            "Not found",         # raw_message
            "Queja no encontrada" # crm_action
        )

        # 1er Intento M3 Fraude: Falla con 404 (SFC no conoce el caso)
        # 2do Intento M3 Fraude: Retorna éxito tras la creación
        self.orquestador.m3_service.ejecutar_gestion_fraude.side_effect = [
            mock_404_error,
            {"status": "success", "message": "Fraude actualizado"}
        ]

        # Creación M2 exitosa
        self.orquestador.m2_service.ejecutar_envio_momento_2.return_value = {
            "status": "success", "codigo_queja_sfc": "1423999000111222"
        }

        # Cierre M3 exitoso
        self.orquestador.m3_service.ejecutar_cierre_definitivo.return_value = {
            "status": "success", "message": "Caso cerrado definitivamente"
        }

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "success")
        self.orquestador.m2_service.ejecutar_envio_momento_2.assert_called_once_with(payload=payload)
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
            "ClosedDate": "2026-07-22",
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

        # Cierre M3 da 404
        self.orquestador.m3_service.ejecutar_cierre_definitivo.side_effect = mock_404_error

        # Creación M2 falla
        self.orquestador.m2_service.ejecutar_envio_momento_2.return_value = {
            "status": "error", "message": "Pipeline interrumpido: Timeout"
        }

        resultado = await self.orquestador.procesar_despacho(payload)

        self.assertEqual(resultado["status"], "error")
        self.assertIn("Pipeline interrumpido", resultado["message"])
        self.orquestador.m2_service.ejecutar_envio_momento_2.assert_called_once()
        self.assertEqual(self.orquestador.m3_service.ejecutar_cierre_definitivo.call_count, 1)


if __name__ == "__main__":
    unittest.main()