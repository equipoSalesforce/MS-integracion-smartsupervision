import hmac
import hashlib
import json
import logging
import os
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Header, HTTPException, status, Request, Query
from fastapi.responses import JSONResponse, Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MockSFC")

app = FastAPI(
    title="Mock Oficial Smartsupervisión - Superintendencia Financiera de Colombia",
    description="API de pruebas locales que simula al 100% las respuestas y comportamiento de la SFC (Incluye Módulo de Caos)",
    version="1.1.0"
)

# ======================================================================
# ⚙️ PARSEO SEGURO DE VARIABLES DE ENTORNO Y ESTADO GLOBAL
# ======================================================================
SECRET_KEY_TEST = os.getenv("SFC_SECRET_KEY", "global66_sfc_secret_key_testing_2026")

env_verify = os.getenv("SFC_VERIFY_SIGNATURES", "true")
if isinstance(env_verify, str):
    VERIFY_SIGNATURES = env_verify.lower() in ("true", "1", "yes")
else:
    VERIFY_SIGNATURES = bool(env_verify)

# 💥 Variable global para controlar la simulación de caída de la SFC
SFC_MODO_CAIDO: bool = False

logger.info(f"Mock SFC inicializado. ¿Verificación de firmas activa?: {VERIFY_SIGNATURES}")


# ======================================================================
# 🧪 MÓDULO DE CAOS / CONTRASEÑA DE SIMULACIÓN DE INDISPONIBILIDAD
# ======================================================================
def chequear_estado_servidor():
    """
    Si el modo de caos está activo, interrumpe inmediatamente cualquier
    petición devolviendo un 502 Bad Gateway (Servidor caído).
    """
    if SFC_MODO_CAIDO:
        logger.warning("[MOCK SFC] Petición bloqueada por MODO CAOS ACTIVO (Simulando SFC Caída).")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"detail": "Servidor de la Superintendencia Financiera fuera de servicio (502 Bad Gateway simulado)"}
        )


@app.post("/api/mock/simular-caida", status_code=status.HTTP_200_OK, tags=["Mock Chaos Admin"])
async def toggle_sfc_status(caido: bool = Query(True, description="True para simular caída (502), False para operacion normal")):
    """
    Endpoint de administración local para alternar el estado de disponibilidad de la SFC.
    """
    global SFC_MODO_CAIDO
    SFC_MODO_CAIDO = caido
    estado = "CAÍDA SIMULADA (502 Bad Gateway)" if caido else "OPERANDO NORMALMENTE (200 OK)"
    logger.info(f"💥 [MOCK SFC CAOS] Estado cambiado manualmente a: {estado}")
    return {
        "status": "ok",
        "modo_caido": SFC_MODO_CAIDO,
        "message": f"El Mock de la SFC ahora está en estado: {estado}"
    }


@app.get("/api/mock/estado", status_code=status.HTTP_200_OK, tags=["Mock Chaos Admin"])
async def obtener_estado_mock():
    """Retorna el estado actual del servidor Mock."""
    return {
        "modo_caido": SFC_MODO_CAIDO,
        "verify_signatures": VERIFY_SIGNATURES
    }


# ======================================================================
# 🔐 UTILERÍA: Verificador de Firmas de la SFC
# ======================================================================
async def verificar_firma_sfc(
    request: Request, 
    signature_recibida: Optional[str], 
    is_file_upload: bool = False
) -> bool:
    if not VERIFY_SIGNATURES:
        return True

    if not signature_recibida:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail={"status_code": 400, "message": "missing header X-SFC-Signature"}
        )

    key_bytes = bytes(SECRET_KEY_TEST, 'utf-8')
    method = request.method.upper()

    # 1. Estrategia GET (Firma de URL)
    if method == "GET":
        # Usamos request.url o la ruta relativa según el acuerdo de tu cliente
        url_target = str(request.url)
        expected_sig = hmac.new(key_bytes, msg=url_target.encode('utf-8'), digestmod=hashlib.sha256).hexdigest().upper()

    # 2. Estrategia Multipart / Archivos (/api/storage/)
    elif is_file_upload:
        form = await request.form()
        filtered_data = {
            "codigo_queja": form.get("codigo_queja"),
            "type": form.get("type")
        }
        serialized = json.dumps(filtered_data, ensure_ascii=False)
        expected_sig = hmac.new(key_bytes, msg=serialized.encode('utf-8'), digestmod=hashlib.sha256).hexdigest().upper()

    # 3. Estrategia JSON Standard (POST, PUT, PATCH)
    else:
        try:
            body_json = await request.json()
            # Serializamos exactamente igual a como lo hace el cliente
            serialized = json.dumps(body_json, ensure_ascii=False)
            expected_sig = hmac.new(key_bytes, msg=serialized.encode('utf-8'), digestmod=hashlib.sha256).hexdigest().upper()
        except Exception:
            # Fallback a body plano si no fuera un JSON válido
            body_bytes = await request.body()
            expected_sig = hmac.new(key_bytes, msg=body_bytes, digestmod=hashlib.sha256).hexdigest().upper()

    # Comparación segura en tiempo constante
    if not hmac.compare_digest(expected_sig, signature_recibida.upper()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail={"status_code": 400, "message": "Sign verification failed"}
        )

    return True


