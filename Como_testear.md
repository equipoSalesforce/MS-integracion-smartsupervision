# Guía de Pruebas y Despliegue Local - MS Integración SmartSupervisión

Este documento detalla los pasos para configurar, ejecutar y probar el microservicio de integración con **SmartSupervisión (SFC)** en un entorno local, utilizando emulación de **AWS S3 (MinIO)**, **Redis** para gestión de colas/concurrencia, y arquitecturas tanto de **instancia única** como **multi-instancia (cluster con balanceador de carga Nginx)**.

---

## 1. Requisitos Previos

* **Python:** versión 3.10 o superior.
* **Docker y Docker Compose:** instalados y en ejecución.
* **Google Chrome:** (Opcional) para pruebas de automatización con Playwright CDP.

---

## 2. Configuración del Entorno Local

### 2.1. Entorno Virtual de Python

En la raíz del proyecto, ejecuta los siguientes comandos para crear y activar el entorno virtual e instalar dependencias:

**En Windows (PowerShell):**

```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
```

### 2.2. Archivo de Variables de Entorno (`infrastructure/.env.test`)

Dentro de la carpeta `infrastructure/`, crea o edita el archivo `.env.test` (puedes tomar de referencia `.env.example`). Este archivo contiene la configuración compartida entre la aplicación, Redis y MinIO:

```code
# Entorno
ENVIRONMENT="qa"

# API SFC QA / Prod
SFC_API_BASE_URL="https://qasmart.superfinanciera.gov.co/"
SFC_USERNAME="tu_usuario_sfc"
SFC_PASSWORD="tu_password_sfc"
SFC_SECRET_KEY="tu_secret_key_sfc"

# Configuración MinIO (S3 Local)
AWS_S3_BUCKET="global66-sfc-bucket-local"
AWS_ACCESS_KEY_ID="minioadmin"
AWS_SECRET_ACCESS_KEY="minioadmin"
AWS_REGION="us-east-1"
AWS_ENDPOINT_URL="http://minio:9000"

# Configuración Redis
REDIS_HOST="redis"
REDIS_PORT=6379

# Seguridad API Interna
CRM_API_KEY="g66_sk_test_super_secreto_12345"
```

---

## 3. Entornos de Pruebas en Docker (MinIO + Redis)

El proyecto cuenta con dos topologías de despliegue local en Docker para validar el microservicio bajo distintos escenarios.

---

### 3.1. Modo 1: Instancia Única (`docker-compose.test.yml`)

Diseñado para pruebas de desarrollo cotidianas, depuración de logs y verificación funcional básica.

```bash
docker compose -f infrastructure/docker-compose.test.yml up --build
```

#### Servicios Desplegados:

1. **`app` (Microservicio):** Expone el puerto `8000` (`http://localhost:8000`). La documentación interactiva Swagger estará disponible en `http://localhost:8000/docs`.
2. **`minio` (Emulador S3):**
   * API S3: Puerto `9000` (`http://localhost:9000`).
   * Consola Web: Puerto `9001` (`http://localhost:9001`).
3. **`redis` (Cola Centralizada):** Puerto `6379`.

---

### 3.2. Modo 2: Multi-Instancia con Balanceador de Carga (`docker-compose.nginx.test.yml`)

Diseñado para simular un entorno de producción distribuido (AWS ECS / Kubernetes), probando concurrencia, bloqueo de colas distribuidas en Redis y consistencia de archivos en MinIO ante múltiples réplicas en paralelo.

```bash
# Levantar el cluster con 3 instancias del microservicio en paralelo
docker compose -f infrastructure/docker-compose.nginx.test.yml up --build --scale app=3
```

### 3.3. Configuración Inicial de MinIO (Creación del Bucket)

La primera vez que levantes cualquier entorno con MinIO, debes verificar o crear el bucket de trabajo:

1. **Ingresar a la Consola Web:** Abre en tu navegador `http://localhost:9001`.
2. **Credenciales:**
   * **Username:** `minioadmin`
   * **Password:** `minioadmin`
3. **Crear Bucket:**
   * Ve al menú lateral **Buckets** -> **Create Bucket**.
   * Nombre exacto: `global66-sfc-bucket-local` (debe coincidir con la variable `AWS_S3_BUCKET`).
   * Guarda los cambios. (Los datos persistirán en el volumen de Docker `minio_data`).

---

## 4. Ejecución de Suites de Pruebas

### 4.1. Tests Unitarios e Integración Interna

Ejecuta el runner de pruebas unitarias de Python:

```bash
   python -m unittest discover -s tests
```

---

### 4.2. Pruebas Secuenciales de Estrés y Casos Borde (`test_secuencial_despacho.py`)

Este script ejecuta **180 peticiones secuenciales** cubriendo **90 escenarios base** (casos de éxito M2/M3, errores de Pydantic, XSS, fuzzing, adjuntos corruptos, 0 bytes, clientes internacionales de Chile/Perú, formatos ISO con microsegundos y aliases):

```bash

   python tests/generators/test_secuencial_despacho.py
```

* **Comportamiento Automático:** El script se conecta autónomamente a MinIO (`http://localhost:9000`) antes de iniciar, pre-cargando los archivos `archivo_vacio.pdf` (0 bytes) y `soporte_corrupto.pdf` (magic bytes inválidos) para validar el rechazo de archivos dañados.
* **Resultados:** Guarda la trazabilidad detallada de la ejecución en `test_secuencial_results.json`.

---

## 5. Tabla de Endpoints Registrados

Todos los endpoints están registrados bajo el prefijo `/api/v1`:

| Método        | Endpoint                          | Descripción                                                                                                                                         |
| :------------- | :-------------------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------- |
| **GET**  | `/health`                       | Chequeo de salud del servicio (Usado por AWS ALB y Nginx).                                                                                           |
| **POST** | `/api/v1/quejas/sync/momento-1` | **Sincronización Momento 1 (SFC -> CRM):** Descarga quejas asignadas, respalda adjuntos en MinIO/S3 y reporta el lote ACK.                    |
| **POST** | `/api/v1/quejas/sync/momento-2` | **Radicación Momento 2 (CRM -> SFC):** Valida payload de entrada, radica el caso inicial en la SFC y gestiona sus anexos.                     |
| **POST** | `/api/v1/quejas/sync/despacho`  | **Orquestador Unificado (Momento 2 + Momento 3):** Maneja cierres, fraudes, actualizaciones, adjuntos y auto-recuperación (*self-healing*). |
