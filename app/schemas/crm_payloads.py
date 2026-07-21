# app/schemas/crm_payloads.py
from pydantic import BaseModel, Field, model_validator
from typing import List, Optional, Literal
from datetime import date

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
    CreatedDate: str = Field(..., description="fecha_creacion traducida a ISO")
    Smart_Code__c: str = Field(..., description="codigo_queja traducido")
    codigo_pais__c: str = Field(..., description="codigo_pais traducido")
    Departamento__c: str = Field(..., description="departamento_cod traducido a texto")
    SC_municipio__c: str = Field(..., description="municipio_cod traducido a texto")
    SuppliedName: str = Field(..., description="nombres traducido")
    SC_id_type__c: str = Field(..., description="tipo_id_CF traducido")
    id_number__c: str = Field(..., description="numero_id_CF traducido")
    SuppliedPhone: Optional[str] = Field(None, description="telefono traducido")
    SuppliedEmail: Optional[str] = Field(None, description="correo traducido")
    tipo_de_persona__c: str = Field(..., description="tipo_persona traducido a texto")
    sc_genero__c: str = Field(..., description="sexo traducido a texto")
    sc_LGBTIQ__c: str = Field(..., description="lgbtiq traducido")
    canal__c: str = Field(..., description="canal_cod traducido a texto")
    sc_Condicion_especial__c: str = Field(..., description="condicion_especial traducido a texto")
    Product__c: str = Field(..., description="producto_cod traducido")
    smart_Producto_nombre__c: Optional[str] = Field(None, description="producto_nombre traducido")
    Categorias_COL__c: str = Field(..., description="macro_motivo_cod traducido")
    Description: str = Field(..., description="texto_queja traducido y libre de HTML")
    smart_anexo_queja__c: bool = Field(..., description="anexo_queja traducido")
    Tutela__c: str = Field(..., description="tutela traducida")
    Ente_de_control__c: str = Field(..., description="ente_control traducido a texto")
    smart_escalamiento_DCF__c: str = Field(..., description="escalamiento_DCF traducido") #Si/No
    replica__c: str = Field(..., description="replica traducida") #Si/No
    argumento_replica__c: Optional[str] = Field(None, description="argumento_replica traducido")
    Desistimiento__c: str = Field(..., description="desistimiento_queja traducido")
    Quejas_express__c: str = Field(..., description="queja_expres traducido") #Si/No
    direccion__c: str = Field(..., description="Dirección física de domicilio del cliente")

    # Inyección indispensable de los adjuntos procesados
    archivos_s3: List[ArchivoS3Schema] = Field(default=[],
                                               description= "Colección de metadatos de archivos alojados en S3",
                                               json_schema_extra={
                                                    "example": [
                                                        {
                                                            "nombre_archivo": "soporte_reclamo.pdf",
                                                            "s3_key": "quejas/16551509974606/soporte_reclamo.pdf",
                                                            "bucket": "mi-bucket-smartsupervision"
                                                        }
                                                    ]
                                                }
                                               )
    