# ======================================================================
# 🔑 SEGMENTO: Autenticación (Login)
# ======================================================================
@app.post("/api/login/", status_code=status.HTTP_200_OK, tags=["Autenticación"])
async def login_mock(request: Request, x_sfc_signature: Optional[str] = Header(None)):
    chequear_estado_servidor()
    
    body_bytes = await request.body()
    body_str = body_bytes.decode('utf-8')
    verificar_firma_sfc(request, x_sfc_signature, body_str)
    
    mock_access_jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoyNTI0NjA4MDAwLCJ1c2VyX2lkIjoxfQ."
        "mock_signature_field_here_three_segments_ok"
    )
    mock_refresh_jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJ0b2tlbl90eXBlIjoicmVmcmVzaCIsImV4cCI6MjUyNDYwODAwMCwidXNlcl9pZCI6MX0."
        "mock_signature_field_here_three_segments_ok"
    )
    
    return {
        "access": mock_access_jwt,
        "refresh": mock_refresh_jwt,
        "access_token": mock_access_jwt,
        "refresh_token": mock_refresh_jwt,
        "Response": {
            "access": mock_access_jwt,
            "refresh": mock_refresh_jwt,
            "access_token": mock_access_jwt,
            "refresh_token": mock_refresh_jwt
        }
    }


# ======================================================================
# 📥 MOMENTO 1: Sincronización (SFC -> Entidad)
# ======================================================================
@app.get("/api/queja/", status_code=status.HTTP_200_OK, tags=["Momento 1"])
async def get_quejas_momento_1(request: Request, x_sfc_signature: Optional[str] = Header(None)):
    chequear_estado_servidor()
    verificar_firma_sfc(request, x_sfc_signature)
    
    return {
        "count": 3,
        "pages": 1,
        "next": None,
        "previous": None,
        "results": [
            # --- CASO 1: Camila Salas (Tiene exactamente 1 archivo adjunto) ---
            {
                "tipo_entidad": 1,
                "entidad_cod": "423",
                "fecha_creacion": "2026-07-16T08:30:00",
                "codigo_queja": "142316551509974606",
                "codigo_pais": "COL",
                "departamento_cod": "11",
                "municipio_cod": "11001",
                "nombres": "Camila Salas Mock",
                "tipo_id_CF": 1,
                "numero_id_CF": "1040011014",
                "telefono": "3007654321",
                "correo": "camila.salas@mockglobal.com",
                "tipo_persona": 1,
                "sexo": 1,
                "lgbtiq": 2,
                "canal_cod": 13,
                "condicion_especial": 98,
                "producto_cod": 209,
                "producto_nombre": "Global Account Digital",
                "macro_motivo_cod": 209,
                "texto_queja": "Petición de prueba local 1: Caso con un solo archivo adjunto.",
                "anexo_queja": True,
                "tutela": 2,
                "ente_control": 99,
                "escalamiento_DCF": 2,
                "replica": 2,
                "argumento_replica": None,
                "desistimiento_queja": 2,
                "queja_expres": 2,
                "direccion": "carrera 1"
            },
            # --- CASO 2: Mateo Bermúdez (Sin ningún archivo adjunto) ---
            {
                "tipo_entidad": 1,
                "entidad_cod": "423",
                "fecha_creacion": "2026-07-16T09:15:00",
                "codigo_queja": "142316551509974607",
                "codigo_pais": "COL",
                "departamento_cod": "05",
                "municipio_cod": "05001",
                "nombres": "Mateo Bermudez Mock",
                "tipo_id_CF": 1,
                "numero_id_CF": "1050099887",
                "telefono": "3104567890",
                "correo": "mateo.bermudez@mockglobal.com",
                "tipo_persona": 1,
                "sexo": 1,
                "lgbtiq": 2,
                "canal_cod": 13,
                "condicion_especial": 98,
                "producto_cod": 209,
                "producto_nombre": "Global Account Digital",
                "macro_motivo_cod": 209,
                "texto_queja": "Petición de prueba local 2: Caso sin archivos adjuntos para probar bypassing.",
                "anexo_queja": False,
                "tutela": False,
                "ente_control": 99,
                "escalamiento_DCF": 2,
                "replica": 2,
                "argumento_replica": None,
                "desistimiento_queja": 2,
                "queja_expres": 2,
                "direccion": "carrera 1"
            },
            # --- CASO 3: Valentina Gómez (Con múltiples archivos adjuntos concurrentes) ---
            {
                "tipo_entidad": 1,
                "entidad_cod": "423",
                "fecha_creacion": "2026-07-16T10:00:00",
                "codigo_queja": "142316551509974608",
                "codigo_pais": "COL",
                "departamento_cod": "76",
                "municipio_cod": "76001",
                "nombres": "Valentina Gomez Mock",
                "tipo_id_CF": 1,
                "numero_id_CF": "1060044332",
                "telefono": "3127894561",
                "correo": "valentina.gomez@mockglobal.com",
                "tipo_persona": 1,
                "sexo": 2,
                "lgbtiq": 2,
                "canal_cod": 13,
                "condicion_especial": 98,
                "producto_cod": 209,
                "producto_nombre": "Global Account Digital",
                "macro_motivo_cod": 209,
                "texto_queja": "Petición de prueba local 3: Caso pesado con múltiples archivos de soporte adjuntos.",
                "anexo_queja": True,
                "tutela": 2,
                "ente_control": 99,
                "escalamiento_DCF": 2,
                "replica": 2,
                "argumento_replica": None,
                "desistimiento_queja": 2,
                "queja_expres": 2,
                "direccion": "carrera 1"
            }
        ]
    }


