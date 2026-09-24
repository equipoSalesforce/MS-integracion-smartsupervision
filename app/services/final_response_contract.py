"""CLOSE-only ordering and identity; no customer data retained in checkpoint keys."""
import hashlib
import json


def attachment_name(item):
    if isinstance(item, dict):
        return str(item.get('nombre_archivo') or item.get('nombre') or item.get('s3_key') or '').rsplit('/',1)[-1]
    return str(getattr(item,'nombre_archivo',None) or getattr(item,'s3_key','')).rsplit('/',1)[-1]


def is_final_response(item):
    return 'RESP_FINAL_SFC' in attachment_name(item).upper()


def order_close_attachments(items):
    def key(item):
        technical = item.get('s3_key','') if isinstance(item,dict) else getattr(item,'s3_key','')
        return is_final_response(item), attachment_name(item).casefold(), str(technical)
    return sorted(items,key=key)


def closure_identity(crm):
    # ClosedDate identifies the closing cycle. Legacy callers without it use the
    # response content, never the current time or newly generated PDF bytes.
    cycle = crm.get('ClosedDate') or crm.get('cuerpo_respuesta_final') or ''
    return hashlib.sha256(json.dumps([crm.get('Smart_Code__c'),crm.get('Case_id'),cycle],
                                     default=str,ensure_ascii=False).encode()).hexdigest()
