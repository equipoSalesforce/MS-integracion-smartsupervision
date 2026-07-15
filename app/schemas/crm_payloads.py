from pydantic import BaseModel, Field

class Momento2Trigger(BaseModel):
    """
    Esquema de validación para recibir el disparador desde el CRM
    e iniciar el pipeline de envío de información del Momento 2 hacia la SFC.
    """
    Smart_Code__c: str = Field(
        ..., 
        description="Identificador único del caso/queja en Salesforce",
        example="16551509974606"
    )

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "Smart_Code__c": "16551509974606"
            }
        }