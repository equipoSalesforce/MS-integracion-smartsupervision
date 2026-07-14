from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Float
from sqlalchemy.ext.declarative import declarative_base
from app.core.constants import SmartStatus

Base = declarative_base()

class Queja(Base):
    __tablename__ = "quejas"

    # --- Campos Base e Identificador ---
    codigo_queja = Column(String(30), primary_key=True, index=True) # Obligatorio
    
    # --- Estado interno de la integración ---
    # Posibles valores: 'NUEVA_DESDE_SFC', 'ACK_ENVIADO', 'CREADA_CRM', 'EN_GESTION', 'CERRADA_SFC', etc.
    status_smart = Column(String(50), default=SmartStatus.CREATED.value, index=True) 

    # --- Campos del Momento 1 (Datos de creación SFC -> Entidad) ---
    tipo_entidad = Column(Integer, nullable=True)
    entidad_cod = Column(String(5), nullable=True)
    fecha_creacion = Column(DateTime, nullable=True)
    codigo_pais = Column(String(3), nullable=True)
    departamento_cod = Column(String(3), nullable=True)
    municipio_cod = Column(String(5), nullable=True)
    nombres = Column(String(50), nullable=True)
    tipo_id_CF = Column(Integer, nullable=True)
    numero_id_CF = Column(String(15), nullable=True)
    telefono = Column(String(15), nullable=True)
    correo = Column(String(50), nullable=True)
    tipo_persona = Column(Integer, nullable=True)
    sexo = Column(Integer, nullable=True)
    lgbtiq = Column(Integer, nullable=True)
    canal_cod = Column(Integer, nullable=True)
    condicion_especial = Column(Integer, nullable=True)
    producto_cod = Column(Integer, nullable=True)
    producto_nombre = Column(String(100), nullable=True)
    macro_motivo_cod = Column(Integer, nullable=True)
    texto_queja = Column(Text, nullable=True)
    anexo_queja = Column(Boolean, nullable=True)
    tutela = Column(Integer, nullable=True)
    ente_control = Column(Integer, nullable=True)
    escalamiento_DCF = Column(Integer, nullable=True)
    replica = Column(Integer, nullable=True)
    argumento_replica = Column(Text, nullable=True)
    desistimiento_queja = Column(Integer, nullable=True)
    queja_expres = Column(Integer, nullable=True)

    # --- Campos Exclusivos de Momento 3 (Gestión, Fraude y Cierre) ---
    estado_cod = Column(Integer, nullable=True)
    fecha_actualizacion = Column(DateTime, nullable=True)
    producto_digital = Column(Integer, nullable=True)
    a_favor_de = Column(Integer, nullable=True)
    aceptacion_queja = Column(Integer, nullable=True)
    rectificacion_queja = Column(Integer, nullable=True)
    prorroga_queja = Column(Integer, nullable=True)
    admision = Column(Integer, nullable=True)
    documentacion_rta_final = Column(String(255), nullable=True)
    fecha_cierre = Column(DateTime, nullable=True)
    marcacion = Column(String(255), nullable=True)
    tipo_fraude = Column(Integer, nullable=True)
    modalidad_fraude = Column(Integer, nullable=True)
    monto_reclamado = Column(Float, nullable=True)
    monto_reconocido = Column(Float, nullable=True)