"""
Healthcheck para el contenedor worker (APScheduler + Cola Redis).
No depende de HTTP: valida que el proceso principal siga operando y
escribiendo su archivo de heartbeat con conectividad activa a Redis.

Exit 0 = sano, Exit 1 = no sano (Docker/ECS lo marcará unhealthy).
"""
import sys
import time

HEARTBEAT_FILE = "/tmp/worker_heartbeat"
MAX_AGE_SECONDS = 150


def main() -> int:
    try:
        with open(HEARTBEAT_FILE, "r") as f:
            last_beat = float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 1

    age = time.time() - last_beat
    if age > MAX_AGE_SECONDS:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())