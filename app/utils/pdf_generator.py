# app/utils/pdf_generator.py
import textwrap
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject


def ajustar_ancho_texto(texto: str, max_caracteres_por_linea: int = 80) -> str:
    """
    Aplica word-wrapping automático a cada párrafo del texto para evitar 
    que las líneas largas se salgan de los márgenes del PDF.
    """
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


def generar_pdf_respuesta_final(
    caso_nombre: str, 
    smart_code: str, 
    texto_crm: str, 
    ruta_salida: str | Path
) -> Path:
    """
    Lee la plantilla PDF interactiva, ajusta el ancho de línea del texto,
    inyecta los valores correspondientes y aplica protección contra escritura.
    """
    # 🎯 Resolver la ruta de forma absoluta respecto al archivo actual
    ruta_plantilla = Path(__file__).resolve().parent.parent / "resources" / "plantilla_respuesta_final.pdf"
    ruta_output = Path(ruta_salida)

    if not ruta_plantilla.exists():
        raise FileNotFoundError(f"No se encontró la plantilla en: {ruta_plantilla.resolve()}")

    ruta_output.parent.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(ruta_plantilla)
    writer = PdfWriter()
    writer.append(reader)

    texto_ajustado = ajustar_ancho_texto(texto_crm, max_caracteres_por_linea=80)

    datos_formulario = {
        "caso_nombre": f"Caso - {caso_nombre}",
        "smart_code": smart_code,
        "mensaje_cuerpo": texto_ajustado
    }

    writer.update_page_form_field_values(
        writer.pages[0], 
        datos_formulario
    )

    # Configuración de seleccionabilidad y seguridad lógica
    if "/AcroForm" in writer._root_object:
        acro = writer._root_object["/AcroForm"].get_object()
        if "/Fields" in acro:
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

    if "/Annots" in writer.pages[0]:
        for annot in writer.pages[0]["/Annots"]:
            obj = annot.get_object()
            if obj.get("/Subtype") == "/Widget":
                if "/Parent" in obj:
                    parent = obj["/Parent"].get_object()
                    p_flags = parent.get("/Ff", 0)
                    parent[NameObject("/Ff")] = NumberObject(p_flags | 1)
                else:
                    obj_flags = obj.get("/Ff", 0)
                    obj[NameObject("/Ff")] = NumberObject(obj_flags | 1)

                obj[NameObject("/F")] = NumberObject(4)

    with open(ruta_output, "wb") as f_out:
        writer.write(f_out)

    return ruta_output