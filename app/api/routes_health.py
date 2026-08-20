# app/api/routes_health.py
from fastapi import APIRouter, status, Response
from app.db.redis import ping_redis

router = APIRouter(tags=["Health"])

@router.get("/health/live", status_code=status.HTTP_200_OK)
async def liveness():
    """El contenedor está vivo."""
    return {"status": "alive"}

@router.get("/health/ready")
async def readiness(response: Response):
    """El contenedor está listo (revisa conexiones clave)."""
    redis_ok = await ping_redis()

    if not redis_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unhealthy", "redis": False}

    return {"status": "ready", "redis": True}