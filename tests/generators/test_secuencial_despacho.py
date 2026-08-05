import os
import json
import random
import string
import time
from typing import Dict, Any, List, Optional
import httpx
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# ==============================================================================
# ⚙️ CONFIGURACIÓN DEL TEST SECUENCIAL
# ==============================================================================
TARGET_URL = os.getenv("TEST_TARGET_URL", "http://localhost:8000/api/v1/quejas/sync/despacho")
API_KEY = os.getenv("CRM_API_KEY", "g66_sk_test_super_secreto_12345")
OUTPUT_LOG_FILE = "test_secuencial_results.json"

# 🎯 PARÁMETROS CONFIGURABLES
TOTAL_PETICIONES = 80   # Cantidad total de peticiones
COOLDOWN_SECONDS = 2.0  # Pausa entre peticiones para no agotar la cuota de la SFC

# 🎯 CONFIGURACIÓN ÚNICA DE S3 / MINIO
DEFAULT_DIRECTORIO_S3 = "caso/STRESS_TEST_DEFAULT/"
FIXED_FILE_NAME = "soporte_prueba.pdf"
FIXED_S3_KEY = f"{DEFAULT_DIRECTORIO_S3}{FIXED_FILE_NAME}"
FIXED_BUCKET = "global66-sfc-bucket-local"

# 🎯 Total de escenarios base ajustado (0 al 20 = 21 escenarios)
NUM_ESCENARIOS_BASE = 21

# ==============================================================================
# 📋 CATÁLOGOS Y SEMILLAS
# ==============================================================================
DOCUMENT_TYPES = ["CC", "CE", "RUT", "DNI", "PASS", "PEP", "PPT"]
PRODUCTS = ["Cuenta perfil", "Wallet", "Exchange", "Transactions", "P2P", "Tarjeta Digital", "Tarjeta Fisica"]
CHANNELS = [
    "Aplicaciones móviles", "Centro de atención telefónica (Call center/Contact center)",
    "Asistente virtual", "Internet", "POS no propios"
]

FRAUD_CATEGORY = "Transacción no reconocida"
NON_FRAUD_CATEGORIES = [
    "Remesas", 
    "Dificultad en el acceso a la información",
    "Incumplimiento de los términos del contrato", 
    "Inconformidades relacionadas con el proceso de cobranza",
    "Cobro por operaciones fallidas en cajeros electrónicos", 
    "Inconsistencia en el cobro de comisiones - Descuentos injustificados"
]

PUNTOS_RECEPCION = ["Web", "WhatsApp", "Email", "Manual"]

UNICODE_EMOJI_SEEDS = [
    "Renée-Ángel 🦙 ñandú", "Иван 🤖 Смирнов", "佐藤 🐉 健", 
    "María 🚀 Ñuñez", "Jöhn 💥 Døe", "<script>alert('XSS')</script> 🥷"
]

BAD_EMAIL_PATTERNS = [
    "correo_sin_arroba_ni_dominio.com",
    "usuario@@dosarrobas.com",
    "usuario @espacios.com",
    "sin_tld@dominio",
    "@sinusuario.com"
]

SQL_XSS_INJECTIONS = [
    "DROP TABLE quejas; --",
    "' OR '1'='1",
    "<svg/onload=alert('xss')>",
    "{\"hack\": true}",
    "../../../../etc/passwd"
]

# ==============================================================================
# 🎲 HELPER FUNCTIONS
# ==============================================================================
def _generar_ids(secuencia: int) -> tuple[str, str]:
    """Genera Case_id e id_number__c únicos por prueba."""
    timestamp = str(int(time.time()))[-6:]
    case_id = f"SEQ_{timestamp}_{secuencia:04d}"
    doc_number = f"1000{secuencia:04d}{random.randint(100, 999)}"
    return case_id, doc_number


