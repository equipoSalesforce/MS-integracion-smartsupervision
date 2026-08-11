# ======================================================================
# FASE 1: Construcción y Compilación de Dependencias (Builder)
# ======================================================================
FROM python:3.12-slim AS builder

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
# FASE 2: Entorno de Ejecución Mínimo y Seguro (Final / Production)
# ======================================================================
FROM python:3.12-slim AS final

WORKDIR /code

# 🟢 WEB_CONCURRENCY: Ajusta el número de workers (por defecto 2 para ECS Fargate)
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

EXPOSE 8000

# 🚦 HEALTHCHECK nativo con Python (evita instar curl/wget)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')" || exit 1

# 🟢 COMANDO DE ARRANQUE MULTIPROCESO CON GUNICORN + UVICORN WORKERS
# Utiliza Gunicorn como Process Manager y UvicornWorker para manejar peticiones asíncronas
CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--timeout", "120", "--graceful-timeout", "30"]