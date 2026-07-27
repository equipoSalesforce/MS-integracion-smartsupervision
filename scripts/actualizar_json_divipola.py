import json
import os
import pandas as pd

# Nombre exacto de tu archivo CSV descargado
NOMBRE_ARCHIVO_CSV = "app/core/resources/DIVIPOLA-_Códigos_municipios_20260727.csv"
RUTA_SALIDA_JSON = "app/core/resources/divipola_sfc_crm.json"


def convertir_csv_a_divipola_json():
    if not os.path.exists(NOMBRE_ARCHIVO_CSV):
        print(f"❌ No se encontró el archivo '{NOMBRE_ARCHIVO_CSV}' en la ruta actual.")
        return

    print(f"🔄 Leyendo el archivo CSV: {NOMBRE_ARCHIVO_CSV}...")
    
    # Cargar CSV asegurando que los códigos se lean como String para no perder ceros a la izquierda
    df = pd.read_csv(NOMBRE_ARCHIVO_CSV, dtype=str)

    departamentos = {}
    municipios = {}

    for _, row in df.iterrows():
        # Formatear Código Departamento a 2 dígitos con ceros a la izquierda (ej. "05")
        cod_depto = str(row["Código Departamento"]).strip().zfill(2)
        nom_depto = str(row["Nombre Departamento"]).strip().title()

        # Formatear Código Municipio a 5 dígitos con ceros a la izquierda (ej. "05001")
        cod_muni = str(row["Código Municipio"]).strip().zfill(5)
        nom_muni = str(row["Nombre Municipio"]).strip().title()

        # Normalización para Bogotá D.C.
        if cod_depto == "11" or "bogota" in nom_depto.lower():
            nom_depto = "Bogotá D.C."
            cod_depto = "11"

        if cod_muni == "11001" or "bogota" in nom_muni.lower():
            nom_muni = "Bogotá D.C."
            cod_muni = "11001"

        departamentos[cod_depto] = nom_depto
        municipios[cod_muni] = nom_muni

    # Construir el JSON ordenado
    json_estructurado = {
        "departamentos": dict(sorted(departamentos.items())),
        "municipios": dict(sorted(municipios.items()))
    }

    # Asegurar que el directorio de salida exista
    os.makedirs(os.path.dirname(RUTA_SALIDA_JSON), exist_ok=True)

    # Escribir archivo JSON en UTF-8
    with open(RUTA_SALIDA_JSON, "w", encoding="utf-8") as f:
        json.dump(json_estructurado, f, ensure_ascii=False, indent=2)

    print(
        f"\n✅ ¡Conversión completada con éxito!\n"
        f"📂 Archivo generado: {RUTA_SALIDA_JSON}\n"
        f"📊 Total Departamentos: {len(departamentos)}\n"
        f"📊 Total Municipios: {len(municipios)}"
    )


if __name__ == "__main__":
    convertir_csv_a_divipola_json()