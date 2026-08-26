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
- [¿Por qué "caso ya cerrado" se trata como éxito?](#por-qué-caso-ya-cerrado-se-trata-como-éxito)
- [¿Por qué la clasificación de errores de negocio de la SFC usa coincidencia de texto?](#por-qué-la-clasificación-de-errores-de-negocio-de-la-sfc-usa-coincidencia-de-texto)
- [¿Por qué el webhook al CRM siempre reporta `status: "CREATED"`?](#por-qué-el-webhook-al-crm-siempre-reporta-status-created)
- [¿Por qué la cola de fallidos (DLQ) no tiene endpoint de replay?](#por-qué-la-cola-de-fallidos-dlq-no-tiene-endpoint-de-replay)
- [¿Por qué la cascada de timeouts no llega hasta nginx/ALB?](#por-qué-la-cascada-de-timeouts-no-llega-hasta-nginxalb)
- [¿Por qué la deduplicación de alertas por correo es por proceso, no global?](#por-qué-la-deduplicación-de-alertas-por-correo-es-por-proceso-no-global)
- [¿Por qué la validación de ownership de adjuntos en S3 usa Case_id y es estrictamente posicional?](#por-qué-la-validación-de-ownership-de-adjuntos-en-s3-usa-case_id-y-es-estrictamente-posicional)
- [Refresco periódico de catálogos/mapeos (hallazgo C1)](#refresco-periódico-de-catálogosmapeos-hallazgo-c1)
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

**Por qué es así:** al igual que el webhook de resolución, un endpoint de replay o una notificación
de fallo definitivo requieren que el CRM tenga **algo que hacer** con esa señal — un flujo que
reabra el caso, lo marque para revisión manual, o dispare un reintento desde su lado. Hoy ese
contrato no existe: el CRM no tiene un endpoint que reciba "este caso falló definitivamente", ni un
estado en Salesforce pensado para representarlo. Construir un endpoint de replay sin que el CRM
pueda invocarlo (o sin que sepa qué hacer con la notificación) no resuelve el problema real, que es
de coordinación entre equipos, no de código faltante en este microservicio.

**El costo real de esta limitación** (y por qué no es "aceptalo y ya"): hoy la única señal de un
caso perdido es un correo a operaciones (`notificar_caso_fallido_definitivo` — éste sí se envía uno
por caso, sin deduplicar, a diferencia de las alertas de infraestructura descritas más abajo) — así
que si nadie lo lee, un caso regulatorio puede perderse sin que nadie se entere hasta una auditoría
de la SFC. Ese riesgo residual es real y queda anotado — la falta de endpoint de replay es la parte
que no se puede cerrar sin el contrato del CRM.

**Corregido (hallazgo N1, revisión externa v5, 2026-08-25):** hasta esta ronda, `QueueService.
cancelar_pendiente_por_smart_code` (invocado tras un despacho síncrono exitoso, ver más arriba)
podía descartar de la cola un evento que **nunca había llegado a intentarse siquiera** — no un
fallo definitivo, directamente lo borraba sin que pasara por la DLQ ni generara ningún correo,
porque el endpoint de despacho es unificado y ese método cancelaba cualquier item pendiente del
mismo `smart_code` sin mirar si era la misma operación (ej. un trámite exitoso podía borrar un
reporte de fraude que seguía genuinamente pendiente de transmitir). Ahora sólo cancela si el item
pendiente es la misma categoría de operación que el despacho que acaba de tener éxito -- ver
`app/services/queue_service.py::cancelar_pendiente_por_smart_code` y
`tests/test_queue_cancelar_pendiente_tras_exito_sincrono.py::TestCancelarPendienteRespetaCategoriaDeOperacion`.

---

## ¿Por qué la cascada de timeouts no llega hasta nginx/ALB?

**Código:** `infrastructure/Dockerfile` (`gunicorn --timeout 330 --graceful-timeout 60`),
`infrastructure/nginx.conf` (`proxy_read_timeout 60s`), `SFC_SYNC_MAX_SEGUNDOS` en `app/core/config.py`.

El presupuesto de tiempo de Momento 1 (`SFC_SYNC_MAX_SEGUNDOS`, 300s) necesita que gunicorn no mate
al worker a mitad de un despacho — por eso se subió `--timeout` de 120s a 330s. Pero nginx sigue
cortando la conexión con el cliente a los 60s, y el listener del ALB (fuera de este repositorio) no
se tocó: un request que de verdad tarde más de 60s sigue devolviendo un timeout al CRM aunque el
worker de gunicorn continúe procesándolo de fondo hasta los 330s.

**Por qué se dejó así, deliberadamente, en esta ronda:** la corrección completa de este hallazgo
tiene dos caminos — (a) subir también `proxy_read_timeout` y el idle timeout del ALB en la misma
proporción, o (b) sacar Momento 1 del camino síncrono por completo (convertirlo en un job de
background con su propio mecanismo de polling/ACK, en vez de una llamada HTTP que se mantiene
abierta mientras dura toda la paginación contra la SFC). La opción (b) es la solución de fondo, pero
es un cambio de arquitectura no trivial (persistir quejas obtenidas-pero-no-confirmadas + cursor de
paginación resumible en Redis, preservando el contrato síncrono actual con el CRM) que se evaluó y
se decidió explícitamente **posponer** — no es necesario para el problema inmediato, que era evitar
que gunicorn matara el worker a mitad de un despacho de Momento 3 normal (el caso de uso dominante,
sin relación con la paginación larga de Momento 1). Subir sólo `--timeout` de gunicorn resuelve ese
caso dominante sin tocar arquitectura.

**Qué sigue pendiente, sin resolver:** para que Momento 1 realmente aproveche los 330s de
presupuesto sin que el cliente (CRM) se desconecte antes por su cuenta a los 60s, hace falta (a) o
(b). Mientras tanto, el riesgo señalado por la auditoría es real pero acotado: un request colgado
retiene uno de los dos workers de gunicorn durante más tiempo que antes (5.5 min en vez de 2), lo
cual es un costo aceptado a cambio de que Momento 3 no se corte a mitad de un despacho normal.

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

**Qué NO está cubierto por esta justificación** (issue real, no decisión de diseño): la clave global
`"falla_infraestructura"` es compartida por más de nueve call sites distintos
(`notificar_falla_infraestructura` se invoca desde `routes_quejas.py`, `idempotency_service.py`,
`momento_1_sync.py`, `momento_4_sync.py`, `queue_service.py`, `scheduler.py` y `worker.py`,
cubriendo desde el fail-closed de Redis hasta la caída de la SFC en distintos Momentos y el
healthcheck del worker) — durante la misma ventana de 15 minutos, el primero en dispararse silencia
a todos los demás, incluido el mensaje de "riesgo de duplicado" tras una persistencia post-SFC
fallida, que es el más accionable de todos. Es una mejora pendiente, no parte de esta justificación.

**Ya corregido:** el diccionario sí llegó a no tener cota de tamaño (`error_no_mapeado` incorpora
`sfc_field`, cuya cardinalidad depende de las claves que la SFC use en su JSON de error) — se
resolvió con una purga best-effort de entradas expiradas más un tope duro
(`MAX_ENTRADAS_DEDUP = 500`) en `_deberia_enviar` (hallazgo N7, revisión externa v5, 2026-08-25).

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

## Refresco periódico de catálogos/mapeos (hallazgo C1)

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
`obtener_catalogos_y_mapeos()`. No hace falta lógica nueva de refresco: el
fast-path interno de ese método ya hace que una llamada con caché fresca sea
barata, y una falla de red ahí no borra el catálogo ya cargado en RAM (sólo
retrasa el próximo intento). El job corre en ambos procesos (API cuando
`RUN_SCHEDULER` está activo, y worker) porque cada uno mantiene su propia
copia de `CATALOGOS` en memoria de proceso.

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
| Clasificación de errores SFC por texto | `app/core/exceptions.py::SfcErrorTranslator.procesar_y_lanzar`, `errores_sfc.json` |
| Colección Postman oficial de la SFC (referencia de mensajes de error) | `docs/Smartsupervision - Doc API Quejas - Momento 4.postman_collection (2) (1).json` |
| Webhook al CRM (`status: "CREATED"` fijo) | `app/services/crm_webhook_service.py::notificar_resolucion_contingencia` |
| DLQ / fallo definitivo | `app/services/queue_service.py::registrar_fallo`, `EmailAlertService.notificar_caso_fallido_definitivo` |
| Cancelación de pendiente tras éxito síncrono (respeta la operación) | `app/services/queue_service.py::cancelar_pendiente_por_smart_code`, `tests/test_queue_cancelar_pendiente_tras_exito_sincrono.py` |
| Cascada de timeouts | `infrastructure/Dockerfile`, `infrastructure/nginx.conf`, `SFC_SYNC_MAX_SEGUNDOS` en `app/core/config.py` |
| Deduplicación de alertas por correo | `app/services/email_service.py::EmailAlertService._deberia_enviar` |
| Ownership de adjuntos S3 (Case_id, posicional) | `app/services/s3_service.py::S3StorageService._validar_ownership_key`, `_validar_prefijo_pertenece_al_caso` |
| Refresco periódico de catálogos/mapeos | `app/core/mapping.py::SfcSalesforceMapper.obtener_catalogos_y_mapeos`, `app/workers/scheduler.py::refrescar_catalogos_job` |