def _obtener_anexos_sin_fraude() -> Dict[str, Any]:
    """Decide al azar si incluir directorio_s3 o dejar vacío."""
    if random.choice([True, False]):
        return {
            "smart_anexo_queja__c": True,
            "directorio_s3": DEFAULT_DIRECTORIO_S3
        }
    return {
        "smart_anexo_queja__c": False,
        "archivos_s3": []
    }


def _obtener_cuerpo_respuesta_azar(case_id: str) -> Optional[str]:
    """Decide al azar si incluir respuesta final en cierres."""
    if random.choice([True, False]):
        return (
            f"<html><body>"
            f"<h2>Respuesta Final Oficial</h2>"
            f"<p>Estimado Cliente, confirmamos la revisión y cierre formal de su caso <strong>{case_id}</strong>.</p>"
            f"</body></html>"
        )
    return None


def _base_mandatory_payload(case_id: str, doc_number: str, secuencia: int) -> Dict[str, Any]:
    """Genera los 11 campos base obligatorios comunes."""
    return {
        "Case_id": case_id,
        "SuppliedName": f"Usuario Prueba Secuencial {secuencia}",
        "SC_id_type__c": random.choice(DOCUMENT_TYPES),
        "id_number__c": doc_number,
        "tipo_de_persona__c": "B2C",
        "direccion__c": f"Calle {secuencia} # {random.randint(10, 90)}-{random.randint(10, 99)}",
        "punto_recepcion": random.choice(PUNTOS_RECEPCION),
        "Description": f"Descripción detallada del caso secuencial {secuencia} para validación de integración.",
        "smart_escalamiento_DCF__c": "No",
        "Product__c": random.choice(PRODUCTS),
        "Categorias_COL__c": random.choice(NON_FRAUD_CATEGORIES),
    }

