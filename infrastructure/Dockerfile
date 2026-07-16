# ======================================================================
# FASE 1: Construcción (Builder)
# ======================================================================
FROM python:3.11-slim AS builder

WORKDIR /build

# Evitar que Python escriba archivos .pyc en disco y habilitar el buffer de salida inmediato
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Instalar dependencias del sistema necesarias para compilar paquetes si aplica
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ======================================================================
# FASE 2: Entorno de Ejecución (Final)
# ======================================================================
FROM python:3.11-slim AS final

WORKDIR /code

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH=/home/appuser/.local/bin:$PATH

# Crear un usuario no root por motivos de seguridad corporativa
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -m -s /bin/bash appuser

# Copiar dependencias compiladas desde el builder hacia el directorio del nuevo usuario
COPY --from=builder --chown=appuser:appgroup /root/.local /home/appuser/.local

# Copiar el código fuente del proyecto con permisos apropiados
COPY --chown=appuser:appgroup . .

# Cambiar al usuario seguro
USER appuser

# Exponer el puerto por defecto del microservicio real
EXPOSE 8000

# Punto de entrada por defecto (Microservicio Real)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]