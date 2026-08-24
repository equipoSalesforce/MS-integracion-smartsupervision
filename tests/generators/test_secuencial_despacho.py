import os
import json
import random
import string
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
import boto3
import httpx
from dotenv import load_dotenv
from botocore.config import Config

# Cargar variables de entorno
load_dotenv()

# ==============================================================================
# ⚙️ CONFIGURACIÓN DEL TEST SECUENCIAL EXTREMO
# ==============================================================================
TARGET_URL = os.getenv("TEST_TARGET_URL", "http://localhost:8000/api/v1/quejas/sync/despacho")
API_KEY = os.getenv("CRM_API_KEY", "g66_sk_test_super_secreto_12345")

OUTPUT_LOG_FILE = "test_secuencial_results.json"

# 🎯 PARÁMETROS CONFIGURABLES
TOTAL_PETICIONES = 182   # Peticiones para cubrir los 92 escenarios base + fuzzing
# 🟢 SFC_URL_BASE apunta al sandbox QA real de la SFC (no un mock local) — ese
# ambiente tiene cuotas de tasa reales (se observó "RESOURCE_EXHAUSTED: Quota
# exceeded for quota metric 'Read Requests'" en una corrida a 0.0s de cooldown).
# Un cooldown pequeño evita que el propio test se autothrottlee contra el sandbox.
COOLDOWN_SECONDS = 0.4   # Pausa entre peticiones

# 🎯 CONFIGURACIÓN DE S3 / MINIO
# 🟢 P1-10: la s3_key se valida contra el Case_id del propio caso — ya no se puede
# usar un directorio/archivo FIJO y compartido entre todos los escenarios (como antes:
# DEFAULT_DIRECTORIO_S3 = "caso/STRESS_TEST_DEFAULT/" para todos). Cada caso genera y
# precarga sus propios archivos bajo "caso/{Case_id}/", calculados en generar_caso().
FIXED_FILE_NAME = "soporte_prueba.pdf"
FIXED_BUCKET = os.getenv("AWS_S3_BUCKET", "global66-sfc-bucket-local")

# 🎯 Total de escenarios base ampliado (0 al 91 = 92 escenarios base)
NUM_ESCENARIOS_BASE = 92

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
    "María 🚀 Ñuñez", "Jöhn 💥 Døe"
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
# 📄 PDF GENÉRICO PARA PRECARGA EN S3/MINIO
# ==============================================================================
def _cargar_pdf_generico() -> bytes:
    """
    Usa la plantilla real de respuesta final (app/resources/plantilla_respuesta_final.pdf)
    si está disponible; si no, arma en memoria un PDF mínimo pero válido (magic bytes
    '%PDF-' correctos, que es lo único que valida validar_integridad_archivo en la app).
    """
    ruta_plantilla = os.path.join(
        os.path.dirname(__file__), "..", "..", "app", "resources", "plantilla_respuesta_final.pdf"
    )
    try:
        with open(ruta_plantilla, "rb") as f:
            contenido = f.read()
            print(f"📄 Usando plantilla real como PDF genérico de pruebas: '{ruta_plantilla}'\n")
            return contenido
    except OSError:
        print(f"⚠️ No se encontró la plantilla PDF en '{ruta_plantilla}'; se usará un PDF mínimo generado en memoria.\n")
        return (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
            b"trailer<</Size 4/Root 1 0 R>>\n"
            b"%%EOF"
        )


PDF_GENERICO_BYTES = _cargar_pdf_generico()
CONTENIDO_CORRUPTO = b"Este texto plano no es un PDF valido y fallara en magic bytes."

# ==============================================================================
# 🎲 HELPER FUNCTIONS
# ==============================================================================
def _generar_ids(secuencia: int) -> tuple[str, str]:
    """Genera Case_id e id_number__c únicos por prueba."""
    timestamp = str(int(time.time()))[-6:]
    case_id = f"SEQ_{timestamp}_{secuencia:04d}"
    doc_number = f"1002{secuencia:04d}{random.randint(100, 999)}"
    return case_id, doc_number


def _anexo_estandar_por_caso(cid: str) -> Tuple[Dict[str, Any], List[Tuple[str, bytes]]]:
    """
    Decide al azar si incluir un directorio_s3 (con un PDF genérico precargado bajo
    'caso/{cid}/', para que la validación de ownership P1-10 lo acepte) o dejarlo vacío.
    """
    if random.choice([True, False]):
        directorio = f"caso/{cid}/"
        s3_key = f"{directorio}{FIXED_FILE_NAME}"
        return (
            {"smart_anexo_queja__c": True, "directorio_s3": directorio},
            [(s3_key, PDF_GENERICO_BYTES)]
        )
    return ({"smart_anexo_queja__c": False, "archivos_s3": []}, [])


def _anexo_fraude_directorio_por_caso(cid: str) -> Tuple[Dict[str, Any], List[Tuple[str, bytes]]]:
    """directorio_s3 propio del caso, con un PDF genérico ya precargado en esa ruta."""
    directorio = f"caso/{cid}/"
    s3_key = f"{directorio}{FIXED_FILE_NAME}"
    return ({"directorio_s3": directorio}, [(s3_key, PDF_GENERICO_BYTES)])


