# RECLOSE: shared CLOSE pipeline and strict acceptance

## Confirmed failure

The CI attempt on 2026-09-25 at 19:18 UTC for the authorized test Case 3024
did generate and upload `Respuesta_Final_3024_dcd49ae17cdc_REPLICA_RESP_FINAL_SFC.pdf`.
SFC returned HTTP 400. The storage layer classified it as `DUPLICATE_FILE`,
returned `DUPLICATE_OMITTED`, and wrote a per-file checkpoint. Momento 3 ignored
the batch result, wrote `sent=true`, and attempted PATCH. SFC rejected PATCH
with HTTP 400: a final-response document had not been sent.

Correlation: `3972905c-4c25-4d04-a3da-4b8d1c7d56f1`.

Missing `cuerpo_respuesta_final` was **not** the cause of this real failure:
the schema supplies the existing CLOSE default, and the PDF was present in S3.
Both operations already used `ejecutar_cierre_definitivo` ->
`_orquestar_pipeline_momento_3` -> `_generar_y_enviar_pdf_respuesta_final`.
The divergence was acceptance/idempotency: the generic duplicate rule can refer
to the historical final, and changing only the filename does not change the
deterministic default PDF bytes. A historical document is not a replica response.

## Focal correction

- Reuse the existing CLOSE generator, template, body fallback, storage, ordered
  transfer and PATCH. No second RECLOSE pipeline and no artificial CRM body.
- Resolve a final response for missing/null/empty body even for raw-dict callers.
- Keep CLOSE naming and legacy receipt identity unchanged.
- For RECLOSE, keep the cycle-specific `REPLICA_RESP_FINAL_SFC` name and bind
  the PDF to Case/Smart Code/full REOPEN UUID using deterministic PDF metadata.
  No template, form text or customer content is changed. The same bytes are
  reused on retry; a later cycle has distinct bytes and storage identity.
- An existing unconfirmed replica is upgraded in place with that identity;
  its content is not regenerated. The original CLOSE document is never changed.
- Require a successful upload or a strict per-file receipt of that success.
  `DUPLICATE_FILE`, empty batches, foreign documents and old duplicate-only
  checkpoints cannot satisfy RECLOSE.
- Confirm a versioned receipt containing Case, Smart Code, cycle, filename,
  S3 key, SHA-256 and upload acceptance before `estado_cod=4` and
  `documentacion_rta_final=true`. Same-cycle accepted retries only PATCH.
- Emit bounded technical stages, including persisted/accepted reuse. A missing
  PDF becomes `FINAL_RESPONSE_DOCUMENT_NOT_SENT`, never M2 self-healing.

M1/M2/M4/REOPEN/fraud/mapping/authentication/infrastructure are not changed.
The stricter storage behavior is opt-in only for the generated replica final.

## Verification

`tests/test_reclose_pipeline.py` exercises real schema validation, PDF generation,
PDF preservation, transfer ordering and receipts with isolated external systems.
It covers required A-H cases, duplicate rejection, old false-sent recovery,
lost/silent checkpoint writes, missing persisted PDFs and unchanged default text.

Full offline SSV suite: 1,105 tests, no failures/errors. The one discovered
watchdog skip was separately executed against local Redis and passed. Existing
test warnings: FastAPI/Starlette deprecation and unawaited coroutine in the
pre-existing single-flight test mocks. Latest focal/storage/PDF suite: 97 passed.
CRM contract regression: 15 passed; no CRM source change required.

Release script builds exact pushed SSV commits and changes only images of the
existing CI API/worker services. ECR credentials stay in memory over the local
Docker stdio transport. Real validation is limited to authorized Case 3024 and
its existing REOPEN cycle; runtime evidence is recorded separately after deploy.
