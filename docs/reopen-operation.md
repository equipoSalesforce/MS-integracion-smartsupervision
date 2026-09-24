# CRM REOPEN metadata

`QuejaUnificadaCrmInput.crm_operation` is optional technical metadata. Its only
non-null accepted value is `REOPEN`; absent/null values are omitted from model
dumps, preserving old queue messages and normal request idempotency hashes.

Only CRM's existing Closed -> In Progress transition caused by M1 emits this
signal, preserving the same Case and Smart Code. Other operations do not emit it.
The unified API's original payload snapshot, Redis opaque JSON and worker model
rehydration retain the marker without changes to queue or idempotency algorithms.

M3 still serializes the validated SFC DTO with `exclude_none=True`. Only the exact
explicit marker adds `fecha_cierre: null` to that output. The marker is not a field
of the SFC DTO and is never forwarded. No inference from null dates, status or
marcacion is allowed. Other nulls remain omitted. The actual SFC key is `marcacion`.

CRM's REOPEN payload produces `anexo_queja=false`,
`documentacion_rta_final=false`, `estado_cod=2`, `fecha_cierre=null`, `marcacion=1`.
No changes to M1/M2/M4, fraud, attachments, normal dates or idempotency logic.

## Verification

`tests/test_reopen_operation.py` covers optional/exact metadata, compatibility,
real mapper/DTO/HTTP JSON serialization using MockTransport, API -> queue -> worker,
completed retries, and an additional real local Redis/Lua round trip.
`scripts/reopen_regression.py` runs the relevant moments/queue/worker/idempotency
regressions while forbidding non-loopback network connections. It expects an
isolated disposable Redis at `127.0.0.1:56379` (test database 15).

Deployment order: SSV API + worker first, then CRM frontend. CI only; existing
configuration, secrets and IAM remain unchanged. No real SFC request in automated
QA. The user performs the real reopening test after technical health checks.
