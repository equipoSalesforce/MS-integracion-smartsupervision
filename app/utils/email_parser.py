# app/utils/email_parser.py
import html
import re
from dataclasses import dataclass
from typing import List, Optional
from bs4 import BeautifulSoup

from app.core.security.sanitizer import sanitizar_html_para_pdf


# ======================================================================
# 1. MODELO DE DATOS
# ======================================================================

@dataclass
class MensajeHilo:
    indice: int             # 0 = Más reciente (arriba), N = Más antiguo (abajo)
    remitente_cabecera: str # Texto del encabezado / remitente detectado
    cuerpo_texto: str       # Mensaje limpio sin encabezado ni metadatos de UI
    es_soporte: bool        # True si pertenece a Global66


# ======================================================================
# 2. CONVERTIDOR DE HTML Y LIMPIADOR DE DOM
# ======================================================================

def _html_a_texto_estructurado(html_str: str) -> str:
    """Convierte HTML a texto plano conservando estructuras de párrafo y saltos de línea."""
    if not html_str or not html_str.strip():
        return ""

    html_sanitizado = sanitizar_html_para_pdf(html_str)
    
    soup = BeautifulSoup(html_sanitizado, "html.parser")

    # Eliminar scripts, estilos, imágenes y etiquetas <head>/<title>
    for el in soup.find_all(["script", "style", "img", "head", "title"]):
        el.decompose()

    # Reemplazar únicamente los tags <br> por saltos de línea reales
    for br in soup.find_all("br"):
        br.replace_with("\n")

    # Agregar salto de línea al final de bloques contenedores
    for block in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "tr"]):
        block.append("\n")

    texto = soup.get_text()
    
    # Normalizar múltiples saltos de línea continuos
    lineas = [linea.strip() for linea in texto.splitlines()]
    return "\n".join(lineas)


# ======================================================================
# 3. LIMPIADOR DE CABECERAS Y METADATOS DE UI DE WEBMAIL
# ======================================================================

def _limpiar_cabeceras_superiores(texto: str) -> str:
    """Elimina metadatos de destinatarios ('para Daniela...'), remitentes ('María <soporte@...>'), títulos e iniciales."""
    if not texto:
        return ""

    lineas = texto.splitlines()
    lineas_limpias = []
    
    for linea in lineas:
        l_strip = linea.strip()
        if not l_strip:
            lineas_limpias.append("")
            continue

        # 🎯 1. Eliminar líneas de destinatario tipo 'para Daniela Rojas Mock · 29 jul 2026, 10:25' o 'to John...'
        if re.match(r"^(?:para|to)\s+.*(?:·|\b\d{1,2}\b)", l_strip, flags=re.IGNORECASE):
            continue
        if "·" in l_strip and re.match(r"^(?:para|to)\s+", l_strip, flags=re.IGNORECASE):
            continue

        # 🎯 2. Eliminar líneas de asunto duplicadas al inicio (ej: 'Re: Respuesta final...')
        if re.match(r"^Re:\s+", l_strip, flags=re.IGNORECASE) and len(lineas_limpias) < 3:
            continue

        # 🎯 3. Eliminar iniciales de avatar de 2 letras al inicio (ej: 'MG', 'GS')
        if re.match(r"^[A-Z]{2}$", l_strip) and len(lineas_limpias) < 5:
            continue

        # 🎯 4. NUEVA: Eliminar cabecera del remitente tipo 'María González <soporte@global66.com>' o '<soporte@global66.com>'
        if re.search(r"<[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}>", l_strip) and len(lineas_limpias) < 5:
            continue
        if re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", l_strip) and len(lineas_limpias) < 5:
            continue

        lineas_limpias.append(linea)

    texto_resultante = "\n".join(lineas_limpias)
    lineas_finales = [l.strip() for l in texto_resultante.splitlines() if l.strip()]
    return "\n\n".join(lineas_finales).strip()


# ======================================================================
# 4. SEGMENTADOR DE HILOS (LOOKAHEAD REGEX SPLITTER)
# ======================================================================

def _segmentar_hilo_en_bloques(texto_completo: str) -> List[str]:
    """
    Divide el texto completo del correo en bloques independientes utilizando Lookahead Regex.
    Conserva la cabecera original en el inicio de cada bloque resultante.
    """
    patron_frontera = (
        r"(?="
        r"\n\s*(?:El|On|Em)\s+[^\n]+?\b(?:escribi[óo]|wrote|escreveu):"
        r"|\n\s*(?:De|From|Enviado el|Sent|Date):\s+[^\n]+"
        r"|\n\s*-{3,}\s*(?:Mensaje original|Original Message)\s*-{3,}"
        r")"
    )

    bloques_raw = re.split(patron_frontera, texto_completo, flags=re.IGNORECASE | re.DOTALL)
    
    return [b for roving in bloques_raw if (b := roving.strip())]


# ======================================================================
# 5. CLASIFICADOR DE AUTORÍA (GLOBAL66 VS CLIENTE)
# ======================================================================

