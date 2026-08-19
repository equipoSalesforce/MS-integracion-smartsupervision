# ======================================================================
# FASE 1: Construcción y Compilación de Dependencias (Builder)
# ======================================================================
# 🟢 FIX (auditoría adversarial v10, P1-10): pin por digest -- python:3.12-slim es un
# tag mutable, el mismo Dockerfile podía producir binarios distintos según la fecha
# del build. Renovación explícita: `docker buildx imagetools inspect python:3.12-slim`
# resuelve el digest actual del índice multi-arquitectura; actualizar aquí y en la
# FASE 2 (deben coincidir) tras validar la nueva imagen base.
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS builder

WORKDIR /build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# ======================================================================
# FASE 2: Base común de ejecución (sin HEALTHCHECK ni CMD todavía)
# ======================================================================
# Mismo digest que la FASE 1 -- deben apuntar siempre a la misma imagen base.
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS base

WORKDIR /code

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    WEB_CONCURRENCY=2

COPY --from=builder /opt/venv /opt/venv

# 🔒 Seguridad: Crear usuario de sistema sin privilegios y SIN shell interactiva
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -d /code appuser

COPY --chown=appuser:appgroup . /code
RUN chown -R appuser:appgroup /code

USER appuser

# ======================================================================
# FASE 3a: Target API (FastAPI/Gunicorn - expone HTTP en :8000)
# ======================================================================
# infrastructure/Dockerfile (Fragmento FASE 3a: Target API)
FROM base AS final-api

EXPOSE 8000

# 🚦 HEALTHCHECK nativo con Python
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')" || exit 1

# 🟢 FIX HALLAZGO 55: Migración a la clase uvicorn_worker.UvicornWorker del paquete uvicorn-worker
# 🟢 FIX (revisión despliegue AWS): graceful-timeout subido de 30s a 60s -- un 429
# de la SFC puede reintentarse internamente (SFC_MINI_RETRY_ATTEMPTS x
# SFC_MINI_RETRY_DELAY_SECONDS) sobre un read timeout de 15s, superando los 30s
# originales antes de que gunicorn mate el worker a mitad de un request en curso.
CMD gunicorn app.main:app -k uvicorn_worker.UvicornWorker \
    --bind 0.0.0.0:8000 \
    --workers "${WEB_CONCURRENCY:-2}" \
    --timeout 120 \
    --graceful-timeout 60

# ======================================================================
# FASE 3b: Target Worker (APScheduler + Cola Redis - sin HTTP)
# ======================================================================
FROM base AS final-worker

# 🚦 HEALTHCHECK basado en heartbeat de archivo (no HTTP: este contenedor
# no expone ningún puerto). app.worker actualiza /tmp/worker_heartbeat
# en cada ciclo; si el archivo no se refresca, el proceso está trabado.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python /code/infrastructure/healthcheck_worker.py || exit 1

CMD ["python", "-m", "app.worker"]

# ======================================================================
# FASE 3c (opcional): Target "final" por compatibilidad hacia atrás
# Si algún pipeline/build referencia --target final explícitamente,
# apunta al comportamiento API por defecto.
# ======================================================================
FROM final-api AS final