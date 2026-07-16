# app/utils/pdf_generator.py
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject

def generar_pdf_respuesta_final(
    caso_nombre: str, 
    smart_code: str, 
    texto_crm: str, 
    ruta_salida: str | Path
) -> Path:
    """
    Lee la plantilla PDF interactiva, inyecta los valores correspondientes,
    aplica protección contra escritura a nivel lógico pero libera las banderas
    visuales para permitir que el texto sea completamente copiable y seleccionable.
    """
    ruta_plantilla = Path("app/resources/plantilla_respuesta_final.pdf")
    ruta_output = Path(ruta_salida)

    if not ruta_plantilla.exists():
        raise FileNotFoundError(f"No se encontró la plantilla en: {ruta_plantilla.resolve()}")

    ruta_output.parent.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(ruta_plantilla)
    writer = PdfWriter()
    writer.append(reader)

    datos_formulario = {
        "caso_nombre": caso_nombre,
        "smart_code": smart_code,
        "mensaje_cuerpo": texto_crm
    }

    # Inyectamos la información en el formulario
    writer.update_page_form_field_values(
        writer.pages[0], 
        datos_formulario
    )

    # 🎯 CONFIGURACIÓN DE SELECCIONABILIDAD Y SEGURIDAD:
    
    # 1. Bloqueo en el Catálogo Global de Campos
    if "/AcroForm" in writer._root_object:
        acro = writer._root_object["/AcroForm"].get_object()
        if "/Fields" in acro:
            for field_ref in acro["/Fields"]:
                field_obj = field_ref.get_object()
                
                # Forzamos No-Editable (/Ff = 1) en el nodo raíz del campo
                if "/T" in field_obj:
                    f_flags = field_obj.get("/Ff", 0)
                    field_obj[NameObject("/Ff")] = NumberObject(f_flags | 1)
                
                # Si tiene hijos lógicos, los protegemos también
                if "/Kids" in field_obj:
                    for kid_ref in field_obj["/Kids"]:
                        k = kid_ref.get_object()
                        f_flags = k.get("/Ff", 0)
                        k[NameObject("/Ff")] = NumberObject(f_flags | 1)

    # 2. Configuración en las Anotaciones Visuales de la Página (Aquí ocurre la magia)
    if "/Annots" in writer.pages[0]:
        for annot in writer.pages[0]["/Annots"]:
            obj = annot.get_object()
            
            # Verificamos si es un Widget de formulario
            if obj.get("/Subtype") == "/Widget":
                # A) Aseguramos que el campo lógico adjunto sea No-Editable
                if "/Parent" in obj:
                    parent = obj["/Parent"].get_object()
                    p_flags = parent.get("/Ff", 0)
                    parent[NameObject("/Ff")] = NumberObject(p_flags | 1)
                else:
                    obj_flags = obj.get("/Ff", 0)
                    obj[NameObject("/Ff")] = NumberObject(obj_flags | 1)

                obj[NameObject("/F")] = NumberObject(4)

    # Escribir el PDF optimizado
    with open(ruta_output, "wb") as f_out:
        writer.write(f_out)

    return ruta_output