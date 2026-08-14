# app/services/momento_2_sync.py
import logging
from typing import Dict, Any, Union, Optional
import httpx

from app.integrations.sfc_client import SfcClient
from app.services.s3_service import S3StorageService
from app.schemas.crm_payloads import QuejaUnificadaCrmInput, Momento2QuejaCrmInput
from app.schemas.sfc_payloads import SfcNuevaQuejaPayload
from app.core.mapping import SfcSalesforceMapper
from app.core.exceptions import SfcIntegrationException
from app.services.email_service import EmailAlertService

logger = logging.getLogger(__name__)


def _es_error_queja_ya_existe_m2(exc_raw_msg: str, error_type: Optional[str] = None) -> bool:
    """
    Evalúa si la SFC rechazó la creación porque EL MISMO código de queja ya existía previamente.
    Distingue la colisión por el mismo codigo_queja de la regla anti-duplicados funcional por motivo/producto/canal.
    """
    msg = (exc_raw_msg or "").lower()

    # 1. Prioridad: Si el mensaje indica duplicidad por motivo/producto/canal (otra queja distinta), NO es el mismo código
    frases_colision_funcional = [
        "mismo motivo",
        "mismo producto",
        "verifique el motivo",
        "already_exist"
    ]
    if any(frase in msg for frase in frases_colision_funcional):
        return False

    # 2. Si el error fue por duplicidad real del mismo código
    if error_type == "ALREADY_EXISTS":
        return True

    keywords = ["ya existe", "already exists", "registrado en la sfc"]
    return any(kw in msg for kw in keywords)


class Momento2SincronizacionService:
    def __init__(self, sfc_client: SfcClient, s3_client=None):     
        self.sfc_client = sfc_client
        self.s3_service = S3StorageService(
            s3_client=s3_client, 
            http_client=getattr(sfc_client, "client", None)
        )

    async def ejecutar_envio_momento_2(
        self, 
        payload: Union[QuejaUnificadaCrmInput, Momento2QuejaCrmInput, Dict[str, Any]]
    ) -> Dict[str, Any]:
        if isinstance(payload, dict):
            crm_dict = payload
            smart_code = payload.get("Smart_Code__c")
            case_id_crm = payload.get("Case_id") or smart_code
            archivos_s3_raw = payload.get("archivos_s3", [])
        else:
            crm_dict = payload.model_dump()
            smart_code = payload.Smart_Code__c
            case_id_crm = payload.Case_id or smart_code
            archivos_s3_raw = payload.archivos_s3

        # 🟢 FIX HALLAZGO 40: Lanza SfcIntegrationException (400) para errores de validación de entrada
        if not smart_code:
            raise SfcIntegrationException(
                status_code=400,
                error_type="CRM_PAYLOAD_VALIDATION_ERROR",
                sfc_field="Smart_Code__c",
                raw_message="Falta el campo obligatorio 'Smart_Code__c' en el payload de la petición.",
                crm_action="Verifique que el caso contenga un 'Smart_Code__c' válido antes de despachar."
            )

        logger.info(f"[Momento 2] Iniciando transmisión del caso: {smart_code}")

        try:
            sfc_raw_payload = SfcSalesforceMapper.crm_entity_to_sfc_payload(crm_dict)
            sfc_raw_payload["codigo_queja"] = smart_code
            
            directorio_s3 = getattr(payload, "directorio_s3", None) or crm_dict.get("directorio_s3")
            if not archivos_s3_raw and directorio_s3:
                archivos_s3_raw = await self.s3_service.listar_archivos_en_directorio(prefix=directorio_s3)

            payload_validado = SfcNuevaQuejaPayload(**sfc_raw_payload)

            # 1. Intentar crear la queja en la SFC
            try:
                await self.sfc_client.post_nueva_queja(payload_validado.model_dump())
            except SfcIntegrationException as exc:
                # 🛡️ CAPTURA DE TIMEOUT PREVIO: Si la queja ya fue creada en un intento anterior
                if _es_error_queja_ya_existe_m2(exc.raw_message, exc.error_type):
                    logger.info(
                        f"ℹ️ [Momento 2] La queja {smart_code} ya se encontraba radicada en la SFC "
                        f"(Creación completada en intento previo/timeout). Procediendo con la verificación de adjuntos."
                    )
                else:
                    raise

            # 2. Transmisión de adjuntos
            if archivos_s3_raw:
                await self.s3_service.transferir_lote_s3_a_sfc(
                    sfc_client=self.sfc_client,
                    sfc_codigo_queja=smart_code,
                    adjuntos_crm=archivos_s3_raw,
                    case_id=case_id_crm  # 🟢 FIX P1-10
                )

            return {
                "status": "success",
                "message": "Queja procesada correctamente en la SFC (Radicación confirmada)",
                "Smart_Code__c": smart_code
            }

        except SfcIntegrationException as exc:
            if getattr(exc, "is_unmapped", False) or getattr(exc, "error_type", None) == "UNKNOWN_ERROR":
                await EmailAlertService.notificar_error_no_mapeado(
                    status_code=getattr(exc, "status_code", 500),
                    raw_message=str(exc),
                    sfc_field=getattr(exc, "sfc_field", None),
                    smart_code=smart_code
                )
            raise

        except (httpx.RequestError, httpx.TimeoutException, ConnectionError, OSError) as net_err:
            logger.error(f"❌ [Momento 2] Fallo de red/conexión para {smart_code}: {net_err}")
            raise net_err

        except Exception as e:
            # 🟢 FIX HALLAZGO 40: Se relanza la excepción no controlada para tratarse como 500
            logger.error(f"🔥 [Momento 2] Fallo no controlado en pipeline M2 para caso {smart_code}: {str(e)}", exc_info=True)
            raise e