@app.post("/api/complaint/ack", status_code=status.HTTP_200_OK, tags=["Momento 1"])
async def confirmacion_ack_momento_1(request: Request, x_sfc_signature: Optional[str] = Header(None)):
    chequear_estado_servidor()
    body_bytes = await request.body()
    body_str = body_bytes.decode('utf-8')
    verificar_firma_sfc(request, x_sfc_signature, body_str)
    
    return {
        "message": "update code",
        "pqrs_error": []
    }


@app.get("/api/storage/", status_code=status.HTTP_200_OK, tags=["Adjuntos"])
async def listado_archivos_momento_1(
    request: Request, 
    codigo_queja__codigo_queja: str = Query(...), 
    x_sfc_signature: Optional[str] = Header(None)
):
    chequear_estado_servidor()
    verificar_firma_sfc(request, x_sfc_signature)
    
    results = []
    
    if codigo_queja__codigo_queja == "142316551509974606":
        results = [
            {
                "id": 13,
                "file": "https://file-examples.com/wp-content/uploads/2017/10/file-sample_150kB.pdf",
                "type": "pdf",
                "state": 1,
                "codigo_queja": codigo_queja__codigo_queja,
                "reference": "1"
            }
        ]
    elif codigo_queja__codigo_queja == "142316551509974607":
        results = []
    elif codigo_queja__codigo_queja == "142316551509974608":
        results = [
            {
                "id": 24,
                "file": "https://file-examples.com/wp-content/uploads/2017/10/file-sample_150kB.pdf",
                "type": "pdf",
                "state": 1,
                "codigo_queja": codigo_queja__codigo_queja,
                "reference": "1"
            },
            {
                "id": 25,
                "file": "https://file-examples.com/wp-content/uploads/2017/10/file_example_PNG_500kB.png",
                "type": "png",
                "state": 1,
                "codigo_queja": codigo_queja__codigo_queja,
                "reference": "1"
            }
        ]
    else:
        results = []

    return {
        "count": len(results),
        "pages": 1,
        "next": None,
        "previous": None,
        "results": results
    }


