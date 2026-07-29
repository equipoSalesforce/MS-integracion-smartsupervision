# tests/test_email_parser.py
import unittest

from app.utils.email_parser import (
    extraer_texto_limpio_de_html,
    limpiar_texto_para_campo_pdf,
)


class TestEmailParser(unittest.TestCase):

    def setUp(self):
        """Configuración de fixtures HTML para reutilizar en los casos de prueba."""
        
        # 1. Ejemplo Complejo: Hilo largo con múltiples réplicas intercaladas estilo Gmail
        self.html_hilo_complejo_gmail = """<html><head><meta charset='UTF-8'><title>Re: Respuesta final caso TEST-CIERRE-DIRECTO-SSV-002</title></head><body style='margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;color:#202124;'><div style='max-width:760px;margin:0 auto;background:#ffffff;padding:24px 28px;border:1px solid #E5E7EB;'><div style='font-size:20px;font-weight:600;color:#202124;margin-bottom:18px;'>Re: Respuesta final sobre tu reclamo - Global66</div><div style='border-bottom:1px solid #E5E7EB;padding-bottom:16px;margin-bottom:18px;'><table width='100%' cellpadding='0' cellspacing='0'><tr><td width='42' valign='top'><div style='width:36px;height:36px;border-radius:50%;background:#0b57d0;color:#ffffff;text-align:center;line-height:36px;font-weight:bold;'>MG</div></td><td valign='top'><div style='font-size:14px;color:#202124;'><strong>María González</strong> &lt;soporte@global66.com&gt;</div><div style='font-size:12px;color:#5f6368;margin-top:2px;'>para Daniela Rojas Mock · 29 jul 2026, 10:25</div></td><td align='right' valign='top'><img src='https://dummyimage.com/110x30/ffffff/003B8F.png&text=Global66' alt='Global66' style='display:block;border:1px solid #E5E7EB;border-radius:6px;'></td></tr></table></div><div style='font-size:14px;line-height:22px;color:#202124;'><p>Hola Daniela,</p><p>Te escribimos para informarte el resultado final de la revisión asociada a tu reclamo por la transacción que no reconocías en tu cuenta Global66.</p><p>Después de revisar la trazabilidad de la operación, los registros de seguridad y la información que nos compartiste, confirmamos que tu solicitud fue resuelta de manera <strong>favorable</strong>. La aceptación queda registrada como <strong>parcialmente favorable</strong>, ya que acogimos la revisión principal solicitada, pero se mantiene registro de algunos antecedentes operacionales para efectos de trazabilidad y control interno.</p><p>Con esto, dejamos el caso cerrado y registrado con el número <strong>TEST-CIERRE-DIRECTO-SSV-002</strong>.</p><p>Saludos,</p><div style='margin-top:18px;padding-top:14px;border-top:1px solid #E5E7EB;'><table cellpadding='0' cellspacing='0'><tr><td valign='top' style='padding-right:12px;'><img src='https://dummyimage.com/64x64/003B8F/ffffff.png&text=G66' alt='Global66' style='border-radius:12px;display:block;'></td><td valign='top' style='font-size:13px;line-height:20px;color:#374151;'><strong style='font-size:14px;color:#111827;'>María González</strong><br>Ejecutiva de Atención al Cliente<br>Global66<br><span style='color:#0b57d0;'>soporte@global66.com</span><br><span style='color:#6b7280;'>www.global66.com</span></td></tr></table></div><div style='margin-top:18px;background:#f8fafc;border:1px solid #E5E7EB;border-radius:10px;padding:12px;color:#6b7280;font-size:12px;line-height:18px;'>Este mensaje y sus anexos pueden contener información confidencial. Si recibiste este correo por error, por favor elimínalo e informa al remitente. Este correo corresponde a una prueba técnica de integración SmartSupervisión.</div></div><div style='margin-top:28px;border-left:3px solid #DADCE0;padding-left:14px;color:#3c4043;font-size:13px;line-height:21px;'><div style='color:#5f6368;margin-bottom:10px;'>El mié, 29 jul 2026 a las 10:13, Daniela Rojas Mock &lt;daniela.rojas.mock@global66.test&gt; escribió:</div><div style='background:#ffffff;'><p>Hola María,</p><p>Gracias por la revisión. Entiendo que encontraron antecedentes para resolver mi caso y acepto que se deje como parcialmente favorable, pero necesito que quede constancia de que yo no reconocía inicialmente esa transacción.</p><p>También agradezco que me confirmen por escrito que el reclamo fue revisado y cerrado.</p><p>Saludos,<br>Daniela</p></div><div style='margin-top:18px;border-left:3px solid #DADCE0;padding-left:14px;color:#3c4043;'><div style='color:#5f6368;margin-bottom:10px;'>El mié, 29 jul 2026 a las 09:58, María González &lt;soporte@global66.com&gt; escribió:</div><p>Hola Daniela,</p><p>Seguimos revisando tu caso con el equipo interno. Validamos la información de la transacción, la fecha, el estado de la cuenta y la evidencia que nos compartiste.</p><p>De forma preliminar, vemos que corresponde acoger tu solicitud principal. Antes del cierre, queremos confirmar contigo que recibiste esta actualización.</p><p>Saludos,<br>María González<br>Global66</p><div style='margin-top:14px;border-left:3px solid #DADCE0;padding-left:14px;color:#3c4043;'><div style='color:#5f6368;margin-bottom:10px;'>El mié, 29 jul 2026 a las 09:41, Daniela Rojas Mock &lt;daniela.rojas.mock@global66.test&gt; escribió:</div><p>Hola,</p><p>Adjunto captura del movimiento que no reconozco. Me preocupa que aparezca en mi cuenta porque no recuerdo haber autorizado esa operación.</p><div style='margin:12px 0;padding:10px;border:1px dashed #CBD5E1;border-radius:10px;background:#f8fafc;'><img src='https://dummyimage.com/600x160/f8fafc/334155.png&text=Captura+adjunta+por+cliente:+movimiento+no+reconocido' alt='Captura adjunta movimiento no reconocido' style='width:100%;max-width:600px;display:block;border-radius:8px;'></div><p>Quedo atenta.</p><p>Daniela</p><div style='margin-top:14px;border-left:3px solid #DADCE0;padding-left:14px;color:#3c4043;'><div style='color:#5f6368;margin-bottom:10px;'>El mié, 29 jul 2026 a las 09:26, María González &lt;soporte@global66.com&gt; escribió:</div><p>Hola Daniela,</p><p>Gracias por contactarnos. Recibimos tu solicitud y abrimos la revisión correspondiente. Por favor envíanos la captura del movimiento y cualquier antecedente que tengas para completar el análisis.</p><p>Saludos,<br>María González<br>Global66</p><div style='margin-top:14px;border-left:3px solid #DADCE0;padding-left:14px;color:#3c4043;'><div style='color:#5f6368;margin-bottom:10px;'>El mié, 29 jul 2026 a las 09:12, Daniela Rojas Mock &lt;daniela.rojas.mock@global66.test&gt; escribió:</div><p>Hola equipo Global66,</p><p>Reporto una transacción que no reconozco en mi cuenta. Necesito que revisen el movimiento y me indiquen cómo proceder.</p><p>Gracias,<br>Daniela Rojas</p></div></div></div></div></div></div></body></html>"""

        # 2. Ejemplo Simple: Respuesta directa sin hilo previo
        self.html_respuesta_simple_soporte = """<html><head><meta charset='UTF-8'><title>Re: Respuesta final caso TEST-CIERRE-DIRECTO-SSV-002</title></head><body style='margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;color:#202124;'><div style='max-width:760px;margin:0 auto;background:#ffffff;padding:24px 28px;border:1px solid #E5E7EB;'><div style='font-size:20px;font-weight:600;color:#202124;margin-bottom:18px;'>Re: Respuesta final sobre tu reclamo - Global66</div><div style='border-bottom:1px solid #E5E7EB;padding-bottom:16px;margin-bottom:18px;'><table width='100%' cellpadding='0' cellspacing='0'><tr><td width='42' valign='top'><div style='width:36px;height:36px;border-radius:50%;background:#0b57d0;color:#ffffff;text-align:center;line-height:36px;font-weight:bold;'>MG</div></td><td valign='top'><div style='font-size:14px;color:#202124;'><strong>María González</strong> &lt;soporte@global66.com&gt;</div><div style='font-size:12px;color:#5f6368;margin-top:2px;'>para Daniela Rojas Mock · 29 jul 2026, 10:25</div></td><td align='right' valign='top'><img src='https://dummyimage.com/110x30/ffffff/003B8F.png&text=Global66' alt='Global66' style='display:block;border:1px solid #E5E7EB;border-radius:6px;'></td></tr></table></div><div style='font-size:14px;line-height:22px;color:#202124;'><p>Hola Daniela,</p><p>Te escribimos para informarte el resultado final de la revisión asociada a tu reclamo por la transacción que no reconocías en tu cuenta Global66.</p><p>Después de revisar la trazabilidad de la operación, los registros de seguridad y la información que nos compartiste, confirmamos que tu solicitud fue resuelta de manera <strong>favorable</strong>. La aceptación queda registrada de conformidad con las políticas de la entidad.</p><p>Con esto, dejamos el caso cerrado y registrado con el número <strong>TEST-CIERRE-DIRECTO-SSV-002</strong>.</p><p>Saludos,</p><div style='margin-top:18px;padding-top:14px;border-top:1px solid #E5E7EB;'><table cellpadding='0' cellspacing='0'><tr><td valign='top' style='padding-right:12px;'><img src='https://dummyimage.com/64x64/003B8F/ffffff.png&text=G66' alt='Global66' style='border-radius:12px;display:block;'></td><td valign='top' style='font-size:13px;line-height:20px;color:#374151;'><strong style='font-size:14px;color:#111827;'>María González</strong><br>Ejecutiva de Atención al Cliente<br>Global66<br><span style='color:#0b57d0;'>soporte@global66.com</span><br><span style='color:#6b7280;'>www.global66.com</span></td></tr></table></div></div></div></body></html>"""

        # 3. Ejemplo Intermedio: Dictamen formal de la Gerencia de Seguridad
        self.html_dictamen_seguridad_fraude = """<html><head><meta charset='UTF-8'><title>Re: Cierre definitivo de caso TEST-ALL-IN-ONE-SSV-003</title></head><body style='margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;color:#202124;'><div style='max-width:760px;margin:0 auto;background:#ffffff;padding:24px 28px;border:1px solid #E5E7EB;'><div style='font-size:20px;font-weight:600;color:#202124;margin-bottom:18px;'>Re: Dictamen final sobre caso de suplantación - Global66</div><div style='border-bottom:1px solid #E5E7EB;padding-bottom:16px;margin-bottom:18px;'><table width='100%' cellpadding='0' cellspacing='0'><tr><td width='42' valign='top'><div style='width:36px;height:36px;border-radius:50%;background:#0b57d0;color:#ffffff;text-align:center;line-height:36px;font-weight:bold;'>GS</div></td><td valign='top'><div style='font-size:14px;color:#202124;'><strong>Gerencia de Seguridad y Riesgo</strong> &lt;seguridad@global66.com&gt;</div><div style='font-size:12px;color:#5f6368;margin-top:2px;'>para Carlos Alberto Mendoza · 29 jul 2026, 11:00</div></td><td align='right' valign='top'><img src='https://dummyimage.com/110x30/ffffff/003B8F.png&text=Global66' alt='Global66' style='display:block;border:1px solid #E5E7EB;border-radius:6px;'></td></tr></table></div><div style='font-size:14px;line-height:22px;color:#202124;'><p>Estimado Don Carlos Alberto,</p><p>Le informamos que el área de Seguridad Operacional ha concluido la investigación técnica correspondiente a las transacciones no reconocidas en su Tarjeta Digital.</p><p>El análisis pericial confirmó que la operación respondió a un evento de <strong>Fraude Externo por Suplantación de Identidad</strong>. En consecuencia, su solicitud ha sido resuelta de manera <strong>FAVORABLE</strong> y se ha dispuesto el reembolso total por valor de <strong>$4.800.000 COP</strong> a su cuenta origen.</p><p>Adjunto a este envío encontrará la documentación de respaldo. Damos por concluido y clausurado el caso con el código <strong>TEST-ALL-IN-ONE-SSV-003</strong>.</p><p>Atentamente,</p><div style='margin-top:18px;padding-top:14px;border-top:1px solid #E5E7EB;'><table cellpadding='0' cellspacing='0'><tr><td valign='top' style='padding-right:12px;'><img src='https://dummyimage.com/64x64/003B8F/ffffff.png&text=G66' alt='Global66' style='border-radius:12px;display:block;'></td><td valign='top' style='font-size:13px;line-height:20px;color:#374151;'><strong style='font-size:14px;color:#111827;'>Gerencia de Experiencia y Seguridad</strong><br>Global66 Colombia<br><span style='color:#0b57d0;'>contacto@global66.com</span></td></tr></table></div></div></div></body></html>"""

        # 4. Ejemplo Intermedio: Hilo con formato Microsoft Outlook / Exchange
        self.html_estilo_outlook = """<html>
        <body>
            <p>Hola Juan,</p>
            <p>Tu reclamo referente al cobro duplicado fue atendido y procesado con éxito.</p>
            <p>Saludos,<br>Soporte Global66</p>
            <hr style="display:inline-block;width:98%">
            <div id="divRplyFwdMsg" dir="ltr">
                <font face="Calibri, sans-serif" color="#000000" size="2">
                <b>From:</b> Juan Pérez &lt;juan.perez@test.com&gt;<br>
                <b>Sent:</b> Wednesday, July 29, 2026 8:30 AM<br>
                <b>To:</b> soporte@global66.com<br>
                <b>Subject:</b> Reclamo por cobro duplicado<br>
                </font><br>
            </div>
            <div>
                <p>Hola, requiero revisar un cobro duplicado en mi cuenta.</p>
            </div>
        </body>
        </html>"""

    # ======================================================================
    # 🧪 MÉTODOS DE PRUEBA
    # ======================================================================

    def test_parser_hilo_complejo_gmail(self):
        """
        Verifica que en un hilo largo estilo Gmail se extraiga la respuesta superior de Global66
        y se omitan las réplicas pasadas de la cliente y del agente.
        """
        resultado = extraer_texto_limpio_de_html(self.html_hilo_complejo_gmail)

        # 1. Debe contener la respuesta oficial más reciente
        self.assertIn("Te escribimos para informarte el resultado final", resultado)
        self.assertIn("parcialmente favorable", resultado)
        self.assertIn("TEST-CIERRE-DIRECTO-SSV-002", resultado)
        self.assertIn("María González", resultado)

        # 2. NO debe incluir réplicas anteriores de la cliente
        self.assertNotIn("Entiendo que encontraron antecedentes para resolver mi caso", resultado)
        self.assertNotIn("Daniela Rojas Mock", resultado)
        self.assertNotIn("Adjunto captura del movimiento que no reconozco", resultado)

        # 3. NO debe incluir el aviso de confidencialidad/disclaimer del footer
        self.assertNotIn("Este mensaje y sus anexos pueden contener información confidencial", resultado)

    def test_parser_respuesta_simple_soporte(self):
        """Verifica la extracción limpia en una plantilla sin hilo previo."""
        resultado = extraer_texto_limpio_de_html(self.html_respuesta_simple_soporte)

        self.assertIn("Te escribimos para informarte el resultado final", resultado)
        self.assertIn("María González", resultado)
        self.assertIn("soporte@global66.com", resultado)

    def test_parser_dictamen_seguridad_fraude(self):
        """Verifica el procesamiento de dictámenes formales de seguridad operacional."""
        resultado = extraer_texto_limpio_de_html(self.html_dictamen_seguridad_fraude)

        self.assertIn("Estimado Don Carlos Alberto", resultado)
        self.assertIn("Fraude Externo por Suplantación de Identidad", resultado)
        self.assertIn("$4.800.000 COP", resultado)
        self.assertIn("Gerencia de Seguridad y Riesgo", resultado)

    def test_parser_estilo_outlook(self):
        """Verifica el corte limpio de cabeceras en formato Outlook (From: / Sent: / To:)."""
        resultado = extraer_texto_limpio_de_html(self.html_estilo_outlook)

        self.assertIn("Tu reclamo referente al cobro duplicado fue atendido y procesado con éxito.", resultado)
        self.assertIn("Soporte Global66", resultado)
        self.assertNotIn("Juan Pérez", resultado)
        self.assertNotIn("Reclamo por cobro duplicado", resultado)

    def test_parser_texto_plano_directo(self):
        """Verifica el comportamiento cuando la entrada es texto plano sin HTML."""
        texto_raw = "Hola,\nTu caso fue resuelto correctamente por Global66.\nSaludos."
        resultado = extraer_texto_limpio_de_html(texto_raw)

        self.assertEqual(resultado, "Hola,\n\nTu caso fue resuelto correctamente por Global66.\n\nSaludos.")

    def test_parser_casos_vacios_o_nulos(self):
        """Verifica la resiliencia ante valores nulos o vacíos."""
        self.assertEqual(extraer_texto_limpio_de_html(""), "")
        self.assertEqual(extraer_texto_limpio_de_html(None), "")
        self.assertEqual(extraer_texto_limpio_de_html("   "), "")

    def test_limpiar_texto_para_campo_pdf(self):
        """Valida la función fachada consumida por la generación de PDFs."""
        resultado_pdf = limpiar_texto_para_campo_pdf(self.html_hilo_complejo_gmail)

        self.assertIn("Te escribimos para informarte el resultado final", resultado_pdf)
        self.assertNotIn("Daniela Rojas Mock", resultado_pdf)


if __name__ == "__main__":
    unittest.main()