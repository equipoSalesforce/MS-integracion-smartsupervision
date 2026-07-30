import os
import random
import re
from pathlib import Path
from playwright.sync_api import Playwright, sync_playwright

# ======================================================================
# ⚙️ CONFIGURACIÓN GENERAL
# ======================================================================
TOTAL_QUEJAS = 120  # Número de quejas a generar
HEADLESS = (
    True  # False = Muestra el navegador / True = Corre invisible en fondo
)
BASE_DIR = Path(__file__).resolve().parent
ARCHIVO_PDF = str(BASE_DIR / "archivo" / "soporte_traza_1.pdf")

# 📋 Catálogo de Canales para rotar y garantizar ternas únicas (Motivo + Producto + Canal)
LISTA_CANALES = [
    "Aplicaciones móviles",
    "Internet",
    "Centro de atención telefónica",
    "Oficinas",
    "Asistente virtual",
]

# 🧠 Memoria de combinaciones ya usadas en la ejecución actual para evitar colisiones
COMBINACIONES_USADAS = set()


def asegurar_archivo_pdf(nombre_archivo: str):
    """Crea un archivo PDF ficticio si no existe en la carpeta actual."""
    if not os.path.exists(nombre_archivo):
        with open(nombre_archivo, "wb") as f:
            f.write(
                b"%PDF-1.4 %...\n1 0 obj << /Type /Catalog >> endobj\ntrailer <<"
                b" /Root 1 0 R >>\n%%EOF"
            )
        print(f"📄 Se creó el archivo de soporte de prueba: {nombre_archivo}")


