# app/utils/email_parser.py
import html
from html.parser import HTMLParser
import re
from bs4 import BeautifulSoup


class HTMLToPlainTextParser(HTMLParser):
    """Parseador HTML nativo para extraer texto plano conservando la estructura de párrafos."""

    def __init__(self):
        super().__init__()
        self.reset()
        self.fed = []
        self.skip = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style"):
            self.skip = True
        elif tag in ("br", "tr"):
            self.fed.append("\n")
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "li"):
            self.fed.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style"):
            self.skip = False
        elif tag in ("p", "div", "h1", "h2", "h3", "h4"):
            self.fed.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.fed.append(d)

    def get_data(self) -> str:
        return "".join(self.fed)


def aplicar_guillotina_de_hilo_y_footer(texto_plano: str) -> str:
    """Aplica guillotina por expresiones regulares sobre texto plano:
    1. Guillotina Superior: Elimina cabeceras de correo tipo '... escribió:'.
    2. Guillotina Inferior: Corta disclaimers, hilos antiguos de Outlook/Gmail y footers legales.
    """
    if not texto_plano or not texto_plano.strip():
        return ""

    # 1. GUILLOTINA SUPERIOR: Si la cadena contiene "... escribió:", borra desde el inicio hasta esa palabra
    if re.search(r"\b(?:escribi[óo]|wrote):\s*", texto_plano, flags=re.IGNORECASE):
        texto_plano = re.sub(r"(?is)^.*?\b(?:escribi[óo]|wrote):\s*", "", texto_plano)

    # 2. GUILLOTINA INFERIOR: Cortar en seco al encontrar disclaimers o footers
    patrones_corte_inferior = [
        r"\n\s*AVISO DE CONFIDENCIALIDAD.*",
        r"\n\s*De:\s+[^\n]+",
        r"\n\s*From:\s+[^\n]+",
        r"\n\s*Enviado el:\s+[^\n]+",
        r"\n\s*Sent:\s+[^\n]+",
        r"\n\s*En cumplimiento del literal a.*",
        r"¿Tienes dudas\?.*",
        r"Centro de ayuda.*",
        r"You received this message because.*",
        r"To unsubscribe from this group.*",
        r"thread::.*",
    ]

    patron_unificado = "|".join(f"(?:{p})" for p in patrones_corte_inferior)
    partes = re.split(patron_unificado, texto_plano, flags=re.IGNORECASE | re.DOTALL)
    texto_limpio = partes[0]

    # 3. Normalizar párrafos y espacios vacíos
    lineas = [l.strip() for l in texto_limpio.splitlines() if l.strip()]
    return "\n\n".join(lineas).strip()


def extraer_y_limpiar_dom(soup_node) -> str:
    """Elimina imágenes y marcadores invisibles del DOM y extrae texto plano con saltos de línea."""
    node_copy = BeautifulSoup(str(soup_node), "html.parser")

    for img in node_copy.find_all("img"):
        img.decompose()
    for attr in node_copy.find_all(class_=re.compile(r"gmail_attr|gmail_quote_container")):
        attr.decompose()

    texto_crudo = node_copy.get_text(separator="\n")
    return aplicar_guillotina_de_hilo_y_footer(texto_crudo)


