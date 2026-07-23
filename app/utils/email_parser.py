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
    """Aplica guillotina sobre texto plano para eliminar cabeceras de hilos o avisos legales."""
    if not texto_plano or not texto_plano.strip():
        return ""

    # 1. GUILLOTINA SUPERIOR: Si el texto arranca con "... escribió:", "... wrote:" o "... escreveu:"
    patron_superior = r"\b(?:escribi[óo]|wrote|escreveu):\s*"
    if re.search(patron_superior, texto_plano, flags=re.IGNORECASE):
        texto_plano = re.sub(f"(?is)^.*?{patron_superior}", "", texto_plano)

    # 2. GUILLOTINA INFERIOR: Cortar si encuentra cabeceras de hilos o disclaimers
    patrones_corte_inferior = [
        r"\n\s*El\s+[^\n]*?\b(?:escribi[óo]|wrote|escreveu):.*",
        r"\n\s*On\s+[^\n]*?\bwrote:.*",
        r"\n\s*Em\s+[^\n]*?\bescreveu:.*",
        r"\n\s*(?:De|From|Para|To|Enviado el|Sent|Data):\s+[^\n]+",
        r"\n\s*Subject:\s+[^\n]+",
        r"\n\s*Asunto:\s+[^\n]+",
        r"\n\s*AVISO DE CONFIDENCIALIDAD.*",
        r"\n\s*CONFIDENTIALITY NOTICE.*",
        r"\n\s*En cumplimiento del literal a.*",
        r"¿Tienes dudas\?.*",
        r"Centro de ayuda.*",
        r"Help center.*",
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
    """Elimina imágenes, scripts y clases de citas del DOM antes de extraer texto plano."""
    node_copy = BeautifulSoup(str(soup_node), "html.parser")

    for el in node_copy.find_all(["img", "script", "style"]):
        el.decompose()

    selectores_hilos = [
        ".gmail_quote", ".x_gmail_quote", ".gmail_attr", ".gmail_signature",
        "#divRplyFwdMsg", "#appendonsend", ".yahoo_quoted", ".AppleMailSignature",
        "[id*='isGmailWithSsl']"
    ]
    for sel in selectores_hilos:
        for match in node_copy.select(sel):
            match.decompose()

    for hr in node_copy.find_all("hr"):
        for sibling in list(hr.next_siblings):
            if hasattr(sibling, "decompose"):
                sibling.decompose()
        hr.decompose()

    texto_crudo = node_copy.get_text(separator="\n")
    return aplicar_guillotina_de_hilo_y_footer(texto_crudo)


def limpiar_cita_interna_dom(cita_dom) -> str:
    """Procesa un blockquote eliminando cualquier cita anidada o encabezado interno."""
    cita_copy = BeautifulSoup(str(cita_dom), "html.parser")

    for inner_bq in cita_copy.find_all("blockquote"):
        inner_bq.decompose()

    selectores_hilos = [
        ".gmail_quote", ".x_gmail_quote", ".gmail_attr", ".gmail_signature",
        "#divRplyFwdMsg", "#appendonsend", ".yahoo_quoted", ".AppleMailSignature"
    ]
    for sel in selectores_hilos:
        for match in cita_copy.select(sel):
            match.decompose()

    texto_crudo = cita_copy.get_text(separator="\n")
    return aplicar_guillotina_de_hilo_y_footer(texto_crudo)


def es_replica_corta_cliente(texto: str) -> bool:
    """Detecta si un bloque corresponde a un acuse corto del cliente (ej: 'Gracias, ya funciona')."""
    if not texto:
        return False
    
    texto_lower = texto.lower()
    frases_cliente = [
        "gracias", "muchas gracias", "ya funciona", "ya pude", "ya quedo",
        "de acuerdo", "perfecto", "resuelto", "thank you", "obrigado"
    ]
    return len(texto) < 200 and any(f in texto_lower for f in frases_cliente)


def extraer_texto_limpio_de_html(html_str: str) -> str:
    """Recibe un HTML o texto plano y retorna el texto limpio formateado."""
    if not html_str or not html_str.strip():
        return ""

    raw_html = html.unescape(html_str)

    if not any(tag in raw_html.lower() for tag in ("<html", "<p", "<div", "<br", "<span")):
        texto_base = raw_html.strip()
    else:
        parser = HTMLToPlainTextParser()
        parser.feed(raw_html)
        texto_base = parser.get_data()

    return aplicar_guillotina_de_hilo_y_footer(texto_base)


def limpiar_texto_para_campo_pdf(html_str: str) -> str:
    """Extrae la ÚLTIMA RESPUESTA ENVIADA POR @GLOBAL66.COM para el PDF de la SFC.

    Garantiza que si el usuario envió un mensaje final de agradecimiento (ej: 'Gracias por solucionar'),
    el parser descienda al hilo (blockquote) y extraiga la resolución oficial de Global66.
    """
    if not html_str or not html_str.strip():
        return ""

    raw_html = html.unescape(html_str)

    # Si es texto plano directo
    if not any(
        tag in raw_html.lower()
        for tag in ("<html", "<p", "<div", "<br", "<span", "<table", "<blockquote")
    ):
        return aplicar_guillotina_de_hilo_y_footer(raw_html)

    soup = BeautifulSoup(raw_html, "html.parser")

    # 1. Candidato Superior (Fuera de blockquotes)
    soup_top = BeautifulSoup(str(soup), "html.parser")
    for bq in soup_top.find_all("blockquote"):
        bq.decompose()
    cand_top = extraer_y_limpiar_dom(soup_top)

    # 2. Candidato en la Cita Anterior (Dentro del primer blockquote)
    cita_hilo = soup.find("blockquote")
    cand_quote = ""
    if cita_hilo:
        cand_quote = limpiar_cita_interna_dom(cita_hilo)

    # 3. CRITERIO DE AUTORÍA @GLOBAL66.COM:
    if cita_hilo and cand_quote:
        top_es_cliente = es_replica_corta_cliente(cand_top)
        quote_es_global = "global66.com" in str(cita_hilo).lower() or "global66" in cand_quote.lower() or "global 66" in cand_quote.lower()

        if top_es_cliente or quote_es_global:
            return cand_quote

    # Por defecto, retornamos el texto superior
    return cand_top if cand_top.strip() else aplicar_guillotina_de_hilo_y_footer(raw_html)