# ======================================================================
# 🏁 MOMENTO 2: PAYLOADS DE ENTRADA DESDE EL CRM (SALESFORCE)
# ======================================================================
class Momento2QuejaCrmInput(BaseModel):
    """
    Valida el Request Body enviado por Salesforce (CRM) para iniciar 
    el pipeline de despacho síncrono del Momento 2 hacia la SFC.
    """
    # --- Identificadores y Control de Estado ---
    Smart_Code__c: str = Field(..., description="Código único de la queja (Smart Code o CaseNumber de respaldo)")
    CreatedDate: str = Field(..., description="Fecha/Hora de creación del caso en Salesforce (Formato ISO)")
    Status: Optional[str] = Field("New", description="Estado del caso dentro del CRM")

    # --- Información Demográfica del Consumidor Financiero ---
    SuppliedName: str = Field(..., description="Nombre completo del cliente afectado")
    SC_id_type__c: str = Field(..., description="Acrónimo del tipo de identificación del cliente (Picklist: CC, CE, etc.)")
    id_number__c: str = Field(..., description="Número de identificación del cliente")
    sc_genero__c: str = Field(..., description="Género del cliente (Picklist: Femenino, Masculino, etc.)")
    tipo_de_persona__c: str = Field(..., description="Tipo de persona en el CRM (Picklist: B2C, B2B)")
    sc_LGBTIQ__c: str = Field(..., description="Identificación de comunidad LGBTIQ (Picklist: Si, No)")
    sc_Condicion_especial__c: str = Field(..., description="Condición de vulnerabilidad del cliente (Picklist o 'No aplica')")

    # --- Datos de Contacto y Ubicación ---
    SuppliedPhone: Optional[str] = Field(None, description="Teléfono de contacto registrado")
    SuppliedEmail: Optional[str] = Field(None, description="Correo electrónico de contacto")
    direccion__c: str = Field(..., description="Dirección física de domicilio del cliente")
    Departamento__c: str = Field(..., description="Nombre del departamento oficial de residencia (ej: Bogotá D.C.)")
    SC_municipio__c: str = Field(..., description="Nombre del municipio oficial de residencia (ej: Bogotá D.C.)")

    # --- Origen y Gestión de Recepción ---
    canal__c: str = Field(..., description="Canal por donde ingresó la queja (Picklist: Internet, Oficinas, etc.)")
    punto_recepcion: str = Field(..., description="Punto físico o digital de radicación (Picklist: Manual, Web, etc.)")
    Instancia_de_recepcion__c: str = Field(..., description="Entidad que recibe inicialmente (Picklist: Entidad vigilada, etc.)")
    admision_col__c: str = Field("No Aplica", description="Estado inicial de admisión de la queja")

    # --- Detalles de la Reclamación ---
    Description: str = Field(..., description="Cuerpo del texto original del reclamo (Se limpiará HTML en el pipeline)")
    smart_anexo_queja__c: bool = Field(..., description="Indica si el caso posee archivos adjuntos")
    Tutela__c: str = Field("No", description="Indica si corresponde a una acción de tutela (Picklist: Si, No)")
    Ente_de_control__c: str = Field("Otros", description="Mapeo de ente regulador involucrado si aplica")
    smart_escalamiento_DCF__c: str = Field(..., description="Indica si hubo escalamiento con la DCF")
    
    # --- Clasificación de Producto y Motivo (Tipificación Global66) ---
    Product__c: str = Field(..., description="Línea de producto asociada (Picklist: Cuenta perfil, Wallet, etc.)")
    smart_Producto_nombre__c: Optional[str] = Field(None, description="Nombre descriptivo del producto digital")
    Categorias_COL__c: str = Field(..., description="Picklist descriptivo del motivo de reclamación CRM")   
    # --- Gestión de Adjuntos en la Nube ---
    archivos_s3: List[ArchivoS3Schema] = Field(default=[],
                                               description= "Colección de metadatos de archivos alojados en S3",
                                               json_schema_extra={
                                                    "example": [
                                                        {
                                                            "nombre_archivo": "soporte_reclamo.pdf",
                                                            "s3_key": "quejas/16551509974606/soporte_reclamo.pdf",
                                                            "bucket": "mi-bucket-smartsupervision"
                                                        }
                                                    ]
                                                }
                                               )
# ======================================================================
# 🏁 MOMENTO 3: PAYLOADS DE ENTRADA DESDE EL CRM (SALESFORCE)
# ======================================================================

class Momento3BaseCrmInput(BaseModel):
    """
    Campos base compartidos por cualquier flujo de actualización de hitos en M3.
    """
    Smart_Code__c: str = Field(..., description="Código único de la queja asignado por la SFC / CRM")
    canal__c: Optional[str] = Field(None, description="Canal de atención mapeado (ej: Internet)")
    Product__c: Optional[str] = Field(None, description="Código del producto financiero")
    Categorias_COL__c: Optional[str] = Field(None, description="Código del macro motivo de la queja")
    
    # El CRM manda los archivos con sus nombres originales de Salesforce (ej: "respuesta_cliente_v2.pdf")
    archivos_s3: List[ArchivoS3Schema] = Field(default=[],
                                               description= "Colección de metadatos de archivos alojados en S3",
                                               json_schema_extra={
                                                    "example": [
                                                        {
                                                            "nombre_archivo": "soporte_reclamo.pdf",
                                                            "s3_key": "quejas/16551509974606/soporte_reclamo.pdf",
                                                            "bucket": "mi-bucket-smartsupervision"
                                                        }
                                                    ]
                                                }
                                               )

