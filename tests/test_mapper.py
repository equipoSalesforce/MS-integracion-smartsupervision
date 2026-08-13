# tests/test_mapper.py
import unittest
from unittest.mock import patch

from app.core.mapping import SfcSalesforceMapper


class TestSfcSalesforceMapper(unittest.TestCase):

    def setUp(self):
        self.patcher = patch('app.core.mapping.settings')
        self.mock_settings = self.patcher.start()
        
        self.mock_settings.SFC_TIPO_ENTIDAD = 1
        self.mock_settings.SFC_ENTIDAD_COD = "423"

        self.salesforce_case_data = {
            "Smart_Code__c": "16551509974606",
            "CreatedDate": "2026-07-16T12:00:00",
            "SuppliedName": "Camila Salas",
            "id_number__c": "1040011014",
            "person_birthdate__c": "1995-04-12",
            "SuppliedEmail": "camila.salas@global66.com",
            "SuppliedPhone": "3007654321",
            "company_name__c": "Global66 Colombia SAS",
            "direccion__c": "Calle 93 # 11-11",
            "Departamento__c": "Bogotá D.C.",
            "SC_municipio__c": "Bogotá D.C.",
            "Description": "<p>Prueba de texto HTML de la queja.</p>",
            "smart_anexo_queja__c": True,
            "LastModifiedDate": "2026-07-17",
            "sinRespuestaFinal__c": True,
            "ClosedDate": "2026-07-16",
            "card_amount__c": 500000.0,
            "Total_Devuelto_por_Desconocimiento__c": 450000.0,
            "Aceptacion__c": True,
            "Prorroga__c": False,
            "Rectificacion__c": False,
            "canal__c": "Internet",
            "Ente_de_control__c": "Otros",
            "tipo_de_persona__c": "Natural",
            "Status": "In progress"
        }

        # 🪐 DATOS ORIGEN SUPERINTENDENCIA (SFC)
        self.sfc_payload_data = {
            "fecha_creacion": "2026-07-16 08:30:00",
            "codigo_queja": "16551509974606",
            # 🟢 FIX: Se usa el código numérico regulatorio '170' oficial de Colombia para la SFC
            "codigo_pais": "170",
            "departamento_cod": "11",
            "municipio_cod": "11001",
            "nombres": "Camila Salas",
            "tipo_id_CF": 1,
            "numero_id_CF": "1040011014",
            "telefono": "3007654321",
            "correo": "camila.salas@global66.com",
            "tipo_persona": 1,
            "sexo": 2,
            "lgbtiq": 2,
            "canal_cod": 13,
            "condicion_especial": 98,
            "producto_cod": 209,
            "macro_motivo_cod": 209,
            "texto_queja": "Texto de prueba de la queja de la SFC.",
            "anexo_queja": True,
            "tutela": 2,
            "ente_control": 99,
            "escalamiento_DCF": 2,
            "replica": 2,
            "desistimiento_queja": 2,
            "queja_expres": 2
        }

    def tearDown(self):
        self.patcher.stop()

    def test_mapeo_crm_hacia_sfc_excel_valores(self):
        res_m2 = SfcSalesforceMapper.crm_entity_to_sfc_payload(self.salesforce_case_data, momento=2)

        self.assertEqual(res_m2["texto_queja"], "Prueba de texto HTML de la queja.")
        self.assertEqual(res_m2["departamento_cod"], "11")
        self.assertEqual(res_m2["municipio_cod"], "11001")
        self.assertEqual(res_m2["fecha_creacion"], "2026-07-16T12:00:00")
        self.assertEqual(res_m2["canal_cod"], 13)
        self.assertEqual(res_m2["ente_control"], 99)
        self.assertEqual(res_m2["tipo_persona"], 1)

        res_m3 = SfcSalesforceMapper.crm_entity_to_sfc_payload(self.salesforce_case_data, momento=3)

        self.assertEqual(res_m3["monto_reclamado"], 500000.0)
        self.assertEqual(res_m3["monto_reconocido"], 450000.0)
        self.assertTrue(res_m3["fecha_cierre"].startswith("2026-07-16T"))
        self.assertEqual(res_m3["canal_cod"], 13)
        self.assertEqual(res_m3["ente_control"], 99)
        self.assertEqual(res_m3["estado_cod"], 2)

    def test_mapeo_sfc_hacia_crm(self):
        """Valida que la traducción del Momento 1 des-homologue códigos SFC a strings legibles para el CRM."""
        resultado_crm = SfcSalesforceMapper.sfc_payload_to_db_dict(self.sfc_payload_data)

        self.assertEqual(resultado_crm["Smart_Code__c"], "16551509974606")
        self.assertEqual(resultado_crm["SuppliedName"], "Camila Salas")
        self.assertEqual(resultado_crm["id_number__c"], "1040011014")
        self.assertEqual(resultado_crm["codigo_pais__c"], "Colombia")

        self.assertEqual(resultado_crm["Departamento__c"], "Bogotá D.C.")
        self.assertEqual(resultado_crm["SC_municipio__c"], "Bogotá D.C.")

        self.assertEqual(resultado_crm["canal__c"], "Internet")
        self.assertEqual(resultado_crm["Ente_de_control__c"], "Otros")
        self.assertEqual(resultado_crm["tipo_de_persona__c"], "B2C")
        self.assertEqual(resultado_crm["sc_genero__c"], "Masculino")
        self.assertEqual(resultado_crm["sc_Condicion_especial__c"], "No aplica")

        self.assertTrue(resultado_crm["smart_anexo_queja__c"])
        self.assertEqual(resultado_crm["sc_LGBTIQ__c"], "No")
        self.assertEqual(resultado_crm["Tutela__c"], "No")
        self.assertEqual(resultado_crm["Desistimiento__c"], "Queja o reclamo no desistida por el CF")
        self.assertEqual(resultado_crm["Quejas_express__c"], "No")
        self.assertEqual(resultado_crm["smart_escalamiento_DCF__c"], "No")
        self.assertEqual(resultado_crm["replica__c"], "No")

        self.assertEqual(resultado_crm["CreatedDate"], "2026-07-16T08:30:00")
        self.assertEqual(resultado_crm["SuppliedEmail"], "camila.salas@global66.com")
        self.assertEqual(resultado_crm["SuppliedPhone"], "3007654321")
        self.assertEqual(resultado_crm["Description"], "Texto de prueba de la queja de la SFC.")


if __name__ == "__main__":
    unittest.main()