import os
import re
import json
import random
import asyncio
import time
from datetime import datetime
from typing import Dict, Any, List
from zoneinfo import ZoneInfo
import httpx
from dotenv import load_dotenv

# Cargar variables de entorno del archivo .env local
load_dotenv()

# ==============================================================================
# ⚙️ CONFIGURACIÓN DEL TEST DE ESTRÉS
# ==============================================================================
TARGET_URL = os.getenv("TEST_TARGET_URL", "http://localhost:8000/api/v1/quejas/sync/despacho")
API_KEY = os.getenv("CRM_API_KEY", "g66_sk_test_super_secreto_12345")

# Parámetros de ejecución configurables
TOTAL_REQUESTS = 20        # Cantidad total de peticiones a enviar
CONCURRENCY = 4           # Número máximo de peticiones concurrentes simultáneas
CHAOS_RATIO = 0.0         # Porcentaje de payloads con errores intencionados (20%)
OUTPUT_LOG_FILE = "test_stress_results.json"

# Valores por defecto para evitar inconsistencias DIVIPOLA y rutas S3 inexistentes
DEFAULT_DEPARTAMENTO = "Bogotá D.C."
DEFAULT_MUNICIPIO = "Bogotá D.C."
DEFAULT_DIRECTORIO_S3 = "caso/STRESS_TEST_DEFAULT/"  # 🎯 Directorio S3 compartido por defecto

# ==============================================================================
# 📋 CATÁLOGOS DE MUESTRA PARA VARIACIÓN
# ==============================================================================
DOCUMENT_TYPES = ["CC", "CE", "RUT", "PEP", "PPT", "DNI", "PASS"]
PRODUCTS = ["Wallet", "Cuenta perfil", "Exchange", "Transactions", "P2P", "Tarjeta Digital", "Tarjeta Fisica"]
CHANNELS = [
    "Internet", "Aplicaciones móviles", "Asistente virtual", 
    "Centro de atención telefónica (Call center/Contact center)", "POS no propios"
]
CATEGORIES = [
    "Remesas", "Dificultad en el acceso a la información",
    "Incumplimiento de los términos del contrato", "Inconformidades relacionadas con el proceso de cobranza",
    "Inconsistencia en el cobro de comisiones - Descuentos injustificados"
]

# ==============================================================================
# 🎲 GENERADORES DE PAYLOADS
# ==============================================================================
def _generar_identificadores_unicos(secuencia: int) -> tuple[str, str]:
    """Genera Case_id e id_number__c totalmente únicos por cada petición."""
    # Cambiar primeras 4 cifras de doc_number para testear nuevas opciones
    timestamp_compacto = datetime.now(ZoneInfo("America/Bogota")).strftime("%y%m%d%H%M%S")
    case_id = f"STRESS_{timestamp_compacto}_{secuencia:04d}"
    doc_number = f"1280{secuencia:06d}"
    return case_id, doc_number


