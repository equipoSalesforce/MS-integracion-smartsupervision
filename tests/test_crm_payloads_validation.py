# tests/test_crm_payloads_validation.py
"""
Cobertura de las reglas de negocio en los validators de
app/schemas/crm_payloads.py (Momento2QuejaCrmInput y QuejaUnificadaCrmInput)
-- rango de fechas, saneamiento de texto, normalización de archivos_s3,
catálogos con fallback normalizado, y las reglas de cierre/fraude de
QuejaUnificadaCrmInput. Antes sin cobertura directa: los demás tests
del microservicio construyen payloads ya válidos y no ejercitan las
ramas de error de estos validators.
"""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.schemas.crm_payloads import (
    ConfirmacionAckInput,
    Momento2QuejaCrmInput,
    QuejaUnificadaCrmInput,
)


def _fecha_reciente(dias_atras: int = 5) -> str:
    return (datetime.now(ZoneInfo("America/Bogota")) - timedelta(days=dias_atras)).strftime("%Y-%m-%dT%H:%M:%S")


def _payload_m2_base(**overrides) -> dict:
    base = {
        "Smart_Code__c": "16551509974606",
        "CreatedDate": _fecha_reciente(),
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
        "admision_col__c": "No Aplica",
        "Status": "New",
        "Description": "Prueba de queja",
        "smart_anexo_queja__c": False,
        "Tutela__c": "No",
        "Ente_de_control__c": "Otros",
        "Product__c": "Cuenta perfil",
        "smart_Producto_nombre__c": "Ahorro",
        "Categorias_COL__c": "Transacción no reconocida",
        "smart_escalamiento_DCF__c": "No",
        "archivos_s3": [],
    }
    base.update(overrides)
    return base


def _payload_unificado_base(**overrides) -> dict:
    return _payload_m2_base(**overrides)


class TestValidarRango30DiasCreatedDate(unittest.TestCase):

    def test_fecha_anterior_a_30_dias_es_rechazada(self):
        fecha_vieja = _fecha_reciente(dias_atras=40)
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(CreatedDate=fecha_vieja))
        self.assertIn("30 días", str(ctx.exception))

    def test_fecha_futura_es_rechazada(self):
        fecha_futura = (datetime.now(ZoneInfo("America/Bogota")) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(CreatedDate=fecha_futura))
        self.assertIn("posterior", str(ctx.exception))

    def test_formato_invalido_no_se_confunde_con_error_de_rango(self):
        # El parseo inválido se traga en silencio en este validator (no contiene "30 días"
        # ni "posterior") -- el siguiente validator (formato ISO) es el que realmente falla.
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(CreatedDate="fecha-no-valida"))
        self.assertIn("ISO 8601", str(ctx.exception))


class TestSanitizarCamposTexto(unittest.TestCase):

    def test_campo_no_string_pasa_intacto_al_validador_de_tipo(self):
        # El validator "before" no toca valores no-string (los retorna intactos) --
        # es el propio tipo `str` requerido del campo el que rechaza None después.
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(direccion__c=None))
        self.assertIn("valid string", str(ctx.exception))

    def test_elimina_scripts_y_tags_html(self):
        payload = Momento2QuejaCrmInput(
            **_payload_m2_base(Description="Hola <script>alert(1)</script><b>mundo</b>")
        )
        self.assertNotIn("<script", payload.Description)
        self.assertNotIn("<b>", payload.Description)


class TestNormalizarArchivosS3(unittest.TestCase):

    def test_none_se_normaliza_a_lista_vacia(self):
        payload = Momento2QuejaCrmInput(**_payload_m2_base(archivos_s3=None))
        self.assertEqual(payload.archivos_s3, [])

    def test_dict_vacio_se_normaliza_a_lista_vacia(self):
        payload = Momento2QuejaCrmInput(**_payload_m2_base(archivos_s3={}))
        self.assertEqual(payload.archivos_s3, [])

    def test_dict_unico_se_envuelve_en_lista(self):
        payload = Momento2QuejaCrmInput(
            **_payload_m2_base(archivos_s3={"nombre_archivo": "a.pdf", "s3_key": "k", "bucket": "b"})
        )
        self.assertEqual(len(payload.archivos_s3), 1)

    def test_dict_con_forma_irreconocible_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(archivos_s3={"clave_rara": "valor"}))
        self.assertIn("forma no reconocida", str(ctx.exception))


