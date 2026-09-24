"""REOPEN-only evidence: exact contractual primitives, never customer values."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import re

logger = logging.getLogger(__name__)
_current = ContextVar('reopen_diagnostic', default=None)
FIELDS = ('anexo_queja', 'documentacion_rta_final', 'estado_cod', 'fecha_cierre', 'marcacion')


@contextmanager
def reopen_diagnostic(smart_code):
    from app.core.dispatch_observability import _evidence
    diagnostic = {'crm_operation':'REOPEN', 'smart_code_present':bool(smart_code),
                  'update_attempted':False, 'create_fallback_suppressed':False,
                  'sfc_http_status':None, 'provider_error_code':None, 'contract_values':{}}
    token = _current.set(diagnostic)
    evidence = _evidence.get()
    if evidence is not None:
        evidence['enabled'] = True
        evidence['reopen'] = diagnostic
    try:
        yield
    except Exception as error:
        diagnostic['create_fallback_suppressed'] = True
        code = getattr(error, 'error_type', None)
        diagnostic['provider_error_code'] = code if isinstance(code,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}',code) else 'SSV_UPDATE_ERROR'
        raise
    finally:
        logger.info('REOPEN_DIAGNOSTIC', extra={'extra_data':diagnostic})
        _current.reset(token)


def capture_reopen_payload(payload):
    diagnostic = _current.get()
    if diagnostic is not None:
        diagnostic['update_attempted'] = True
        diagnostic['contract_values'] = {key:value for key in FIELDS if key in payload
            for value in [payload[key]] if value is None or type(value) in (bool,int)}


def capture_reopen_http_status(response):
    diagnostic = _current.get()
    if diagnostic is not None and response.request.method == 'PATCH':
        diagnostic['sfc_http_status'] = response.status_code
