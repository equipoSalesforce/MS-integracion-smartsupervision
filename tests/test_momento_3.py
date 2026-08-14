# tests/test_momento_3.py
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import status
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.api.dependencies import get_sfc_client, get_s3_client
from app.services.momento_3_sync import Momento3SincronizacionService
from app.core.exceptions import SfcIntegrationException
from app.schemas.crm_payloads import QuejaUnificadaCrmInput


class TestMomento3UnitAndIntegration(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # 🪐 Mocks de Infraestructura Externa
        self.sfc_client_mock = MagicMock()
        self.s3_client_mock = MagicMock()
        
        # Inyección de Dependencias nativa para los tests de Integración
        app.dependency_overrides[get_sfc_client] = lambda: self.sfc_client_mock
        app.dependency_overrides[get_s3_client] = lambda: self.s3_client_mock

        # 🟢 SIMULACIÓN DE REDIS: Evita que la política Fail-Closed bloquee con 503 durante unit tests
        self.redis_patcher = patch("app.api.routes_quejas.get_redis_client")
        self.mock_get_redis = self.redis_patcher.start()

        self.mock_redis = AsyncMock()
        self.mock_redis.get.return_value = None
        self.mock_redis.set.return_value = True
        self.mock_get_redis.return_value = self.mock_redis
        
        self.client = TestClient(app)
        
        # 🎯 INYECCIÓN DE API KEY: Permite pasar la validación de seguridad
        self.client.headers.update({"X-API-Key": settings.CRM_API_KEY})

        self.smart_code_test = "16551509974609"
        self.sfc_id_largo_esperado = f"{settings.SFC_TIPO_ENTIDAD}{settings.SFC_ENTIDAD_COD}{self.smart_code_test}"

        # 📄 Base de proforma simulada para la SFC
        self.mock_mapper_response = {
            "canal_cod": 13,          # "Internet" -> 13
            "producto_cod": 207,       # "Cuenta perfil" -> Wildcard 207
            "macro_motivo_cod": 940,   # "Transacción no reconocida" -> 940
        }

        # Fechas relativas a "hoy" para que las validaciones de ventana de 30 días
        # (CreatedDate/ClosedDate) no dependan de cuándo corra el test.
        hoy_bogota = datetime.now(ZoneInfo("America/Bogota"))
        fecha_creacion_reciente = (hoy_bogota - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        self.fecha_cierre_reciente = (hoy_bogota - timedelta(days=1)).strftime("%Y-%m-%d")

        # 📦 Base de datos obligatoria para construir payloads válidos con QuejaUnificadaCrmInput
        self.base_crm_payload = {
            "Smart_Code__c": self.smart_code_test,
            "CreatedDate": fecha_creacion_reciente,
            "Status": "In Progress",
            "SuppliedName": "Camila Salas",
            "SC_id_type__c": "CC",
            "id_number__c": "1040011014",
            "sc_genero__c": "Femenino",
            "tipo_de_persona__c": "B2C",
            "sc_LGBTIQ__c": "No",
            "sc_Condicion_especial__c": "No aplica",
            "SuppliedPhone": "3001234567",
            "SuppliedEmail": "camila@test.com",
            "direccion__c": "Calle 93 # 11-11",
            "Departamento__c": "Bogotá D.C.",
            "SC_municipio__c": "Bogotá D.C.",
            "canal__c": "Internet",
            "punto_recepcion": "Manual",
            "Instancia_de_recepcion__c": "Entidad vigilada",
            "admision_col__c": "Queja o reclamo admitida por el DCF",
            "Description": "Prueba de caso de seguimiento y cierre.",
            "smart_anexo_queja__c": False,
            "Tutela__c": "No",
            "Ente_de_control__c": "Otros",
            "smart_escalamiento_DCF__c": "No",
            "Product__c": "Cuenta perfil",
            "smart_Producto_nombre__c": "Ahorro",
            "Categorias_COL__c": "Transacción no reconocida",
            "archivos_s3": []
        }

    def tearDown(self):
        app.dependency_overrides.clear()
        self.redis_patcher.stop()

    # ======================================================================
    # 🧪 SUITE 1: PRUEBAS DE INTEGRACIÓN (HTTP ENDPOINT UNIFICADO & PYDANTIC)
    # ======================================================================

    def test_endpoint_fraude_fallo_pydantic_ambiguedad_archivos(self):
        """Verifica el rechazo si vienen múltiples archivos en fraude pero no se especifica el principal."""
        payload_ambiguo = self.base_crm_payload.copy()
        payload_ambiguo.update({
            "tipo_fraude__c": "Interno",                  
            "modalidad_fraude__c": "Vulneración de cuenta o producto", 
            "card_amount__c": 50000.0,                
            "Total_Devuelto_por_Desconocimiento__c": 0.0,                   
            "nombre_archivo_fraude": None,
            "archivos_s3": [
                {"nombre_archivo": "soporte1.pdf", "s3_key": "k1", "bucket": "b1"},
                {"nombre_archivo": "soporte2.xlsx", "s3_key": "k2", "bucket": "b1"}
            ]
        })
        
        response = self.client.post("/api/v1/quejas/sync/despacho", json=payload_ambiguo)
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Es obligatorio especificar 'nombre_archivo_fraude'", response.text)

    @patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload")
    def test_endpoint_tramite_exito_stateless(self, mock_mapper):
        """Prueba de punta a punta de una actualización rutinaria sin adjuntos."""
        sfc_mock = self.mock_mapper_response.copy()
        sfc_mock["estado_cod"] = 2                        
        mock_mapper.return_value = sfc_mock
        
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(return_value={"Status": "updated"})

        payload_tramite = self.base_crm_payload.copy()
        payload_tramite.update({
            "Status": "In Progress",
            "producto_digital__c": "Si"
        })

        response = self.client.post("/api/v1/quejas/sync/despacho", json=payload_tramite)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK, msg=f"Fallo en esquema: {response.text}")
        self.sfc_client_mock.put_actualizar_queja.assert_called_once()

    # ======================================================================
    # 🧪 SUITE 2: PRUEBAS UNITARIAS (LÓGICA CORE DEL SERVICIO & GENERACIÓN DE PDF)
    # ======================================================================

    @patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload")
    async def test_servicio_cierre_generacion_pdf_respuesta_final(self, mock_mapper):
        """Verifica la generación del PDF de respuesta final y su transmisión a la SFC en cierre."""
        sfc_mock = self.mock_mapper_response.copy()
        sfc_mock["estado_cod"] = 4                        
        sfc_mock["fecha_cierre"] = "2026-07-16"           
        sfc_mock["a_favor_de"] = 1                        
        sfc_mock["aceptacion_queja"] = 1                  
        sfc_mock["rectificacion_queja"] = 2               
        sfc_mock["prorroga_queja"] = 2                    
        mock_mapper.return_value = sfc_mock
        
        self.sfc_client_mock.post_adjunto_queja = AsyncMock(return_value={"id": 99})
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(return_value={"Status": "closed"})

        servicio = Momento3SincronizacionService(sfc_client=self.sfc_client_mock, s3_client=self.s3_client_mock)
        
        payload_dict = self.base_crm_payload.copy()
        payload_dict.update({
            "Status": "Closed",
            "ClosedDate": self.fecha_cierre_reciente,
            "Favorabilidad__c": "Favorable",
            "a_favor_de__c": "1",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            "Rectificacion__c": "No",
            "Prorroga__c": 1,
            "cuerpo_respuesta_final": "<p>Estimado cliente, su reclamación ha sido resuelta a favor.</p>",
            "archivos_s3": []
        })
        
        input_pydantic = QuejaUnificadaCrmInput(**payload_dict)

        resultado = await servicio.ejecutar_cierre_definitivo(payload=input_pydantic)

        self.assertEqual(resultado["status"], "success")
        self.sfc_client_mock.post_adjunto_queja.assert_called_once()
        kwargs_archivo = self.sfc_client_mock.post_adjunto_queja.call_args[1]
        
        # Aserta que el PDF generado incluya la convención de afijo oficial
        self.assertEqual(kwargs_archivo["file_name"], f"Respuesta_Final_{self.sfc_id_largo_esperado}_RESP_FINAL_SFC.pdf")
        self.assertEqual(kwargs_archivo["sfc_codigo_queja"], self.sfc_id_largo_esperado)

        self.sfc_client_mock.put_actualizar_queja.assert_called_once()
        payload_formulario_sfc = self.sfc_client_mock.put_actualizar_queja.call_args[1]["payload"]
        
        self.assertEqual(payload_formulario_sfc["estado_cod"], 4) 
        self.assertEqual(payload_formulario_sfc["fecha_cierre"], "2026-07-16")
        self.assertTrue(payload_formulario_sfc["anexo_queja"])

    @patch("app.core.mapping.SfcSalesforceMapper.crm_entity_to_sfc_payload")
    async def test_servicio_aislamiento_de_fallas_sfc_exception(self, mock_mapper):
        """Verifica que si la SFC reconoce la falla, la excepción controlada suba intacta."""
        sfc_mock = self.mock_mapper_response.copy()
        sfc_mock["estado_cod"] = 2
        mock_mapper.return_value = sfc_mock
        
        self.sfc_client_mock.put_actualizar_queja = AsyncMock(
            side_effect=SfcIntegrationException(
                status_code=400,
                error_type="VALIDATION_ERROR",
                sfc_field="estado_cod",
                raw_message="El código de estado no es válido",
                crm_action="Validar flujo de estados"
            )
        )

        servicio = Momento3SincronizacionService(sfc_client=self.sfc_client_mock, s3_client=None)
        
        payload_dict = self.base_crm_payload.copy()
        payload_dict.update({
            "Status": "In Progress",
            "producto_digital__c": "Si"
        })
        input_tramite = QuejaUnificadaCrmInput(**payload_dict)

        with self.assertRaises(SfcIntegrationException):
            await servicio.ejecutar_actualizacion_tramite(payload=input_tramite)


if __name__ == "__main__":
    unittest.main()