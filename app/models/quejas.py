from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, Float
from app.api.dependencies import Base # O tu Base declarativa de SQLAlchemy

class Queja(Base):
    __tablename__ = "cases"

    # Campo identificador de la queja mapeado a Smart_Code__c
    Smart_Code__c = Column(String(30), primary_key=True)
    
    # Metadato interno para el control del estado de sincronización local
    status_smart = Column(String(30), nullable=False)

    # Campos de metadata de control requeridos para firmas/reglas SFC
    tipo_entidad = Column(Integer, nullable=True)
    entidad_cod = Column(String(5), nullable=True)

    # --- Mapeo a Campos Salesforce (Case / Account) ---
    CreatedDate = Column(DateTime, nullable=True)                  # fecha_creacion / fecha_creación
    SuppliedName = Column(String(50), nullable=True)                # nombres
    id_type__c = Column(Integer, nullable=True)                     # tipo_id_CF
    id_number__c = Column(String(15), nullable=True)                # numero_id_CF
    SuppliedPhone = Column(String(15), nullable=True)               # telefono
    SuppliedEmail = Column(String(50), nullable=True)               # correo
    sex__c = Column(Integer, nullable=True)                         # sexo
    Country__c = Column(String(3), nullable=True)                   # codigo_pais
    Departamento__c = Column(String(3), nullable=True)              # departamento_cod
    Ciudad__c = Column(String(5), nullable=True)                    # municipio_cod
    Origin = Column(Integer, nullable=True)                         # canal_cod
    Product__c = Column(Integer, nullable=True)                     # producto_cod
    Product_Name__c = Column(String(100), nullable=True)            # producto_nombre
    Categorias_COL__c = Column(Integer, nullable=True)              # macro_motivo_cod
    Description = Column(Text, nullable=True)                       # texto_queja
    archivo_adjunto__c = Column(Boolean, nullable=True)             # anexo_queja
    Urgent_Case__c = Column(Integer, nullable=True)                 # tutela
    Ente_de_control__c = Column(Integer, nullable=True)             # ente_control
    
    # Atributos de flujo adicionales de la SFC
    Escalamiento_DCF__c = Column(Integer, nullable=True)            # escalamiento_DCF
    Replica__c = Column(Integer, nullable=True)                     # replica
    Argumento_Replica__c = Column(Text, nullable=True)              # argumento_replica
    Desistimiento__c = Column(Integer, nullable=True)               # desistimiento_queja
    Quejas_express__c = Column(Integer, nullable=True)              # queja_expres
    Instancia_de_recepcion__c = Column(Integer, nullable=True)      # insta_recepcion

    # --- Campos de Momento 3 ---
    estado_cod = Column(Integer, nullable=True)
    fecha_actualizacion = Column(DateTime, nullable=True)
    producto_digital = Column(Integer, nullable=True)
    a_favor_de = Column(Integer, nullable=True)
    aceptacion_queja = Column(Integer, nullable=True)
    rectificacion_queja = Column(Integer, nullable=True)
    prorroga_queja = Column(Integer, nullable=True)
    admision = Column(Integer, nullable=True)
    documentacion_rta_final = Column(String(250), nullable=True)
    fecha_cierre = Column(DateTime, nullable=True)
    marcacion = Column(String(100), nullable=True)
    tipo_fraude = Column(Integer, nullable=True)
    modalidad_fraude = Column(Integer, nullable=True)
    monto_reclamado = Column(Float, nullable=True)
    monto_reconocido = Column(Float, nullable=True)