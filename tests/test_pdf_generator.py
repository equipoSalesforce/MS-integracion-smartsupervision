# tests/test_pdf_generator.py
import unittest  # 👈 Importamos la librería nativa
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfReader
from app.utils.pdf_generator import generar_pdf_respuesta_final, ajustar_ancho_texto


class TestAjustarAnchoTexto(unittest.TestCase):
    """
    Cobertura directa de ajustar_ancho_texto -- hasta ahora sólo ejercitada
    indirectamente vía el único test de generar_pdf_respuesta_final, nunca
    aislada para sus casos borde.
    """

    def test_texto_vacio_retorna_vacio(self):
        self.assertEqual(ajustar_ancho_texto(""), "")

    def test_texto_none_retorna_vacio(self):
        self.assertEqual(ajustar_ancho_texto(None), "")

    def test_linea_corta_no_se_modifica(self):
        self.assertEqual(ajustar_ancho_texto("Hola mundo"), "Hola mundo")

    def test_linea_larga_se_envuelve_en_multiples_lineas(self):
        linea_larga = "palabra " * 30  # muy por encima de 90 caracteres
        resultado = ajustar_ancho_texto(linea_larga, max_caracteres_por_linea=90)

        self.assertIn("\n", resultado)
        for linea in resultado.split("\n"):
            self.assertLessEqual(len(linea), 90)

    def test_parrafos_multiples_preservan_saltos_de_linea_originales(self):
        texto = "Primer párrafo corto.\nSegundo párrafo también corto."
        resultado = ajustar_ancho_texto(texto)

        self.assertEqual(resultado, texto)

    def test_palabra_unica_mas_larga_que_el_ancho_no_se_rompe(self):
        """break_long_words=False -- una palabra sin espacios más larga que el
        ancho máximo se deja intacta en su propia línea en vez de partirse a la
        mitad (evita cortar, ej., una URL o un código a la mitad)."""
        palabra_larguisima = "a" * 150
        resultado = ajustar_ancho_texto(palabra_larguisima, max_caracteres_por_linea=90)

        self.assertIn(palabra_larguisima, resultado)


class TestPdfGenerator(unittest.TestCase):  # 👈 Debe heredar de unittest.TestCase

    def test_generar_pdf_respuesta_final_exito(self):  # 👈 El método debe empezar con "test_"
        caso_nombre = "Caso smartsupervision"
        smart_code = "142316551509974606"
        texto_crm = (
            "Hola:\n"
            "Te escribe Joseph del equipo de Experiencia al Cliente.\n"
            "Para nosotros es un placer haberte atendido..."
        )
        
        nombre_archivo_salida = f"caso_{smart_code}_RESP_FINAL_SFC.pdf"
        ruta_salida = Path("app/resources") / nombre_archivo_salida

        if ruta_salida.exists():
            ruta_salida.unlink()

        try:
            ruta_generada = generar_pdf_respuesta_final(
                caso_nombre=caso_nombre,
                smart_code=smart_code,
                texto_crm=texto_crm,
                ruta_salida=ruta_salida
            )

            # En unittest usamos las aserciones de la clase self.assert...
            self.assertTrue(ruta_generada.exists(), "El archivo PDF no fue creado.")
            self.assertEqual(ruta_generada, ruta_salida)

            size_bytes = ruta_generada.stat().st_size
            self.assertTrue(size_bytes > 0, "El archivo generado está vacío.")

            reader = PdfReader(ruta_generada)
            campos_formulario = reader.get_fields()
            
            # Verificamos que el flattening haya funcionado
            assert campos_formulario is not None, "No se detectaron los campos en el PDF generado."
            for nombre_campo, info_campo in campos_formulario.items():
                field_flags = info_campo.get("/Ff", 0)
                # Verificamos si el primer bit (Read-Only) está encendido
                assert (field_flags & 1) == 1, f"El campo {nombre_campo} no quedó protegido contra escritura."

        except Exception as e:
            if ruta_salida.exists():
                ruta_salida.unlink()
            raise e

    def test_sin_ruta_salida_retorna_bytes_en_memoria(self):
        """
        Cubre el ÚNICO camino que la producción realmente ejercita:
        momento_3_sync.py::_generar_y_enviar_pdf_respuesta_final llama a esta
        función SIEMPRE sin `ruta_salida` (necesita los bytes en memoria para
        subirlos a S3 e inmediatamente a la SFC, nunca escribe a disco) -- el
        único test existente hasta ahora sólo cubría el camino de disco, que
        production nunca usa.
        """
        resultado = generar_pdf_respuesta_final(
            caso_nombre="Caso smartsupervision",
            smart_code="142316551509974606",
            texto_crm="Respuesta final de prueba, sin archivo de salida."
        )

        self.assertIsInstance(resultado, bytes)
        self.assertGreater(len(resultado), 0)
        # Debe ser un PDF válido y leíble, no sólo bytes cualquiera.
        import io
        reader = PdfReader(io.BytesIO(resultado))
        self.assertEqual(len(reader.pages), 1)

    def test_texto_vacio_no_crashea_y_genera_pdf_valido(self):
        """texto_crm='' es un caso real: momento_3_sync.py lo sustituye por un
        texto por defecto ANTES de llamar acá, pero esta función no debe
        depender de ese caller -- debe seguir generando un PDF válido si
        alguien la llama directamente con texto vacío."""
        resultado = generar_pdf_respuesta_final(
            caso_nombre="Caso vacío", smart_code="SC-VACIO", texto_crm=""
        )

        self.assertIsInstance(resultado, bytes)
        self.assertGreater(len(resultado), 0)

    def test_plantilla_ausente_lanza_filenotfounderror_explicito(self):
        """Si la plantilla PDF no está en el paquete (deploy roto/incompleto),
        debe fallar rápido y explícito -- no con un traceback críptico de pypdf
        sobre un archivo que no existe."""
        with patch("pathlib.Path.exists", return_value=False):
            with self.assertRaises(FileNotFoundError):
                generar_pdf_respuesta_final(
                    caso_nombre="Caso X", smart_code="SC-X", texto_crm="texto"
                )