def _anexo_fraude_archivo_fijo_por_caso(
    cid: str, nombre: str = FIXED_FILE_NAME
) -> Tuple[Dict[str, Any], List[Tuple[str, bytes]]]:
    """archivos_s3 con una única entrada fija, apuntando al directorio del propio caso."""
    s3_key = f"caso/{cid}/{nombre}"
    return (
        {"archivos_s3": [{"nombre_archivo": nombre, "s3_key": s3_key, "bucket": FIXED_BUCKET}]},
        [(s3_key, PDF_GENERICO_BYTES)]
    )


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


def construir_cliente_s3():
    """Construye el cliente boto3 apuntando a MinIO/S3 según variables de entorno."""
    raw_endpoint = os.getenv("AWS_S3_ENDPOINT_URL") or os.getenv("AWS_ENDPOINT_URL") or "http://localhost:9000"
    endpoint = raw_endpoint.strip('"').strip("'").strip()

    # Traducir el hostname 'minio' a 'localhost' si el script se ejecuta en la máquina
    # host fuera de Docker (dentro de la red de docker-compose, 'minio' sí resuelve).
    if "minio" in endpoint:
        endpoint = endpoint.replace("minio", "localhost")

    bucket = os.getenv("AWS_S3_BUCKET", "global66-sfc-bucket-local").strip('"').strip("'").strip()
    access_key = (os.getenv("AWS_ACCESS_KEY_ID") or "minioadmin").strip('"').strip("'").strip()
    secret_key = (os.getenv("AWS_SECRET_ACCESS_KEY") or "minioadmin").strip('"').strip("'").strip()
    region = (os.getenv("AWS_REGION") or "us-east-1").strip('"').strip("'").strip()

    s3_client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        # 🎯 Forzar addressing_style = 'path' para compatibilidad total con MinIO local
        config=Config(s3={"addressing_style": "path"})
    )
    return s3_client, bucket, endpoint


def asegurar_bucket(s3_client, bucket: str):
    """Crea el bucket de pruebas en MinIO si todavía no existe (idempotente)."""
    try:
        s3_client.head_bucket(Bucket=bucket)
    except Exception:
        try:
            s3_client.create_bucket(Bucket=bucket)
            print(f"🪣 Bucket '{bucket}' creado en MinIO.\n")
        except Exception as err:
            print(f"⚠️ [Advertencia S3] No se pudo verificar/crear el bucket '{bucket}': {err}\n")


