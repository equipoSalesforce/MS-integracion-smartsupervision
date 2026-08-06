import asyncio
import json
from pathlib import Path
import sys
import os
from dotenv import load_dotenv

root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))

# Cargar variables de entorno locales
load_dotenv()
load_dotenv("infrastructure/.env.test")

from app.core.mapping import SfcSalesforceMapper


async def testear_sincronizacion_sheets():
    print("=" * 85)
    print("🧪 INICIANDO PRUEBA LECTURA DE CATÁLOGOS Y MAPEOS DESDE GOOGLE SHEETS")
    print("=" * 85)

    # 1. Resetear memoria RAM para forzar lectura fresca
    SfcSalesforceMapper.CATALOGOS = {}
    SfcSalesforceMapper.ULTIMA_ACTUALIZACION = 0

    # 2. Ejecutar sincronización
    await SfcSalesforceMapper.obtener_catalogos_y_mapeos()

    catalogos = SfcSalesforceMapper.CATALOGOS
    m1_map = SfcSalesforceMapper.MAPPING_MOMENTO_1_SFC_TO_CRM
    m4_map = SfcSalesforceMapper.MAPPING_MOMENTO_4_SFC_TO_CRM

    print("\n📋 1. VERIFICACIÓN DE MAPEO DE CAMPOS (Pestaña: Mapeo_Campos)")
    print("-" * 60)
    print(f"• Campos en Momento 1 (SFC -> CRM): {len(m1_map)}")
    print(f"• Campos en Momento 4 (SFC -> CRM): {len(m4_map)}")
    print("\nEjemplo Mapeo Momento 1:")
    for sfc_f, crm_f in list(m1_map.items())[:5]:
        print(f"   [{sfc_f}] -> {crm_f}")

    print("\n📚 2. VERIFICACIÓN DE CATÁLOGOS DE VALORES")
    print("-" * 60)
    print(f"• Total catálogos/pestañas leídas: {len(catalogos)}")
    print(f"• Listado de pestañas: {list(catalogos.keys())}\n")

    # Muestreo de catálogos clave
    cat_claves = ["genero", "tipo_id", "canal", "tipo_fraude", "macro_motivo", "producto"]

    for cat in cat_claves:
        if cat in catalogos:
            items = catalogos[cat]
            print(f"✅ Catálogo '{cat}' ({len(items)} ítems leídos):")
            # Mostrar los primeros 3 ítems
            for code, val in list(items.items())[:3]:
                print(f"      Código SFC [{code}]  -->  Valor CRM '{val}'")
        else:
            print(f"❌ ATENCIÓN: La pestaña o catálogo '{cat}' no fue encontrada en Google Sheets.")

    print("\n" + "=" * 85)
    print("🏁 PRUEBA FINALIZADA (Sin efectos secundarios ni envíos a APIs externas)")
    print("=" * 85)


if __name__ == "__main__":
    asyncio.run(testear_sincronizacion_sheets())