# ==============================================================================
# 🧪 GENERADOR DINÁMICO DE CASOS (ESTÁNDAR + FUZZING EN BORDES)
# ==============================================================================
def generar_caso(secuencia: int, tipo_escenario: int) -> Dict[str, Any]:
    cid, doc = _generar_ids(secuencia)
    payload = _base_mandatory_payload(cid, doc, secuencia)

    # --------------------------------------------------------------------------
    # 🟢 CASOS ESTÁNDAR VÁLIDOS (0 a 7)
    # --------------------------------------------------------------------------
    if tipo_escenario == 0:
        nombre = "M2 Radicación Inicial B2C (Con Opcionales)"
        payload.update({
            "sc_genero__c": random.choice(["Masculino", "Femenino", "Trans", "No binario"]),
            "sc_LGBTIQ__c": random.choice(["Si", "No"]),
            "sc_Condicion_especial__c": "No aplica",
            "SuppliedPhone": f"31{random.randint(0,9)}{random.randint(1000000, 9999999)}",
            "SuppliedEmail": f"usuario_{secuencia}_{random.randint(100,999)}@test.com",
            "Departamento__c": "Bogotá D.C.",
            "SC_municipio__c": "Bogotá D.C.",
            "canal__c": random.choice(CHANNELS),
            "Instancia_de_recepcion__c": "Entidad vigilada",
            "admision_col__c": "No Aplica",
            "Tutela__c": "No",
            "Ente_de_control__c": "Otros",
        })
        payload.update(_obtener_anexos_sin_fraude())
        espera_exito = True

    elif tipo_escenario == 1:
        nombre = "M3 Trámite B2B Corporativo (NIT)"
        payload.update({
            "tipo_de_persona__c": "B2B",
            "SC_id_type__c": "NIT",
            "SuppliedName": f"Empresa Corporativa {secuencia}-{random.randint(100, 999)} SAS",
            "Departamento__c": "Antioquia",
            "SC_municipio__c": "Medellín",
        })
        payload.update(_obtener_anexos_sin_fraude())
        espera_exito = True

    elif tipo_escenario == 2:
        nombre = "M3 Reporte de Fraude (Uso de directorio_s3)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        monto = float(random.randint(100000, 5000000))
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": random.choice(["Phishing", "Suplantación de identidad", "Smishing"]),
            "card_amount__c": monto,
            "Total_Devuelto_por_Desconocimiento__c": monto if random.choice([True, False]) else 0.0,
            "smart_anexo_queja__c": True,
            "directorio_s3": DEFAULT_DIRECTORIO_S3
        })
        espera_exito = True

    elif tipo_escenario == 3:
        nombre = "M3 Reporte de Fraude (Uso de archivos_s3 fijado)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        payload.update({
            "tipo_fraude__c": "Interno",
            "modalidad_fraude__c": "Sim Swapping",
            "card_amount__c": float(random.randint(200000, 3000000)),
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "smart_anexo_queja__c": True,
            "archivos_s3": [{
                "nombre_archivo": FIXED_FILE_NAME,
                "s3_key": FIXED_S3_KEY,
                "bucket": FIXED_BUCKET
            }]
        })
        espera_exito = True

    elif tipo_escenario == 4:
        nombre = "M3 Cierre Definitivo Favorable (Status: Closed)"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
        })
        cuerpo = _obtener_cuerpo_respuesta_azar(cid)
        if cuerpo:
            payload["cuerpo_respuesta_final"] = cuerpo
        payload.update(_obtener_anexos_sin_fraude())
        espera_exito = True

    elif tipo_escenario == 5:
        nombre = "M3 Cierre Definitivo No Favorable (Status: Closed)"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "No favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero no aceptadas por la entidad",
        })
        cuerpo = _obtener_cuerpo_respuesta_azar(cid)
        if cuerpo:
            payload["cuerpo_respuesta_final"] = cuerpo
        payload.update(_obtener_anexos_sin_fraude())
        espera_exito = True

    elif tipo_escenario == 6:
        nombre = "M3 Flujo Unificado (Fraude + Cierre)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        monto = float(random.randint(500000, 2000000))
        payload.update({
            "Status": "Closed",
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Suplantación de identidad",
            "card_amount__c": monto,
            "Total_Devuelto_por_Desconocimiento__c": monto,
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            "directorio_s3": DEFAULT_DIRECTORIO_S3
        })
        cuerpo = _obtener_cuerpo_respuesta_azar(cid)
        if cuerpo:
            payload["cuerpo_respuesta_final"] = cuerpo
        espera_exito = True

    elif tipo_escenario == 7:
        nombre = "Campos Mínimos Obligatorios (11 campos exactos)"
        espera_exito = True

    # --------------------------------------------------------------------------
    # 🔴 CASOS DE ERROR CONTROLADOS ESTÁNDAR (8 a 13)
    # --------------------------------------------------------------------------
    elif tipo_escenario == 8:
        nombre = "Error Controlado: Nombre en Blanco"
        payload["SuppliedName"] = ""
        espera_exito = False

    elif tipo_escenario == 9:
        nombre = "Error Controlado: Documento > 15 caracteres"
        longitud_excesiva = random.randint(16, 25)
        payload["id_number__c"] = "".join(random.choices(string.digits, k=longitud_excesiva))
        espera_exito = False

    elif tipo_escenario == 10:
        nombre = "Error Controlado: Documento sin números"
        payload["id_number__c"] = "".join(random.choices(string.ascii_uppercase, k=8))
        espera_exito = False

    elif tipo_escenario == 11:
        nombre = "Error Controlado: Categoría fuera de catálogo"
        payload["Categorias_COL__c"] = f"Categoría Inexistente {random.randint(100, 999)}"
        espera_exito = False

    elif tipo_escenario == 12:
        nombre = "Error Controlado: Cierre sin Favorabilidad/Aceptación"
        payload["Status"] = "Closed"
        espera_exito = False

    elif tipo_escenario == 13:
        nombre = "Error Controlado: Fraude sin archivos ni directorio"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        payload["tipo_fraude__c"] = "Externo"
        payload["modalidad_fraude__c"] = "Phishing"
        payload["archivos_s3"] = []
        payload["directorio_s3"] = None
        espera_exito = False

    # --------------------------------------------------------------------------
    # 🤪 CASOS BORDES DINÁMICOS / FUZZING (14 a 20)
    # --------------------------------------------------------------------------
    elif tipo_escenario == 14:
        nombre = "Error Límite: Descripción Gigante (> 4500 caracteres)"
        longitud_azar = random.randint(4501, 8000)
        payload["Description"] = "".join(random.choices(string.ascii_letters + " ", k=longitud_azar))
        espera_exito = False

    elif tipo_escenario == 15:
        nombre = "Error Límite: Directorio S3 Inexistente en MinIO/S3"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        payload["tipo_fraude__c"] = "Externo"
        payload["modalidad_fraude__c"] = "Phishing"
        payload["directorio_s3"] = f"caso/CARPETA_INEXISTENTE_{random.randint(10000, 99999)}/"
        espera_exito = False

    elif tipo_escenario == 16:
        nombre = "Prueba Límite: Caracteres Unicode, Emojis y Script HTML en Nombre"
        seed_nombre = random.choice(UNICODE_EMOJI_SEEDS)
        payload["SuppliedName"] = f"{seed_nombre} #{secuencia}"
        payload["direccion__c"] = f"Av. Cañasgordas 🐉 # {random.randint(1,100)}-{random.randint(1,99)} (Apto {secuencia})"
        espera_exito = True

    elif tipo_escenario == 17:
        nombre = "Prueba Límite: Cierre con HTML Pesado (~30KB a 80KB de HTML)"
        multiplicador = random.randint(500, 1500)
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            "cuerpo_respuesta_final": f"<html><body><h2>Dictamen {secuencia}</h2>" + ("<p>Párrafo de trazabilidad pericial de prueba de estrés en PDF.</p>" * multiplicador) + "</body></html>"
        })
        espera_exito = True

    elif tipo_escenario == 18:
        nombre = "Error Límite: Monto de Fraude Negativo"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        payload["tipo_fraude__c"] = "Externo"
        payload["modalidad_fraude__c"] = "Phishing"
        monto_negativo = float(-1 * random.randint(1000, 999999))
        payload["card_amount__c"] = monto_negativo
        payload["directorio_s3"] = DEFAULT_DIRECTORIO_S3
        espera_exito = False

    elif tipo_escenario == 19:
        nombre = "Error Límite: Formato de Email Malformado"
        payload["SuppliedEmail"] = random.choice(BAD_EMAIL_PATTERNS)
        espera_exito = False

    else:
        nombre = "Prueba Límite: Inyección de Campos Extra Inexistentes en JSON"
        inyeccion_sql = random.choice(SQL_XSS_INJECTIONS)
        payload["campo_hacker_desconocido__c"] = inyeccion_sql
        payload["objeto_extra_rnd"] = {"random_id": random.randint(1000, 9999), "test": True}
        espera_exito = True

    return {
        "id": secuencia,
        "nombre": nombre,
        "espera_exito": espera_exito,
        "payload": payload
    }