def subir_archivo_s3(s3_client, bucket: str, s3_key: str, contenido: bytes):
    """Sube (o sobreescribe) un objeto en MinIO/S3. No detiene la ejecución si falla."""
    try:
        s3_client.put_object(Bucket=bucket, Key=s3_key, Body=contenido)
    except Exception as err:
        print(f"⚠️ [Advertencia S3] No se pudo precargar '{s3_key}': {err}")


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
# 🧪 GENERADOR DINÁMICO DE CASOS (ESTÁNDAR + FUZZING + CASOS EXTREMOS)
# ==============================================================================
# NOSONAR: script generador de datos de prueba (fuzzing/stress), no código de
# producción -- el dispatch por tipo_escenario es deliberadamente exhaustivo y
# lineal (un bloque por escenario), no vale la pena partirlo en sub-funciones.
def generar_caso(secuencia: int, tipo_escenario: int) -> Dict[str, Any]:  # NOSONAR
    cid, doc = _generar_ids(secuencia)
    payload = _base_mandatory_payload(cid, doc, secuencia)
    precargar: List[Tuple[str, bytes]] = []

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
        anexo, pre = _anexo_estandar_por_caso(cid)
        payload.update(anexo)
        precargar.extend(pre)
        espera_exito = True

    elif tipo_escenario == 1:
        nombre = "M3 Trámite B2B Corporativo (NIT)"
        payload.update({
            "tipo_de_persona__c": "B2B",
            "SC_id_type__c": "RUT",
            "SuppliedName": f"Empresa Corporativa {secuencia}-{random.randint(100, 999)} SAS",
            "Departamento__c": "Antioquia",
            "SC_municipio__c": "Medellín",
        })
        anexo, pre = _anexo_estandar_por_caso(cid)
        payload.update(anexo)
        precargar.extend(pre)
        espera_exito = True

    elif tipo_escenario == 2:
        nombre = "M3 Reporte de Fraude (Uso de directorio_s3)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        monto = float(random.randint(100000, 5000000))
        anexo, pre = _anexo_fraude_directorio_por_caso(cid)
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": random.choice(["Phishing", "Suplantación de identidad", "Smishing"]),
            "card_amount__c": monto,
            "Total_Devuelto_por_Desconocimiento__c": monto if random.choice([True, False]) else 0.0,
            "smart_anexo_queja__c": True,
            **anexo
        })
        precargar.extend(pre)
        espera_exito = True

    elif tipo_escenario == 3:
        nombre = "M3 Reporte de Fraude (Uso de archivos_s3 fijado)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        anexo, pre = _anexo_fraude_archivo_fijo_por_caso(cid)
        payload.update({
            "tipo_fraude__c": "Interno",
            "modalidad_fraude__c": "Sim Swapping",
            "card_amount__c": float(random.randint(200000, 3000000)),
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "smart_anexo_queja__c": True,
            **anexo
        })
        precargar.extend(pre)
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
        anexo, pre = _anexo_estandar_por_caso(cid)
        payload.update(anexo)
        precargar.extend(pre)
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
        anexo, pre = _anexo_estandar_por_caso(cid)
        payload.update(anexo)
        precargar.extend(pre)
        espera_exito = True

    elif tipo_escenario == 6:
        nombre = "M3 Flujo Unificado (Fraude + Cierre)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        monto = float(random.randint(500000, 2000000))
        anexo, pre = _anexo_fraude_directorio_por_caso(cid)
        payload.update({
            "Status": "Closed",
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Suplantación de identidad",
            "card_amount__c": monto,
            "Total_Devuelto_por_Desconocimiento__c": monto,
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            **anexo
        })
        precargar.extend(pre)
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
        nombre = "Error Controlado: Documento sin números ni letras válidas"
        payload["id_number__c"] = "---===---"
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
        anexo, pre = _anexo_fraude_directorio_por_caso(cid)
        monto_negativo = float(-1 * random.randint(1000, 999999))
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": monto_negativo,
            **anexo
        })
        precargar.extend(pre)
        espera_exito = False

    elif tipo_escenario == 19:
        nombre = "Error Límite: Formato de Email Malformado"
        payload["SuppliedEmail"] = random.choice(BAD_EMAIL_PATTERNS)
        espera_exito = False

    elif tipo_escenario == 20:
        nombre = "Prueba Límite: Inyección de Campos Extra Inexistentes en JSON"
        inyeccion_sql = random.choice(SQL_XSS_INJECTIONS)
        payload["campo_hacker_desconocido__c"] = inyeccion_sql
        payload["objeto_extra_rnd"] = {"random_id": random.randint(1000, 9999), "test": True}
        espera_exito = False

    # --------------------------------------------------------------------------
    # 🔥 EXTREME EDGE CASES & SCHEMA TESTS (21 a 35)
    # --------------------------------------------------------------------------
    elif tipo_escenario == 21:
        nombre = "Error Extremo: Nombre compuesto 100% por tags HTML/Script/Style (Sanitizado a Vacío)"
        payload["SuppliedName"] = "<script>alert('hacked')</script><style>body{display:0;}</style><iframe></iframe>"
        espera_exito = False

    elif tipo_escenario == 22:
        nombre = "Error Extremo: Omitir simultáneamente Case_id y Smart_Code__c"
        payload.pop("Case_id", None)
        payload.pop("Smart_Code__c", None)
        espera_exito = False

    elif tipo_escenario == 23:
        nombre = "Error Extremo: Case_id con Caracteres Especiales no Permitidos"
        payload["Case_id"] = f"CASE#INVALIDO%{random.randint(10,99)}"
        espera_exito = False

    elif tipo_escenario == 24:
        nombre = "Error Extremo: Case_id Demasiado Largo (> 26 caracteres)"
        payload["Case_id"] = "A" * 30
        espera_exito = False

    elif tipo_escenario == 25:
        nombre = "Error Extremo: CreatedDate con Formato No-ISO"
        payload["CreatedDate"] = "05/08/2026 10:30:00"
        espera_exito = False

    elif tipo_escenario == 26:
        nombre = "Error Extremo: Fecha de Cierre (ClosedDate) en el Futuro"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            "ClosedDate": "2099-12-31"
        })
        espera_exito = False

    elif tipo_escenario == 27:
        nombre = "Error Extremo: Prórroga fuera del rango permitido (Prorroga__c = 15 > 9)"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            "Prorroga__c": 15
        })
        espera_exito = False

    elif tipo_escenario == 28:
        nombre = "Error Extremo: Fraude con Múltiples Archivos S3 sin 'nombre_archivo_fraude'"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        s3_key_1 = f"caso/{cid}/soporte1.pdf"
        s3_key_2 = f"caso/{cid}/soporte2.pdf"
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "archivos_s3": [
                {"nombre_archivo": "soporte1.pdf", "s3_key": s3_key_1, "bucket": FIXED_BUCKET},
                {"nombre_archivo": "soporte2.pdf", "s3_key": s3_key_2, "bucket": FIXED_BUCKET}
            ]
        })
        precargar.extend([(s3_key_1, PDF_GENERICO_BYTES), (s3_key_2, PDF_GENERICO_BYTES)])
        espera_exito = False

    elif tipo_escenario == 29:
        nombre = "Error Extremo: Fraude con 'nombre_archivo_fraude' Inexistente en la Lista S3"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        s3_key_real = f"caso/{cid}/soporte_real.pdf"
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "nombre_archivo_fraude": "archivo_fantasma.pdf",
            "archivos_s3": [
                {"nombre_archivo": "soporte_real.pdf", "s3_key": s3_key_real, "bucket": FIXED_BUCKET}
            ]
        })
        precargar.append((s3_key_real, PDF_GENERICO_BYTES))
        espera_exito = False

    elif tipo_escenario == 30:
        nombre = "Error Extremo: Producto Inexistente en Catálogo"
        payload["Product__c"] = "CriptoInversionesFalsas 3000"
        espera_exito = False

    elif tipo_escenario == 31:
        nombre = "Prueba Límite: Punto de Recepción Fuera de Catálogo (Mapeado a Fallback 'Manual')"
        payload["punto_recepcion"] = "TikTok Direct Message"
        espera_exito = False

    elif tipo_escenario == 32:
        nombre = "Error Extremo: Tipo de Persona Inexistente"
        payload["tipo_de_persona__c"] = "B2G_GOBIERNO"
        espera_exito = False

    elif tipo_escenario == 33:
        nombre = "Prueba Límite: Normalización de Minúsculas y Sin Tildes en Picklists (Exitoso)"
        payload.update({
            "SC_id_type__c": "cc",
            "sc_genero__c": "femenino",
            "tipo_de_persona__c": "b2c",
            "punto_recepcion": "web",
            "Product__c": "wallet"
        })
        espera_exito = True

    elif tipo_escenario == 34:
        nombre = "Prueba Límite: Mapeo de Alias de Tipo Documento 'N.I.T.' -> 'RUT' (Exitoso)"
        payload.update({
            "tipo_de_persona__c": "B2B",
            "SC_id_type__c": "N.I.T.",
            "SuppliedName": f"Empresa Alias {secuencia} SAS"
        })
        espera_exito = True

    elif tipo_escenario == 35:
        nombre = "Prueba Límite: Inyección XSS en Descripción con Texto Válido (Sanitización Exitosa)"
        payload["Description"] = "Reclamo normal de cliente <script>alert('hack')</script> con continuación de texto legítimo."
        espera_exito = True

    # --------------------------------------------------------------------------
    # ⚡ ESCENARIOS EXTREMOS DE BORDES Y LÍMITES DE ESQUEMA (36 a 50)
    # --------------------------------------------------------------------------
    elif tipo_escenario == 36:
        nombre = "Error Extremo: SuppliedEmail Excediendo Longitud Máxima (> 100 caracteres)"
        email_largo = "usuario_extremadamente_largo_para_probar_limites_max_length_pydantic" * 2 + "@dominioextremadamentelargo.com"
        payload["SuppliedEmail"] = email_largo
        espera_exito = False

    elif tipo_escenario == 37:
        nombre = "Error Extremo: SuppliedName Excediendo Longitud Máxima (> 100 caracteres)"
        payload["SuppliedName"] = "Juan " * 35  # > 140 caracteres
        espera_exito = False

    elif tipo_escenario == 38:
        nombre = "Error Extremo: Prórroga Negativa (Prorroga__c = -1 < 0)"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            "Prorroga__c": -1
        })
        espera_exito = False

    elif tipo_escenario == 39:
        nombre = "Error Extremo: Total_Devuelto_por_Desconocimiento__c Negativo"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        anexo, pre = _anexo_fraude_directorio_por_caso(cid)
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 500000.0,
            "Total_Devuelto_por_Desconocimiento__c": -100.0,
            **anexo
        })
        precargar.extend(pre)
        espera_exito = False

    elif tipo_escenario == 40:
        nombre = "Error Extremo: Documento con Formateo Especial que Supera 15 Dígitos al Limpiar"
        payload["id_number__c"] = "1.040.011.014.015.016"  # 16 dígitos limpios > 15
        espera_exito = False

    elif tipo_escenario == 41:
        nombre = "Error Extremo: smart_Producto_nombre__c Excediendo Longitud Máxima (> 100 caracteres)"
        payload["smart_Producto_nombre__c"] = "Producto " * 25
        espera_exito = False

    elif tipo_escenario == 42:
        nombre = "Error Extremo: Categorias_COL__c Excediendo Longitud Máxima (> 150 caracteres)"
        payload["Categorias_COL__c"] = "Categoría " * 30
        espera_exito = False

    elif tipo_escenario == 43:
        nombre = "Error Extremo: Cierre con Favorabilidad__c Cadena Vacía o Solo Espacios"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "   ",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad"
        })
        espera_exito = False

    elif tipo_escenario == 44:
        nombre = "Error Extremo: Cierre con Aceptacion__c Cadena Vacía o Solo Espacios"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "   "
        })
        espera_exito = False

    elif tipo_escenario == 45:
        nombre = "Prueba Límite: Documento con Puntos y Guiones Limpiado Exitosamente (<= 15 dígitos)"
        payload["id_number__c"] = "1.040.011.014-9"  # Se limpia a "10400110149" (11 caracteres)
        espera_exito = True

    elif tipo_escenario == 46:
        nombre = "Prueba Límite: CreatedDate en Formato ISO con Sufijo Zulu 'Z' (Exitoso)"
        payload["CreatedDate"] = "2026-08-05T10:30:00Z"
        espera_exito = True

    elif tipo_escenario == 47:
        nombre = "Prueba Límite: Cierre enviando cuerpo_respuesta_final en Blanco (Autogenera Fallback Exitoso)"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            "cuerpo_respuesta_final": "   "
        })
        espera_exito = True

    elif tipo_escenario == 48:
        nombre = "Prueba Límite: Canal Fuera de Catálogo (Mapeado a Fallback 'Internet')"
        payload["canal__c"] = "Telepatía Cuántica 5G"
        espera_exito = False

    elif tipo_escenario == 49:
        nombre = "Prueba Límite: Ente de Control Fuera de Catálogo (Mapeado a Fallback 'Otros')"
        payload["Ente_de_control__c"] = "Comisión Intergaláctica de Vigilancia"
        espera_exito = False

    elif tipo_escenario == 50:
        nombre = "Prueba Límite: Condición Especial Fuera de Catálogo (Mapeado a Fallback 'No aplica')"
        payload["sc_Condicion_especial__c"] = "Superhéroe de Cómics"
        espera_exito = False

    # --------------------------------------------------------------------------
    # 💥 ESCENARIOS ULTRA EXTREMOS BORDES Y DE SANITIZACIÓN (51 a 65)
    # --------------------------------------------------------------------------
    elif tipo_escenario == 51:
        nombre = "Error Extremo: Teléfono Excediendo Longitud Máxima (SuppliedPhone > 15 caracteres)"
        payload["SuppliedPhone"] = "+57 310 9876 5432 101"  # > 15 caracteres
        espera_exito = False

    elif tipo_escenario == 52:
        nombre = "Prueba Límite: Dirección 100% HTML (Sanitizada a Fallback 'Dirección no registrada')"
        payload["direccion__c"] = "<script>alert('xss')</script><style>body{color:red;}</style>"
        espera_exito = True

    elif tipo_escenario == 53:
        nombre = "Prueba Límite: Inyección de Caracteres de Control Nulos (\\x00) en Nombre"
        payload["SuppliedName"] = "Juan\x00Perez\x00Inyeccion"
        espera_exito = True

    elif tipo_escenario == 54:
        nombre = "Error Extremo: Cierre Definitivo con ClosedDate Anterior a Fecha de Creación"
        payload.update({
            "CreatedDate": "2026-08-05T10:00:00",
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            "ClosedDate": "2020-01-01"
        })
        espera_exito = False

    elif tipo_escenario == 55:
        nombre = "Prueba Límite: Descripción Exactamente con 4500 Caracteres (Límite Superior Permitido Exitoso)"
        payload["Description"] = "A" * 4500
        espera_exito = True

    elif tipo_escenario == 56:
        nombre = "Prueba Límite: Cierre Definitivo con Favorabilidad 'Parcialmente favorable' (Exitoso)"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "Parcialmente favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad"
        })
        espera_exito = True

    elif tipo_escenario == 57:
        nombre = "Prueba Límite: Género Fuera de Catálogo (Mapeado a Fallback 'No Aplica')"
        payload["sc_genero__c"] = "Alienígena"
        espera_exito = False

    elif tipo_escenario == 58:
        nombre = "Prueba Límite: LGBTIQ Fuera de Catálogo (Mapeado a Fallback 'No')"
        payload["sc_LGBTIQ__c"] = "Tal vez"
        espera_exito = False

    elif tipo_escenario == 59:
        nombre = "Prueba Límite: Tutela Fuera de Catálogo (Mapeado a Fallback 'No')"
        payload["Tutela__c"] = "En tramite judicial"
        espera_exito = False

    elif tipo_escenario == 60:
        nombre = "Prueba Límite: Escalamiento DCF Fuera de Catálogo (Mapeado a Fallback 'No')"
        payload["smart_escalamiento_DCF__c"] = "Quizás"
        espera_exito = False

    elif tipo_escenario == 61:
        nombre = "Prueba Límite: SuppliedEmail con Espacios en Extremos (Trim y Validación Exitosa)"
        payload["SuppliedEmail"] = f"   usuario_limpio_{secuencia}@global66.com   "
        espera_exito = True

    elif tipo_escenario == 62:
        nombre = "Error Extremo: Objeto de Archivo S3 con s3_key Vacía"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "archivos_s3": [
                {"nombre_archivo": "soporte.pdf", "s3_key": "", "bucket": FIXED_BUCKET}
            ]
        })
        espera_exito = False

    elif tipo_escenario == 63:
        nombre = "Prueba Límite: Bucket del payload es ignorado server-side (sólo importa la s3_key propia)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        # 🟢 Descubierto al validar en vivo: s3_service.py IGNORA por completo el campo
        # "bucket" recibido en el payload — siempre usa self.default_bucket, precisamente
        # para blindarse contra inyección de bucket (ver comentario "Retirado bucket
        # opcional para evitar inyecciones" en obtener_stream_archivo). Un bucket vacío o
        # inválido en el payload es entonces inofensivo: la solicitud debe tener éxito
        # igual, ya que sólo la s3_key (validada contra el Case_id) determina el archivo.
        s3_key_propia = f"caso/{cid}/soporte.pdf"
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "archivos_s3": [
                {"nombre_archivo": "soporte.pdf", "s3_key": s3_key_propia, "bucket": "   "}
            ]
        })
        precargar.append((s3_key_propia, PDF_GENERICO_BYTES))
        espera_exito = True

    elif tipo_escenario == 64:
        nombre = "Prueba Límite: Unificación de Genero 'No binario' y LGBTIQ 'Si' (Exitoso)"
        payload.update({
            "sc_genero__c": "No binario",
            "sc_LGBTIQ__c": "Si",
            "sc_Condicion_especial__c": "Adulto mayor"
        })
        espera_exito = True

    elif tipo_escenario == 65:
        nombre = "Prueba Límite: Teléfono Internacional Válido con Prefijo '+' (<= 15 caracteres Exitoso)"
        payload["SuppliedPhone"] = "+573001234567"  # 13 caracteres
        espera_exito = True

    # --------------------------------------------------------------------------
    # 🚀 ESCENARIOS ADICIONALES DE BORDES Y LÍMITES AVANZADOS (66 a 79)
    # --------------------------------------------------------------------------
    elif tipo_escenario == 66:
        nombre = "Prueba Límite: Fecha de Creación (CreatedDate) Antigua (1999 ISO Exitoso)"
        payload["CreatedDate"] = "1999-01-01T00:00:00"
        espera_exito = False

    elif tipo_escenario == 67:
        nombre = "Prueba Límite: Cierre enviando Alias de Estado 'RESOLVED' (Normalizado a Closed)"
        payload.update({
            "Status": "RESOLVED",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad"
        })
        espera_exito = True

    elif tipo_escenario == 68:
        nombre = "Prueba Límite: Prórroga Mínima Permitida (Prorroga__c = None)"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            "Prorroga__c": None
        })
        espera_exito = True

    elif tipo_escenario == 69:
        nombre = "Prueba Límite: Prórroga Máxima no Permitida (Prorroga__c = 9) sin pasar por valores anteriores"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            "Prorroga__c": 9
        })
        espera_exito = False

    elif tipo_escenario == 70:
        nombre = "Error Extremo: Prórroga Superando Límite Superior (Prorroga__c = 10)"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            "Prorroga__c": 10
        })
        espera_exito = False

    elif tipo_escenario == 71:
        nombre = "Prueba Límite: Teléfono con Paréntesis y Guiones Formateado (<= 15 Dígitos Limpios)"
        payload["SuppliedPhone"] = "(310) 123-4567"  # Limpia a "3101234567"
        espera_exito = True

    elif tipo_escenario == 72:
        nombre = "Error Extremo: Teléfono Formateado que Supera 15 Dígitos al Limpiar"
        payload["SuppliedPhone"] = "+57 (310) 123-4567 ext 89012"  # Limpia a > 15 caracteres
        espera_exito = False

    elif tipo_escenario == 73:
        nombre = "Prueba Límite: Favorabilidad__c en Minúsculas y Variación Texto (Normalización Exitosa)"
        payload.update({
            "Status": "Closed",
            "Favorabilidad__c": "favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad"
        })
        espera_exito = True

    elif tipo_escenario == 74:
        nombre = "Prueba Límite: Monto Reclamado en Fraude de Valor Cero (card_amount__c = 0.0 Exitoso)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        anexo, pre = _anexo_fraude_directorio_por_caso(cid)
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 0.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            **anexo
        })
        precargar.extend(pre)
        espera_exito = True

    elif tipo_escenario == 75:
        nombre = "Prueba Límite: Total Devuelto en Fraude de Valor Cero (Total_Devuelto = 0.0 Exitoso)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        anexo, pre = _anexo_fraude_directorio_por_caso(cid)
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            **anexo
        })
        precargar.extend(pre)
        espera_exito = True

    elif tipo_escenario == 76:
        nombre = "Prueba Límite: directorio_s3 con Espacios en Extremos (Trim y Éxito)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        directorio = f"caso/{cid}/"
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "directorio_s3": f"   {directorio}   "
        })
        precargar.append((f"{directorio}{FIXED_FILE_NAME}", PDF_GENERICO_BYTES))
        espera_exito = True

    elif tipo_escenario == 77:
        nombre = "Prueba Límite: Descripción con Caracteres de Salto de Línea y Tabuladores (\\n, \\r, \\t)"
        payload["Description"] = "Línea 1 del reclamo.\nLínea 2 con tabulador:\tDetalle pericial.\r\nLínea final."
        espera_exito = True

    elif tipo_escenario == 78:
        nombre = "Prueba Límite: Case_id con Guiones Bajos y Medios Combinados (Exitoso)"
        payload["Case_id"] = f"SEQ-TEST_2026-{secuencia:04d}"
        espera_exito = True

    elif tipo_escenario == 79:
        nombre = "Prueba Límite: Instancia de Recepción Fuera de Catálogo (Mapeado a Fallback 'Entidad vigilada')"
        payload["Instancia_de_recepcion__c"] = "Tribunal de Justicia Especial"
        espera_exito = False

    # --------------------------------------------------------------------------
    # 🌍 ESCENARIOS INTERNACIONALES, S3 INTEGRITY & ALIASES (80 a 89)
    # --------------------------------------------------------------------------
    elif tipo_escenario == 80:
        nombre = "Prueba Internacional: Reclamante de Chile (CHL) sin Departamento ni Municipio (Exitoso)"
        payload.update({
            "codigo_pais__c": "Chile",
            "Departamento__c": None,
            "SC_municipio__c": None,
            "direccion__c": "Av. Las Condes 12345, Santiago"
        })
        espera_exito = True

    elif tipo_escenario == 81:
        nombre = "Prueba Internacional: Código de País fuera de catálogo (Rechazado por valor inválido)"
        payload.update({
            "codigo_pais__c": "JAPON",
            "direccion__c": "Shibuya Crossing 1-1-1, Tokyo"
        })
        espera_exito = False

    elif tipo_escenario == 82:
        nombre = "Error Extremo: Archivo S3 de 0 Bytes (FILE_EMPTY_ERROR)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        s3_key_vacio = f"caso/{cid}/archivo_vacio.pdf"
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "archivos_s3": [
                {"nombre_archivo": "archivo_vacio.pdf", "s3_key": s3_key_vacio, "bucket": FIXED_BUCKET}
            ]
        })
        # 🟢 P1-10: el archivo se precarga bajo el directorio del PROPIO caso (para que
        # pase ownership) pero con 0 bytes reales, para seguir probando específicamente
        # la validación de integridad (FILE_EMPTY_ERROR), no la de ownership.
        precargar.append((s3_key_vacio, b""))
        espera_exito = False

    elif tipo_escenario == 83:
        nombre = "Error Extremo: Archivo S3 Corrupto / Magic Bytes Inválidos (CORRUPTED_OR_INVALID_FILE)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        s3_key_corrupto = f"caso/{cid}/soporte_corrupto.pdf"
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "archivos_s3": [
                {"nombre_archivo": "soporte_corrupto.pdf", "s3_key": s3_key_corrupto, "bucket": FIXED_BUCKET}
            ]
        })
        precargar.append((s3_key_corrupto, CONTENIDO_CORRUPTO))
        espera_exito = False

    elif tipo_escenario == 84:
        nombre = "Prueba Límite: CreatedDate en Formato ISO con Milisegundos y Zulu 'Z' (Exitoso)"
        payload["CreatedDate"] = "2026-08-05T10:30:00.123456Z"
        espera_exito = True

    elif tipo_escenario == 85:
        nombre = "Prueba Límite: ClosedDate enviada como Datetime ISO completo YYYY-MM-DDThh:mm:ss (Exitoso)"
        # 🟢 Antes CreatedDate quedaba en auto-relleno (hoy) y ClosedDate hardcodeada a una
        # fecha absoluta pasada ("2026-08-05T14:20:00"): con el paso del tiempo, ClosedDate
        # terminó ANTERIOR a CreatedDate, violando esa regla del lado de la SFC por causas
        # ajenas a lo que el escenario realmente quería probar (formato datetime ISO
        # completo). Se fijan ambas fechas relativas a "ahora": CreatedDate unos días atrás
        # (dentro de la ventana de 30 días) y ClosedDate hoy — combinación siempre válida.
        hoy = datetime.now()
        payload.update({
            "CreatedDate": (hoy - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S"),
            "Status": "Closed",
            "Favorabilidad__c": "Favorable",
            "Aceptacion__c": "Respuesta final a favor del consumidor financiero aceptadas por la entidad",
            "ClosedDate": hoy.strftime("%Y-%m-%dT%H:%M:%S")
        })
        espera_exito = True

    elif tipo_escenario == 86:
        nombre = "Prueba Límite: Reembolso en Fraude Mayor al Monto Reclamado (Exitoso)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        anexo, pre = _anexo_fraude_directorio_por_caso(cid)
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 100000.0,
            "Total_Devuelto_por_Desconocimiento__c": 500000.0,
            **anexo
        })
        precargar.extend(pre)
        espera_exito = True

    elif tipo_escenario == 87:
        nombre = "Prueba Límite: Monto de Fraude con Decimales Elevados (Redondeo Exitoso)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        anexo, pre = _anexo_fraude_directorio_por_caso(cid)
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 150000.789,
            "Total_Devuelto_por_Desconocimiento__c": 150000.789,
            **anexo
        })
        precargar.extend(pre)
        espera_exito = True

    elif tipo_escenario == 88:
        nombre = "Prueba Límite: Coexistencia de 'archivos_s3' y 'directorio_s3' Simultáneamente (Exitoso)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        directorio = f"caso/{cid}/"
        s3_key = f"{directorio}{FIXED_FILE_NAME}"
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": 200000.0,
            "Total_Devuelto_por_Desconocimiento__c": 200000.0,
            # directorio_s3 coexiste pero el orquestador lo ignora porque archivos_s3
            # ya viene poblado explícitamente — sólo el archivo referenciado ahí importa.
            "directorio_s3": directorio,
            "archivos_s3": [
                {"nombre_archivo": FIXED_FILE_NAME, "s3_key": s3_key, "bucket": FIXED_BUCKET}
            ]
        })
        precargar.append((s3_key, PDF_GENERICO_BYTES))
        espera_exito = True

    elif tipo_escenario == 89:
        nombre = "Prueba Límite: Alias de Tipo de Documento 'PASAPORTE' -> 'PASS' (Exitoso)"
        payload.update({
            "SC_id_type__c": "PASS",
            "SuppliedName": f"Cliente Pasaporte {secuencia}"
        })
        espera_exito = True

    # --------------------------------------------------------------------------
    # 🔒 REGRESIÓN DE OWNERSHIP DE S3 (P1-10, auditoría 2026-08-13) (90 a 91)
    # --------------------------------------------------------------------------
    elif tipo_escenario == 90:
        nombre = "🔒 P1-10: S3 key de OTRO caso (archivo real, existente) debe ser rechazada (403 S3_KEY_OWNERSHIP_MISMATCH)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        otro_case_id = f"CASO_AJENO_{random.randint(100000, 999999)}"
        s3_key_ajena = f"caso/{otro_case_id}/{FIXED_FILE_NAME}"
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": float(random.randint(100000, 500000)),
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "smart_anexo_queja__c": True,
            "archivos_s3": [
                {"nombre_archivo": FIXED_FILE_NAME, "s3_key": s3_key_ajena, "bucket": FIXED_BUCKET}
            ]
        })
        # El archivo SÍ existe realmente en S3 (para descartar un falso "no encontrado"),
        # pero bajo el directorio de OTRO caso — la API debe rechazarlo por ownership.
        precargar.append((s3_key_ajena, PDF_GENERICO_BYTES))
        espera_exito = False

    elif tipo_escenario == 91:
        nombre = "🔒 P1-10: S3 key propia del caso (mismo Case_id) debe aceptarse (Control positivo de ownership)"
        payload["Categorias_COL__c"] = FRAUD_CATEGORY
        anexo, pre = _anexo_fraude_archivo_fijo_por_caso(cid)
        payload.update({
            "tipo_fraude__c": "Externo",
            "modalidad_fraude__c": "Phishing",
            "card_amount__c": float(random.randint(100000, 500000)),
            "Total_Devuelto_por_Desconocimiento__c": 0.0,
            "smart_anexo_queja__c": True,
            **anexo
        })
        precargar.extend(pre)
        espera_exito = True

    else:
        nombre = "Fallback: repite Campos Mínimos Obligatorios (fuera de rango)"
        espera_exito = True

    return {
        "id": secuencia,
        "nombre": nombre,
        "espera_exito": espera_exito,
        "payload": payload,
        "archivos_a_precargar": precargar
    }


