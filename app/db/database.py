import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.core.config import settings

# Creación del engine asíncrono para SQLite
engine = create_async_engine(
    settings.SQLITE_DB_URL,
    connect_args={"check_same_thread": False},
    echo=False
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

Base = declarative_base()

async def init_db():
    """Crea la tabla en SQLite de forma idempotente al arrancar la app."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)