def construir_banco_de_pruebas(total_peticiones: int) -> List[Dict[str, Any]]:
    casos = []
    for i in range(1, total_peticiones + 1):
        if i <= NUM_ESCENARIOS_BASE:
            # Cobertura inicial garantizada (Casos 0 al 20)
            tipo_escenario = i - 1
        else:
            # Elección aleatoria de escenarios para peticiones posteriores
            tipo_escenario = random.randint(0, NUM_ESCENARIOS_BASE - 1)

        caso = generar_caso(secuencia=i, tipo_escenario=tipo_escenario)
        casos.append(caso)

    return casos

# ==============================================================================
# 🚀 EJECUTOR SECUENCIAL
# ==============================================================================
def ejecutar_pruebas_secuenciales():
    print("=" * 85)
    print("🚀 INICIANDO PRUEBAS SECUENCIALES CON FUZZING Y CASOS BORDES (1 A LA VEZ)")
    print(f"🎯 URL Destino: {TARGET_URL}")
    print(f"📦 Total Peticiones Solicitadas: {TOTAL_PETICIONES}")
    print(f"📌 Estrategia: Cobertura base de {NUM_ESCENARIOS_BASE} casos + muestreo fuzzing aleatorio")
    print(f"📁 Directorio por defecto: {DEFAULT_DIRECTORIO_S3}")
    print(f"📄 Archivo fijo de prueba: {FIXED_S3_KEY}")
    print("=" * 85 + "\n")

    banco_casos = construir_banco_de_pruebas(TOTAL_PETICIONES)
    total_casos = len(banco_casos)
    resultados_log = []
    pasados = 0
    fallados = 0

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
        "User-Agent": "SequentialFuzzTester/1.0"
    }

    with httpx.Client(timeout=35.0) as client:
        for caso in banco_casos:
            case_num = caso["id"]
            nombre = caso["nombre"]
            espera_exito = caso["espera_exito"]
            payload = caso["payload"]
            case_id = payload.get("Case_id", "N/A")

            fase = "COBERTURA BASE" if case_num <= NUM_ESCENARIOS_BASE else "FUZZING ALEATORIO"
            print(f"▶️ [{case_num:02d}/{total_casos:02d}] ({fase}) Probando: {nombre} (Case_id: {case_id})...")

            start_time = time.perf_counter()
            try:
                response = client.post(TARGET_URL, json=payload, headers=headers)
                elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                status_code = response.status_code

                try:
                    res_body = response.json()
                except Exception:
                    res_body = {"raw_text": response.text}

                if espera_exito:
                    cumplio = status_code in (200, 201, 202)
                else:
                    cumplio = status_code in (400, 422)

                if cumplio:
                    pasados += 1
                    print(f"   ✅ RESULTADO: SEGÚN LO PLANEADO (HTTP {status_code}) - {elapsed_ms}ms")
                else:
                    fallados += 1
                    print(f"   ❌ RESULTADO: DESVIACIÓN INESPERADA (HTTP {status_code}) - {elapsed_ms}ms")
                    print(f"      📄 Detalle/Error: {json.dumps(res_body, ensure_ascii=False)}")

            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
                fallados += 1
                status_code = 0
                cumplio = False
                res_body = {"exception": str(exc)}
                print(f"   💥 RESULTADO: EXCEPCIÓN DE RED/SISTEMA - {elapsed_ms}ms")
                print(f"      📄 Detalle Excepción: {exc}")

            resultados_log.append({
                "id": case_num,
                "fase": fase,
                "escenario": nombre,
                "esperaba_exito": espera_exito,
                "status_code": status_code,
                "cumplió_expectativa": cumplio,
                "elapsed_ms": elapsed_ms,
                "respuesta": res_body,
                "payload_enviado": payload
            })
            
            if case_num < total_casos and COOLDOWN_SECONDS > 0:
                print(f"   ⏳ Pausa de cooldown: {COOLDOWN_SECONDS}s...")
                time.sleep(COOLDOWN_SECONDS)
                
            print("-" * 85)

    # ==============================================================================
    # 📊 RESUMEN FINAL
    # ==============================================================================
    print("\n" + "=" * 85)
    print("📈 RESUMEN FINAL DE LA EJECUCIÓN SECUENCIAL")
    print("=" * 85)
    print(f"📦 Total Casos Evaluados: {total_casos}")
    print(f"✅ Pruebas Conformes (Según lo planeado): {pasados} / {total_casos}")
    print(f"❌ Pruebas Con Desviación / Fallo Inesperado: {fallados} / {total_casos}")

    with open(OUTPUT_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(resultados_log, f, ensure_ascii=False, indent=2)

    print(f"\n💾 Reporte detallado guardado en: '{OUTPUT_LOG_FILE}'")
    print("=" * 85)


if __name__ == "__main__":
    ejecutar_pruebas_secuenciales()