def construir_banco_de_pruebas(total_peticiones: int) -> List[Dict[str, Any]]:
    casos = []
    for i in range(1, total_peticiones + 1):
        if i <= NUM_ESCENARIOS_BASE:
            # Cobertura inicial garantizada (Casos 0 al NUM_ESCENARIOS_BASE - 1)
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
    print("🚀 INICIANDO PRUEBAS SECUENCIALES EXTREMAS CON FUZZING Y CASOS BORDES (1 A LA VEZ)")
    print(f"🎯 URL Destino: {TARGET_URL}")
    print(f"📦 Total Peticiones Solicitadas: {TOTAL_PETICIONES}")
    print(f"📌 Cobertura Base: {NUM_ESCENARIOS_BASE} escenarios configurados")
    print("📁 Directorio S3: por caso ('caso/{Case_id}/'), precargado bajo demanda")
    print("=" * 85 + "\n")

    banco_casos = construir_banco_de_pruebas(TOTAL_PETICIONES)
    total_casos = len(banco_casos)
    resultados_log = []
    pasados = 0
    fallados = 0

    s3_client, bucket, endpoint = construir_cliente_s3()
    print(f"🪣 MinIO/S3 destino: {endpoint} (bucket '{bucket}')")
    asegurar_bucket(s3_client, bucket)

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
        "User-Agent": "SequentialFuzzTesterExtreme/7.0"
    }

    with httpx.Client(timeout=35.0) as client:
        for caso in banco_casos:
            case_num = caso["id"]
            nombre = caso["nombre"]
            espera_exito = caso["espera_exito"]
            payload = caso["payload"]
            case_id = payload.get("Case_id", "SIN_CASE_ID")

            # 🟢 Precarga en MinIO/S3 (bajo la key propia del caso) justo antes de
            # despachar la petición, para que la validación de ownership P1-10
            # encuentre el archivo real donde el payload dice que está.
            for s3_key, contenido in caso.get("archivos_a_precargar", []):
                subir_archivo_s3(s3_client, bucket, s3_key, contenido)

            fase = "COBERTURA BASE" if case_num <= NUM_ESCENARIOS_BASE else "FUZZING ALEATORIO"
            print(f"▶️ [{case_num:03d}/{total_casos:03d}] ({fase}) Probando: {nombre} (Case_id: {case_id})...")

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
                    # 🟢 P1-10: 403 es una respuesta de error legítima cuando la
                    # validación de ownership de S3 rechaza la petición.
                    cumplio = status_code in (400, 403, 422)

                if cumplio:
                    pasados += 1
                    print(f"   ✅ RESULTADO: SEGÚN LO PLANEADO (HTTP {status_code}) - {elapsed_ms}ms")
                else:
                    fallados += 1
                    print(f"   ❌ RESULTADO: DESVIACIÓN INESPERADA (HTTP {status_code}) - {elapsed_ms}ms")
                    print(f"      📄 Detalle/Respuesta: {json.dumps(res_body, ensure_ascii=False)}")

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
                time.sleep(COOLDOWN_SECONDS)

    # ==============================================================================
    # 📊 RESUMEN FINAL
    # ==============================================================================
    print("\n" + "=" * 85)
    print("📈 RESUMEN FINAL DE LA EJECUCIÓN SECUENCIAL EXTREMA")
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
