# app/schemas/crm_payloads.py
from pydantic import BaseModel, Field
from typing import List, Optional

class ArchivoS3Schema(BaseModel):
    nombre_archivo: str = Field(..., description="Nombre final del archivo guardado")
    s3_key: str = Field(..., description="Ruta/Clave única de acceso en el bucket S3")
    bucket: str = Field(..., description="Bucket de S3 donde se alojó")

class QuejaMapeadaCrmResponse(BaseModel):
    """
    Equivalente exacto y mapeado de los 29 campos que entrega la SFC en el Momento 1,
    traducidos a la nomenclatura del CRM local.
    """
    #TODO: Manejar estos dos datos como constantes, consultarlos luego
    tipo_entidad: int
    entidad_cod: str
    CreatedDate: str = Field(..., description="fecha_creacion traducida a ISO")
    Smart_Code__c: str = Field(..., description="codigo_queja traducido")
    codigo_pais__c: str = Field(..., description="codigo_pais traducido")
    Departamento__c: str = Field(..., description="departamento_cod traducido a texto")
    SC_municipio__c: str = Field(..., description="municipio_cod traducido a texto")
    SuppliedName: str = Field(..., description="nombres traducido")
    id_type__c: int = Field(..., description="tipo_id_CF traducido")
    id_number__c: str = Field(..., description="numero_id_CF traducido")
    SuppliedPhone: Optional[str] = Field(None, description="telefono traducido")
    SuppliedEmail: Optional[str] = Field(None, description="correo traducido")
    tipo_de_persona__c: str = Field(..., description="tipo_persona traducido a texto")
    sc_genero__c: str = Field(..., description="sexo traducido a texto")
    lgbtiq__c: bool = Field(..., description="lgbtiq traducido")
    canal__c: str = Field(..., description="canal_cod traducido a texto")
    sc_Condicion_especial__c: str = Field(..., description="condicion_especial traducido a texto")
    Product__c: int = Field(..., description="producto_cod traducido")
    smart_Producto_nombre__c: Optional[str] = Field(None, description="producto_nombre traducido")
    Categorias_COL__c: int = Field(..., description="macro_motivo_cod traducido")
    Description: str = Field(..., description="texto_queja traducido y libre de HTML")
    smart_anexo_queja__c: bool = Field(..., description="anexo_queja traducido")
    Urgent_Case__c: bool = Field(..., description="tutela traducida")
    Ente_de_control__c: str = Field(..., description="ente_control traducido a texto")
    escalamiento_DCF__c: bool = Field(..., description="escalamiento_DCF traducido")
    replica__c: bool = Field(..., description="replica traducida")
    argumento_replica__c: Optional[str] = Field(None, description="argumento_replica traducido")
    Desistimiento__c: bool = Field(..., description="desistimiento_queja traducido")
    Quejas_express__c: bool = Field(..., description="queja_expres traducido")

    # Inyección indispensable de los adjuntos procesados
    archivos_s3: List[ArchivoS3Schema] = Field(default=[])