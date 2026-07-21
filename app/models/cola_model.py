from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, JSON
from app.db.database import Base

class ColaDespachoModel(Base):
    __tablename__ = "cola_despacho"

    id = Column(Integer, primary_key=True, autoincrement=True)
    smart_code = Column(String(50), nullable=False, index=True)
    tipo_operacion = Column(String(20), nullable=False)
    payload_json = Column(JSON, nullable=False)
    
    estado = Column(String(20), default="PENDIENTE", index=True)  # 'PENDIENTE', 'EXITOSO', 'FALLIDO_DEFINITIVO'
    intentos = Column(Integer, default=0)
    max_intentos = Column(Integer, default=10)
    ultimo_error = Column(Text, nullable=True)
    
    proximo_reintento_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)