def crear_payload_valido(secuencia: int) -> Dict[str, Any]:
    """Genera un payload válido alternando entre 5 escenarios de negocio."""
    case_id, doc_number = _generar_identificadores_unicos(secuencia)
    escenario = secuencia % 5

    # Base común obligatoria con ubicación fija y directorio S3 por defecto
    payload_base = {
        "Case_id": case_id,
        "SuppliedName": f"Usuario Prueba Estrés {secuencia}",
        "SC_id_type__c": random.choice(DOCUMENT_TYPES),
        "id_number__c": doc_number,
        "tipo_de_persona__c": "B2C",
        "direccion__c": f"Calle {random.randint(1, 150)} # {random.randint(1, 100)}-{random.randint(1, 99)}",
        "punto_recepcion": random.choice(["Web", "WhatsApp", "Email", "Manual"]),
        "Description": f"Petición de prueba de carga masiva local. Secuencia {secuencia}.",
        "smart_escalamiento_DCF__c": "No",
        "Product__c": random.choice(PRODUCTS),
        "Categorias_COL__c": random.choice(CATEGORIES),
        "canal__c": random.choice(CHANNELS),
        "Departamento__c": DEFAULT_DEPARTAMENTO,
        "SC_municipio__c": DEFAULT_MUNICIPIO,
        # 🎯 Uso de directorio_s3 por defecto y lista de archivos vacía
        "directorio_s3": DEFAULT_DIRECTORIO_S3,
    }

    if escenario == 0:
        # ESCENARIO 1: Trámite B2C Ordinario
        payload_base["sc_genero__c"] = random.choice(["Masculino", "Femenino", "Trans", "No binario"])
        payload_base["sc_LGBTIQ__c"] = random.choice(["Si", "No"])
        payload_base["sc_Condicion_especial__c"] = random.choice(["No aplica", "Adulto mayor", "Discapacidad física"])
        return payload_base

    elif escenario == 1:
        # ESCENARIO 2: Trámite B2B
        payload_base["tipo_de_persona__c"] = "B2B"
        payload_base["SC_id_type__c"] = "NIT"
        payload_base["SuppliedName"] = f"Empresa Corporativa {secuencia} SAS"
        return payload_base

    elif escenario == 2:
        # ESCENARIO 3: Reporte de Fraude en Trámite (Usa directorio_s3 en lugar de archivos_s3 singulares)
        payload_base["Categorias_COL__c"] = "Transacción no reconocida"
        payload_base["tipo_fraude__c"] = random.choice(["Externo", "Interno"])
        payload_base["modalidad_fraude__c"] = random.choice(["Phishing", "Suplantación de identidad", "Sim Swapping"])
        payload_base["card_amount__c"] = float(random.randint(100000, 5000000))
        payload_base["Total_Devuelto_por_Desconocimiento__c"] = 0.0
        return payload_base

    elif escenario == 3:
        # ESCENARIO 4: Cierre Definitivo Favorable (Status = Closed)
        payload_base["Status"] = "Closed"
        payload_base["Favorabilidad__c"] = "Favorable"
        payload_base["Aceptacion__c"] = "Respuesta final a favor del consumidor financiero aceptadas por la entidad"
        payload_base["cuerpo_respuesta_final"] = (
            f"<html><body><p>Estimado Cliente {secuencia},</p>"
            f"<p>Confirmamos el cierre favorable de su caso <strong>{case_id}</strong>.</p></body></html>"
        )
        return payload_base

    else:
        # ESCENARIO 5: Cierre Definitivo No Favorable
        payload_base["Status"] = "Closed"
        payload_base["Favorabilidad__c"] = "No favorable"
        payload_base["Aceptacion__c"] = "Respuesta final a favor del consumidor financiero no aceptadas por la entidad"
        payload_base["cuerpo_respuesta_final"] = (
            f"<html><body><p>Estimado Cliente {secuencia},</p>"
            f"<p>Tras la investigación técnica, el caso <strong>{case_id}</strong> concluye como no favorable.</p></body></html>"
        )
        return payload_base


def crear_payload_invalido(secuencia: int) -> tuple[Dict[str, Any], str]:
    """Genera un payload intencionadamente incorrecto para probar la validación de errores."""
    payload = crear_payload_valido(secuencia)
    error_tipo = secuencia % 4

    if error_tipo == 0:
        payload["SuppliedName"] = ""
        return payload, "Nombre en blanco"
    elif error_tipo == 1:
        payload["id_number__c"] = "1234567890123456789"
        return payload, "Documento > 15 caracteres"
    elif error_tipo == 2:
        payload["Categorias_COL__c"] = "Categoría Totalmente Inexistente 999"
        return payload, "Categoría fuera de catálogo"
    else:
        payload["Status"] = "Closed"
        payload["Favorabilidad__c"] = None
        payload["Aceptacion__c"] = None
        return payload, "Cierre sin Favorabilidad/Aceptación"

# ==============================================================================
# 🚀 MOTOR ASÍNCRONO DE PETICIONES
# ==============================================================================
async def enviar_peticion(
    client: httpx.AsyncClient, 
    semaphore: asyncio.Semaphore, 
    secuencia: int,
    es_invalido: bool
) -> Dict[str, Any]:
    
    if es_invalido:
        payload, motivo_caos = crear_payload_invalido(secuencia)
    else:
        payload = crear_payload_valido(secuencia)
        motivo_caos = "N/A (Payload Válido)"

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
        "User-Agent": f"StressTesterAsync/1.0 (Seq-{secuencia})"
    }

    start_time = time.perf_counter()
    
    async with semaphore:
        try:
            response = await client.post(TARGET_URL, json=payload, headers=headers, timeout=15.0)
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            
            status_code = response.status_code
            cid = response.headers.get("X-Correlation-ID", response.headers.get("x-correlation-id", "N/A"))

            try:
                response_json = response.json() if response.text else {}
            except Exception:
                response_json = {"raw_text": response.text}

            es_exito = status_code in (200, 201, 202)

            return {
                "secuencia": secuencia,
                "case_id": payload.get("Case_id"),
                "status_code": status_code,
                "success": es_exito,
                "elapsed_ms": elapsed_ms,
                "correlation_id": cid,
                "es_invalido_intencional": es_invalido,
                "motivo_caos": motivo_caos,
                "response_body": response_json,
                "payload_enviado": payload
            }

        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return {
                "secuencia": secuencia,
                "case_id": payload.get("Case_id"),
                "status_code": 0,
                "success": False,
                "elapsed_ms": elapsed_ms,
                "correlation_id": "EXCEPT",
                "es_invalido_intencional": es_invalido,
                "motivo_caos": motivo_caos,
                "response_body": {"error_exception": str(exc)},
                "payload_enviado": payload
            }