class TestAsegurarDireccionValida(unittest.TestCase):

    def test_direccion_en_blanco_usa_default(self):
        payload = Momento2QuejaCrmInput(**_payload_m2_base(direccion__c="   "))
        self.assertEqual(payload.direccion__c, "Dirección no registrada")


class TestValidarNombreNoVacio(unittest.TestCase):

    def test_nombre_en_blanco_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(SuppliedName="   "))
        self.assertIn("SuppliedName", str(ctx.exception))


class TestResolverYArmarSmartCode(unittest.TestCase):

    def test_smart_code_final_supera_30_caracteres_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(Smart_Code__c="X" * 30))
        self.assertIn("30", str(ctx.exception))

    def test_smart_code_en_blanco_se_deriva_de_case_id(self):
        payload_dict = _payload_m2_base(Smart_Code__c="   ", Case_id="CASE-0001")
        payload = Momento2QuejaCrmInput(**payload_dict)
        self.assertTrue(payload.Smart_Code__c.endswith("CASE-0001"))
        self.assertEqual(payload.Case_id, "CASE-0001")


class TestValidarFormatoEmail(unittest.TestCase):

    def test_email_con_formato_invalido_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(SuppliedEmail="no-es-un-correo"))
        self.assertIn("formato válido", str(ctx.exception))

    def test_email_none_pasa_intacto(self):
        payload = Momento2QuejaCrmInput(**_payload_m2_base(SuppliedEmail=None))
        self.assertIsNone(payload.SuppliedEmail)


class TestLimpiarIdCaracteresEspeciales(unittest.TestCase):

    def test_id_sin_caracteres_alfanumericos_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(id_number__c="---..."))
        self.assertIn("id_number__c", str(ctx.exception))

    def test_id_elimina_caracteres_especiales(self):
        payload = Momento2QuejaCrmInput(**_payload_m2_base(id_number__c="10.400-110/14"))
        self.assertEqual(payload.id_number__c, "1040011014")


class TestValidarFormatoIsoFecha(unittest.TestCase):

    def test_fecha_con_formato_no_iso_es_rechazada(self):
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(CreatedDate="31/12/2025"))
        self.assertIn("ISO 8601", str(ctx.exception))


class TestLimpiarEspaciosYCaracteres(unittest.TestCase):

    def test_smart_code_con_caracteres_no_permitidos_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(Smart_Code__c="SC ESPACIO#!"))
        self.assertIn("caracteres no permitidos", str(ctx.exception))


class TestValidarPicklistContraMapper(unittest.TestCase):

    def test_nit_se_normaliza_a_rut(self):
        payload = Momento2QuejaCrmInput(**_payload_m2_base(SC_id_type__c="NIT"))
        self.assertEqual(payload.SC_id_type__c, "RUT")

    def test_booleano_si_no_invalido_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(Tutela__c="tal vez"))
        self.assertIn("Tutela__c", str(ctx.exception))

    def test_booleano_si_no_acepta_variantes(self):
        payload = Momento2QuejaCrmInput(**_payload_m2_base(Tutela__c="1"))
        self.assertEqual(payload.Tutela__c, "Si")

    def test_valor_de_catalogo_se_normaliza_por_texto_equivalente(self):
        # "transaccion no reconocida" (sin tilde, minúsculas) debe resolver al valor
        # canónico "Transacción no reconocida" vía _normalize_text.
        payload = Momento2QuejaCrmInput(**_payload_m2_base(Categorias_COL__c="transaccion no reconocida"))
        self.assertEqual(payload.Categorias_COL__c, "Transacción no reconocida")

    def test_valor_de_catalogo_no_soportado_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(Categorias_COL__c="Motivo Que No Existe En El Catalogo"))
        self.assertIn("Categorias_COL__c", str(ctx.exception))

    def test_campo_fuera_de_catalogo_y_no_booleano_pasa_intacto(self):
        # marcacion__c no está en field_to_catalog ni en boolean_si_no_fields.
        payload = Momento2QuejaCrmInput(**_payload_m2_base(marcacion__c="cualquier valor libre"))
        self.assertEqual(payload.marcacion__c, "cualquier valor libre")

    def test_valor_en_blanco_de_catalogo_pasa_intacto(self):
        payload = Momento2QuejaCrmInput(**_payload_m2_base(canal__c="   "))
        self.assertEqual(payload.canal__c, "   ")