def run(playwright: Playwright) -> None:
    asegurar_archivo_pdf(ARCHIVO_PDF)

    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context()
    page = context.new_page()

    # ------------------------------------------------------------------
    # 🔑 1. INICIO DE SESIÓN (Una sola vez fuera del bucle)
    # ------------------------------------------------------------------
    print("🔐 Iniciando sesión en SFC QA...")
    page.goto("https://qasmart.superfinanciera.gov.co/login")

    page.get_by_role("textbox", name="Ingrese su correo electrónico").fill(
        "juan.camargo@global66.com"
    )
    page.get_by_role("textbox", name="Ingrese su contraseña").fill(
        "Prueba2026-"
    )
    page.get_by_role("button", name="INICIAR SESIÓN").click()

    boton_queja = page.get_by_role(
        "link",
        name=(
            "Presentar una queja Suministre la información relacionada con su"
            " inconformidad"
        ),
    )
    boton_queja.wait_for(state="visible", timeout=15000)

    print("✅ Sesión iniciada con éxito.\n")

    # ------------------------------------------------------------------
    # 🔄 2. BUCLE MASIVO DE CREACIÓN DE QUEJAS
    # ------------------------------------------------------------------
    for i in range(1, TOTAL_QUEJAS + 1):
        print(f"🚀 [{i}/{TOTAL_QUEJAS}] Diligenciando formulario de queja...")

        try:
            # 1. Ir a la opción de presentar queja
            page.get_by_role(
                "link",
                name=(
                    "Presentar una queja Suministre la información relacionada"
                    " con su inconformidad"
                ),
            ).get_by_label("Diligenciar formulario para").click()

            # 2. Autorizaciones
            page.get_by_role(
                "radio",
                name="¿Autorizo el tratamiento de datos personales? - Sí",
            ).check()
            page.get_by_role(
                "radio",
                name="¿Autorizo el tratamiento de datos sensibles? - Sí",
            ).check()
            page.get_by_role("button", name="ACEPTAR").click()

            # 🛑 ESPERA CLAVE: Esperar a que el modal termine de desvanecerse
            page.wait_for_timeout(1000)

            # 3. Variables dinámicas y Rotación de Canal
            id_ref = random.randint(100000, 999999)
            sexo_random = random.choice(["Masculino", "Femenino"])

            # Rotar el canal secuencialmente para variar la combinación (Motivo + Producto + Canal)
            canal_actual = LISTA_CANALES[(i - 1) % len(LISTA_CANALES)]

            # 4. Formulario - Datos básicos
            page.get_by_label("LGTBIQ+", exact=True).get_by_text("No").click()

            # 🎯 CONDICIÓN ESPECIAL
            page.get_by_role(
                "radio", name="¿Tiene alguna condición especial? - Sí"
            ).check()

            cual_select = page.get_by_label("¿Cuál?")
            cual_select.wait_for(state="visible", timeout=5000)
            cual_select.click(force=True)

            opcion_otra = page.get_by_role("option", name="Otra")
            opcion_otra.wait_for(state="visible", timeout=5000)
            opcion_otra.click()

            # 🎯 SEXO
            sexo_select = page.get_by_label("Sexo")
            sexo_select.wait_for(state="visible", timeout=5000)
            sexo_select.click(force=True)

            opcion_sexo = page.get_by_role("option", name=sexo_random)
            opcion_sexo.wait_for(state="visible", timeout=5000)
            opcion_sexo.click()

            # 5. Ubicación (Campos Dependientes)
            page.get_by_role(
                "radio",
                name=(
                    "¿La inconformidad que motiva su queja ocurrió en el"
                    " exterior? - No"
                ),
            ).check()

            # --- DEPARTAMENTO ---
            dept_select = page.get_by_label("Seleccione un departamento")
            dept_select.wait_for(state="visible", timeout=5000)
            dept_select.click(force=True)

            opcion_dept = page.get_by_role("option", name="BOGOTÁ, D.C.")
            opcion_dept.wait_for(state="visible", timeout=5000)
            opcion_dept.click()

            page.wait_for_timeout(1000)

            # --- MUNICIPIO ---
            mun_select = page.get_by_label("Seleccione un municipio")
            mun_select.wait_for(state="visible", timeout=5000)
            mun_select.click(force=True)

            opcion_mun = page.get_by_role("option", name="BOGOTÁ, D.C.")
            opcion_mun.wait_for(state="visible", timeout=5000)
            opcion_mun.click()

            page.wait_for_timeout(500)

            # 6. Entidad (Global Colombia)
            entidad_input = page.get_by_label(re.compile(r"entidad tuvo", re.I))
            entidad_input.wait_for(state="visible", timeout=5000)
            entidad_input.click(force=True)

            search_entidad = page.locator(
                ".uppercase.ant-select.ant-select-open"
                " .ant-select-search__field"
            )
            search_entidad.wait_for(state="visible", timeout=5000)
            search_entidad.fill("global")

            opcion_entidad = page.get_by_role(
                "option", name="GLOBAL COLOMBIA 81 SA"
            )
            opcion_entidad.wait_for(state="visible", timeout=5000)
            opcion_entidad.click()

            print("    - Esperando carga de motivos para Global Colombia...")
            page.wait_for_timeout(1500)

            # 7. Motivo (Inteligente Anti-Duplicados) y Producto (Fijo)

            # --- A. MOTIVO ---
            motivo_input = page.get_by_label(re.compile(r"motivo de su", re.I))
            motivo_input.wait_for(state="visible", timeout=7000)
            motivo_input.click(force=True)

            menu_motivo = page.locator("#reason-listbox")
            menu_motivo.wait_for(state="visible", timeout=5000)

            opciones_validas = menu_motivo.locator(
                ".ant-select-dropdown-menu-item:not(.ant-select-dropdown-menu-item-disabled)"
            ).filter(has_not_text=re.compile(r"Seleccionar", re.I))

            opciones_validas.first.wait_for(state="visible", timeout=5000)
            lista_motivos = opciones_validas.all()

            # 🧠 Selección de un motivo que no haya sido emparejado con este canal antes
            motivo_elegido = None
            for opt in random.sample(lista_motivos, len(lista_motivos)):
                texto_motivo = opt.inner_text().strip()
                pair_key = (texto_motivo, canal_actual)
                if pair_key not in COMBINACIONES_USADAS:
                    motivo_elegido = opt
                    COMBINACIONES_USADAS.add(pair_key)
                    break

            # Fallback en caso de haber agotado todas las combinaciones locales
            if not motivo_elegido:
                motivo_elegido = random.choice(lista_motivos)
                texto_motivo = motivo_elegido.inner_text().strip()
                COMBINACIONES_USADAS.add((texto_motivo, canal_actual))

            motivo_elegido.click()
            page.wait_for_timeout(1000)

            # --- B. PRODUCTO (Fijo en Depósitos) ---
            producto_input = page.get_by_label(
                re.compile(r"inconformidad está", re.I)
            )
            producto_input.wait_for(state="visible", timeout=5000)
            producto_input.click(force=True)

            opcion_producto = page.locator(
                ".ant-select-dropdown-menu-item"
            ).filter(has_text=re.compile(r"Depósitos", re.I)).first
            opcion_producto.wait_for(state="visible", timeout=5000)
            opcion_producto.click()

            page.wait_for_timeout(500)

            # 8. Detalle del producto
            page.get_by_role(
                "textbox", name="Si lo desea amplíe el detalle"
            ).fill(f"Cuenta perfil Ref-{id_ref}")

            # 9. Canal (Usando la rotación de canal actual)
            page.get_by_label("¿A través de que canal se").click(force=True)
            opcion_canal = page.get_by_role(
                "option", name=re.compile(re.escape(canal_actual[:10]), re.I)
            ).first
            opcion_canal.wait_for(state="visible", timeout=5000)
            opcion_canal.click()

            # 10. Relato / Descripción
            descripcion = (
                f"Este es el test numero {i} (ID Interno: {id_ref}) de creación"
                " de quejas en el servidor de la SFC mediante automatización"
                " Playwright."
            )
            page.get_by_role(
                "textbox", name="Haga un relato cronológico de"
            ).fill(descripcion)

            # 11. Adjuntar archivo de soporte
            page.get_by_text("SELECCIONE SUS ARCHIVOS").click()
            page.get_by_text("SELECCIONE SUS ARCHIVOS").set_input_files(
                ARCHIVO_PDF
            )

            # 12. Enviar formulario
            page.get_by_role(
                "radio", name="¿Quiere ingresar cuantía? - No"
            ).check()
            page.get_by_role("button", name="Registrar queja").click()

            # 13. Confirmación
            page.get_by_role("button", name="ACEPTAR").click()

            # Pop-up opcional "Ahora no"
            try:
                page.get_by_role("button", name="Ahora no").click(timeout=3000)
            except Exception:
                pass

            print(
                f"  🟢 Queja #{i} registrada exitosamente (Canal: {canal_actual},"
                f" Ref: {id_ref}).\n"
            )

        except Exception as e:
            print(f"  🔴 Error al registrar la queja #{i}: {e}\n")
            try:
                page.goto(
                    "https://qasmart.superfinanciera.gov.co/dashboard"
                )  # Recargar si falla
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 🔚 FINALIZACIÓN DE PROCESO
    # ------------------------------------------------------------------
    print("🎉 ¡Proceso completado con éxito!")
    context.close()
    browser.close()


if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)