"""
Healthcheck para el contenedor worker (APScheduler + Cola Redis).
No depende de HTTP: valida que el proceso principal siga escribiendo
su archivo de heartbeat con una antiguedad razonable.

Exit 0 = sano, Exit 1 = no sano (Docker/ECS lo marcara unhealthy).
"""
import sys
import time

HEARTBEAT_FILE = "/tmp/worker_heartbeat"

# Debe ser mayor al intervalo de heartbeat configurado en app/worker.py
# (recomendado: 2-3x el intervalo, para tolerar jitter del event loop)
MAX_AGE_SECONDS = 150


def main() -> int:
    try:
        with open(HEARTBEAT_FILE, "r") as f:
            last_beat = float(f.read().strip())
    except (FileNotFoundError, ValueError):
        # Si el proceso ni siquiera arranco a escribir el heartbeat,
        # dentro del start-period esto es normal; fuera de el, es fallo real.
        return 1

    age = time.time() - last_beat
    if age > MAX_AGE_SECONDS:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())