"""

# 🚀 Microservicio de Integración SFC (SmartSupervisión) - Global66

Microservicio *Stateless* de alto rendimiento diseñado para la sincronización y orquestación bidireccional de reclamaciones (PQRS) entre **Salesforce CRM** y la **Superintendencia Financiera de Colombia (SFC)**, garantizando tolerancia a fallos, resiliencia ante saturación de red y sanitización de seguridad.

---

## 🏗️ Arquitectura del Sistema

El microservicio está diseñado bajo una arquitectura distribuida y desacoplada, con capacidad de escalado horizontal en **AWS ECS + Fargate**

```mermaid
    graph TD
        CRM[Salesforce / Client API] -->|HTTP POST/PUT| ALB[AWS Application Load Balancer]
        ALB --> App1[FastAPI Cluster - Instancia 1]
        ALB --> App2[FastAPI Cluster - Instancia 2]

        App1 -->|1. Validación DTO < 40ms| DTO[Pydantic v2]
        App1 -->|2. Intento Síncrono Timeout 3s| SFC[SFC API Gateway]

        App1 -- 3. Fallo 429 / Timeout --> Redis[(Redis Centralized Queue)]

        Worker[APScheduler Worker] -->|Lote de Reintentos| Redis
        Worker -->|Despacho Asíncrono| SFC

        App1 -->|Gestión de Adjuntos / PDF| S3[(AWS S3 / MinIO)]
```

---

## ✨ Características Principales

* **🛡️ Sanitización Preventiva contra Stored XSS:** Filtro automático en la capa de entrada (Pydantic DTO) que remueve bloques `<script>`, `<iframe>` y etiquetas HTML peligrosas en campos de texto libre (`SuppliedName`, `direccion__c`, `Description`), preservando caracteres Unicode y Emojis.
* **⚡ Validación de Capa de Entrada Ultra-Rápida:** Rechazo de payloads inválidos (documentos mayores a 15 caracteres, categorías fuera de catálogo, cierres sin favorabilidad, nombres vacíos) en menos de 40 ms, evitando saturar la API externa.
* **🔄 Mecanismo de Contingencia y Resiliencia (Redis + APScheduler):** Si la API de la SFC está lenta (>3s) o devuelve cuota agotada (`HTTP 429 / RESOURCE_EXHAUSTED`), el caso se encola de inmediato en Redis y responde `HTTP 202 Accepted` al cliente, procesándose en segundo plano.
* **🩹 Mecanismo de Auto-Recuperación (Self-Healing):** Si la SFC devuelve un error `404 Not Found` al intentar actualizar un caso en Momento 3, el orquestador radicará automáticamente la queja base en Momento 2 y re-ejecutará la actualización de M3 de forma transparente.
* **📄 Generación Automática de Dictámenes PDF:** Renderizado dinámico de respuestas finales de cierre en formato PDF mediante ReportLab, subida automática a S3 y transmisión del adjunto a la SFC con el afijo normativo `RESP_FINAL_SFC`.
* **📊 Observabilidad vía CloudWatch EMF:** Cada ciclo del *Scheduler* emite métricas en formato CloudWatch Embedded Metric Format (namespace `SSV/Queue`: `queue_depth`, `oldest_pending_age_seconds`, `dispatch_success`, `dispatch_failure`) directo a stdout — sin infraestructura adicional, ya que CloudWatch las extrae automáticamente del log del contenedor.

---

## 🛠️ Requisitos del Sistema

* **Python:** 3.12+
* **Docker & Docker Compose:** v2.20+
* **Redis:** 7.0+ (Servidor centralizado de colas)
* **Storage S3:** AWS S3 o MinIO (Emulador local)

---

## ⚙️ Variables de Entorno (`.env`)

Crea un archivo `.env` en la raíz del proyecto tomando como base `.env.example`:

| Variable                    | Descripción                                        | Valor por Defecto                 |
| :-------------------------- | :-------------------------------------------------- | :-------------------------------- |
| `ENVIRONMENT`             | Entorno de ejecución (`local`, `qa`, `prod`) | `local`                            |
| `PROJECT_NAME`            | Nombre del Microservicio                            | `MS-integracion-smartsupervision`           |
| `SFC_URL_BASE`            | URL Base del API Gateway de la SFC                  | *(obligatoria, sin default)* |
| `SFC_TIPO_ENTIDAD`        | Tipo de entidad asignado por la SFC                 | `128`                            |
| `SFC_ENTIDAD_COD`         | Código único de la entidad                        | `6`                            |
| `REDIS_HOST`              | Host del servidor Redis                             | `localhost`                         |
| `REDIS_PORT`              | Puerto de Redis                                     | `6379`                          |
| `AWS_S3_ENDPOINT_URL`     | URL de S3 o MinIO (dejar vacío para AWS S3 real)    | *(sin default; sugerido local: `http://minio:9000`)* |
| `SFC_MINI_RETRY_ATTEMPTS` | Reintentos HTTP síncronos inmediatos               | `2`           |
| `SMTP_FROM_EMAIL`         | Remitente visible de alertas (separado de `SMTP_USER`, obligatorio en SES) | *(sin default; cae a `SMTP_USER`)* |
| `SFC_SYNC_MAX_PAGINAS`    | Máx. páginas por ciclo de paginación SFC (M1/M4)    | `1000`                          |
| `SFC_SYNC_MAX_SEGUNDOS`   | Tiempo máx. por ciclo de paginación SFC (M1/M4)     | `300`                          |

---

## 🚀 Despliegue Local con Docker

### 1. Levantar la infraestructura completa con Balanceador y Réplicas

Para simular el entorno de producción (ECS + Fargate + ALB):

```bash
    python tests/generators/test_secuencial_despacho.py
```

### 2. Verificar la salud de la aplicación

Abre tu navegador o realiza un `curl`:

* **Healthcheck:** `http://localhost:8000/health`
* **Swagger UI:** `http://localhost:8000/docs`

---

## 🧪 Pruebas y Suites de Stress

El proyecto incluye scripts generadores para validar la tolerancia a fallos, reglas de negocio y cargas concurrentes:

### Pruebas Secuenciales E2E (40 escenarios)

Ejecuta la batería de pruebas funcionales que cubre casos borde, Unicode, HTMLs pesados y errores controlados:

```bash
    python tests/generators/test_secuencial_despacho.py
```

### Pruebas de Carga y Estrés Masivo (Concurrencia)

Simula ráfagas de peticiones concurrentes enviadas directamente al balanceador Nginx:

```bash
    python tests/generators/test_stress_local.py
```

---

## 📂 Estructura del Proyecto

ms-test-integracion/
├── app/
│   ├── api/                   # Endpoints HTTP (Routes, Health, Dependencies)
│   ├── core/                  # Mappers, Exceptions, Security (XSS), Config
│   ├── db/                    # Clientes de Redis
│   ├── integrations/          # SfcClient (HTTPX, TLS 1.2, Interceptores)
│   ├── models/                # Modelos ORM y estructuras de cola
│   ├── schemas/               # DTOs Pydantic v2 (Validation & Sanitization)
│   ├── services/              # Orquestador, Momento 2/3 Sync, S3 Service
│   ├── utils/                 # Generador PDF y Parser HTML
│   └── workers/               # APScheduler (Scheduler de reintentos)
├── docs/                      # Planes de desarrollo y especificaciones
├── infrastructure/            # Dockerfile, Docker Compose, Nginx Config
├── tests/                     # Tests unitarios, de integración y generadores
├── Como_testear.md            # Guía detallada para testers QA
├── requirements.txt           # Dependencias de Python
└── README.md                  # Portada técnica del proyecto

---

## 🔔 Notificaciones, Webhooks y Sistema de Alertas (SFC ↔ CRM)

Este documento detalla los mecanismos de comunicación asíncrona, confirmación de entregas hacia **Salesforce CRM** y el sistema de **Alertas Automáticas de Ingeniería (SMTP)** ante eventos operativos o caídas de infraestructura.

---

### 📡 1. Webhook de Confirmación al CRM (Salesforce)

Cuando un caso es encolado en Redis debido a una indisponibilidad temporal de la SFC, el microservicio asume la responsabilidad de completar la entrega. Tan pronto como el *Scheduler Worker* logra despachar el caso exitosamente a la SFC con respuesta **HTTP 200 OK**, dispara de inmediato una notificación **POST** hacia Salesforce.

#### ⚙️ Especificación del Contrato HTTP

* **Método:** `POST`
* **URL:** Configurada en variable de entorno `CRM_WEBHOOK_URL` (Ej: `https://crm.global66.com/api/integrations/smartsupervision/complaint-code`)
* **Headers Obligatorios:**
  * `Content-Type`: `application/json`
  * `X-API-Key`: Clave de autenticación (`CRM_WEBHOOK_API_KEY`)
  * `X-Correlation-ID`: Identificador único de traza (UUIDv4)
  * `User-Agent`: `MS-SmartSupervision-WebhookBot/1.0`

#### 📦 Payload Enviado al CRM

```json
    {
        "case_number": "CASO_SF_2026_0012",
        "smart_code": "1286CASO_SF_2026_0012",
        "status": "CREATED"
    }
```

#### 🖼️ Ejemplo de Auditoría HTTP en Logs / Consola

![1785941870832](image/Readme/1785941870832.png)

---

### 📧 2. Sistema de Alertas por Correo Electrónico (EmailAlertService)

El microservicio cuenta con un módulo de monitoreo proactivo que envía alertas en formato HTML estilizado mediante transporte **SMTP con TLS** (`EmailAlertService`).

#### 📋 Matriz de Eventos y Despacho de Alertas

| Evento de Alerta                       | Disparador / Gatillo                                                           | Frecuencia                  |
| :------------------------------------- | :----------------------------------------------------------------------------- | :-------------------------- |
| **1. Caída de Infraestructura** | Primer caso que entra a la cola cuando estaba vacía (0 pendientes).           | Inmediata                   |
| **2. Umbral de Acumulación**    | La cola de Redis alcanza múltiplos de**100 casos pendientes**.          | Por cada 100 casos          |
| **3. Error No Mapeado SFC**      | La SFC responde con un error no registrado en`errores_sfc.json`.             | Inmediata                   |
| **4. Digest SLA (>12h)**         | Existen casos retenidos en cola por más de**12 horas**.                 | En cada ciclo del Scheduler |
| **5. Dead Letter Queue (DLQ)**   | Un caso alcanza el límite de**10 reintentos** (`FALLIDO_DEFINITIVO`). | Inmediata                   |
| **6. Autorrecuperación SFC**    | La SFC vuelve a estar online y la cola se vacía por completo (0 pendientes).  | Eventual                    |

---

#### 📸 Especificación y Capturas por Tipo de Alerta

##### 🚨 Alerta 1: Caída de Infraestructura / Indisponibilidad SFC

Notifica inmediatamente al equipo cuando se detecta un corte de red, error 502/503 o cuota superada (429) y el primer caso es resguardado en Redis.

![1785942180285](image/Readme/1785942180285.png)

---

##### 📊 Alerta 2: Umbral de Acumulación en Cola (Múltiplos de 100)

Mantiene informado al negocio sobre el volumen de casos represados durante contingencias prolongadas.

![1785943499490](image/Readme/1785943499490.png)

---

##### ⚠️ Alerta 3: Error No Mapeado en la SFC (Exclusivo Dev)

Envía directamente al desarrollador la respuesta raw JSON enviada por la SFC cuando no coincide con ninguna regla de la matriz, facilitando la actualización de `errores_sfc.json` o Google Sheets.

![1785942318790](image/Readme/1785942318790.png)

---

##### ⏳ Alerta 4: Digest de Control de SLA (> 12 Horas Retenidos)

Genera una tabla resumen con los casos que llevan más de 12 horas esperando ser entregados a la SFC, mostrando tiempo acumulado, reintentos y último error.

![1785942357010](image/Readme/1785942357010.png)

---

##### ❌ Alerta 5: Caso Fallido Definitivo / Dead Letter Queue (DLQ)

Se dispara cuando un caso agota sus **10 reintentos automáticos** (~3.75 horas en cola). Informa que el caso requiere auditoría manual.

![1785942390072](image/Readme/1785942390072.png)

---

##### ✅ Alerta 6: Restablecimiento y Autorrecuperación de Servicio

Confirma al equipo que la comunicación con la SFC se restableció y que el *Scheduler* logró vaciar la cola satisfactoriamente.

![1785942411743](image/Readme/1785942411743.png)

---

## 🧮 3. Ciclo de Vida, Retención y Backoff en Redis

La cola utiliza un algoritmo de **Backoff Lineal** (`espera = QUEUE_RETRY_INTERVAL_MINUTES × intentos`) para espaciar los reintentos y evitar saturar la SFC.

* **Intervalo Base (`QUEUE_RETRY_INTERVAL_MINUTES`):** 5 minutos.
* **Máximo de Reintentos (`QUEUE_MAX_RETRIES`):** 10 intentos.
* **Tiempo Máximo en Cola:** **≈225 minutos (3.75 horas)** antes de pasar a DLQ.

### Tabla de Progresión de Reintentos

| N° Intento | Tiempo de Espera |  Tiempo Acumulado  | Horas Acumuladas |
| :--------: | :---------------: | :-----------------: | :---------------: |
|     1     |       5 min       |        5 min        |       0.08 h       |
|     2     |       10 min       |       15 min       |       0.25 h       |
|     5     |       25 min       |       75 min       |       1.25 h       |
|     8     |       40 min       |       180 min       |       3.00 h       |
|     9     |       45 min       | **225 min** |     **3.75 h**     |
|     10     |         —         | **Pasa a DLQ** | **`FALLIDO_DEFINITIVO`** |

> 🛡️ **Nota sobre caídas prolongadas:** Si la SFC responde con un error de infraestructura (`429 Throttled`, `502 Bad Gateway`), el *Circuit Breaker* pospone la ejecución de la tanda **sin incrementar el contador de intentos**, protegiendo el caso de ser descartado prematuramente.
