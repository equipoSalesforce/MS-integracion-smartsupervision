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
    # A real M1 reopening already has a durable CRM operation UUID. It is the
    # replica cycle identity across retries and changes for a future replica.
    reopen_cycle = crm.get('crm_reopen_operation_id')
    if reopen_cycle:
        return str(reopen_cycle)
    # ClosedDate identifies the closing cycle. Legacy callers without it use the
    # response content, never the current time or newly generated PDF bytes.
    cycle = crm.get('ClosedDate') or crm.get('cuerpo_respuesta_final') or ''
    return hashlib.sha256(json.dumps([crm.get('Smart_Code__c'),crm.get('Case_id'),cycle],
                                     default=str,ensure_ascii=False).encode()).hexdigest()


def final_response_filename(case_id, close_id, *, replica=False):
    if not replica:
        return f"Respuesta_Final_{case_id}_RESP_FINAL_SFC.pdf"
    cycle_token = ''.join(char for char in str(close_id) if char.isalnum())[:12]
    return f"Respuesta_Final_{case_id}_{cycle_token}_REPLICA_RESP_FINAL_SFC.pdf"
