# tests/test_pdf_generator.py
import unittest  # 👈 Importamos la librería nativa
from pathlib import Path
from pypdf import PdfReader
from app.utils.pdf_generator import generar_pdf_respuesta_final

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