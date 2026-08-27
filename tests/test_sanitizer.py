# tests/test_sanitizer.py
import unittest

from app.core.security.sanitizer import (
    sanitizar_payload,
    sanitizar_headers,
    sanitizar_texto_plano,
    sanitizar_html_para_pdf,
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


class TestSanitizarHtmlParaPdf(unittest.TestCase):
    """
    🔴 FIX (hallazgo propio, 2026-08-27, auditoría final de cobertura): esta función
    es la barrera de seguridad usada antes de convertir HTML del CRM a PDF regulatorio
    (bloquea scripts/iframes ejecutables y SSRF vía file:///IPs privadas/metadata de
    AWS), pero ningún test la ejercitaba con contenido realmente peligroso -- sólo se
    confiaba en la lectura del regex.
    """

    def test_vacio_retorna_cadena_vacia(self):
        self.assertEqual(sanitizar_html_para_pdf(""), "")
        self.assertEqual(sanitizar_html_para_pdf(None), "")

    def test_script_tag_es_removido(self):
        resultado = sanitizar_html_para_pdf("<p>hola</p><script>alert(1)</script><p>chau</p>")
        self.assertNotIn("<script", resultado)
        self.assertIn("hola", resultado)
        self.assertIn("chau", resultado)

    def test_iframe_embed_object_link_meta_base_son_removidos(self):
        for tag in ("iframe", "embed", "object", "link", "meta", "base"):
            with self.subTest(tag=tag):
                resultado = sanitizar_html_para_pdf(f"<{tag} src='x'>contenido</{tag}>")
                self.assertNotIn(f"<{tag}", resultado)

    def test_html_benigno_no_se_modifica(self):
        html = "<p>Estimado cliente, su caso fue <b>resuelto</b>.</p>"
        self.assertEqual(sanitizar_html_para_pdf(html), html)

    def test_file_scheme_es_neutralizado(self):
        resultado = sanitizar_html_para_pdf('<img src="file:///etc/passwd">')
        self.assertNotIn("file://", resultado)
        self.assertIn('src="#"', resultado)

    def test_aws_metadata_ip_es_neutralizada(self):
        """El regex neutraliza el prefijo peligroso (esquema+host) del atributo
        src/href -- no re-escribe la URL completa. El resto de la cadena queda como
        texto suelto sin efecto (no vuelve a formar un atributo src/href real)."""
        resultado = sanitizar_html_para_pdf('<img src="http://169.254.169.254/latest/meta-data/">')
        self.assertIn('src="#"', resultado)
        self.assertNotIn('src="http://169.254', resultado)

    def test_localhost_e_ips_privadas_son_neutralizadas(self):
        for url in (
            "http://localhost/admin",
            "http://127.0.0.1/x",
            "http://10.0.0.5/x",
            "http://172.16.0.1/x",
            "http://192.168.1.1/x",
        ):
            with self.subTest(url=url):
                resultado = sanitizar_html_para_pdf(f'<a href="{url}">link</a>')
                self.assertNotIn(url, resultado)

    def test_url_publica_no_se_toca(self):
        html = '<a href="https://www.superfinanciera.gov.co">SFC</a>'
        self.assertEqual(sanitizar_html_para_pdf(html), html)


if __name__ == "__main__":
    unittest.main()