class TestAutoCompletarYValidarFechaCreacion(unittest.TestCase):

    def test_fecha_ausente_se_autogenera(self):
        payload = Momento2QuejaCrmInput(**_payload_m2_base(CreatedDate=None))
        self.assertIsNotNone(payload.CreatedDate)
        datetime.fromisoformat(payload.CreatedDate)

    def test_fecha_con_formato_invalido_antes_de_validar_es_rechazada(self):
        with self.assertRaises(ValidationError) as ctx:
            Momento2QuejaCrmInput(**_payload_m2_base(CreatedDate="no-es-fecha"))
        self.assertIn("ISO 8601", str(ctx.exception))


class TestClosedDateNormalizacion(unittest.TestCase):

    def _payload_cierre(self, **overrides):
        defaults = dict(
            Status="Closed",
            Favorabilidad__c="Favorable",
            Aceptacion__c="Respuesta final a favor del consumidor financiero aceptadas por la entidad",
        )
        defaults.update(overrides)
        return _payload_unificado_base(**defaults)

    def test_closed_date_en_blanco_se_normaliza_a_none(self):
        payload = QuejaUnificadaCrmInput(**self._payload_cierre(ClosedDate="   "))
        self.assertIsNotNone(payload.ClosedDate)  # _validar_reglas_cierre lo completa con hoy.

    def test_closed_date_con_formato_datetime_iso_se_convierte_a_date(self):
        hoy_iso = datetime.now(ZoneInfo("America/Bogota")).strftime("%Y-%m-%dT10:00:00")
        payload = QuejaUnificadaCrmInput(**self._payload_cierre(ClosedDate=hoy_iso))
        self.assertEqual(str(payload.ClosedDate), hoy_iso.split("T")[0])

    def test_closed_date_con_formato_invalido_es_rechazada(self):
        with self.assertRaises(ValidationError) as ctx:
            QuejaUnificadaCrmInput(**self._payload_cierre(ClosedDate="fecha-invalida"))
        self.assertIn("ClosedDate", str(ctx.exception))


class TestLimpiarYStripCierreStrings(unittest.TestCase):

    def test_favorabilidad_en_blanco_se_normaliza_a_none(self):
        with self.assertRaises(ValidationError) as ctx:
            QuejaUnificadaCrmInput(**_payload_unificado_base(Status="Closed", Favorabilidad__c="   "))
        # Al quedar None, cae en la regla de "Favorabilidad__c obligatorio para el cierre".
        self.assertIn("Favorabilidad__c", str(ctx.exception))


