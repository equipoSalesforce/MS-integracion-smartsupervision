from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, Float
from app.api.dependencies import Base

class Queja(Base):
    __tablename__ = "quejas"

    # Llaves de control e Identificadores únicos
    Smart_Code__c = Column(String(50), primary_key=True)
    status_smart = Column(String(50), nullable=False) # Metadata de control local del cron
    
    tipo_entidad = Column(Integer, nullable=True)
    entidad_cod = Column(String(10), nullable=True)

    # --- Objeto Case / Account Mapeado Exacto a tu Diccionario ---
    CreatedDate = Column(DateTime, nullable=True)
    SuppliedName = Column(String(100), nullable=True)
    id_type__c = Column(String(50), nullable=True)
    id_number__c = Column(String(50), nullable=True)
    SuppliedPhone = Column(String(50), nullable=True)
    SuppliedEmail = Column(String(100), nullable=True)
    company_name__c = Column(String(200), nullable=True)
    
    # Campos Picklist / Valores Homologados Regulados
    sc_genero__c = Column(String(50), nullable=True)          # sexo (SFC)
    canal__c = Column(String(100), nullable=True)             # canal_cod (SFC)
    Ente_de_control__c = Column(String(100), nullable=True)   # ente_control (SFC)
    sc_Condicion_especial__c = Column(String(100), nullable=True) # condicion_especial (SFC)
    tipo_de_persona__c = Column(String(50), nullable=True)    # tipo_persona (SFC)
    
    # Control de Flujos y Momentos
    smart_anexo_queja__c = Column(Boolean, nullable=True)     # anexo_queja (SFC)
    Urgent_Case__c = Column(Boolean, nullable=True)            # tutela (SFC)
    Desistimiento__c = Column(String(50), nullable=True)      # desistimiento_queja (SFC)
    Quejas_express__c = Column(String(50), nullable=True)     # queja_expres (SFC)
    Instancia_de_recepcion__c = Column(String(50), nullable=True) # insta_recepcion (SFC)
    
    # Metadata Adicional de Auditoría SFC
    Product__c = Column(String(100), nullable=True)
    smart_Producto_nombre__c = Column(String(200), nullable=True)
    Categorias_COL__c = Column(String(250), nullable=True)
    Description = Column(Text, nullable=True)

    # --- Campos de Cierre / Momento 3 ---
    Smart_Status__c = Column(String(50), nullable=True)       # estado_cod (SFC)
    ClosedDate = Column(DateTime, nullable=True)               # fecha_cierre (SFC)
    card_amount__c = Column(Float, nullable=True)              # monto_reconocido / Importe Afectación (SFC)
    Aceptacion__c = Column(String(50), nullable=True)
    Prorroga__c = Column(String(50), nullable=True)
    admision_col__c = Column(String(50), nullable=True)
    Rectificacion__c = Column(String(50), nullable=True)