def evaluar_puntaje_respuesta_oficial(texto: str) -> int:
    """Evalúa la probabilidad de que un texto sea la RESOLUCIÓN FINAL DE GLOBAL66 a la queja:

    - Prioriza firmas institucionales de Global66 y lenguaje regulatorio (SFC/DCF).
    - Evalúa la presencia de la decisión tomada (favorable/abono/investigación).
    - Penaliza expresiones de reclamos o réplicas del consumidor.
    """
    if not texto or len(texto.strip()) < 10:
        return -100

    texto_lower = texto.lower()
    score = 0

    # 1. SUPER-BONUS IDENTIDAD GLOBAL66 / FIRMA OFICIAL DE QUEJAS (+50 Pts)
    identificadores_global = [
        "global66", "global 66", "@global66.com", "respuesta@global66",
        "atención al consumidor financiero", "equipo de reclamaciones",
        "defensor del consumidor financiero"
    ]
    for id_g in identificadores_global:
        if id_g in texto_lower:
            score += 50

    # 2. CONTEXTO REGULATORIO Y RESOLUCIÓN DE QUEJA (+10 Pts c/u)
    jerga_queja_global = [
        "nos complace informarle", "hemos finalizado", "auditoría",
        "caso de reclamación", "caso #", "favorable", "desestimado",
        "se ha abonado", "reembolso", "devolución", "solicitud de manera definitiva",
        "escalado a un equipo especialista", "se encuentra activamente trabajando",
        "damos respuesta", "en atención a su"
    ]
    for jq in jerga_queja_global:
        if jq in texto_lower:
            score += 10

    # 3. ESTRUCTURA Y SALUDO FORMAL (+5 Pts c/u)
    estructuras_formales = ["estimado", "estimada", "hola ", "atentamente", "cordialmente"]
    for ef in estructuras_formales:
        if ef in texto_lower:
            score += 5

    # 4. PENALIZACIÓN POR TEXTOS DEL CONSUMIDOR / RECLAMO INICIAL (-20 Pts c/u)
    expresiones_cliente = [
        "espero solución", "ya verifiqué", "de acuerdo", "muchas gracias por",
        "adjunto el soporte", "mi cuenta", "me cobraron", "sigo a la espera",
        "necesito respuesta", "solicito devolución"
    ]
    for ec in expresiones_cliente:
        if ec in texto_lower:
            score -= 20

    return score


def extraer_texto_limpio_de_html(html_str: str) -> str:
    """Recibe un HTML o texto plano y retorna el texto limpio formateado."""
    if not html_str or not html_str.strip():
        return ""

    raw_html = html.unescape(html_str)

    if not any(
        tag in raw_html.lower() for tag in ("<html", "<p", "<div", "<br", "<span")
    ):
        texto_base = raw_html.strip()
    else:
        parser = HTMLToPlainTextParser()
        parser.feed(raw_html)
        texto_base = parser.get_data()

    return aplicar_guillotina_de_hilo_y_footer(texto_base)


def limpiar_texto_para_campo_pdf(html_str: str) -> str:
    """Extrae ÚNICAMENTE la respuesta oficial de fondo emitida por Global66 para el PDF de la SFC.

    Garantiza que el texto seleccionado sea la resolución oficial de la entidad,
    descartando réplicas del cliente o historiales de correo.
    """
    if not html_str or not html_str.strip():
        return ""

    raw_html = html.unescape(html_str)

    if not any(
        tag in raw_html.lower()
        for tag in ("<html", "<p", "<div", "<br", "<span", "<table", "<blockquote")
    ):
        return aplicar_guillotina_de_hilo_y_footer(raw_html)

    soup = BeautifulSoup(raw_html, "html.parser")
    cita_hilo = soup.find("blockquote")

    if not cita_hilo:
        return extraer_y_limpiar_dom(soup)

    # CANDIDATO A: Texto Fuera de la Cita (Parte Superior)
    soup_afuera = BeautifulSoup(str(soup), "html.parser")
    for bq in soup_afuera.find_all("blockquote"):
        bq.decompose()
    cand_fuera = extraer_y_limpiar_dom(soup_afuera)

    # CANDIDATO B: Texto Dentro de la Cita Principal (Parte Inferior, sin citas anidadas)
    cita_copy = BeautifulSoup(str(cita_hilo), "html.parser")
    for inner_bq in cita_copy.find_all("blockquote"):
        inner_bq.decompose()
    cand_dentro = extraer_y_limpiar_dom(cita_copy)

    # Evaluación focalizada en la Respuesta Final de Global66
    score_fuera = evaluar_puntaje_respuesta_oficial(cand_fuera)
    score_dentro = evaluar_puntaje_respuesta_oficial(cand_dentro)

    # Retorna el bloque que corresponde a la Respuesta de Global66
    return cand_dentro if score_dentro > score_fuera else cand_fuera