async def ejecutar_test_estres():
    print("=" * 80)
    print("🚀 INICIANDO PRUEBA DE CARGA Y ESTRÉS LOCAL")
    print(f"🎯 URL Destino: {TARGET_URL}")
    print(f"📍 Ubicación fija: {DEFAULT_DEPARTAMENTO} / {DEFAULT_MUNICIPIO}")
    print(f"📁 Directorio S3 por defecto: {DEFAULT_DIRECTORIO_S3}")
    print(f"📦 Peticiones Totales: {TOTAL_REQUESTS} | Concurrencia Simultánea: {CONCURRENCY}")
    print(f"🧪 Tasa de Errores Inducidos (Caos): {int(CHAOS_RATIO * 100)}%")
    print("=" * 80)

    semaphore = asyncio.Semaphore(CONCURRENCY)
    
    num_invalidas = int(TOTAL_REQUESTS * CHAOS_RATIO)
    indices_invalidos = set(random.sample(range(1, TOTAL_REQUESTS + 1), num_invalidas))

    limits = httpx.Limits(max_keepalive_connections=CONCURRENCY, max_connections=CONCURRENCY + 10)
    
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [
            enviar_peticion(
                client=client,
                semaphore=semaphore,
                secuencia=i,
                es_invalido=(i in indices_invalidos)
            )
            for i in range(1, TOTAL_REQUESTS + 1)
        ]
        
        start_total = time.perf_counter()
        resultados = await asyncio.gather(*tasks)
        total_time_sec = round(time.perf_counter() - start_total, 2)

    # ==============================================================================
    # 📊 CONSOLIDACIÓN Y RESUMEN
    # ==============================================================================
    exitosos = [r for r in resultados if r["success"]]
    encolados = [r for r in resultados if r["status_code"] == 202]
    fallos = [r for r in resultados if not r["success"]]
    
    tiempos = [r["elapsed_ms"] for r in resultados]
    avg_time = round(sum(tiempos) / len(tiempos), 2) if tiempos else 0
    max_time = max(tiempos) if tiempos else 0
    min_time = min(tiempos) if tiempos else 0

    codigos_status: Dict[int, int] = {}
    for r in resultados:
        st = r["status_code"]
        codigos_status[st] = codigos_status.get(st, 0) + 1

    print("\n" + "=" * 80)
    print("📈 RESUMEN DE EJECUCIÓN Y RENDIMIENTO")
    print("=" * 80)
    print(f"⏱️ Tiempo Total de Ejecución: {total_time_sec} segundos")
    print(f"⚡ Rendimiento: {round(TOTAL_REQUESTS / total_time_sec, 2)} req/seg")
    print(f"📊 Respuestas Exitosas (200/201/202): {len(exitosos)} / {TOTAL_REQUESTS}")
    if encolados:
        print(f"📦 Casos Encolados en Redis (HTTP 202): {len(encolados)}")
    print(f"❌ Respuestas con Error/Rechazo: {len(fallos)} / {TOTAL_REQUESTS}")
    print(f"⏱️ Tiempo Mín / Prom / Máx: {min_time}ms / {avg_time}ms / {max_time}ms")
    print("\n🔢 Desglose por Código HTTP:")
    for code, count in sorted(codigos_status.items()):
        print(f"   • HTTP {code}: {count} peticiones")

    reporte_log = {
        "timestamp_ejecucion": datetime.now().isoformat(),
        "configuracion": {
            "target_url": TARGET_URL,
            "ubicacion_fija": f"{DEFAULT_DEPARTAMENTO} / {DEFAULT_MUNICIPIO}",
            "directorio_s3_defecto": DEFAULT_DIRECTORIO_S3,
            "total_peticiones": TOTAL_REQUESTS,
            "concurrencia": CONCURRENCY,
            "chaos_ratio": CHAOS_RATIO
        },
        "metricas_rendimiento": {
            "tiempo_total_segundos": total_time_sec,
            "req_por_segundo": round(TOTAL_REQUESTS / total_time_sec, 2),
            "promedio_ms": avg_time,
            "max_ms": max_time,
            "min_ms": min_time,
            "desglose_http": codigos_status
        },
        "detalles_errores_y_fallos": [
            {
                "secuencia": f["secuencia"],
                "case_id": f["case_id"],
                "status_code": f["status_code"],
                "correlation_id": f["correlation_id"],
                "es_invalido_intencional": f["es_invalido_intencional"],
                "motivo_caos": f["motivo_caos"],
                "respuesta_microservicio": f["response_body"],
                "payload_enviado": f["payload_enviado"]
            }
            for f in fallos
        ]
    }

    with open(OUTPUT_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(reporte_log, f, ensure_ascii=False, indent=2)

    print(f"\n💾 Reporte detallado de errores y logs guardado en: '{OUTPUT_LOG_FILE}'")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(ejecutar_test_estres())