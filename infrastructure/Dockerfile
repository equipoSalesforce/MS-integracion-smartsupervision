# ======================================================================
# FASE 1: Construcción y Compilación de Dependencias (Builder)
# ======================================================================
FROM python:3.12-slim AS builder

WORKDIR /build

# Prevenir archivos .pyc y habilitar salida inmediata en logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Instalar herramientas esenciales de compilación (se descartan al finalizar esta fase)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Crear un Entorno Virtual aislado (Venv) para simplificar la transferencia
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Optimización de Caché de Capas: Copiar e instalar dependencias primero
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# ======================================================================
# FASE 2: Entorno de Ejecución Mínimo y Seguro (Final / Production)
# ======================================================================
FROM python:3.12-slim AS final

WORKDIR /code

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Copiar exclusivamente el virtualenv precompilado desde la fase de construcción
COPY --from=builder /opt/venv /opt/venv

# 🔒 Seguridad: Crear usuario de sistema sin privilegios y SIN shell interactiva
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -d /code appuser

# Copiar el código fuente asignando la propiedad al usuario seguro
COPY --chown=appuser:appgroup . /code

# Asignar permisos explícitos sobre el directorio de trabajo
RUN chown -R appuser:appgroup /code

# Cambiar al usuario no-root
USER appuser

# Exponer el puerto del microservicio
EXPOSE 8000

# 🚦 HEALTHCHECK nativo con Python (evita instalar curl/wget en la imagen final)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')" || exit 1

# Comando de arranque de la aplicación FastAPI
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]