# app/core/distributed_lock.py
"""
Lock distribuido genérico sobre Redis (SETNX + heartbeat de renovación + release
compare-and-delete vía Lua). Extraído de app/workers/scheduler.py::SchedulerJobLock
(hallazgo E, revisión externa v5, 2026-08-25) para que routes_quejas.py pueda
reutilizarlo como lock por Smart_Code__c sin duplicar la implementación -- el
mecanismo (token de dueño + heartbeat + release atómico) es el mismo que ya
protegía a los jobs periódicos del scheduler, sólo cambia la clave.
"""
import asyncio
import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

RELEASE_LOCK_LUA_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

EXTEND_LOCK_LUA_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], tonumber(ARGV[2]))
else
    return 0
end
"""


class RedisLock:
    def __init__(
        self,
        redis_client,
        lock_key: str,
        lease_segundos: int = 60,
        intervalo_heartbeat: int = 15
    ):
        self.redis = redis_client
        self.lock_key = lock_key
        self.lease_segundos = lease_segundos
        self.intervalo_heartbeat = intervalo_heartbeat
        self.owner_token = str(uuid.uuid4())
        self._heartbeat_task: Optional[asyncio.Task] = None
        self.acquired = False

    async def acquire(self) -> bool:
        if not self.redis:
            return False
        try:
            res = await self.redis.set(
                self.lock_key,
                self.owner_token,
                nx=True,
                ex=self.lease_segundos
            )
            self.acquired = bool(res)
            if self.acquired:
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                logger.debug(f"🔑 [Redis Lock] Candado '{self.lock_key}' adquirido por token: {self.owner_token}")
            return self.acquired
        except Exception as e:
            logger.error(f"❌ Error al adquirir lock distribuido '{self.lock_key}': {e}")
            return False

    async def _heartbeat_loop(self):
        lease_ms = str(self.lease_segundos * 1000)
        while self.acquired:
            await asyncio.sleep(self.intervalo_heartbeat)
            try:
                res = await self.redis.eval(
                    EXTEND_LOCK_LUA_SCRIPT,
                    1,
                    self.lock_key,
                    self.owner_token,
                    lease_ms
                )
                if res != 1:
                    logger.warning(
                        f"⚠️ [Redis Lock] No se pudo extender el lock '{self.lock_key}'. "
                        f"El candado expiró o pertenece a otro dueño."
                    )
                    break
                logger.debug(f"🔄 [Redis Lock] Heartbeat: Lock '{self.lock_key}' renovado exitosamente.")
            except Exception as e:
                logger.error(f"❌ Error renovando lock '{self.lock_key}': {e}")

    async def release(self):
        if not self.acquired:
            return
        self.acquired = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self.redis:
            try:
                await self.redis.eval(
                    RELEASE_LOCK_LUA_SCRIPT,
                    1,
                    self.lock_key,
                    self.owner_token
                )
                logger.debug(f"🔓 [Redis Lock] Lock '{self.lock_key}' liberado limpiamente (CAD).")
            except Exception as e:
                logger.error(f"❌ Error al liberar lock '{self.lock_key}': {e}")

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()
