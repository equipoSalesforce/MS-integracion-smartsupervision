# app/api/routes_health.py
from fastapi import APIRouter, status, Response
from app.db.redis import get_redis_client

router = APIRouter(tags=["Health"])

@router.get("/health/live", status_code=status.HTTP_200_OK)
async def liveness():
    """El contenedor está vivo."""
    return {"status": "alive"}

@router.get("/health/ready")
async def readiness(response: Response):
    """El contenedor está listo (revisa conexiones clave)."""
    redis = get_redis_client()
    redis_ok = False
    
    if redis:
        try:
            await redis.ping()
            redis_ok = True
        except Exception:
            redis_ok = False

    if not redis_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unhealthy", "redis": False}

    return {"status": "ready", "redis": True}