# ======================================================================
# 📤 MOMENTO 2: Envío de Quejas Nuevas con Inyección de Errores
# ======================================================================
@app.post("/api/queja/", status_code=status.HTTP_201_CREATED, tags=["Momento 2"])
async def post_queja_momento_2(request: Request, x_sfc_signature: Optional[str] = Header(None)):
    chequear_estado_servidor()
    body_bytes = await request.body()
    body_str = body_bytes.decode('utf-8')
    verificar_firma_sfc(request, x_sfc_signature, body_str)
    
    payload_recibido = await request.json()
    body_data = payload_recibido.get("Body", payload_recibido)
    
    nombres_val = body_data.get('nombres', "")
    id_number_val = body_data.get("numero_id_CF", "")
    dept_val = body_data.get("departamento_cod", "")
    muni_val = body_data.get("municipio_cod", "")
    
    # 1. Validación de Nombres Nulos
    if nombres_val == "TRIGGER_ERR_NAME_NULL":
        return JSONResponse(
            status_code=400,
            content={"nombres": ["Este campo no puede ser nulo."]}
        )
    
    # 2. Validación de Nombres > 50 caracteres
    if nombres_val == "TRIGGER_ERR_NAME_LIMIT":
        return JSONResponse(
            status_code=400,
            content={"nombres": ["Asegúrese de que este campo no tenga más de 50 caracteres."]}
        )
        
    # 3. Validación de Tipo ID Inválido o vacío (Clave primaria "0")
    if body_data.get("tipo_id_CF") == 0:
        return JSONResponse(
            status_code=400,
            content={"tipo_id_CF": ["Clave primaria \"0\" inválida - objeto no existe."]}
        )
        
    # 4. Validación de Número ID Inválido o vacío (Clave primaria "0")
    if id_number_val == "0":
        return JSONResponse(
            status_code=400,
            content={"numero_id_CF": ["Clave primaria \"0\" inválida - objeto no existe."]}
        )

    # 5. Validación de Departamento Inválido (Clave primaria "00")
    if dept_val == "00":
        return JSONResponse(
            status_code=400,
            content={"departamento_cod": ["Clave primaria \"00\" inválida - objeto no existe."]}
        )

    # 6. Validación de Municipio no coincide con Departamento
    if muni_val == "INVALIDO_MUNI":
        return JSONResponse(
            status_code=400,
            content={"municipio_cod": ["El código del municipio no corresponde al departamento asignado."]}
        )

    # 7. Caso de Queja ya Duplicada en el sistema
    if nombres_val == "TRIGGER_ERR_ALREADY_EXISTS":
        return JSONResponse(
            status_code=400,
            content={
                "queja_entidad_motivo_producto_already_exist": [
                    "Señor(a) consumidor, en el sistema ya existe una Queja radicada para la entidad con el mismo motivo, producto y canal, con número de radicado [142312345]. Si la queja es diferente o corresponde a otros hechos, verifique el motivo y producto seleccionado para poder continuar con el proceso de radicación."
                ]
            }
        )

    # 8. Simulación de Caída de Servicio de la SFC (vía Trigger individual)
    if nombres_val == "TRIGGER_ERR_SERVICE_DOWN":
        return Response(
            status_code=503,
            content="El servicio no está disponible por el momento. Vuelva a intentarlo mas tarde."
        )

    # 9. Simulación de Error Crítico Inesperado de la SFC
    if nombres_val == "TRIGGER_ERR_UNEXPECTED":
        return Response(
            status_code=500,
            content="Error inesperado. Código de Error: 20260716111621_exc"
        )
        
    # 10. Error no mapeado por el sistema    
    if nombres_val == "TRIGGER_ERR_UNMAPPED":
        return JSONResponse(
            status_code=400,
            content={
                "codigo_desconocido_sfc": [
                    "Error Regla 999: Fallo de consistencia no documentado en la resolución 2026."
                ]
            }
        )
        
    # Flujo regular de creación exitosa (Eco)
    return {
        "Response": body_data
    }


