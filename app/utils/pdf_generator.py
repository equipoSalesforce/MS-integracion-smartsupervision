# app/utils/pdf_generator.py
import io
import textwrap
import logging
from pathlib import Path
from typing import Optional, Union
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject

logger = logging.getLogger(__name__)

def ajustar_ancho_texto(texto: str, max_caracteres_por_linea: int = 90) -> str:
    """Aplica word-wrapping automático a cada párrafo del texto."""
    if not texto:
        return ""

    lineas_formateadas = []
    for linea in texto.split("\n"):
        if len(linea.strip()) > max_caracteres_por_linea:
            linea_envuelta = textwrap.fill(
                linea, 
                width=max_caracteres_por_linea, 
                break_long_words=False,
                replace_whitespace=False
            )
            lineas_formateadas.append(linea_envuelta)
        else:
            lineas_formateadas.append(linea)

    return "\n".join(lineas_formateadas)


def _marcar_campos_formulario_solo_lectura(writer: PdfWriter) -> None:
    if "/AcroForm" not in writer._root_object:
        return
    acro = writer._root_object["/AcroForm"].get_object()
    if "/Fields" not in acro:
        return

    for field_ref in acro["/Fields"]:
        field_obj = field_ref.get_object()
        if "/T" in field_obj:
            f_flags = field_obj.get("/Ff", 0)
            field_obj[NameObject("/Ff")] = NumberObject(f_flags | 1)
        if "/Kids" in field_obj:
            for kid_ref in field_obj["/Kids"]:
                k = kid_ref.get_object()
                f_flags = k.get("/Ff", 0)
                k[NameObject("/Ff")] = NumberObject(f_flags | 1)


def _marcar_widgets_pagina_solo_lectura(page) -> None:
    if "/Annots" not in page:
        return

    for annot in page["/Annots"]:
        obj = annot.get_object()
        if obj.get("/Subtype") != "/Widget":
            continue

        if "/Parent" in obj:
            parent = obj["/Parent"].get_object()
            p_flags = parent.get("/Ff", 0)
            parent[NameObject("/Ff")] = NumberObject(p_flags | 1)
        else:
            obj_flags = obj.get("/Ff", 0)
            obj[NameObject("/Ff")] = NumberObject(obj_flags | 1)

        obj[NameObject("/F")] = NumberObject(4)


def generar_pdf_respuesta_final(
    caso_nombre: str, 
    smart_code: str, 
    texto_crm: str, 
    ruta_salida: Optional[Union[str, Path]] = None
) -> Union[bytes, Path]:
    """
    Lee la plantilla PDF interactiva, ajusta el texto y retorna los bytes en memoria RAM.
    Si se especifica 'ruta_salida', escribe el archivo a disco y retorna Path(ruta_salida).
    """
    ruta_plantilla = Path(__file__).resolve().parent.parent / "resources" / "plantilla_respuesta_final.pdf"

    if not ruta_plantilla.exists():
        raise FileNotFoundError(f"No se encontró la plantilla en: {ruta_plantilla.resolve()}")

    reader = PdfReader(ruta_plantilla)
    writer = PdfWriter()
    writer.append(reader)

    texto_ajustado = ajustar_ancho_texto(texto_crm, max_caracteres_por_linea=80)

    datos_formulario = {
        "caso_nombre": f"Caso - {caso_nombre}",
        "smart_code": smart_code,
        "mensaje_cuerpo": texto_ajustado
    }

    writer.update_page_form_field_values(writer.pages[0], datos_formulario)

    _marcar_campos_formulario_solo_lectura(writer)
    _marcar_widgets_pagina_solo_lectura(writer.pages[0])

    buffer = io.BytesIO()
    writer.write(buffer)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    # Si se pasa una ruta explícita (p. ej. en pruebas unitarias), guardamos en disco y retornamos Path
    if ruta_salida:
        ruta_output = Path(ruta_salida)
        ruta_output.parent.mkdir(parents=True, exist_ok=True)
        with open(ruta_output, "wb") as f_out:
            f_out.write(pdf_bytes)
        return ruta_output

    return pdf_bytes