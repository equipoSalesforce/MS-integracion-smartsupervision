# tests/test_sanitizer.py
import unittest

from app.core.security.sanitizer import (
    sanitizar_payload,
    sanitizar_headers,
    sanitizar_texto_plano,
    mask_value,
)


class TestSanitizarPayloadAllowlist(unittest.TestCase):

    def test_campos_conocidos_como_pii_se_enmascaran(self):
        """PII clásica (nombre, id, correo, teléfono, dirección) nunca pasa en claro."""
        payload = {
            "SuppliedName": "Camila Salas",
            "id_number__c": "1040011014",
            "SuppliedEmail": "camila@test.com",
            "SuppliedPhone": "3001234567",
            "direccion__c": "Calle 93 # 11-11",
        }
        limpio = sanitizar_payload(payload)

        for campo, original in payload.items():
            self.assertNotEqual(limpio[campo], original, f"'{campo}' no debería quedar en claro")

    def test_campo_nuevo_no_clasificado_se_enmascara_por_defecto(self):
        """
        Núcleo de la allowlist: un campo que NUNCA se agregó a ninguna lista (ni
        allowlist ni blocklist) debe redactarse por defecto, no loggearse en claro.
        Esto es lo que una blocklist no garantiza.
        """
        payload = {"un_campo_inventado_que_nadie_clasifico": "dato-potencialmente-sensible"}
        limpio = sanitizar_payload(payload)

        self.assertNotEqual(
            limpio["un_campo_inventado_que_nadie_clasifico"],
            "dato-potencialmente-sensible"
        )

    def test_campos_estructurales_conocidos_pasan_en_claro(self):
        """Identificadores/códigos de clasificación sí deben quedar legibles para depurar."""
        payload = {
            "Smart_Code__c": "142316551509974606",
            "Case_id": "ecd8b88c-954b-400b-87a2-cad2ddc024bd",
            "Status": "New",
            "canal__c": "Internet",
            "correlation_id": "abc-123",
        }
        limpio = sanitizar_payload(payload)

        self.assertEqual(limpio, payload)

    def test_texto_libre_se_enmascara(self):
        """Description/texto_queja/cuerpo_respuesta_final no están en la allowlist."""
        payload = {
            "Description": "El cliente Juan Pérez con cédula 123456 reclama un cobro no reconocido",
            "cuerpo_respuesta_final": "<p>Estimado Juan...</p>",
        }
        limpio = sanitizar_payload(payload)

        self.assertNotIn("Juan Pérez", limpio["Description"])
        self.assertNotIn("123456", limpio["Description"])
        self.assertNotEqual(limpio["cuerpo_respuesta_final"], payload["cuerpo_respuesta_final"])

    def test_archivos_s3_metadata_pasa_en_claro(self):
        """archivos_s3 es metadata de ubicación, no PII: debe quedar legible."""
        payload = {
            "archivos_s3": [
                {"s3_key": "quejas/123/soporte.pdf", "nombre_archivo": "soporte.pdf", "bucket": "mi-bucket"}
            ]
        }
        limpio = sanitizar_payload(payload)

        self.assertEqual(limpio, payload)

    def test_lista_de_dicts_con_pii_anidada_se_enmascara(self):
        payload = {"usuarios": [{"nombres": "Camila", "numero_id_CF": "1234"}]}
        limpio = sanitizar_payload(payload)

        self.assertNotEqual(limpio["usuarios"][0]["nombres"], "Camila")
        self.assertNotEqual(limpio["usuarios"][0]["numero_id_CF"], "1234")

    def test_valor_none_se_preserva(self):
        payload = {"sc_genero__c": None, "campo_no_clasificado": None}
        limpio = sanitizar_payload(payload)

        self.assertIsNone(limpio["sc_genero__c"])
        self.assertIsNone(limpio["campo_no_clasificado"])

    def test_valor_no_string_no_clasificado_se_redacta(self):
        """Un campo numérico/booleano no reconocido también debe ocultarse, no sólo strings."""
        payload = {"un_monto_no_clasificado": 999999.99, "un_flag_no_clasificado": True}
        limpio = sanitizar_payload(payload)

        self.assertNotEqual(limpio["un_monto_no_clasificado"], 999999.99)
        self.assertNotEqual(limpio["un_flag_no_clasificado"], True)


class TestSanitizarTextoPlano(unittest.TestCase):

    def test_texto_vacio(self):
        self.assertEqual(sanitizar_texto_plano(None), "<vacío>")
        self.assertEqual(sanitizar_texto_plano(""), "<vacío>")

    def test_texto_corto_no_se_trunca_pero_no_es_el_original_solo(self):
        texto = "Error de validación"
        resultado = sanitizar_texto_plano(texto)
        self.assertIn(texto, resultado)
        self.assertIn(str(len(texto)), resultado)

    def test_texto_largo_se_trunca_y_no_incluye_el_contenido_completo(self):
        texto = "X" * 5000
        resultado = sanitizar_texto_plano(texto, max_chars=120)

        self.assertLess(len(resultado), len(texto))
        self.assertIn("5000 caracteres", resultado)
        self.assertNotIn("X" * 5000, resultado)


class TestSanitizarHeaders(unittest.TestCase):

    def test_headers_sensibles_se_enmascaran(self):
        headers = {"Authorization": "Bearer super-secreto-token-largo", "X-Api-Key": "abcd1234"}
        limpio = sanitizar_headers(headers)

        self.assertNotEqual(limpio["Authorization"], headers["Authorization"])
        self.assertNotEqual(limpio["X-Api-Key"], headers["X-Api-Key"])

    def test_headers_ruidosos_se_omiten(self):
        headers = {"Host": "example.com", "User-Agent": "test"}
        limpio = sanitizar_headers(headers)

        self.assertEqual(limpio, {})


if __name__ == "__main__":
    unittest.main()
