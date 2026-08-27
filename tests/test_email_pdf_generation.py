import unittest
from pathlib import Path

from app.utils.email_parser import extraer_texto_limpio_de_html
from app.utils.pdf_generator import generar_pdf_respuesta_final


class TestEmailToPdfGeneration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 🎯 Creamos el directorio app/temp para guardar los PDFs de inspección visual
        cls.output_dir = Path("app/temp")
        cls.output_dir.mkdir(parents=True, exist_ok=True)

    # ======================================================================
    # 🟢 CASO 1: CORREO SIMPLE (Texto plano / HTML Básico)
    # ======================================================================
    def test_1_generar_pdf_correo_simple(self):
        html_simple = """
        <p>Estimado(a) Juan Pérez,</p>
        <p>Le informamos que su solicitud ha sido resuelta de manera <b>favorable</b>.</p>
        <p>Atentamente,<br>Servicio al Cliente Global66</p>
        """

        texto_limpio = extraer_texto_limpio_de_html(html_simple)
        pdf_path = self.output_dir / "test_1_correo_simple.pdf"

        resultado_path = generar_pdf_respuesta_final(
            caso_nombre="Juan Perez",
            smart_code="1423999000111",
            texto_crm=texto_limpio,
            ruta_salida=pdf_path
        )

        self.assertTrue(resultado_path.exists())
        self.assertGreater(resultado_path.stat().st_size, 0)
        print(f"\n[OK] PDF Simple generado en: {resultado_path.resolve()}")

    # ======================================================================
    # 🟡 CASO 2: CORREO INTERMEDIO (Formato, Listas, Viñetas y Entidades HTML)
    # ======================================================================
    def test_2_generar_pdf_correo_intermedio(self):
        html_intermedio = """
        <div>
            <h2>Notificación de Resolución de Reclamación</h2>
            <p>Estimada Consumidora <b>María Rodríguez</b>,</p>
            <p>Hacemos referencia a su reclamación radicada con el código &nbsp;<b>1423888777666</b>.&nbsp;
            Luego de revisar la transacción, procedimos con los siguientes ajustes:</p>
            <ul>
                <li><b>Abono principal:</b> $150.000 COP</li>
                <li><b>Devolución de intereses:</b> $12.500 COP</li>
            </ul>
            <br>
            <p>El dinero estará disponible en su cuenta en un plazo máximo de 24 horas hábiles.</p>
            <p>Si tiene inquietudes adicionales, puede responder a este mensaje.</p>
            <p>Cordialmente,<br><b>Defensoría del Consumidor Financiero - Global66</b></p>
        </div>
        """

        texto_limpio = extraer_texto_limpio_de_html(html_intermedio)
        pdf_path = self.output_dir / "test_2_correo_intermedio.pdf"

        resultado_path = generar_pdf_respuesta_final(
            caso_nombre="Maria Rodriguez",
            smart_code="1423888777666",
            texto_crm=texto_limpio,
            ruta_salida=pdf_path
        )

        self.assertTrue(resultado_path.exists())
        self.assertGreater(resultado_path.stat().st_size, 0)
        print(f"[OK] PDF Intermedio generado en: {resultado_path.resolve()}")

    # ======================================================================
    # 🔴 CASO 3: CORREO COMPLEJO (Hilo largo de correo con respuestas anteriores)
    # ======================================================================
    def test_3_generar_pdf_correo_complejo_hilo(self):
        html_complejo = """
        <html>
            <head><style>body { font-family: Arial; }</style></head>
            <body>
                <p>Estimado <b>Carlos Mendoza</b>,</p>
                <p>Dando seguimiento a la conversación y a la investigación realizada por nuestro equipo de seguridad, 
                adjuntamos el dictamen final sobre el caso.</p>
                <p>Se determinó que la transacción no fue autorizada, por lo cual se aplicó el reintegro total en su <b>Global Account</b>.</p>

                <p>Atentamente,<br>
                <b>Equipo de Operaciones y Compliance</b><br>
                Soporte Global66</p>

                <hr>
                <div style="color: #555555;">
                    <p><b>De:</b> carlos.mendoza@email.com<br>
                    <b>Enviado el:</b> Lunes, 20 de Julio de 2026 14:30<br>
                    <b>Para:</b> soporte@global66.com<br>
                    <b>Asunto:</b> Re: Reclamación transacción no reconocida #1423555444333</p>
                    
                    <p>Hola equipo,</p>
                    <p>Adjunto los extractos bancarios que me solicitaron para continuar con la investigación.</p>
                    <p>Quedo atento a su respuesta.</p>
                </div>

                <hr>
                <div style="color: #888888;">
                    <p><b>De:</b> soporte@global66.com<br>
                    <b>Enviado el:</b> Viernes, 17 de Julio de 2026 09:15<br>
                    <b>Para:</b> carlos.mendoza@email.com<br>
                    <b>Asunto:</b> Reclamación recibida #1423555444333</p>

                    <p>Estimado Carlos, hemos recibido su queja y asignado el caso a un analista.</p>
                </div>
            </body>
        </html>
        """

        texto_limpio = extraer_texto_limpio_de_html(html_complejo)
        pdf_path = self.output_dir / "test_3_correo_complejo_hilo.pdf"

        resultado_path = generar_pdf_respuesta_final(
            caso_nombre="Carlos Mendoza",
            smart_code="1423555444333",
            texto_crm=texto_limpio,
            ruta_salida=pdf_path
        )

        self.assertTrue(resultado_path.exists())
        self.assertGreater(resultado_path.stat().st_size, 0)
        print(f"[OK] PDF Complejo Hilo generado en: {resultado_path.resolve()}")


if __name__ == "__main__":
    unittest.main()