# ======================================================================
# 📂 CONTROL DE ADJUNTOS CON ERROR DE DUPLICADOS
# ======================================================================
@app.post("/api/storage/", status_code=status.HTTP_201_CREATED, tags=["Adjuntos"])
async def upload_file_momento_2_y_3(request: Request, x_sfc_signature: Optional[str] = Header(None)):
    chequear_estado_servidor()

    await verificar_firma_sfc(request, x_sfc_signature, is_file_upload=True)
    
    form_data = await request.form()
    
    file_obj = form_data.get("file")
    
    if file_obj and ("TRIGGER_DUPLICATE" in file_obj.filename):
        return JSONResponse(
            status_code=400,
            content={"file": ["El anexo ya existe, con el ID [8998896]."]}
        )
        
    logger.info("Recibido archivo multipart/form-data de forma correcta en el Mock.")
    
    codigo_queja_val = form_data.get("codigo_queja")
    if codigo_queja_val == "TRIGGER_M3_CLOSED":
        return JSONResponse(
            status_code=400,
            content={
                "status_code": 400,
                "messages": {
                    "non_field_errors": [
                        "the complaint is already closed"
                    ]
                },
                "detail": "Error APIException"
            }
        )
    
    return {
        "id": 99,
        "file": "https://storage.googleapis.com/mock-sfc-bucket/uploaded_file.pdf",
        "type": "pdf",
        "state": 1,
        "codigo_queja": form_data.get("codigo_queja", "142316551509974606")
    }


# ======================================================================
# 🏁 MOMENTO 3: Actualización y Cierre de Quejas (SFC <- Entidad)
# ======================================================================
@app.put("/api/queja/{codigo_queja}/", status_code=status.HTTP_200_OK, tags=["Momento 3"])
@app.patch("/api/queja/{codigo_queja}/", status_code=status.HTTP_200_OK, tags=["Momento 3"])
async def actualizar_queja_momento_3(
    codigo_queja: str,
    request: Request, 
    x_sfc_signature: Optional[str] = Header(None)
):
    chequear_estado_servidor()
    body_bytes = await request.body()
    body_str = body_bytes.decode('utf-8')
    verificar_firma_sfc(request, x_sfc_signature, body_str)
    
    payload_recibido = await request.json()
    body_data = payload_recibido.get("Body", payload_recibido)

    if codigo_queja == "TRIGGER_M3_NOT_FOUND" or codigo_queja == "142347622214657":
        return JSONResponse(
            status_code=404,
            content={"detail": "Not found."}
        )
        
    if body_data.get("estado_cod") == 999:
        return JSONResponse(
            status_code=400,
            content={
                "status_code": 400,
                "messages": {
                    "estado_cod": ["El código de estado no es válido para el cierre de esta tipología."]
                },
                "detail": "Error APIException"
            }
        )

    if body_data.get("estado_cod") == 4 and "TRIGGER_ERR_M3_NO_DOC" in codigo_queja:
        return JSONResponse(
            status_code=400,
            content={
                "status_code": 400,
                "messages": {
                    "non_field_errors": ["Se detectó la intención de cierre (estado_cod: 4) pero no se ha cargado previamente un archivo válido con el afijo obligatorio RESP_FINAL_SFC."]
                },
                "detail": "Error APIException"
            }
        )

    if body_data.get("tipo_fraude") and "TRIGGER_ERR_M3_NO_DOC" in codigo_queja:
        return JSONResponse(
            status_code=400,
            content={
                "status_code": 400,
                "messages": {
                    "non_field_errors": ["Se detectó gestión de fraude pero no se ha cargado previamente la investigación correspondiente con el afijo obligatorio INV_FRAUDE_SFC."]
                },
                "detail": "Error APIException"
            }
        )

    return {
        "Response": {
            "codigo_queja": codigo_queja,
            "sexo": 2,
            "lgbtiq": 2,
            "condicion_especial": 8,
            "canal_cod": body_data.get("canal_cod", 13),
            "producto_cod": body_data.get("producto_cod", 213),
            "macro_motivo_cod": body_data.get("macro_motivo_cod", 910),
            "estado_cod": body_data.get("estado_cod", 4),
            "fecha_actualizacion": "2026-07-16T15:30:00",
            "producto_digital": body_data.get("producto_digital", 1),
            "a_favor_de": body_data.get("a_favor_de", 1),
            "aceptacion_queja": 1,
            "rectificacion_queja": 1,
            "desistimiento_queja": 1,
            "prorroga_queja": 2,
            "admision": 1,
            "documentacion_rta_final": body_data.get("documentacion_rta_final", True),
            "anexo_queja": body_data.get("anexo_queja", True),
            "fecha_cierre": body_data.get("fecha_cierre", "2026-07-16T15:30:00"),
            "tutela": 1,
            "ente_control": 99,
            "marcacion": 2,
            "queja_expres": 1
        }
    }