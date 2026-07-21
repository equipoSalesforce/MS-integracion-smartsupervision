# tests/test_mapper.py
import unittest
from datetime import datetime, date
from unittest.mock import patch

# 🚀 Importamos el mapper al inicio. Esto inicializa app.core.config y app.core.mapping de forma limpia.
from app.core.mapping import SfcSalesforceMapper

class TestSfcSalesforceMapper(unittest.TestCase):

    def setUp(self):
        """Inicializa los mocks de configuración y los conjuntos de datos basados en el Excel."""
        # 🎯 SOLUCIÓN: Parchamos 'settings' directamente en el espacio de nombres donde el mapper lo consume
        self.patcher = patch('app.core.mapping.settings')
        self.mock_settings = self.patcher.start()
        
        # Inyectamos los valores regulatorios requeridos para las pruebas
        self.mock_settings.SFC_TIPO_ENTIDAD = 1
        self.mock_settings.SFC_ENTIDAD_COD = "423"

        # 🪐 DATOS ORIGEN SALESFORCE (CRM)
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
            "Departamento__c": "Bogotá D.C.",          # Debe homologar a DIVIPOLA '11'
            "SC_municipio__c": "Bogotá D.C.",          # Debe homologar a DIVIPOLA '11001'
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
            "tipo_de_persona__c": "Natural"
        }

        # 🪐 DATOS ORIGEN SUPERINTENDENCIA (SFC)
        self.sfc_payload_data = {
            "fecha_creacion": "2026-07-16 08:30:00",
            "codigo_queja": "16551509974606",
            "codigo_pais": "COL",
            "departamento_cod": "11",                  # Debe transformarse a 'Bogotá D.C.'
            "municipio_cod": "11001",                  # Debe transformarse a 'Bogotá D.C.'
            "nombres": "Camila Salas",
            "tipo_id_CF": 1,
            "numero_id_CF": "1040011014",
            "telefono": "3007654321",
            "correo": "camila.salas@global66.com",
            "tipo_persona": 1,
            "sexo": 2,
            "lgbtiq": 0,
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
        """Detiene el parche criptográfico al finalizar cada test individual."""
        self.patcher.stop()

    # ======================================================================
    # 🧪 TEST 1: CRM -> SFC (MOMENTOS 2 Y 3)
    # ======================================================================
    def test_mapeo_crm_hacia_sfc_excel_valores(self):
        """Valida que el diccionario enviado a la SFC cumpla con los tipos y equivalencias del Excel."""
        resultado_sfc = SfcSalesforceMapper.crm_entity_to_sfc_payload(self.salesforce_case_data)

        # 1. Comprobación de Limpieza de HTML y Normalización de Texto[cite: 5]
        self.assertEqual(resultado_sfc["texto_queja"], "Prueba de texto HTML de la queja.")

        # 2. Comprobación de Homologación de Códigos DIVIPOLA (Excel: Bogotá -> 11)[cite: 5]
        self.assertEqual(resultado_sfc["departamento_cod"], "11")
        self.assertEqual(resultado_sfc["municipio_cod"], "11001")

        # 3. Comprobación de Nombres de variables corregidos del Excel[cite: 5]
        self.assertEqual(resultado_sfc["monto_reclamado"], 500000.0)      # card_amount__c -> monto_reclamado
        self.assertEqual(resultado_sfc["monto_reconocido"], 450000.0)
        self.assertTrue(resultado_sfc["documentacion_rta_final"])         # sinRespuestaFinal__c -> documentacion_rta_final

        # 4. Verificación de Formateo de Fechas e Inyección de Variables con Tilde[cite: 5]
        self.assertEqual(resultado_sfc["fecha_creación"], "2026-07-16T12:00:00")
        self.assertEqual(resultado_sfc["fecha_cierre"], "2026-07-16")
        
        # 5. Verificación de Picklists y Catálogos Numéricos
        self.assertEqual(resultado_sfc["canal_cod"], 13)                  # Internet -> 13
        self.assertEqual(resultado_sfc["ente_control"], 99)               # Otros -> 99
        self.assertEqual(resultado_sfc["tipo_Persona"], 1)                # Natural -> 1

    def test_mapeo_fallback_casenumber_si_no_hay_smartcode(self):
        """Valida la nota técnica del Excel: si no viene Smart_Code__c usa CaseNumber de respaldo."""
        payload_sin_smartcode = {
            "CaseNumber": "00045612",
            "SuppliedName": "Test Fallback"
        }
        resultado = SfcSalesforceMapper.crm_entity_to_sfc_payload(payload_sin_smartcode)
        self.assertEqual(resultado["codigo_queja"], "00045612")

    # ======================================================================
    # 🧪 TEST 2: SFC -> CRM (MOMENTO 1)
    # ======================================================================
    def test_mapeo_sfc_hacia_crm(self):
        """Valida que la traducción del Momento 1 des-homologue códigos SFC a strings legibles para el CRM.[cite: 1]"""
        resultado_crm = SfcSalesforceMapper.sfc_payload_to_db_dict(self.sfc_payload_data)

        # 1. Verificación de Códigos de Control e Identificadores Básicos[cite: 1]
        self.assertEqual(resultado_crm["Smart_Code__c"], "16551509974606")
        self.assertEqual(resultado_crm["SuppliedName"], "Camila Salas")
        self.assertEqual(resultado_crm["id_number__c"], "1040011014")
        self.assertEqual(resultado_crm["codigo_pais__c"], "COL")

        # 2. Comprobación DIVIPOLA Inversa (Códigos de la SFC -> Picklists con Tilde del CRM)[cite: 1]
        self.assertEqual(resultado_crm["Departamento__c"], "Bogotá D.C.")
        self.assertEqual(resultado_crm["SC_municipio__c"], "Bogotá D.C.")

        # 3. Comprobación de traducción de Catálogos / Picklists de Entrada[cite: 1]
        self.assertEqual(resultado_crm["canal__c"], "Internet")
        self.assertEqual(resultado_crm["Ente_de_control__c"], "Otros")
        self.assertEqual(resultado_crm["tipo_de_persona__c"], "B2C")
        self.assertEqual(resultado_crm["sc_genero__c"], "Masculino")
        self.assertEqual(resultado_crm["sc_Condicion_especial__c"], "No aplica")

        # 4. Comprobación de Tipos Nativos (Numéricos/Enteros SFC -> Booleanos Reales CRM)[cite: 1]
        self.assertTrue(resultado_crm["smart_anexo_queja__c"])            # 1 -> True
        self.assertEqual(resultado_crm["sc_LGBTIQ__c"], "No")                        # 1 -> True
        self.assertEqual(resultado_crm["Tutela__c"], "No")                  # 0 -> False
        self.assertEqual(resultado_crm["Desistimiento__c"], "Queja o reclamo no desistida por el CF")                # 0 -> False
        self.assertEqual(resultado_crm["Quejas_express__c"], "No")               # 0 -> False
        self.assertEqual(resultado_crm["smart_escalamiento_DCF__c"], "No")             # 0 -> False
        self.assertEqual(resultado_crm["replica__c"], "No")                      # 0 -> False

        # 5. Validación de Mutación de Fechas (Formato SFC con espacio -> ISO con 'T' para Salesforce)[cite: 1]
        self.assertEqual(resultado_crm["CreatedDate"], "2026-07-16T08:30:00")

        # 6. Datos de Contacto Directos[cite: 1]
        self.assertEqual(resultado_crm["SuppliedEmail"], "camila.salas@global66.com")
        self.assertEqual(resultado_crm["SuppliedPhone"], "3007654321")
        self.assertEqual(resultado_crm["Description"], "Texto de prueba de la queja de la SFC.")

if __name__ == "__main__":
    unittest.main()