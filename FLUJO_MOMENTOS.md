# Flujo de los Momentos SFC (1, 2, 3 y 4)

> **Propósito de este documento:** explicar cómo se mueve una queja entre el CRM (Salesforce) y la
> Superintendencia Financiera de Colombia (SFC) a través de los cuatro "Momentos" regulatorios, y
> documentar explícitamente varias decisiones de diseño **deliberadas** que suelen malinterpretarse
> como bugs en revisiones automatizadas — desde cómo la cola centralizada en Redis permite que un
> evento nuevo **sobrescriba** el contenido pendiente de un evento anterior del mismo caso, hasta
> límites de contrato con el CRM y decisiones de alcance tomadas conscientemente. Ver el índice de
> "Decisiones de diseño deliberadas" más abajo para el detalle de cada una.

## Índice

- [Resumen de los 4 Momentos](#resumen-de-los-4-momentos)
- [Momento 1 — SFC → CRM (descarga)](#momento-1--sfc--crm-descarga)
- [Momento 2 — CRM → SFC (alta nueva)](#momento-2--crm--sfc-alta-nueva)
- [Momento 3 — CRM → SFC (trámite / fraude / cierre)](#momento-3--crm--sfc-trámite--fraude--cierre)
- [El endpoint único de despacho y el self-healing M2→M3](#el-endpoint-único-de-despacho-y-el-self-healing-m2m3)
- [Momento 4 — Usuarios (consumidores financieros)](#momento-4--usuarios-consumidores-financieros)
- [¿Por qué la sobrescritura en cola NO es un bug?](#por-qué-la-sobrescritura-en-cola-no-es-un-bug)
- [¿Por qué &#34;caso ya cerrado&#34; se trata como éxito?](#por-qué-caso-ya-cerrado-se-trata-como-éxito)
- [¿Por qué la clasificación de errores de negocio de la SFC usa coincidencia de texto?](#por-qué-la-clasificación-de-errores-de-negocio-de-la-sfc-usa-coincidencia-de-texto)
- [¿Por qué el webhook al CRM siempre reporta `status: "CREATED"`?](#por-qué-el-webhook-al-crm-siempre-reporta-status-created)
- [¿Por qué la cola de fallidos (DLQ) no tiene endpoint de replay?](#por-qué-la-cola-de-fallidos-dlq-no-tiene-endpoint-de-replay)
- [¿Por qué la cascada de timeouts no llega hasta el ALB?](#por-qué-la-cascada-de-timeouts-no-llega-hasta-el-alb)
- [¿Por qué la deduplicación de alertas por correo es por proceso, no global?](#por-qué-la-deduplicación-de-alertas-por-correo-es-por-proceso-no-global)
- [¿Por qué la validación de ownership de adjuntos en S3 usa Case_id y es estrictamente posicional?](#por-qué-la-validación-de-ownership-de-adjuntos-en-s3-usa-case_id-y-es-estrictamente-posicional)
- [Refresco periódico de catálogos/mapeos (hallazgo C1, corregido de nuevo)](#refresco-periódico-de-catálogosmapeos-hallazgo-c1-corregido-de-nuevo)
- [Lock por caso en el despacho síncrono (hallazgo E)](#lock-por-caso-en-el-despacho-síncrono-hallazgo-e)
- [Dos brechas más encontradas en la misma revisión de concurrencia (2026-08-26)](#dos-brechas-más-encontradas-en-la-misma-revisión-de-concurrencia-2026-08-26)
- [¿Por qué la firma HMAC no es byte-exacta sobre el body real? (confirmado, no es un bug)](#por-qué-la-firma-hmac-no-es-byte-exacta-sobre-el-body-real-confirmado-no-es-un-bug)
- [El healthcheck del contenedor no depende de Redis (confirmado, no es un bug)](#el-healthcheck-del-contenedor-no-depende-de-redis-confirmado-no-es-un-bug)
- [Momento 1 no deduplicaba quejas repetidas entre páginas (corregido)](#momento-1-no-deduplicaba-quejas-repetidas-entre-páginas-corregido)
- [El parser de hilos de correo podía atribuirle al soporte una respuesta del cliente (corregido)](#el-parser-de-hilos-de-correo-podía-atribuirle-al-soporte-una-respuesta-del-cliente-corregido)
- [`SfcErrorTranslator` clasificaba "la queja no existe" como si ya existiera (corregido)](#sfcerrortranslator-clasificaba-la-queja-no-existe-como-si-ya-existiera-corregido)
- [Referencias en el código](#referencias-en-el-código)

---

## Resumen de los 4 Momentos

La SFC define 4 "Momentos" (fases regulatorias) para el intercambio de información de quejas/PQRs
entre la entidad vigilada (Global66, vía su CRM) y la Superintendencia:

| Momento     | Dirección    | Qué hace                                                                                                                                     | ¿Se usa en producción?                                                                                                                                                                               |
| ----------- | ------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **1** | SFC → CRM    | Descarga quejas nuevas radicadas directamente en la SFC y sus adjuntos, las crea en el CRM.                                                   | Sí — pipeline propio, cron periódico.                                                                                                                                                               |
| **2** | CRM → SFC    | Alta de una queja completamente nueva, originada en el CRM.                                                                                   | Casi nunca por sí solo — casi todo caso real llega ya con trámite/fraude/cierre, así que el sistema lo resuelve vía self-healing (ver abajo) en vez de una llamada explícita a "Momento 2 puro". |
| **3** | CRM → SFC    | Actualiza una queja que ya existe en la SFC: trámite intermedio, reporte de fraude, cierre definitivo (con generación de PDF de respuesta). | **Sí, es el flujo principal.** Casi todo el tráfico real de despacho es Momento 3.                                                                                                             |
| **4** | Bidireccional | Sincroniza información de consumidores financieros (usuarios) entre SFC y CRM, con su propio ACK.                                            | Sí — pipeline propio, independiente de 1/2/3.                                                                                                                                                        |

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
   una actualización reenviada con la misma información: responde `200 OK` de nuevo.

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

### ¿Por qué rechazar con 409 en vez de un slot por operación? (decisión de contrato, no cerrada)

**Código:** `QueueService.encolar_despacho` (`QUEUE_OPERATION_CONFLICT`), `app/api/routes_quejas.py`.

El punto 1 de arriba ("un `smart_code` = un slot") es correcto y seguro cuando las dos escrituras en
competencia son la **misma** obligación regulatoria (dos versiones sucesivas de un trámite, por
ejemplo). Pero cuando son obligaciones **distintas** (ej. un fraude y un cierre concurrentes para el
mismo caso), sobrescribir perdería una de las dos en silencio — por eso `ENQUEUE_LUA_SCRIPT` rechaza
esa sobrescritura con `QUEUE_OPERATION_CONFLICT` (409) en vez de pisarla (hallazgo W1/V1, ya
corregido).

**El 409 no resuelve el problema, sólo lo hace visible.** Con la SFC caída y dos obligaciones
pendientes para el mismo caso, el orden de llegada decide cuál entra a la cola y cuál recibe 409 — y
el rechazado puede ser el que tiene plazo regulatorio (el cierre). Mientras el primero no drene (hasta
`QUEUE_MAX_RETRIES` reintentos), cada intento del segundo vuelve a recibir 409. Si el CRM no tiene
lógica para reintentar persistentemente ante un `QUEUE_OPERATION_CONFLICT` específicamente (distinto
de un reintento genérico ante 5xx/503), el resultado neto sigue siendo una obligación no transmitida
— mejor registrada y visible en logs/métricas que con la sobrescritura silenciosa de antes, pero
igual de pendiente.

**La alternativa evaluada:** un slot de cola **por operación**, no por `smart_code`
(`index:{smart_code}:{operacion}` en vez de `index:{smart_code}`) — preservaría ambas obligaciones sin
pedirle nada al CRM, y el `RedisLock` por caso (ver más abajo) ya garantiza que no salgan las dos a la
vez hacia la SFC aunque compartan cola. La invariante pasaría de "un `smart_code` = un slot" a "una
obligación pendiente = un slot".

**Por qué no se implementó todavía:** es un cambio de esquema de cola (impacta `ENQUEUE_LUA_SCRIPT`,
los índices de listado/purga, y el contrato de `ColaItemRedis`), no un fix de una función — más
cercano en alcance a los cambios de diseño de esta sección que a una corrección puntual. Queda como
decisión de contrato pendiente de evaluar, no como bug: el 409 actual es estrictamente mejor que la
sobrescritura silenciosa que reemplazó, y el riesgo que deja abierto (obligación distinta bloqueada
por reintentos de otra) es el mismo tipo de brecha que W5/V9 más abajo — depende de un comportamiento
del CRM (reintento persistente, o un endpoint que reciba "esto falló definitivamente") que este
repositorio no controla.

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
- Si el paso es un **trámite simple** (no cierre, no fraude) el que recibe ese error, se marca como
  **`noop`**, no `success` — ver la corrección de abajo.

Es el mismo principio que la sobrescritura de cola: si dos eventos del mismo caso terminan intentando
cerrarlo dos veces (uno ya lo logró, el segundo llega después), el segundo no debe fallar — el resultado
que le importa al CRM (el caso está cerrado en la SFC) ya se cumplió.

**`success` para el trámite era engañoso (hallazgo de revisión externa, 2026-08-26, ronda 4 —
W5/V7/X7):** `_ejecutar_paso_o_exito_si_ya_cerrado` es compartida por el paso de cierre y el de
trámite (ver "Lock por caso en el despacho síncrono" más abajo), y devolvía el mismo
`{"status": "success", ...}` para ambos. Para el cierre eso es correcto — el estado final deseado
(caso cerrado) ya se cumplió, sea porque este request lo cerró o porque otro lo hizo antes. Para un
**trámite** (actualización de gestor, novedades, etc.) sobre un caso que ya está cerrado, la SFC
rechaza la actualización por completo — nada se aplicó — y reportar `success` le afirma al CRM que
esos campos quedaron sincronizados cuando en realidad no se tocó nada. **Corregido:**
`_ejecutar_paso_o_exito_si_ya_cerrado` ahora recibe `es_cierre: bool` y devuelve `status: "noop"`
para el trámite (`status: "success"` sin cambios para el cierre) — `"noop"` no es un error (nada
que reintentar, el caso seguirá cerrado en el próximo intento) pero tampoco fue una escritura
exitosa. Ver `app/services/despacho_queja_orchestrator.py::_resultado_por_cierre_confirmado`.

---

## ¿Por qué la clasificación de errores de negocio de la SFC usa coincidencia de texto?

**Código:** `SfcErrorTranslator.procesar_y_lanzar` en `app/core/exceptions.py`,
`_es_error_caso_ya_cerrado` en `app/services/despacho_queja_orchestrator.py`.

Ambos clasifican un rechazo de la SFC comparando subcadenas (`errores_sfc.json`, o frases como "ya
cuenta con un documento de respuesta final") contra el cuerpo completo de la respuesta HTTP
(`response_text`) — no sólo contra un código de error estructurado. En una revisión superficial esto
parece un riesgo: si la SFC alguna vez **reflejara** contenido libre del request (por ejemplo el
`Description` de hasta 4500 caracteres que manda el consumidor financiero) dentro del texto de un
error, un texto coincidente por casualidad ("...mi queja se encuentra con estado cerrado...")
podría hacer que el orquestador clasifique un rechazo real como éxito idempotente, o dispare el
self-healing sobre un caso que en realidad no existe.

**Por qué no aplica:** la API de la SFC (documentada en
`docs/Smartsupervision - Doc API Quejas - Momento 4.postman_collection (2) (1).json`, la colección
oficial de la Superintendencia — no la de este repo) **nunca** devuelve contenido libre del request
en sus respuestas de error. Todos los rechazos observados, en los cuatro Momentos, son mensajes
fijos y enumerados, con el nombre del campo como clave y una plantilla como valor:

```json
{
    "status_code": 400,
    "messages": {
        "codigo_pais": ["This field is required."],
        "codigo_queja": ["queja with this codigo queja already exists."],
        "entidad_cod": ["You do not have permission to for this complaint for this company"]
    },
    "detail": "Error APIException"
}
```

o mensajes genéricos sin ningún dato del payload (`"Sign verification failed"`, `"missing header"`,
`"Not found."`, `"messages": []`). La SFC no tiene ningún mecanismo, documentado ni observado, para
reflejar el valor de un campo rechazado — y por extensión, tampoco para incluir texto libre de otro
campo (`Description`, `SuppliedName`, etc.) que no fue el que causó el rechazo. Como el `response_text`
completo está compuesto exclusivamente por estas plantillas fijas, comparar subcadenas contra él es
equivalente, en la práctica, a comparar contra un enum cerrado de mensajes conocidos — no hay ninguna
vía por la que texto del consumidor financiero pueda colarse ahí y alterar la clasificación.

**Qué SÍ seguiría siendo un riesgo real:** que la SFC cambie el fraseo exacto de sus mensajes sin
aviso (documentado ya como limitación aceptada — ver el comentario en `errores_sfc.json` y
`notificar_error_no_mapeado`), o que en el futuro agregue un endpoint/campo que sí eche contenido
del request. Si eso ocurre, esta sección deja de aplicar y la clasificación debe migrar a códigos de
error estructurados de la SFC en vez de coincidencia de texto.

**Matiz importante (hallazgo real, ver sección "clasificaba 'la queja no existe' como si ya
existiera" más abajo):** lo anterior descarta el riesgo de **texto libre inyectado** (`Description`,
`SuppliedName`, etc.) — pero no cubre un riesgo distinto y real: la SFC **sí** puede ecoar el
**nombre de un campo** (`codigo_queja`) dentro de un mensaje de error, vía el patrón estándar de
Django REST Framework (`SlugRelatedField`: `"Object with {campo}={valor} does not exist."`). Una
regla de la matriz basada en ese nombre de campo, sin más contexto, puede coincidir con cualquier
error sobre ese recurso — no sólo con el que la regla pretendía capturar. Ver el fix más abajo.

---

## ¿Por qué el webhook al CRM siempre reporta `status: "CREATED"`?

**Código:** `CrmWebhookService.notificar_resolucion_contingencia` en `app/services/crm_webhook_service.py`.

El payload que se envía al CRM tras resolver un caso desde la cola de contingencia es siempre:

```python
payload = {"case_number": case_id_crm, "smart_code": smart_code, "status": "CREATED"}
```

sin distinguir si lo que realmente ocurrió fue un alta (Momento 2), una actualización de trámite, un
reporte de fraude o un cierre definitivo con respuesta final (los tres últimos, Momento 3).

**Por qué es así:** el webhook es un contrato que define el CRM, no este microservicio, y ese
contrato **no tiene hoy un campo ni una semántica para diferenciar el tipo de operación** — sólo
espera una confirmación de que el `smart_code` en cuestión fue procesado exitosamente por la SFC.
`"CREATED"` no se usa en su sentido literal de "alta nueva"; se usa como la única señal que el CRM
sabe interpretar hoy: *"la última operación que se envió a la SFC para este caso ya se completó"*.
Cambiar este valor a algo más expresivo (`"UPDATED"`, `"FRAUD_REPORTED"`, `"CLOSED"`, etc.) sin que
el CRM tenga lógica para consumirlo no aporta nada y arriesga que un valor inesperado rompa
validación del lado del CRM.

**Qué haría falta para cerrar esto de verdad:** que el equipo de CRM defina y documente un contrato
de webhook con estados diferenciados por tipo de operación, y que este microservicio los adopte.
Hasta que eso exista, éste es el límite real de lo que se puede comunicar — no un descuido.

---

## ¿Por qué la cola de fallidos (DLQ) no tiene endpoint de replay?

**Código:** `QueueService.registrar_fallo` (transición a `FAILED_FINAL` en
`app/services/queue_service.py`), `EmailAlertService.notificar_caso_fallido_definitivo`.

Cuando un caso agota `QUEUE_MAX_RETRIES` reintentos, se mueve a estado `FALLIDO_DEFINITIVO`, se
libera su registro de idempotencia y se envía un correo a operaciones. No existe un endpoint
administrativo que permita reprocesar ese caso, ni una notificación de vuelta al CRM avisando que el
caso quedó sin transmitir — el CRM sólo tiene el `202 Accepted` original de cuando el caso se encoló.

**Por qué NO poder notificar al CRM sigue siendo así:** una notificación de fallo definitivo *hacia
el CRM* (que reabra el caso, lo marque para revisión manual, o dispare un reintento desde su lado)
requiere que el CRM tenga **algo que hacer** con esa señal. Hoy ese contrato no existe: el CRM no
tiene un endpoint que reciba "este caso falló definitivamente", ni un estado en Salesforce pensado
para representarlo. Notificar con un valor que el CRM no sabe interpretar no resuelve el problema
real, que es de coordinación entre equipos, no de código faltante en este microservicio.

**Corregido (hallazgo C2, revisión externa v5):** lo anterior es distinto de tener una forma de
*recuperar* el caso -- eso sí era una brecha real de este microservicio, sin ninguna dependencia del
CRM. Antes de esta ronda, la única señal de un caso perdido era el correo a operaciones
(`notificar_caso_fallido_definitivo`), y no existía ninguna forma de recuperarlo salvo manipular
Redis a mano. Se agregó `POST /api/v1/quejas/queue/{registro_id}/reencolar` (tooling administrativo
interno, protegido con `verificar_api_key_admin`, mismo patrón que `GET /queue`): reencola
manualmente el item -- reinicia `intentos` a 0, lo mueve de vuelta a `PENDIENTE`, y reconstruye su
registro `QUEUED` en el idempotency store para que el scheduler lo recoja en el próximo ciclo. Se
niega con 409 si el item no está en `FALLIDO_DEFINITIVO`, o si ya existe un item **más nuevo**
pendiente para el mismo `Smart_Code__c` -- reencolar el viejo en ese caso rompería la invariante de
"un `smart_code` = un slot en cola" de la que depende la sobrescritura por versión y la cancelación
consciente de operación de N1. Ver `app/services/queue_service.py::reencolar_item_fallido`.

**Lo que sigue sin poder cerrarse (y sí depende del CRM):** el CRM sigue sin saber que el caso falló
y fue reencolado -- sólo lo sabe operaciones, a través del mismo correo de siempre. Cerrar eso de
verdad requiere el mismo contrato ausente descrito arriba.

**Corregido (hallazgo N1, revisión externa v5, 2026-08-25):** hasta esta ronda, `QueueService. cancelar_pendiente_por_smart_code` (invocado tras un despacho síncrono exitoso, ver más arriba)
podía descartar de la cola un evento que **nunca había llegado a intentarse siquiera** — no un
fallo definitivo, directamente lo borraba sin que pasara por la DLQ ni generara ningún correo,
porque el endpoint de despacho es unificado y ese método cancelaba cualquier item pendiente del
mismo `smart_code` sin mirar si era la misma operación (ej. un trámite exitoso podía borrar un
reporte de fraude que seguía genuinamente pendiente de transmitir). Ahora sólo cancela si el item
pendiente es la misma categoría de operación que el despacho que acaba de tener éxito -- ver
`app/services/queue_service.py::cancelar_pendiente_por_smart_code` y
`tests/test_queue_cancelar_pendiente_tras_exito_sincrono.py::TestCancelarPendienteRespetaCategoriaDeOperacion`.

**El mismo hallazgo tenía una brecha simétrica sin corregir en `encolar_despacho` (hallazgo de
revisión, 2026-08-26):** N1 hizo consciente de la operación a la *cancelación*, pero no a la
*sobrescritura* -- `ENQUEUE_LUA_SCRIPT` seguía reemplazando el `payload_json` de un item pendiente
incondicionalmente, sin mirar si el evento entrante era la misma obligación regulatoria que el
contenido ya encolado. Reproducido contra Redis real: un reporte de **fraude** encolado (con
`tipo_fraude__c` y evidencia en `archivos_s3`) quedaba completamente reemplazado y perdido cuando
un **trámite** del mismo `smart_code` también fallaba y se encolaba después -- el trámite no traía
ninguno de esos campos, así que se perdían para siempre bajo el mismo `item_id`.

**Corregido:** `ENQUEUE_LUA_SCRIPT` ahora guarda `operacion` (calculada en Python vía
`IdempotencyService.infer_operation_type`, la misma fuente que ya usa
`cancelar_pendiente_por_smart_code`) junto con cada item, y **rechaza la sobrescritura** -- sin
tocar el contenido ya encolado -- si la operación entrante difiere de la ya encolada.
`encolar_despacho` traduce ese rechazo en `SfcIntegrationException(status_code=409,
error_type="QUEUE_OPERATION_CONFLICT")`; el endpoint le responde al CRM con un `409` explícito
("reintente esta operación más tarde") en vez de un `202` que ocultaría la pérdida. Un item
encolado *antes* de este fix (sin el campo `operacion`) se trata como compatible y sí permite la
sobrescritura, para no romper items ya en vuelo al desplegar el cambio -- mismo criterio que
`IdempotencyService._item_de_cola_sigue_vigente`. Ver
`app/services/queue_service.py::encolar_despacho`, `ENQUEUE_LUA_SCRIPT`, y
`tests/test_queue_encolar_respeta_categoria_de_operacion.py`.

**Autocorrección el mismo día (auditoría de los propios fixes):** la primera versión de este fix
reutilizaba `EmailAlertService.notificar_falla_infraestructura` para alertar el conflicto -- pero
esa función tiene el asunto y el cuerpo **hardcodeados** en torno a "SFC Caída" y "Acción Tomada:
Caso encolado automáticamente", ambos **falsos** aquí: un conflicto de operación no tiene relación
alguna con que la SFC o Redis estén caídos, y el caso justamente **no** se encoló (se rechazó).
Operaciones investigando esa alerta habría visto "SFC caída" para un problema que en realidad era
dos categorías de operación compitiendo por el mismo slot de cola. Se agregó
`EmailAlertService.notificar_conflicto_operacion_cola`, con asunto/cuerpo que describen lo que
realmente ocurrió, deduplicada por `smart_code` (a diferencia de las alertas de infraestructura,
que son globales por categoría -- un conflicto de cola sí es específico de un caso). Ver
`tests/test_email_service.py::test_conflicto_operacion_cola` y
`tests/test_queue_encolar_respeta_categoria_de_operacion.py::
test_conflicto_alerta_con_la_funcion_correcta_no_la_de_sfc_caida`.

---

## ¿Por qué la cascada de timeouts no llega hasta el ALB?

**Código:** `infrastructure/Dockerfile` (`gunicorn --timeout 330 --graceful-timeout 60`),
`SFC_SYNC_MAX_SEGUNDOS` en `app/core/config.py`.

**Aclaración (2026-08-26, para no repetir la confusión de una revisión anterior):**
`infrastructure/nginx.conf` (`proxy_read_timeout 60s`) **no forma parte del despliegue real** — sólo
se usa en `docker-compose.nginx.test.yml`, un compose dedicado a simular localmente el comportamiento
de un reverse proxy delante de la API. El contenedor `final-api` real (el que arma
`infrastructure/Dockerfile` y el que despliega `infrastructure/ecs-task-def.json.tpl`) corre gunicorn
escuchando **directo** en el puerto 8000 (`EXPOSE 8000`, `CMD gunicorn ...`), sin nginx de por medio
— `ecs-task-def.json.tpl` y `scripts/render_task_def.py` no mencionan nginx en ningún lado. En
producción, el único componente delante de gunicorn es el **ALB compartido con CRM** (fuera de este
repositorio, gestionado por el IaC central del equipo de infraestructura — ver el comentario de
`.github/workflows/deploy-aws.yml` sobre el smoke post-deploy). Una versión anterior de este
documento hablaba de "nginx/ALB" como si ambos fueran hops reales de producción; sólo el ALB lo es.

El presupuesto de tiempo de Momento 1 (`SFC_SYNC_MAX_SEGUNDOS`, 300s) necesita que gunicorn no mate
al worker a mitad de un despacho — por eso se subió `--timeout` de 120s a 330s. Pero el idle timeout
del listener del ALB (fuera de este repositorio) no se tocó: un request que de verdad tarde más de lo
que el ALB tolera sigue devolviendo un timeout al CRM aunque gunicorn continúe procesándolo de fondo
hasta los 330s.

**Por qué se dejó así, deliberadamente, en esta ronda:** la corrección completa de este hallazgo
tiene dos caminos — (a) coordinar con el equipo de infraestructura para subir el idle timeout del ALB
en la misma proporción (un cambio que este repositorio no puede hacer unilateralmente, al vivir en el
IaC central compartido con CRM), o (b) sacar Momento 1 del camino síncrono por completo (convertirlo
en un job de background con su propio mecanismo de polling/ACK, en vez de una llamada HTTP que se
mantiene abierta mientras dura toda la paginación contra la SFC). La opción (b) es la solución de
fondo, pero es un cambio de arquitectura no trivial (persistir quejas obtenidas-pero-no-confirmadas +
cursor de paginación resumible en Redis, preservando el contrato síncrono actual con el CRM) que se
evaluó y se decidió explícitamente **posponer** — no es necesario para el problema inmediato, que era
evitar que gunicorn matara el worker a mitad de un despacho de Momento 3 normal (el caso de uso
dominante, sin relación con la paginación larga de Momento 1). Subir sólo `--timeout` de gunicorn
resuelve ese caso dominante sin tocar arquitectura.

**Qué sigue pendiente, sin resolver, y por qué no es un cambio unilateral de este repositorio:** para
que Momento 1 realmente aproveche los 330s de presupuesto sin que el cliente (CRM) se desconecte
antes por su cuenta, hace falta (a) o (b) — y (a) depende del equipo dueño del ALB compartido, no de
este microservicio. Mientras tanto, el riesgo señalado por la auditoría es real pero acotado: un
request colgado retiene uno de los dos workers de gunicorn durante más tiempo que antes (5.5 min en
vez de 2), lo cual es un costo aceptado a cambio de que Momento 3 no se corte a mitad de un despacho
normal.

---

## ¿Por qué la deduplicación de alertas por correo es por proceso, no global?

**Código:** `EmailAlertService._ULTIMO_ENVIO_POR_CLAVE`, `_deberia_enviar` en `app/services/email_service.py`.

La deduplicación de correos de alerta (una alerta por clave cada 15 minutos, en vez de una por
request) se implementa con un diccionario en memoria a nivel de clase. Con `WEB_CONCURRENCY=2` y N
tareas de ECS corriendo en paralelo, esto significa que una caída de infraestructura puede generar
hasta `2 × N` correos por ventana de 15 minutos, no exactamente uno — cada proceso gunicorn (y cada
tarea) lleva su propia cuenta, sin coordinación entre sí.

**Por qué se implementó así, deliberadamente:** el objetivo del mecanismo es evitar la tormenta de
"un correo por request" (potencialmente miles durante un incidente), no garantizar una cota exacta
de exactamente un correo por evento en todo el sistema. `2 × N` correos en 15 minutos sigue siendo
una mejora de varios órdenes de magnitud sobre el problema original, y es información igual de
accionable para operaciones (siguen siendo pocos correos, no miles). La alternativa — deduplicación
centralizada en Redis — agregaría una dependencia de Redis a un mecanismo de alertas que
deliberadamente debe seguir funcionando **incluso cuando Redis está caído** (es, de hecho, el
escenario más común que dispara estas alertas): atar el propio alerting a la disponibilidad de Redis
sería introducir el mismo antipatrón que ya se corrigió en otras partes del sistema (ver la
sección de "caída de infraestructura no debe amplificar el incidente").

**Ya corregido (cota de tamaño):** el diccionario sí llegó a no tener cota de tamaño
(`error_no_mapeado` incorpora `sfc_field`, cuya cardinalidad depende de las claves que la SFC use en
su JSON de error) — se resolvió con una purga best-effort de entradas expiradas más un tope duro
(`MAX_ENTRADAS_DEDUP = 500`) en `_deberia_enviar` (hallazgo N7, revisión externa v5, 2026-08-25).

**Ya corregido (clave global demasiado ancha):** la clave `"falla_infraestructura"` era compartida
por los nueve call sites de `notificar_falla_infraestructura` (`routes_quejas.py`,
`idempotency_service.py`, `momento_1_sync.py`, `momento_4_sync.py`, `queue_service.py`,
`scheduler.py`, `worker.py`), cubriendo desde el fail-closed de Redis hasta la caída de la SFC en
distintos Momentos y el healthcheck del worker — durante la misma ventana de 15 minutos, el primero
en dispararse silenciaba a todos los demás, incluido el mensaje de "riesgo de duplicado" tras una
persistencia post-SFC fallida, el más accionable de todos. Se agregó un parámetro `categoria`
obligatorio (`redis_no_disponible`, `sfc_caida_contingencia`, `riesgo_duplicado_post_sfc`,
`fallo_doble_sfc_y_redis`, `paginacion_m1_cortada`, `paginacion_m4_cortada`,
`worker_redis_healthcheck`), y la clave de dedup pasó a ser `f"falla_infraestructura:{categoria}"` —
cada tipo de incidente tiene ahora su propia ventana de 15 minutos, independiente de los demás. Sigue
siendo global por categoría (no por `smart_code`): una caída de infraestructura del mismo tipo sigue
siendo un solo evento, no N eventos independientes por cada caso que la sufre.

**Ya corregido (dos notificadores sin ventana de dedup, hallazgo de revisión, 2026-08-26):**
`notificar_casos_vencimiento_sla` y `notificar_catalogo_stale` no tenían `clave_dedup` en absoluto --
se reenviaban en cada ciclo del job que los dispara (cada `QUEUE_RETRY_INTERVAL_MINUTES`/
`CACHE_TTL_SEGUNDOS`) mientras persistiera la condición. Corregido con el mismo patrón de arriba.

**Ya corregido (un tercer notificador con el mismo problema, barrido de verificación, 2026-08-26):**
`notificar_recuperacion_sfc` tampoco tenía `clave_dedup`. A diferencia de
`notificar_caso_fallido_definitivo` (dispara una vez por transición de estado genuina a
`FALLIDO_DEFINITIVO`, que no puede repetirse para el mismo fallo), "la cola llegó a cero después de
despachar algo" no es necesariamente un evento único: con la SFC intermitente (fallando a ratos, no
caída del todo), la cola de contingencia puede vaciarse y volver a llenarse en ciclos sucesivos de
`_notificar_autorrecuperacion_si_aplica` (scheduler.py, cada `QUEUE_RETRY_INTERVAL_MINUTES`),
reenviando el correo de "recuperación" repetidamente mientras la situación sigue inestable -- justo
el escenario que menos necesita ruido. Se agregó `clave_dedup="recuperacion_sfc"`.

**Nota (hallazgo de revisión externa, 2026-08-26, ronda 4 — X8):** `notificar_conflicto_operacion_cola`
(clave `conflicto_operacion_cola:{smart_code}`, granularidad correcta) tiene la misma limitación de
`2 × N` por ventana ya descrita arriba -- pero a diferencia de las demás alertas de esta sección, su
disparador no es un incidente de infraestructura sino tráfico normal del CRM (dos operaciones
distintas para el mismo caso llegando en la misma ventana de reintento). Vale la pena tenerlo
presente: un caso con tráfico conflictivo sostenido puede generar más correos de este tipo que los
de infraestructura, sin que eso implique una caída real de nada.

---

## ¿Por qué la validación de ownership de adjuntos en S3 usa `Case_id` y es estrictamente posicional?

**Código:** `S3StorageService._validar_ownership_key` y `_validar_prefijo_pertenece_al_caso` en
`app/services/s3_service.py`.

Cuando el CRM despacha una queja con adjuntos (`archivos_s3` o `directorio_s3`), este microservicio
valida que la `s3_key` (o el prefijo del directorio) referenciada realmente pertenezca al caso que
se está procesando, comparándola contra `Case_id` — exigiendo que sea exactamente el penúltimo
segmento de la ruta para un archivo individual (`caso/{Case_id}/archivo.pdf`), o el último segmento
para un prefijo de directorio (`caso/{Case_id}/`). Esto tiene dos particularidades que conviene
explicar juntas, porque comparten la misma raíz.

### Por qué se compara contra `Case_id` y no contra `Smart_Code__c`

`Smart_Code__c` es el identificador que este microservicio usa como clave primaria en casi todo lo
demás (idempotencia, índice de cola, métricas) — pero **no es intercambiable con `Case_id` en
todos los flujos**. En particular, los casos recuperados por Momento 1 (SFC → CRM) pueden tener un
código interno de la SFC que **no coincide** con el `Smart_Code__c` que termina usando este
microservicio (el schema puede derivar/generar uno nuevo cuando sólo llega `Case_id`). Es decir,
`Smart_Code__c` no es necesariamente estable frente al identificador que el CRM conoce como "este
caso".

Sin embargo, **para el tráfico en la dirección CRM → microservicio — que es exactamente el que trae
adjuntos y dispara esta validación — el CRM siempre usa `Case_id`** como identificador del caso, sin
ambigüedad. Por eso la validación de ownership se ancla a `Case_id`: es, en esta dirección
específica, el identificador estable y no derivado — al revés de lo que podría parecer si se mirara
sólo el resto del sistema, donde `Smart_Code__c` es el protagonista.

### Por qué no hay (ni puede haber) una verificación más fuerte que comparar campos del mismo payload

Esta validación compara dos campos que llegan en la **misma petición**, del **mismo emisor** (el
CRM, único consumidor de esta API vía una sola `CRM_API_KEY` compartida): `Case_id` contra la
`s3_key`. No existe ninguna llamada al CRM ni acceso a una base de datos compartida desde la que
este microservicio pueda confirmar independientemente "¿este archivo realmente pertenece a este
caso?" — no hay ese oráculo externo. Por diseño, entonces, esta validación **no puede ser una
prueba criptográfica de propiedad**; es, como máximo, un chequeo de formato/consistencia que agarra
una inconsistencia accidental entre `Case_id` y la `s3_key` dentro de la misma petición (un bug del
lado del CRM al construir el payload), no un mecanismo que pueda detener a alguien que ya tiene la
`CRM_API_KEY` y construye el payload a propósito para que ambos campos "calcen" entre sí.

El formato mismo de la `s3_key` (`caso/{Case_id}/archivo.pdf`, `quejas/{Case_id}/archivo.pdf`, o
`{Case_id}/archivo.pdf` sin prefijo) **no lo define ni lo controla este microservicio** — es una
convención acordada con y gestionada por el equipo de CRM, que es quien sube los archivos a S3 y
quien construye `archivos_s3`/`directorio_s3` en el payload. Este microservicio sólo puede validar
que la petición sea *internamente consistente* con esa convención acordada; la responsabilidad de
que esos valores realmente correspondan al caso que se está despachando es, en última instancia,
del CRM.

**Qué haría falta para cerrar esto de verdad (mismo patrón que C2/C4 más abajo):** una prueba real de
propiedad requiere una fuente de verdad *fuera* de la petición misma — por ejemplo, que el CRM
exponga un endpoint (o este microservicio tenga acceso de sólo lectura a su base) donde consultar
"¿qué archivos pertenecen al caso `Case_id` X?" antes de transmitirlos a la SFC, y comparar contra
eso en vez de contra el propio payload. Eso es una integración nueva del lado del CRM, no un cambio
que este microservicio pueda hacer unilateralmente — no existe hoy, y sin ella cualquier "mejora" a
esta validación sigue siendo, en el mejor de los casos, un chequeo de formato/consistencia, nunca una
prueba de propiedad real.

### Por qué la regla es estrictamente posicional (y no acepta subcarpetas)

`_validar_ownership_key` exige que `Case_id` sea exactamente el penúltimo segmento de la ruta —
ningún archivo dentro de una subcarpeta de la carpeta del caso (ej.
`caso/{Case_id}/anexos/archivo.pdf`) pasa la validación hoy. Esto se evaluó explícitamente como una
posible mejora (aceptar `Case_id` en cualquier posición previa al nombre de archivo) y se descartó:
esa regla más laxa reabre exactamente el problema que esta validación existe para cerrar — si las
keys reales comparten un prefijo literal fijo (`caso/`, `quejas/`), un `Case_id` igual a ese literal
compartido (`"caso"`) volvería a matchear como si fuera un caso legítimo, para archivos de
**cualquier** caso. Sin un ancla verificable externamente (ver el punto anterior), no hay forma de
distinguir "esto es realmente la carpeta del caso" de "esto coincide con un segmento compartido por
casualidad" salvo fijando la posición exacta.

**Esto no es un bug pendiente de corregir, es una restricción aceptada:** se confirmó que ninguna de
las dos rutas de escritura de este microservicio a S3 genera subcarpetas
(`momento_3_sync.py::s3_key = f"caso/{case_id}/{final_pdf_name}"`,
`s3_service.py::s3_key = f"quejas/{codigo_queja}/{filename}"`, ambas planas), no hay ninguna
restricción de formato en el schema que sugiera que el CRM las use, y no hay cobertura de test para
ese caso. Si en el futuro el CRM necesita subcarpetas, la forma correcta de resolverlo es acordar con
el equipo de CRM una convención más estricta (ej. que `Case_id` sea siempre el primer segmento, sin
prefijos compartidos ambiguos) — no relajar unilateralmente esta regla desde el lado del
microservicio.

---

## Refresco periódico de catálogos/mapeos (hallazgo C1, corregido de nuevo)

`SfcSalesforceMapper.obtener_catalogos_y_mapeos()` sincroniza contra Google
Sheets los catálogos/mapeos que usan tanto la validación de payloads
(`crm_payloads.py`) como el mapeo hacia/desde la SFC en los cuatro Momentos.
Ese método siempre tuvo su propio TTL (`CACHE_TTL_SEGUNDOS`, 10 min) y un
lock single-flight para evitar estampidas -- pero **nada lo volvía a invocar
después del arranque**: tanto `main.py` como `worker.py` lo llaman una única
vez, al iniciar el proceso. Un cambio en el catálogo de Google Sheets no se
reflejaba hasta el siguiente despliegue/reinicio del contenedor.

**Corregido (hallazgo C1, revisión externa v5):** se agregó
`refrescar_catalogos_job`, un job periódico de APScheduler (mismo intervalo
que `CACHE_TTL_SEGUNDOS`, con el mismo patrón de lock de Redis que
`purgar_cola_job`) que simplemente vuelve a invocar
`obtener_catalogos_y_mapeos()`.

**Esa primera corrección nunca llegó realmente a la API en producción
(hallazgo de revisión, 2026-08-26):** el job vivía dentro de
`iniciar_scheduler()`, que sólo se ejecuta cuando `RUN_SCHEDULER` está
activo -- y `scripts/render_task_def.py` (el renderer real de la Task
Definition de ECS) fija `run_scheduler = "False"` **incondicionalmente** para
`service_type == "api"`, sin ninguna variable de entorno de por medio (ver
línea ~116). En producción, el job **nunca corría en la API**, sólo en el
worker (`worker.py` fuerza `settings.RUN_SCHEDULER = True` sin importar el
env var). Peor aún: aunque se hubiera hecho correr el job en la API, su lock
(`RedisLock` de un solo ganador global, igual que `purgar_cola_job`) habría
dejado a todas las réplicas MENOS una sin refrescar -- `CATALOGOS` es un
atributo de clase que vive en la memoria de **cada proceso por separado**, no
un valor compartido; un lock de "un solo ganador en todo el fleet" es
exactamente lo opuesto de lo que hace falta acá. El resultado real: los
catálogos de cada réplica de la API quedaban congelados en lo que cargó al
arrancar, indefinidamente, mientras el worker sí refrescaba cada 10 minutos
-- la misma divergencia entre catálogos de API y worker que C1 debía cerrar,
pero peor, porque ahora había un mecanismo que aparentaba estar resuelto.

**Corregido de nuevo:** se retiró `refrescar_catalogos_job` del scheduler por
completo -- el refresco periódico de catálogos ya no depende de
`RUN_SCHEDULER` ni de ningún lock cross-proceso. Se agregó
`SfcSalesforceMapper.iniciar_refresco_periodico()`/`detener_refresco_
periodico()`, un loop de `asyncio.create_task` en segundo plano que cada
proceso (cada réplica de la API vía `main.py`, y el worker vía `worker.py`)
arranca **incondicionalmente** al iniciar y detiene ordenadamente al apagar
-- sin ningún lock distribuido: la protección contra llamadas redundantes ya
la da `obtener_catalogos_y_mapeos()` internamente (TTL fast-path +
`asyncio.Lock` single-flight, pero **process-local**, que es exactamente el
alcance correcto acá). Ver `app/core/mapping.py::iniciar_refresco_periodico`,
`tests/test_mapping_refresco_periodico.py`.

## Lock por caso en el despacho síncrono (hallazgo E)

**Código:** `app/api/routes_quejas.py::despachar_queja_crm`, `app/core/distributed_lock.py::RedisLock`.

La verificación de idempotencia (`IdempotencyService.verificar_o_iniciar_operacion`)
deduplica por `smart_code:operacion:payload_hash` -- protege contra el mismo
payload reenviado, pero **dos payloads distintos para el mismo `Smart_Code__c`**
(ej. un trámite y un cierre que le llegan al endpoint casi al mismo tiempo)
generan claves distintas y no se bloquean entre sí. Sin ninguna serialización de
por medio, ambos podían llamar a la SFC en paralelo, sin ningún orden
garantizado -- el mismo caso podía terminar en un estado distinto según cuál de
los dos ganara la carrera de red.

**Corregido (hallazgo E, revisión externa v5):** se agregó un lock distribuido
por `Smart_Code__c` (`RedisLock`, el mismo mecanismo -- SETNX + heartbeat +
release atómico vía Lua -- que ya protegía a los jobs periódicos del scheduler,
extraído a `app/core/distributed_lock.py` para poder reutilizarlo aquí) alrededor
de la llamada síncrona al orquestador. Si el lock ya está tomado por otra
operación del mismo caso, el evento **se encola** en vez de competir --
reutiliza la misma `_encolar_despacho_por_contingencia` que ya maneja el camino
de contingencia (SFC caída/lenta), así que hereda toda su maquinaria ya
endurecida: un slot por `smart_code`, sobrescritura por versión, y la
cancelación consciente de categoría de operación del hallazgo N1.

**De paso, se cerró una asimetría relacionada:** el paso de cierre y el de
fraude ya trataban "la SFC dice que el caso ya está cerrado" como éxito
idempotente (ver la sección de "caso ya cerrado" más arriba), pero el paso de
**trámite simple** no tenía ese mismo tratamiento -- si un trámite perdía la
carrera contra un cierre concurrente del mismo caso (o simplemente llegaba
tarde sobre un caso ya cerrado), la SFC lo rechazaba y ese rechazo se propagaba
al CRM como un error real, en vez de absorberse como no-op igual que los otros
dos pasos. Ahora los tres pasos comparten el mismo helper
(`_ejecutar_paso_o_exito_si_ya_cerrado` en `despacho_queja_orchestrator.py`).

**El lock también faltaba en el camino del worker (encontrado en la revisión de
concurrencia del 2026-08-26):** lo de arriba sólo protegía el despacho SÍNCRONO.
El ciclo de reintentos del worker (`scheduler.py::reintentar_despachos_pendientes_job`)
llamaba a `orquestador.procesar_despacho_raw_json` directamente para cada item
reclamado de la cola, sin adquirir ningún lock por caso -- un request síncrono
nuevo del mismo `Smart_Code__c` podía correr en paralelo con un reintento en
background del mismo caso, exactamente la carrera que este hallazgo se suponía
que cerraba, por una puerta que no cubría originalmente. Se corrigió agregando
`scheduler.py::_reclamar_y_procesar_si_lock_disponible`, que adquiere el mismo
`RedisLock` (misma llave, `DESPACHO_LOCK_PREFIX` -- movido a `queue_service.py`
para que tanto `routes_quejas.py` como `scheduler.py` lo importen sin crear un
cruce de capas) **antes** de reclamar el item. Si está ocupado, el item se deja
**sin reclamar** para el siguiente ciclo -- no hace falta "diferirlo"
explícitamente, ya que no reclamarlo lo deja pendiente con su
`proximo_reintento_at` intacto.

**`RedisLock.acquired` no se apagaba al perder el lease (hallazgo de revisión,
2026-08-26, menor):** `_heartbeat_loop` detectaba correctamente la pérdida del
candado (el script de extensión devolviendo algo distinto de `1`) y cortaba el
loop con `break`, pero nunca ponía `self.acquired = False` -- cualquier código
que consultara `.acquired` como señal de "sigo teniendo exclusividad" recibía
un falso positivo. Hoy ningún caller real vuelve a chequear `.acquired` tras
adquirirlo (todos hacen `if not await lock.acquire(): ...` una sola vez y
listo), así que el impacto activo era bajo -- pero es un invariante roto del
propio objeto, y un test con nombre engañoso
(`test_heartbeat_detecta_perdida_del_lock_y_deja_de_estar_acquired`) que nunca
verificaba lo que su nombre prometía. Corregido: se apaga `acquired` en el
punto donde Redis confirma la pérdida (no en el `except` de errores
transitorios de conexión, donde no se sabe si el lock sigue vigente). Como
consecuencia, `release()` ya no intenta el CAD contra Redis si el heartbeat ya
detectó que el lock se perdió. Ver `app/core/distributed_lock.py::_heartbeat_loop`,
`tests/test_distributed_lock_edge_cases.py`.

**`RedisLock.acquire()` colapsaba "ocupado" y "Redis falló" en el mismo `False` (hallazgo de
revisión, 2026-08-26):** cuando `acquire()` devolvía `False`, los dos call sites
(`routes_quejas.py::despachar_queja_crm`, `scheduler.py::_reclamar_y_procesar_si_lock_disponible`)
asumían incondicionalmente "otra operación en curso" -- pero `acquire()` también devuelve `False`
cuando Redis lanza una excepción al intentar el `SET NX` (timeout, conexión caída) o cuando el
cliente ni siquiera está inicializado. Con Redis genuinamente degradado, ambos mensajes/categorías
de métrica afirmaban una causa falsa justo durante el incidente de infraestructura que más necesita
un diagnóstico certero. **Corregido:** `RedisLock` expone `redis_error: bool` (se resetea al inicio
de cada `acquire()`, sólo queda en `True` si la llamada a Redis lanzó o el cliente es `None`); ambos
call sites lo consultan para elegir el mensaje/categoría correctos
(`DESPACHO_LOCK_REDIS_ERROR` vs. `CONCURRENT_DISPATCH_LOCKED`). Ver
`app/core/distributed_lock.py::acquire`, `app/api/routes_quejas.py::_motivo_lock_no_adquirido`,
`tests/test_distributed_lock_edge_cases.py::TestRedisLockDistingueOcupadoDeRedisFallo`.

## Dos brechas más encontradas en la misma revisión de concurrencia (2026-08-26)

**1. Webhook duplicado si Redis falla justo después de notificar al CRM.** En
`scheduler.py::_ejecutar_paso_notificacion_crm`, si el webhook al CRM **ya tuvo
éxito** pero `marcar_exitoso` (el registro final en Redis) fallaba, no se
alertaba ni se consumía presupuesto de reintentos -- a diferencia del escenario
hermano (`marcar_sfc_completado` fallando en `_ejecutar_paso_sfc`), que sí
dispara `EmailAlertService.notificar_falla_infraestructura(categoria= "riesgo_duplicado_post_sfc")`. El item quedaba `sfc_completado=True` y
pendiente para siempre: el próximo ciclo saltaba la SFC (ya hecha) y reintentaba
el webhook -- ya exitoso -- reenviando una notificación duplicada al CRM en cada
ciclo, indefinidamente, sin ninguna señal visible salvo un log crítico.
Corregido para alertar con la misma categoría que el escenario hermano.

**2. `diferir_pendientes_por_caida_sfc` no validaba versión.** A diferencia de
`marcar_exitoso`/`marcar_sfc_completado`/`registrar_fallo`, este script no
exigía `expected_version`. Si un evento nuevo del mismo `smart_code`
sobrescribía el item entre el listado del lote (en Python, cuando se decide
diferir) y la ejecución de este script, el reintento programado y el
`ultimo_error` del contenido **nuevo** quedaban pisados con los de la decisión
de infraestructura que en realidad era sobre el contenido **viejo** -- sin
pérdida de datos, pero con un retraso injustificado y un mensaje de error
engañoso. Corregido exigiendo `expected_version` igual que los demás scripts de
transición; ahora recibe los items completos (`registros`, no sólo sus ids)
para poder validarla.

## ¿Por qué la firma HMAC no es byte-exacta sobre el body real? (confirmado, no es un bug)

**Código:** `app/core/security/signatures.py::PayloadSignatureStrategy`,
`app/core/auth.py::_preparar_headers_y_firma`.

`sfc_client.py` construye todas sus peticiones con `client.post(url, json=payload, ...)`, y httpx serializa ese `json=` con separadores **compactos** (`,`/`:`, sin
espacios). Pero la firma que va en `X-SFC-Signature` no se calcula sobre esos bytes
compactos: `_preparar_headers_y_firma` decodifica `request.content` con `json.loads`
y se lo pasa a `PayloadSignatureStrategy.sign()`, que vuelve a serializar con los
separadores **por defecto** de Python (con espacios). El HMAC firma esa
re-serialización, no el body que efectivamente sale por la red -- confirmado
empíricamente (ver `tests/test_auth_flow_interceptor.py:: test_firma_no_es_byte_exacta_sobre_el_body_realmente_enviado`).

**Por qué esto es correcto, no un bug:** `docs/SignatureGenerator_comment (1).txt`
es el script de referencia que la propia SFC entrega a cada entidad vigilada para
generar la firma -- y firma exactamente así: `json.dumps(data, ensure_ascii=False)`,
sin fijar `separators` (es decir, con espacios). El comportamiento de este
microservicio replica al dedillo esa especificación. Confirmado también
operativamente: un intento de "corregir" los separadores para que coincidieran con
los bytes compactos de httpx generó el mismo error de firma inválida del lado del
ambiente QA real de la SFC -- prueba directa de que su verificación depende de esta
re-serialización específica, no del body crudo que reciben. No cambiar estos
separadores.

## El healthcheck del contenedor no depende de Redis (confirmado, no es un bug)

**Código:** `app/api/routes_health.py`, `scripts/render_task_def.py`.

Un hallazgo de revisión externa (2026-08-26) planteaba que el healthcheck del
contenedor estaría acoplado a Redis -- si Redis cae, ECS mataría en masa las
tareas sanas (el servicio no puede alcanzar la SFC sin Redis para el lock/cola,
pero eso no significa que el proceso Python esté muerto). Verificado contra el
repo actual: existen dos endpoints separados, `/health/live` (`liveness`, sin
ninguna dependencia externa, siempre `{"status": "alive"}`) y `/health/ready`
(`readiness`, sí revisa Redis vía `ping_redis`, devuelve 503 si no responde).
`scripts/render_task_def.py` (línea ~132) confirma que el `healthCheck` del
contenedor ECS usa explícitamente `/health/live` para `service_type == "api"`
-- ya desacoplado de Redis. `/health/ready` no está referenciado por ningún
healthcheck de contenedor en este repo (queda disponible para un smoke test
post-deploy externo, `api/v1/quejas/_health/ready`, que sí puede tolerar
fallar sin matar tareas).

**Conclusión:** el hallazgo no aplica al estado actual del repo -- no se
requirió ningún cambio de código. Salvedad: la configuración del target
group del ALB (si existiera un healthcheck de balanceador apuntando a
`/health/ready`) vive fuera de este repositorio, gestionada centralmente, y
no se pudo verificar desde acá.

## Momento 1 no deduplicaba quejas repetidas entre páginas (corregido)

**Código:** `app/services/momento_1_sync.py::_filtrar_duplicados_por_codigo_queja`.

`momento_4_sync.py` tiene un fix explícito ("hallazgo 50") que deduplica usuarios
por `numero_id_CF` dentro de un mismo lote, porque la paginación por cursor `next`
de la SFC puede devolver el **mismo registro en dos páginas consecutivas** si el
backlog cambia entre un fetch y el siguiente (un registro nuevo se inserta antes del
cursor y desplaza al resto una posición). Momento 1 compartía exactamente el mismo
patrón de paginación contra la misma SFC, pero nunca había recibido el mismo
tratamiento por `codigo_queja` -- confirmado con un test que reproducía dos páginas
con la misma queja y mostraba que se entregaba **duplicada** al CRM.

**Corregido (hallazgo del 2026-08-26):** se agregó `_filtrar_duplicados_por_codigo_queja`,
mismo patrón que Momento 4 (`vistos_codigos_queja`, un `set` acumulado a lo largo de
TODAS las páginas del ciclo, no sólo dentro de una). Se filtra antes de despachar el
procesamiento concurrente de cada queja -- evita además descargar sus adjuntos dos
veces. No deduplica códigos ausentes/vacíos entre sí (un registro malformado no debe
"comerse" a otro). Ver `tests/test_momento_1.py:: test_misma_codigo_queja_en_dos_paginas_se_deduplica`,
`test_duplicado_dentro_de_la_misma_pagina_tambien_se_deduplica`,
`test_codigo_queja_ausente_no_se_deduplica_contra_otro_ausente`.

## El parser de hilos de correo podía atribuirle al soporte una respuesta del cliente (corregido)

**Código:** `app/utils/email_parser.py::_clasificar_autor_bloque` (Caso C — bloque superior),
`app/utils/email_parser.py::_linea_identifica_remitente_soporte`.

`email_parser.py` extrae "la respuesta oficial más reciente de Global66" de un hilo de
correo HTML provisto por el CRM, y ese texto se usa directamente como
`cuerpo_respuesta_final` en el PDF de **cierre regulatorio** que se envía a la SFC
(`momento_3_sync.py::_generar_y_enviar_pdf_respuesta_final`). Para el bloque más
reciente del hilo (el que no trae un prefijo de cita tipo "El ... escribió:"), la
clasificación de autoría buscaba el dominio `global66` en **cualquier parte del
texto del bloque**, incluido el cuerpo del mensaje.

Si el mensaje más reciente del hilo era del **cliente** (no de soporte) y su propio
texto mencionaba "Global66" en prosa -- algo muy plausible en una respuesta de
reclamo, ej. *"estoy de acuerdo con la resolución que Global66 me ofreció"* -- ese
mensaje del cliente se clasificaba como la respuesta oficial de soporte, y su texto
terminaba en el PDF de cierre regulatorio en lugar del mensaje real del agente.
Reproducido empíricamente antes del fix.

**Corregido (hallazgo del 2026-08-26):** se agregó `_linea_identifica_remitente_soporte`,
que exige que la mención del dominio aparezca en una línea que realmente identifique
al remitente -- un patrón de correo/dominio explícito (`soporte@global66.com`,
`global66.com`), o una línea corta al estilo de una firma/membrete (ej. "Soporte
Global66", ≤6 palabras) -- y ya no en cualquier oración larga de prosa del cuerpo.
Restringir simplemente a las primeras 4 líneas de cabecera (el mismo criterio que ya
usan los Casos A/B) no bastaba: un mensaje corto de cliente cabe completo dentro de
esa ventana. Ver `tests/test_email_parser.py::test_parser_cliente_mas_reciente_menciona_marca_no_se_confunde_con_soporte`.

## `SfcErrorTranslator` clasificaba "la queja no existe" como si ya existiera (corregido)

**Código:** `app/core/exceptions.py::SfcErrorTranslator.procesar_y_lanzar`,
`_buscar_primera_coincidencia`.

`procesar_y_lanzar` recorre `errores_sfc.json` (o su equivalente en Google
Sheets, la fuente primaria en producción) en orden y se queda con la **primera**
regla que coincide, comparando la subcadena tanto contra el mensaje como contra
`sfc_field` (el nombre del campo, reconstruido por `_extraer_informacion_error`
a partir de las llaves del JSON de error). La matriz real tiene una regla
`"codigo_queja"` → `ALREADY_EXISTS` en la posición ~29 de 46, varias posiciones
antes que `"not found"`/`"no existe"`/`"does not exist"` → `NOT_FOUND_ERROR`
(~35-37). El problema: `"codigo_queja"` es el **nombre del campo**, no un
mensaje — aparece en prácticamente cualquier error de la SFC sobre una queja,
incluida la respuesta real (colección Postman oficial) de `POST /api/storage/`
cuando el `codigo_queja` referenciado **todavía no existe**:

```json
{ "codigo_queja": ["Object with codigo_queja=111635888992248094 does not exist."] }
```

Reproducido: este body clasificaba como `ALREADY_EXISTS`, no `NOT_FOUND_ERROR`.
Dos consecuencias, ambas confirmadas:

1. `Momento2SincronizacionService._es_error_queja_ya_existe_m2` trata
   `error_type == "ALREADY_EXISTS"` como éxito idempotente incondicional — "la
   queja no existe" se reportaba como "ya estaba creada".
2. `despacho_queja_orchestrator._es_caso_no_encontrado` sólo reconoce
   `status_code == 404` o `error_type == "NOT_FOUND_ERROR"` — con
   `error_type="ALREADY_EXISTS"` y `status_code=400`, el **self-healing
   M2→M3** (la razón documentada arriba de por qué "casi nunca hace falta
   Momento 2 puro") nunca se disparaba para esta forma de error real de la
   SFC. El caso fallaba con un `crm_action` que además afirmaba lo contrario
   de lo que había pasado.

**Por qué reordenar sólo `errores_sfc.json` no alcanzaba:** ese archivo es
únicamente el respaldo local (`cargar_matriz_local`, usado cuando Google
Sheets no está configurado o falla) — la fuente primaria en producción es la
hoja de Google Sheets, que comparte el mismo riesgo de orden y no se puede
corregir desde este repositorio.

**Corregido (hallazgo de revisión, 2026-08-26):** `procesar_y_lanzar` ahora
evalúa las reglas `NOT_FOUND_ERROR` de la matriz **con prioridad**, sin
importar su posición real (local o Google Sheets). Un mensaje de "ya existe"
genuino (sin ninguna de esas frases) sigue clasificando `ALREADY_EXISTS`
normalmente.

**Autocorrección el mismo día (auditoría de los propios fixes):** la primera
versión de este fix le daba esa prioridad a `NOT_FOUND_ERROR` sobre **toda**
la matriz, sin restringirla a `codigo_queja` -- demasiado amplio. Reproducido:
un error de VALIDACIÓN genuino de OTRO campo catálogo (`departamento_cod`,
`municipio_cod`, etc.), que la SFC también puede reportar con la misma frase
"does not exist" (mismo patrón `SlugRelatedField` de Django REST Framework)
para SUS PROPIOS valores inválidos, se reclasificaba de `VALIDATION_ERROR` a
`NOT_FOUND_ERROR` -- dañando el self-healing para un caso que en realidad
necesitaba corrección de datos del CRM, no una auto-recuperación. La causa
raíz real es más estrecha que "NOT_FOUND_ERROR debe ganar siempre": sólo
`codigo_queja` tiene una regla (`ALREADY_EXISTS`) cuyo tipo **contradice** lo
que "does not exist" significa -- las reglas de nombre de campo para los
demás campos catálogo ya mapean correctamente a `VALIDATION_ERROR`, el tipo
correcto para "este valor no existe en el catálogo". La prioridad quedó
restringida a mensajes específicamente sobre `codigo_queja` (vía `sfc_field`);
el resto de la matriz conserva su orden real. Ver
`tests/test_sfc_error_translator.py::test_not_found_tiene_prioridad_sobre_coincidencia_por_nombre_de_campo`,
`test_not_found_no_reclasifica_error_de_validacion_de_otro_campo`, y
`TestProcesarYLanzarContraMatrizLocalReal` (ambos escenarios reproducidos
contra la matriz local real, no una matriz de prueba simplificada).

**Vulnerabilidad relacionada, más amplia, encontrada por una revisión externa
posterior:** el fix de arriba cierra la colisión `codigo_queja`↔`NOT_FOUND_ERROR`,
pero deja abierta una clase más amplia de la que esa colisión es sólo un caso: **cualquier
regla de la matriz que sea un solo token sin espacios** (`'556240'`→`DUPLICATE_FILE`, además de
los nombres de campo) matchea como falso positivo si aparece **incrustada** dentro de un
identificador más largo que el cliente controla. Reproducido: un `Smart_Code__c` que contiene
`556240` en medio de su parte numérica (`^[a-zA-Z0-9_-]{1,30}$` lo permite, por azar o a propósito)
hacía que **cualquier** error de la SFC sobre ese caso -- sin relación alguna con un archivo --
se clasificara `DUPLICATE_FILE`. Consecuencia: `_manejar_duplicado_o_cerrado` lo habría absorbido
como éxito, marcando el checkpoint como entregado para un archivo que nunca se transmitió, durante
30 días (el TTL del checkpoint). **Corregido:** `_coincide` ancla las reglas de un solo token a
límites de palabra (`(?<![a-zA-Z0-9])token(?![a-zA-Z0-9])`) -- las frases de varias palabras (ya
suficientemente específicas por su longitud) siguen comparándose tal cual, sin ancla. Esto también
refuerza el fix de `codigo_queja` de arriba con una defensa más general, no sólo ese caso puntual.
Ver `tests/test_sfc_error_translator.py::TestCoincide`,
`test_556240_incrustado_en_smart_code_no_se_clasifica_duplicate_file`.

**El mismo patrón apareció en un segundo lugar (barrido de verificación,
2026-08-26):** `S3StorageService._manejar_duplicado_o_cerrado`
(`app/services/s3_service.py`) usaba `"ya existe" in raw_msg` -- un substring
genérico, sin ancla -- para decidir si un rechazo al subir un adjunto
significaba "la SFC ya tiene este archivo" (duplicado genuino, se absorbe
como éxito y se marca el checkpoint). El mismo problema: "ya existe" también
aparece en reglas reales de `errores_sfc.json` que no tienen nada que ver con
un archivo duplicado -- "Ya existe queja con este codigo queja", "ya existe
una Queja radicada para la entidad con el mismo motivo" (ambas sobre la
**queja**, no sobre el archivo). Reproducido: ese tipo de mensaje se absorbía
igual que un duplicado real, marcando el checkpoint como entregado para un
archivo que la SFC en realidad nunca recibió -- silenciosamente ausente del
expediente regulatorio del caso, sin ningún reintento futuro que lo
recuperara. Corregido acotando a las dos frases reales que `errores_sfc.json`
mapea a `DUPLICATE_FILE` ("El anexo ya existe", "El documento ya existe"),
igual de específicas que `"ya cuenta con un documento"` ya usada en la misma
condición. Ver `tests/test_s3_service.py::
test_mensaje_sobre_existencia_de_la_queja_no_se_confunde_con_archivo_duplicado`.

**Barrido de patrones similares (2026-08-26):** tras estos dos hallazgos se
revisó todo el repositorio buscando la misma forma -- un substring genérico
comparado en una cadena de prioridad "primera coincidencia gana" (`grep` por
`" in raw_msg"`/`" in msg"`/`" in error_msg"` en `app/`). El único otro
candidato con una palabra clave igual de genérica es
`Momento2SincronizacionService._es_error_queja_ya_existe_m2`
(`app/services/momento_2_sync.py`), que también incluye `"ya existe"` en su
lista de keywords -- pero a diferencia de los dos casos de arriba, sólo se
evalúa sobre la respuesta de `POST /api/queja/` (el endpoint de **creación**
de la queja), donde no hay otro recurso plausible al que "ya existe" pudiera
referirse por error -- el riesgo de colisión entre-recursos que sí existía en
los otros dos casos (un archivo vs. la queja que lo contiene) no aplica aquí
de la misma forma. Se dejó sin modificar por falta de un caso reproducible
concreto (a diferencia de los dos anteriores, ambos confirmados con el body
real de la colección Postman/`errores_sfc.json`) -- evitar un cambio
especulativo sin evidencia que lo justifique.

## Referencias en el código

| Concepto                                                                                                  | Archivo                                                                                                                                                                                                           |
| --------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Endpoint único de despacho                                                                               | `app/api/routes_quejas.py::despachar_queja_crm`                                                                                                                                                                 |
| Inferencia M2/M3 + self-healing                                                                           | `app/services/despacho_queja_orchestrator.py`                                                                                                                                                                   |
| Momento 1                                                                                                 | `app/services/momento_1_sync.py`                                                                                                                                                                                |
| Momento 2                                                                                                 | `app/services/momento_2_sync.py`                                                                                                                                                                                |
| Momento 3 (trámite/fraude/cierre)                                                                        | `app/services/momento_3_sync.py`                                                                                                                                                                                |
| Momento 4                                                                                                 | `app/services/momento_4_sync.py`                                                                                                                                                                                |
| Cola centralizada + versión + Lua scripts                                                                | `app/services/queue_service.py`                                                                                                                                                                                 |
| Worker de reintentos                                                                                      | `app/workers/scheduler.py::reintentar_despachos_pendientes_job`                                                                                                                                                 |
| Idempotencia (hash + store)                                                                               | `app/services/idempotency_service.py`                                                                                                                                                                           |
| Test: no-completar contenido sobrescrito                                                                  | `tests/test_queue_race_protection.py`                                                                                                                                                                           |
| Test: fraude + cierre simultáneo                                                                         | `tests/test_despacho_orquestador.py::test_5_despacho_fraude_y_cierre_simultaneo_directo`                                                                                                                        |
| Clasificación de errores SFC por texto (NOT_FOUND_ERROR con prioridad, corregido)                        | `app/core/exceptions.py::SfcErrorTranslator.procesar_y_lanzar`, `_buscar_primera_coincidencia`, `errores_sfc.json`                                                                                            |
| Duplicado de adjunto vs. mensaje sobre la queja (substring genérico acotado, corregido)                  | `app/services/s3_service.py::S3StorageService._manejar_duplicado_o_cerrado`                                                                                                                                    |
| Colección Postman oficial de la SFC (referencia de mensajes de error)                                    | `docs/Smartsupervision - Doc API Quejas - Momento 4.postman_collection (2) (1).json`                                                                                                                            |
| Webhook al CRM (`status: "CREATED"` fijo)                                                               | `app/services/crm_webhook_service.py::notificar_resolucion_contingencia`                                                                                                                                        |
| DLQ / fallo definitivo                                                                                    | `app/services/queue_service.py::registrar_fallo`, `EmailAlertService.notificar_caso_fallido_definitivo`                                                                                                       |
| Replay administrativo de DLQ                                                                              | `app/services/queue_service.py::reencolar_item_fallido`, `app/api/routes_quejas.py::reencolar_registro_fallido` (`POST /queue/{id}/reencolar`)                                                              |
| Cancelación de pendiente tras éxito síncrono (respeta la operación)                                   | `app/services/queue_service.py::cancelar_pendiente_por_smart_code`, `tests/test_queue_cancelar_pendiente_tras_exito_sincrono.py`                                                                              |
| Encolado respeta la categoría de operación (rechaza sobrescritura entre categorías, corregido)        | `app/services/queue_service.py::encolar_despacho`, `ENQUEUE_LUA_SCRIPT`, `tests/test_queue_encolar_respeta_categoria_de_operacion.py`                                                                         |
| Cascada de timeouts (gunicorn → ALB; nginx.conf es sólo para tests locales, no está en el deploy real) | `infrastructure/Dockerfile`, `SFC_SYNC_MAX_SEGUNDOS` en `app/core/config.py`                                                                                                                                |
| Deduplicación de alertas por correo                                                                      | `app/services/email_service.py::EmailAlertService._deberia_enviar`                                                                                                                                              |
| Parser de hilos de correo para el cierre regulatorio (autoría por línea, no por bloque completo)        | `app/utils/email_parser.py::_clasificar_autor_bloque`, `_linea_identifica_remitente_soporte`                                                                                                                  |
| Ownership de adjuntos S3 (Case_id, posicional)                                                            | `app/services/s3_service.py::S3StorageService._validar_ownership_key`, `_validar_prefijo_pertenece_al_caso`                                                                                                   |
| Refresco periódico de catálogos/mapeos (por proceso, sin lock cross-proceso, corregido)                 | `app/core/mapping.py::SfcSalesforceMapper.iniciar_refresco_periodico`, `tests/test_mapping_refresco_periodico.py`                                                                                             |
| Lock por caso en despacho síncrono + "ya cerrado" en trámite                                            | `app/core/distributed_lock.py::RedisLock`, `app/api/routes_quejas.py::despachar_queja_crm`, `app/services/despacho_queja_orchestrator.py::_ejecutar_paso_o_exito_si_ya_cerrado`                             |
| Lock por caso también en el worker de reintentos                                                         | `app/workers/scheduler.py::_reclamar_y_procesar_si_lock_disponible`, `app/services/queue_service.py::DESPACHO_LOCK_PREFIX`                                                                                    |
| `RedisLock` distingue "ocupado" de "Redis falló" (corregido)                                             | `app/core/distributed_lock.py::RedisLock.acquire`, `app/api/routes_quejas.py::_motivo_lock_no_adquirido`                                                                                                     |
| Alerta de riesgo de duplicado si falla la persistencia final tras webhook exitoso                         | `app/workers/scheduler.py::_ejecutar_paso_notificacion_crm`                                                                                                                                                     |
| Version esperada en el diferimiento por caída de SFC                                                     | `app/services/queue_service.py::diferir_pendientes_por_caida_sfc`, `DIFERIR_ITEM_LUA_SCRIPT`                                                                                                                  |
| Firma HMAC no byte-exacta sobre el body real (confirmado, no es un bug)                                   | `app/core/security/signatures.py::PayloadSignatureStrategy`, `docs/SignatureGenerator_comment (1).txt`, `tests/test_auth_flow_interceptor.py::test_firma_no_es_byte_exacta_sobre_el_body_realmente_enviado` |
| Dedup de quejas repetidas entre páginas (Momento 1)                                                      | `app/services/momento_1_sync.py::_filtrar_duplicados_por_codigo_queja`                                                                                                                                          |
| Reclamo de item no deja claim huérfano ante un item con JSON corrupto (orden decode-antes-de-escribir)   | `app/services/queue_service.py::CLAIM_ITEM_LUA_SCRIPT`, `tests/test_queue_resiliencia_datos_corruptos.py`                                                                                                     |
| `_extraer_informacion_error` lee `messages` (plural, el envoltorio estándar real) además de `message` (corregido) | `app/core/exceptions.py::SfcErrorTranslator._extraer_informacion_error`, `tests/test_sfc_error_translator.py::TestExtraerInformacionError`                                                                    |
| Anclaje de `_coincide` cubre el alfabeto real de `Smart_Code__c` (`_`/`-` incluidos, corregido)          | `app/core/exceptions.py::SfcErrorTranslator._coincide`, `tests/test_sfc_error_translator.py::TestCoincide`                                                                                                    |
| `_manejar_duplicado_o_cerrado` ya no reimplementa la clasificación por texto (una sola fuente de verdad, corregido) | `app/services/s3_service.py::S3StorageService._manejar_duplicado_o_cerrado`, `tests/test_s3_service.py`                                                                                                       |
| El lock ocupado libera la idempotencia PROCESSING antes del 409 (corregido)                              | `app/api/routes_quejas.py::despachar_queja_crm`, `tests/test_routes_quejas_despacho.py::TestDespachoLockPorCaso`                                                                                              |
| Conflicto de cola (409) no se loguea como falla de infraestructura (corregido)                            | `app/services/queue_service.py::encolar_despacho`, `tests/test_queue_encolar_respeta_categoria_de_operacion.py::test_conflicto_no_se_loguea_como_error_de_infraestructura`                                    |
| Trámite sobre caso ya cerrado reporta `noop`, no `success` (corregido)                                     | `app/services/despacho_queja_orchestrator.py::_resultado_por_cierre_confirmado`, `tests/test_despacho_orquestador.py::test_12b_tramite_sobre_caso_ya_cerrado_se_absorbe_como_noop`                            |