class Momento3TramiteCrmInput(Momento3BaseCrmInput):
    """
    Payload para actualizaciones ordinarias y transiciones de estados intermedios.
    """
    Status: str = Field(..., description="Código del estado actual del trámite (Debe ser diferente a 4)")
    producto_digital__c: str = Field(..., description="Indica si corresponde a un producto digital")
    admision_col__c: str = Field(..., description="Estado de admisión del caso")


class Momento3FraudeCrmInput(Momento3BaseCrmInput):
    """
    Payload especializado para reportar y actualizar incidentes clasificados como Fraude.
    """
    Status: str = Field(..., description="Estado del trámite durante el proceso de investigación")
    tipo_fraude__c: str = Field(..., description="Código de clasificación del fraude")
    modalidad_fraude__c: str = Field(..., description="Código de la modalidad detectada")
    card_amount__c: float = Field(..., description="Valor total reclamado por el consumidor")
    Total_Devuelto_por_Desconocimiento__c: float = Field(..., description="Valor final reconocido/devuelto")
    
    nombre_archivo_fraude: Optional[str] = Field(
        None, 
        description="Nombre original del archivo en la lista que corresponde al dictamen de fraude"
    )

    @model_validator(mode="after")
    def verificar_existencia_archivo_fraude(self) -> "Momento3FraudeCrmInput":
        """Valida preventivamente que el archivo objetivo realmente venga en el listado."""
        num_archivos = len(self.archivos_s3)
        
        if num_archivos < 1:
            raise ValueError(
                f"No se envió un documento de investigación de fraude, cancelando envío de actualización de queja"
            )
        
        if not self.nombre_archivo_fraude:
            if num_archivos == 1:
                self.nombre_archivo_fraude = self.archivos_s3[0].nombre_archivo
            else:
                raise ValueError(
                f"El archivo especificado '{self.nombre_archivo_fraude}' "
                f"no se encuentra dentro del listado de archivos_s3 provistos."
            )
        else:
            
            nombres_en_lista = [a.nombre_archivo for a in self.archivos_s3]
            if self.nombre_archivo_fraude not in nombres_en_lista:
                raise ValueError(
                    f"El archivo especificado '{self.nombre_archivo_fraude}' "
                    f"no se encuentra dentro del listado de archivos_s3 provistos."
                )
        return self


class Momento3CierreCrmInput(Momento3BaseCrmInput):
    """
    Payload obligatorio para ejecutar el Cierre Definitivo de la queja.
    """
    Status: str = Field(4, description="Código de estado de cierre definitivo fijado en 4")
    ClosedDate: date = Field(..., description="Fecha de cierre definitivo (YYYY-MM-DD)")
    Favorabilidad__c: str = Field(..., description="Sentido de la decisión final")
    Aceptacion__c: str = Field(..., description="Indica si hubo aceptación de la queja")
    Rectificacion__c: bool = Field(False, description="Indica si hubo rectificación")
    Prorroga__c: bool = Field(False, description="Indica si la entidad hizo uso de prórroga")
    
    nombre_archivo_final: Optional[str] = Field(
        None, 
        description="Nombre original del archivo en la lista que corresponde a la respuesta de cierre"
    )

    @model_validator(mode="after")
    def gestionar_nombre_archivo_cierre(self) -> "Momento3CierreCrmInput":
        num_archivos = len(self.archivos_s3)
        
        if num_archivos == 0:
            raise ValueError(
                f"No se envió un documento de cierre del caso, cancelando envío de actualización de queja"
            )
            
        if not self.nombre_archivo_final:
            if num_archivos == 1:
                self.nombre_archivo_final = self.archivos_s3[0].nombre_archivo
            else:
                raise ValueError(
                    f"Se recibieron {num_archivos} archivos. Es obligatorio especificar el parámetro "
                    f"'nombre_archivo_final' para indicarle al sistema cuál corresponde a la RESP_FINAL_SFC.[cite: 3]"
                )
        else:
            nombres_en_lista = [a.nombre_archivo for a in self.archivos_s3]
            if self.nombre_archivo_final not in nombres_en_lista:
                raise ValueError(
                    f"El archivo especificado '{self.nombre_archivo_final}' "
                    f"no se encuentra dentro del listado de archivos_s3 provistos.[cite: 3]"
                )
        return self