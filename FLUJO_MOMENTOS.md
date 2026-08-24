# Flujo de los Momentos SFC (1, 2, 3 y 4)

> **Propósito de este documento:** explicar cómo se mueve una queja entre el CRM (Salesforce) y la
> Superintendencia Financiera de Colombia (SFC) a través de los cuatro "Momentos" regulatorios, y
> documentar explícitamente una decisión de diseño **deliberada** que suele malinterpretarse como bug
> en revisiones automatizadas: la cola centralizada en Redis permite que un evento nuevo **sobrescriba**
> el contenido pendiente de un evento anterior del mismo caso (mismo `Smart_Code__c`), en vez de encolar
> ambos por separado. Ver la sección [¿Por qué la sobrescritura en cola NO es un bug?](#por-qué-la-sobrescritura-en-cola-no-es-un-bug)
> para el detalle completo.

## Índice

- [Resumen de los 4 Momentos](#resumen-de-los-4-momentos)
- [Momento 1 — SFC → CRM (descarga)](#momento-1--sfc--crm-descarga)
- [Momento 2 — CRM → SFC (alta nueva)](#momento-2--crm--sfc-alta-nueva)
- [Momento 3 — CRM → SFC (trámite / fraude / cierre)](#momento-3--crm--sfc-trámite--fraude--cierre)
- [El endpoint único de despacho y el self-healing M2→M3](#el-endpoint-único-de-despacho-y-el-self-healing-m2m3)
- [Momento 4 — Usuarios (consumidores financieros)](#momento-4--usuarios-consumidores-financieros)
- [¿Por qué la sobrescritura en cola NO es un bug?](#por-qué-la-sobrescritura-en-cola-no-es-un-bug)
- [¿Por qué "caso ya cerrado" se trata como éxito?](#por-qué-caso-ya-cerrado-se-trata-como-éxito)
- [Referencias en el código](#referencias-en-el-código)

---

## Resumen de los 4 Momentos

La SFC define 4 "Momentos" (fases regulatorias) para el intercambio de información de quejas/PQRs
entre la entidad vigilada (Global66, vía su CRM) y la Superintendencia:

| Momento | Dirección | Qué hace | ¿Se usa en producción? |
|---|---|---|---|
| **1** | SFC → CRM | Descarga quejas nuevas radicadas directamente en la SFC y sus adjuntos, las crea en el CRM. | Sí — pipeline propio, cron periódico. |
| **2** | CRM → SFC | Alta de una queja completamente nueva, originada en el CRM. | Casi nunca por sí solo — casi todo caso real llega ya con trámite/fraude/cierre, así que el sistema lo resuelve vía self-healing (ver abajo) en vez de una llamada explícita a "Momento 2 puro". |
| **3** | CRM → SFC | Actualiza una queja que ya existe en la SFC: trámite intermedio, reporte de fraude, cierre definitivo (con generación de PDF de respuesta). | **Sí, es el flujo principal.** Casi todo el tráfico real de despacho es Momento 3. |
| **4** | Bidireccional | Sincroniza información de consumidores financieros (usuarios) entre SFC y CRM, con su propio ACK. | Sí — pipeline propio, independiente de 1/2/3. |

Momentos 2 y 3 **no son endpoints separados** desde la perspectiva del CRM: ambos se disparan a través
de un único endpoint de despacho (`POST /api/v1/quejas/sync/despacho`), que infiere automáticamente cuál
de los dos aplica según los campos presentes en el payload. Momento 1 y Momento 4 sí son pipelines/endpoints
independientes.

---

## Momento 1 — SFC → CRM (descarga)

**Código:** `app/services/momento_1_sync.py`, expuesto en `app/api/routes_quejas.py` (`/sync/momento-1`).

1. Pagina sobre `GET /api/queja/` de la SFC (con límite de páginas/tiempo para cortar ante un enlace
   `next` corrupto o un backlog anómalo — ver `SFC_SYNC_MAX_PAGINAS`/`SFC_SYNC_MAX_SEGUNDOS`).
2. Por cada queja recibida, traduce los campos SFC → CRM (`SfcSalesforceMapper.sfc_payload_to_db_dict`,
   contra el catálogo cacheado en RAM) y descarga sus adjuntos (`/api/storage/`) subiéndolos a S3.
3. Retorna el lote mapeado al CRM; el CRM confirma recepción vía `POST /sync/momento-1/ack`
   (`confirmar_recepcion_ack`), que reporta los `Smart_Code__c` procesados de vuelta a la SFC.

No comparte cola/Redis con Momento 2/3 — es un pull síncrono bajo demanda del CRM, sin reintentos
automáticos en segundo plano.

---

## Momento 2 — CRM → SFC (alta nueva)

**Código:** `app/services/momento_2_sync.py` (`Momento2SincronizacionService.ejecutar_envio_momento_2`).

1. Mapea el payload del CRM a la estructura que espera la SFC (`SfcSalesforceMapper.crm_entity_to_sfc_payload`).
2. `POST /api/queja/` para radicar la queja.
   - Si la SFC responde que la queja **ya existe** (`_es_error_queja_ya_existe_m2`, distinta de la regla
     de duplicado funcional por motivo/producto/canal), se trata como éxito idempotente — normalmente
     indica que un intento anterior sí llegó a crear la queja pero la respuesta se perdió por timeout.
3. Si vienen adjuntos (`archivos_s3` o `directorio_s3`), los transmite vía `S3StorageService.transferir_lote_s3_a_sfc`.

En la práctica, Momento 2 casi nunca se dispara como flujo explícito — la mayoría de los casos reales le
llegan al despacho unificado ya con datos de trámite/fraude/cierre, así que terminan resolviéndose por la
ruta de **self-healing** (ver más abajo), que internamente sí llama a este mismo servicio, pero como
recuperación automática dentro de un flujo de Momento 3.

---

## Momento 3 — CRM → SFC (trámite / fraude / cierre)

**Código:** `app/services/momento_3_sync.py` (`Momento3SincronizacionService`), orquestado por
`app/services/despacho_queja_orchestrator.py`.

Es el flujo real de mayor volumen. Cubre tres sub-casos, todos sobre `PATCH /api/queja/{codigo}/`:

- **Actualización de trámite** (`ejecutar_actualizacion_tramite`): cambio de estado intermedio, sin cierre
  ni fraude. Ningún afijo regulatorio especial en los adjuntos.
- **Gestión de fraude** (`ejecutar_gestion_fraude`): exige `tipo_fraude__c`/`modalidad_fraude__c` +
  montos reclamado/devuelto, y **al menos un documento de investigación** (vía `archivos_s3` o
  `directorio_s3` — la validación vive en `QuejaUnificadaCrmInput.validar_reglas_segun_datos_presentes`).
  Los adjuntos se transmiten con el afijo `INV_FRAUDE_SFC`.
- **Cierre definitivo** (`ejecutar_cierre_definitivo`): exige `Favorabilidad__c` + `Aceptacion__c`.
  Genera un PDF de respuesta final (`generar_pdf_respuesta_final`) a partir de `cuerpo_respuesta_final`,
  lo sube a S3 y lo transmite con el afijo `RESP_FINAL_SFC`.

Un caso puede requerir **fraude y cierre a la vez** en un mismo payload — el orquestador
(`_ejecutar_pasos_momento_3`) ejecuta ambos pasos, siempre en ese orden regulatorio: primero fraude,
después cierre.

---

## El endpoint único de despacho y el self-healing M2→M3

**Endpoint:** `POST /api/v1/quejas/sync/despacho` (`despachar_queja_crm` en `routes_quejas.py`).

1. **Verificación de idempotencia** (`IdempotencyService.verificar_o_iniciar_operacion`): si la misma
   operación (mismo `Smart_Code__c` + mismo hash de payload) ya se procesó con éxito, retorna la
   respuesta guardada sin volver a tocar la SFC. Si está en proceso o encolada, responde `202`.
2. **Inferencia de Momento** (`DespachoQuejaOrquestador.procesar_despacho`): según `Status`, `ClosedDate`,
   `Favorabilidad__c`/`Aceptacion__c`, `tipo_fraude__c`/`modalidad_fraude__c`, decide si es Momento 2 puro
   (alta nueva sin trámite ni fraude) o Momento 3 (trámite/fraude/cierre).
3. **Self-healing (auto-recuperación M2→M3):** si al intentar Momento 3 la SFC responde `404`/`NOT_FOUND_ERROR`
   (la queja aún no existe en su base), el orquestador **radica automáticamente la queja base vía Momento 2**
   (sin adjuntos) y luego **reintenta el Momento 3 completo** (con sus adjuntos y afijos normativos). Esto es
   lo que hace que, en la práctica, casi nunca haga falta invocar "Momento 2 puro" explícitamente: cualquier
   caso que llegue con datos de trámite/fraude/cierre para un `Smart_Code__c` que la SFC todavía no conoce
   se resuelve solo.
4. **Contingencia:** si la SFC no responde a tiempo o está caída/regulando cuota (429/5xx/timeout), el caso
   se encola en Redis (`QueueService.encolar_despacho`) y el endpoint responde `202 Accepted` de inmediato.
   El worker (`app/workers/scheduler.py::reintentar_despachos_pendientes_job`) reintenta periódicamente
   los casos encolados.

---

## Momento 4 — Usuarios (consumidores financieros)

**Código:** `app/services/momento_4_sync.py` (`UserSync`).

1. `GET /api/usuarios/info/` paginado (mismo mecanismo de corte por límite de páginas/tiempo que Momento 1),
   deduplicado por `numero_id_CF` dentro del mismo lote.
2. Traduce cada usuario al formato CRM y reporta `failed_items` para los que no pudieron mapearse, sin
   abortar el lote completo por un registro individual corrupto.
3. El CRM confirma recepción vía `POST /sync/momento-4/ack` (`confirmar_recepcion_ack_usuarios`), en lotes
   de máximo 100 `numero_id_CF` por request hacia la SFC.

Es independiente de la cola de Momento 2/3 — no comparte mecanismo de reintento ni idempotencia con el
despacho de quejas.

---

## ¿Por qué la sobrescritura en cola NO es un bug?

**Comportamiento observado:** si un caso (`Smart_Code__c` X) ya tiene un evento pendiente en la cola de
Redis, y llega un evento **nuevo** para ese mismo caso antes de que el anterior termine de procesarse, la
cola **no encola un segundo registro** — sobrescribe el `payload_json` del registro existente y sube su
`version` (`ENQUEUE_LUA_SCRIPT` en `app/services/queue_service.py`, línea ~32). Esto es intencional, no un
gap de deduplicación.

### El mecanismo completo (por qué es seguro)

1. **Un `smart_code` = un solo slot en cola.** El índice `{sfc:queue}:index:{smart_code}` apunta siempre
   al item vigente. Un evento nuevo para el mismo caso reutiliza ese item, no crea uno paralelo.
2. **Cada sobrescritura sube la versión** (`data.version += 1`). Cuando el worker que estaba procesando
   la versión *anterior* termina y llama a `marcar_exitoso`/`marcar_sfc_completado`, esas transiciones
   son atómicas en Lua y **exigen `expected_version`** — si la versión ya no coincide (porque el contenido
   fue sobrescrito mientras tanto), la transición se rechaza (`version_mismatch`) y el item **se queda
   pendiente con el contenido vigente** (el nuevo), no con el que ese worker acababa de enviar. Ver
   `tests/test_queue_race_protection.py::test_overwrite_en_vuelo_no_marca_completed_el_evento_nuevo`.
3. **La SFC es idempotente para las operaciones de Momento 3.** `PATCH /api/queja/{codigo}/`
   (`put_actualizar_queja`) — el endpoint que cubre trámite, fraude y cierre — **no rechaza ni duplica**
   una actualización reenviada con la misma información: responde `200 OK` de nuevo. Confirmado
   operativamente y registrado en memoria del proyecto (`sfc_m3_update_idempotente.md`).

Con esos tres puntos juntos, el peor escenario posible es:

> Worker A está enviando la versión 1 del caso X. Mientras esa llamada está en vuelo, llega un evento
> nuevo que sobrescribe el caso X a versión 2. Worker A termina su envío (con contenido de la versión 1)
> y la SFC lo acepta con `200 OK` — pero al intentar marcarlo `COMPLETED`, el chequeo de versión lo
> rechaza porque el item ya es versión 2. El caso **sigue pendiente**, ahora con el contenido de la
> versión 2, y el próximo ciclo del scheduler lo reintenta — reenviando a la SFC un `PATCH` que, aunque
> se solape parcialmente con lo que Worker A ya envió, es idempotente: la SFC no lo rechaza ni genera un
> estado inconsistente.

No hay pérdida de datos (el contenido más reciente siempre termina llegando), no hay duplicados
regulatorios (la SFC deduplica actualizaciones idénticas por diseño), y no hay condición de carrera
explotable (el `expected_version` impide que un worker "viejo" marque como completo un contenido que ya
no es el vigente).

### Por qué esto es *deseable*, no solo "tolerable"

Sin esta sobrescritura, cada actualización rápida sucesiva del mismo caso (ej. el CRM reenviando el mismo
evento por un reintento de su lado, o dos cambios de estado seguidos antes de que el primero termine de
procesarse) generaría un item de cola independiente — y como cada item se reintenta con su propio backoff,
eso multiplicaría innecesariamente el tráfico hacia la SFC por el mismo caso, sin ningún beneficio: la SFC
de todas formas solo necesita ver el estado *final* del caso, no cada estado intermedio transitorio.

---

## ¿Por qué "caso ya cerrado" se trata como éxito?

**Código:** `_es_error_caso_ya_cerrado` en `app/services/despacho_queja_orchestrator.py`.

Cuando la SFC rechaza una actualización porque el caso **ya está cerrado** (frases como "ya cuenta con un
documento de respuesta final" o "la queja se encuentra con estado cerrado"), el orquestador no lo trata
como una falla:

- Si venía un paso de **fraude seguido de cierre** y el paso de fraude falla con ese error puntual, se
  omite esa falla intermedia y se procede igual con el cierre (el caso ya está donde tiene que estar).
- Si el propio paso de **cierre** recibe ese error, se marca la operación como **éxito** directamente
  (`{"status": "success", "message": "...ya se encuentra cerrado en la SFC (Estado 4)."}`).

Es el mismo principio que la sobrescritura de cola: si dos eventos del mismo caso terminan intentando
cerrarlo dos veces (uno ya lo logró, el segundo llega después), el segundo no debe fallar — el resultado
que le importa al CRM (el caso está cerrado en la SFC) ya se cumplió.

---

## Referencias en el código

| Concepto | Archivo |
|---|---|
| Endpoint único de despacho | `app/api/routes_quejas.py::despachar_queja_crm` |
| Inferencia M2/M3 + self-healing | `app/services/despacho_queja_orchestrator.py` |
| Momento 1 | `app/services/momento_1_sync.py` |
| Momento 2 | `app/services/momento_2_sync.py` |
| Momento 3 (trámite/fraude/cierre) | `app/services/momento_3_sync.py` |
| Momento 4 | `app/services/momento_4_sync.py` |
| Cola centralizada + versión + Lua scripts | `app/services/queue_service.py` |
| Worker de reintentos | `app/workers/scheduler.py::reintentar_despachos_pendientes_job` |
| Idempotencia (hash + store) | `app/services/idempotency_service.py` |
| Test: no-completar contenido sobrescrito | `tests/test_queue_race_protection.py` |
| Test: fraude + cierre simultáneo | `tests/test_despacho_orquestador.py::test_5_despacho_fraude_y_cierre_simultaneo_directo` |
