# Guía de Pruebas y Despliegue Local - MS Integración SmartSupervisión

Este documento detalla los pasos necesarios para configurar, ejecutar y probar el microservicio de integración con **SmartSupervisión (SFC)** en un entorno local.

---

## 1. Requisitos Previos

* **Python:** versión 3.10 o superior.
* **Base de Datos:** SQLite (por defecto en local) o MySQL.

---

## 2. Configuración del Entorno Local

### 2.1. Clonar y Configurar Entorno Virtual

En la raíz del proyecto, ejecuta los siguientes comandos en tu terminal para crear y activar el entorno virtual e instalar las dependencias:

**En Windows (PowerShell):**

```powershell
# Crear el entorno virtual si no existe
python -m venv .venv

# Activar el entorno virtual
.venv\Scripts\Activate.ps1

# Instalar dependencias
pip install -r requirements.txt
```

**En Linux / macOS:**

```bash
# Crear el entorno virtual
python3 -m venv .venv

# Activar el entorno virtual
source .venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt
```

### 2.2. Variables de Entorno (`.env`)

Crea un archivo `.env` en la raíz del proyecto (basado en las variables que utiliza el sistema) con el siguiente contenido:

```env
# Configuración general
ENVIRONMENT="development"

# Conexión a Base de Datos 
DATABASE_URL="url base de datos"

# Credenciales para la API SFC de SmartSupervisión (Ambiente QA o Prod)
SFC_API_BASE_URL="https://qasmart.superfinanciera.gov.co/"
SFC_USERNAME="tu_usuario_sfc"
SFC_PASSWORD="tu_password_sfc"
SFC_SECRET_KEY="tu_secret_key_sfc_suministrada"
```

---

## 3. Ejecución del Servidor Local

Una vez configuradas las variables de entorno, puedes levantar el servidor de desarrollo usando `uvicorn`:

```bash
uvicorn app.main:app --reload --port 8000
```

El servidor estará disponible en [http://localhost:8000](http://localhost:8000). Puedes acceder a la documentación interactiva de la API (Swagger UI) en:

* [http://localhost:8000/docs](http://localhost:8000/docs)

---

## 4. Ejecución de Pruebas (Test Suite)

La suite de pruebas incluye tests unitarios e integrales para verificar el comportamiento de la autenticación, generación de firmas y el flujo de sincronización del Hito/Momento 1.

Para ejecutar todas las pruebas, corre el siguiente comando en la raíz del proyecto con el entorno virtual activo:

```bash
python -m unittest discover -s tests
```

---

## 5. Estado de Implementación y Endpoints Registrados

### 5.1. Qué se lleva implementado

* **Autenticación y Firma Digital (HMAC-SHA256):** Generación automática de firmas para peticiones `GET`, `POST`, `PUT`, `PATCH` y Multipart (adjuntos), e inyección de cabeceras de autorización y firmas mediante interceptor HTTPX.
* **Momento 1 (Sincronización):** Obtención paginada de quejas, descarga asíncrona de archivos adjuntos y confirmación por lotes mediante el reporte ACK.
* **Base de datos:** Modelo `Queja` para persistir la información con transiciones de estado (`Created`, `FileDownload-OK`, `reportACK-OK`, etc.) y lógica de reintentos en caso de fallas (`FileDownload-ERROR` y `reportACK-ERROR`).

### 5.2. Endpoints Registrados

Las siguientes rutas están registradas y listas para ser consumidas bajo el prefijo `/api/v1`:

| Método        | Endpoint                          | Descripción                                                                                                                                      |
| :------------- | :-------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------ |
| **GET**  | `/health`                       | Chequeo de salud del servicio (Usado por AWS ALB).                                                                                                |
| **POST** | `/api/v1/quejas/sync/momento-1` | **Ejecuta la Sincronización Completa del Momento 1:** descarga quejas nuevas, obtiene adjuntos de Storage, y reporta el lote ACK a la SFC. |                                                 |

---

## 6. Consideraciones y Notas de Desarrollo

1. **AWS S3:** En desarrollo local, la subida a S3 está mockeada. Se requiere configurar las credenciales de AWS en producción para que el pipeline guarde los adjuntos permanentemente en el bucket correspondiente.