class TestValidarPicklistCierre(unittest.TestCase):

    def test_favorabilidad_no_soportada_es_rechazada(self):
        with self.assertRaises(ValidationError) as ctx:
            QuejaUnificadaCrmInput(**_payload_unificado_base(
                Status="Closed",
                Favorabilidad__c="Un Valor Que No Existe",
                Aceptacion__c="Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            ))
        self.assertIn("Favorabilidad__c", str(ctx.exception))


class TestValidarReglasCierre(unittest.TestCase):

    def test_cierre_sin_favorabilidad_ni_aceptacion_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            QuejaUnificadaCrmInput(**_payload_unificado_base(Status="Closed"))
        self.assertIn("Cierre Definitivo", str(ctx.exception))

    def test_cierre_sin_aceptacion_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            QuejaUnificadaCrmInput(**_payload_unificado_base(Status="Closed", Favorabilidad__c="Favorable"))
        self.assertIn("Aceptacion__c", str(ctx.exception))

    def test_closed_date_futura_es_rechazada(self):
        fecha_futura = (datetime.now(ZoneInfo("America/Bogota")) + timedelta(days=5)).date().isoformat()
        with self.assertRaises(ValidationError) as ctx:
            QuejaUnificadaCrmInput(**_payload_unificado_base(
                Status="Closed",
                Favorabilidad__c="Favorable",
                Aceptacion__c="Respuesta final a favor del consumidor financiero aceptadas por la entidad",
                ClosedDate=fecha_futura,
            ))
        self.assertIn("posterior a la fecha actual", str(ctx.exception))

    def test_closed_date_anterior_a_created_date_es_rechazada(self):
        created = _fecha_reciente(dias_atras=2)
        closed_antes = (datetime.now(ZoneInfo("America/Bogota")) - timedelta(days=3)).date().isoformat()
        with self.assertRaises(ValidationError) as ctx:
            QuejaUnificadaCrmInput(**_payload_unificado_base(
                CreatedDate=created,
                Status="Closed",
                Favorabilidad__c="Favorable",
                Aceptacion__c="Respuesta final a favor del consumidor financiero aceptadas por la entidad",
                ClosedDate=closed_antes,
            ))
        self.assertIn("fecha de creación", str(ctx.exception))

    def test_cuerpo_respuesta_final_en_blanco_usa_default(self):
        payload = QuejaUnificadaCrmInput(**_payload_unificado_base(
            Status="Closed",
            Favorabilidad__c="Favorable",
            Aceptacion__c="Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            cuerpo_respuesta_final="   ",
        ))
        self.assertIn("cierre definitivo", payload.cuerpo_respuesta_final)


class TestValidarReglasFraude(unittest.TestCase):

    def _payload_fraude(self, **overrides):
        defaults = dict(
            tipo_fraude__c="Externo",
            modalidad_fraude__c="Suplantación de identidad",
            directorio_s3="caso/SC-1/",
            card_amount__c=1000.0,
            Total_Devuelto_por_Desconocimiento__c=1000.0,
        )
        defaults.update(overrides)
        return _payload_unificado_base(**defaults)

    def test_fraude_sin_archivos_ni_directorio_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            QuejaUnificadaCrmInput(**self._payload_fraude(directorio_s3=None))
        self.assertIn("investigación de fraude", str(ctx.exception))

    def test_fraude_sin_card_amount_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            QuejaUnificadaCrmInput(**self._payload_fraude(card_amount__c=None))
        self.assertIn("card_amount__c", str(ctx.exception))

    def test_fraude_sin_total_devuelto_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            QuejaUnificadaCrmInput(**self._payload_fraude(Total_Devuelto_por_Desconocimiento__c=None))
        self.assertIn("Total_Devuelto_por_Desconocimiento__c", str(ctx.exception))

    def test_fraude_con_un_archivo_sin_directorio_autoasigna_nombre(self):
        payload = QuejaUnificadaCrmInput(**self._payload_fraude(
            directorio_s3=None,
            archivos_s3=[{"nombre_archivo": "informe.pdf", "s3_key": "k", "bucket": "b"}],
        ))
        self.assertEqual(payload.nombre_archivo_fraude, "informe.pdf")

    def test_fraude_con_varios_archivos_sin_nombre_fraude_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            QuejaUnificadaCrmInput(**self._payload_fraude(
                directorio_s3=None,
                archivos_s3=[
                    {"nombre_archivo": "a.pdf", "s3_key": "k1", "bucket": "b"},
                    {"nombre_archivo": "b.pdf", "s3_key": "k2", "bucket": "b"},
                ],
            ))
        self.assertIn("nombre_archivo_fraude", str(ctx.exception))

    def test_fraude_con_nombre_archivo_inexistente_en_la_lista_es_rechazado(self):
        with self.assertRaises(ValidationError) as ctx:
            QuejaUnificadaCrmInput(**self._payload_fraude(
                directorio_s3=None,
                archivos_s3=[{"nombre_archivo": "a.pdf", "s3_key": "k1", "bucket": "b"}],
                nombre_archivo_fraude="no-existe.pdf",
            ))
        self.assertIn("no se encuentra dentro de archivos_s3", str(ctx.exception))


class TestConfirmacionAckInput(unittest.TestCase):

    def test_lista_con_solo_elementos_en_blanco_es_rechazada(self):
        with self.assertRaises(ValidationError) as ctx:
            ConfirmacionAckInput(ids_quejas=["   ", ""])
        self.assertIn("identificador válido", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