def _clasificar_autor_bloque(bloque_texto: str, es_bloque_superior: bool) -> tuple[bool, str, str]:
    """
    Determina si un bloque del hilo fue escrito por el equipo de Soporte/Global66.
    Retorna: (es_soporte, remitente_cabecera, cuerpo_sin_cabecera)
    """
    lineas = [l for l in bloque_texto.splitlines() if l.strip()]
    if not lineas:
        return False, "", ""

    cabecera = "\n".join(lineas[:4]) # Primeras 4 líneas como candidatos a cabecera
    
    # Patrones que identifican la autoría de Global66
    dominios_soporte = ["@global66.com", "global66.com", "global66", "global 66"]
    
    # 🎯 CASO A: Es una cita previa (ej: 'El mié, 29 jul... Daniela escribió:')
    match_cita = re.search(
        r"(?:El|On|Em)\s+([^\n]+?)\b(?:escribi[óo]|wrote|escreveu):", 
        cabecera, 
        flags=re.IGNORECASE
    )
    
    if match_cita:
        autor_cita = match_cita.group(1).lower()
        es_soporte = any(d in autor_cita for d in dominios_soporte)
        cuerpo = re.sub(
            r"^(?:El|On|Em)\s+[^\n]+?\b(?:escribi[óo]|wrote|escreveu):\s*", 
            "", 
            bloque_texto, 
            flags=re.IGNORECASE | re.DOTALL
        ).strip()
        return es_soporte, autor_cita, cuerpo

    # 🎯 CASO B: Cabecera 'De: / From:'
    match_de = re.search(r"(?:De|From):\s*([^\n]+)", cabecera, flags=re.IGNORECASE)
    if match_de:
        remitente = match_de.group(1).lower()
        es_soporte = any(d in remitente for d in dominios_soporte)
        cuerpo = re.sub(r"^(?:De|From):\s*[^\n]+\s*", "", bloque_texto, flags=re.IGNORECASE).strip()
        return es_soporte, remitente, cuerpo

    # 🎯 CASO C: Bloque Superior (Llegó al inicio sin prefijo 'El ... escribió')
    if es_bloque_superior:
        bloque_lower = bloque_texto.lower()
        es_soporte = any(d in bloque_lower for d in dominios_soporte)
        return es_soporte, "Mensaje Superior Directo", bloque_texto

    return False, "Desconocido", bloque_texto


# ======================================================================
# 6. SANITIZADOR DE FIRMAS Y DISCLAIMERS LEGALES
# ======================================================================

def _limpiar_disclaimers_y_footers(texto: str) -> str:
    """Remueve avisos de confidencialidad, footers automáticos y firmas legales al final del mensaje."""
    patrones_disclaimers = [
        r"\n\s*(?:Este mensaje y sus anexos|AVISO DE CONFIDENCIALIDAD|CONFIDENTIALITY NOTICE|En cumplimiento del).*$",
        r"\n\s*¿Tienes dudas\?.*$",
        r"\n\s*(?:Centro de ayuda|Help center).*$",
        r"\n\s*You received this message because.*$",
        r"\n\s*thread::.*$"
    ]
    
    patron_unificado = "|".join(f"(?:{p})" for p in patrones_disclaimers)
    partes = re.split(patron_unificado, texto, maxsplit=1, flags=re.IGNORECASE | re.DOTALL)
    
    lineas = [l.strip() for l in partes[0].splitlines() if l.strip()]
    return "\n\n".join(lineas).strip()


# ======================================================================
# 7. ORQUESTADOR PRINCIPAL DEL PARSER
# ======================================================================

def extraer_texto_limpio_de_html(html_str: str) -> str:
    """
    Parseador estructurado de hilos de correo.
    Segmenta la conversación, clasifica la autoría y extrae LA ÚLTIMA RESPUESTA OFICIAL
    emitida por el soporte de Global66.
    """
    if not html_str or not html_str.strip():
        return ""

    # 1. Convertir a texto limpio estructurado
    texto_estructurado = _html_a_texto_estructurado(html_str)

    # 2. Segmentar el hilo en bloques cronológicos (0 = Más reciente)
    bloques_raw = _segmentar_hilo_en_bloques(texto_estructurado)
    
    if not bloques_raw:
        texto_limpio = _limpiar_cabeceras_superiores(texto_estructurado)
        return _limpiar_disclaimers_y_footers(texto_limpio)

    # 3. Construir lista de objetos 'MensajeHilo'
    mensajes_hilo: List[MensajeHilo] = []
    
    for idx, bloque_raw in enumerate(bloques_raw):
        es_soporte, remitente, cuerpo = _clasificar_autor_bloque(
            bloque_texto=bloque_raw, 
            es_bloque_superior=(idx == 0)
        )
        
        cuerpo_limpio = _limpiar_cabeceras_superiores(cuerpo)
        cuerpo_limpio = _limpiar_disclaimers_y_footers(cuerpo_limpio)
        
        mensajes_hilo.append(
            MensajeHilo(
                indice=idx,
                remitente_cabecera=remitente,
                cuerpo_texto=cuerpo_limpio,
                es_soporte=es_soporte
            )
        )

    # 4. Seleccionar la respuesta de Global66 más reciente (menor índice)
    for msg in mensajes_hilo:
        if msg.es_soporte and msg.cuerpo_texto.strip():
            return msg.cuerpo_texto

    # Fallback: Si no se logró clasificar explícitamente, retornar el primer bloque limpio
    if mensajes_hilo:
        return mensajes_hilo[0].cuerpo_texto
    
    texto_limpio = _limpiar_cabeceras_superiores(texto_estructurado)
    return _limpiar_disclaimers_y_footers(texto_limpio)


def limpiar_texto_para_campo_pdf(html_str: str) -> str:
    """Fachada pública para generar la respuesta oficial en el PDF de la SFC."""
    return extraer_texto_limpio_de_html(html_str)