# Plan de Integración con SmartSupervisión (SFC)

La idea es crear un **microservicio** encargado de la comunicación entre el CRM (que puede crear quejas directamente o recibirlas) y la API de la SFC de SmartSupervisión. Deberá funcionar como intermediario entre ambos y, en cierta forma, también como **traductor/adaptador**, ya que la SFC maneja de forma particular cierta información.

Para la implementación se utilizará **FastAPI**, ya que es el mismo framework que usa el CRM principal, y al ser Python, permite reutilizar el mismo algoritmo y librerías para la firma digital, evitando problemas de seguridad con la SFC.

La API de la SFC organiza su información en **flujos llamados "momentos"**, que pueden reutilizar endpoints con objetivos distintos y cuentan con varias medidas de seguridad.

---

## 1. Arquitectura General

Antes de ver cada momento en detalle, hay aspectos transversales que aplican a más de uno de los momentos.

### 1.1. Autenticación

Utiliza el endpoint de login con usuario y contraseña. La autenticación retorna:

- **Access Token**: Vigencia de 30 minutos para peticiones de recursos.
- **Refresh Token**: Vigencia de 12 horas para renovación automática sin intervención manual.

La idea es dejar un **interceptor/authenticator** que maneje automáticamente el ciclo de vida de los tokens (login si no está autenticado y no hay refresh válido, refresh si el token es válido, etc.), y que también agregue el access token a cada petición.

### 1.2. Seguridad y Firmas

Por motivos de integridad, en cada petición hay que firmar su contenido mediante el algoritmo **HMAC-256**. Las reglas para el contenido a firmar son:

| Tipo de petición | Contenido a firmar |
|---|---|
| `POST` / `PUT` | Cuerpo JSON completo de la petición |
| `GET` | URL completa, incluyendo parámetros de consulta |
| Transferencia de archivos | Solo los campos `codigo_queja` y `type` (se omite la llave del archivo) |

Para implementar esto se necesitan dos componentes:

- **Clase de firma**: Se sugiere aplicar el **patrón Strategy**, usando una única interfaz con implementaciones distintas por caso, lo que permite ampliar o cambiar sin afectar el exterior.
- **Interceptor de firma**: Se ejecuta automáticamente antes de cada petición, genera la firma y la agrega al header correspondiente.

### 1.3. Gestión de Constantes

La SFC usa códigos muy específicos para un montón de atributos en las quejas. Se necesita un **"diccionario" centralizado** donde, por cada categoría y código, se tenga su nombre, descripción y demás información necesaria.

La propuesta es manejar una **tabla en base de datos** dedicada a guardar esta información. Queda pendiente definir cómo exponer estos datos al CRM (¿un módulo adicional de mapeo en el microservicio o acceso directo a la base de datos?).

De forma similar, se necesita una tabla para **errores/problemas**, para que los usuarios del CRM entiendan qué falló sin recibir mensajes técnicos crípticos.

### 1.4. Gestión de Archivos

En todos los momentos se gestionan archivos adjuntos de las quejas (generalmente PDF y DOCX). El almacenamiento se hará en **S3**, con la convención propuesta por Katherine: carpetas nombradas con el ID del caso y los archivos dentro.

Puntos clave:
- Los archivos se envían y reciben directamente en la petición HTTP.
- Al menos en las respuestas finales, es necesario **generar PDFs** a partir del contenido del mensaje y una plantilla predefinida.
- El **límite de peso por archivo es de 30 MB**.

> **[OPCIONAL - No se implementará por el momento] Antivirus**
>
> La documentación original contemplaba revisar los archivos con un antivirus antes de enviarlos a la SFC. La opción evaluada fue **ClamAV** (open source), desplegado en un contenedor asociado al microservicio principal, compartiendo red interna y un volumen de almacenamiento temporal. También se evaluó la opción de dos tasks separadas (ClamAV se enciende solo cuando se necesita, comunicadas por eventos), lo que ahorraría costos pero aumentaría la latencia a varios segundos.
>
> **Esta funcionalidad se deja como opcional y no se implementará en la fase inicial.** Al momento de retomarlo, habrá que confirmar con la SFC si ClamAV es válido para ellos o si se requiere una licencia de antivirus comercial aprobado.

### 1.5. Procesamiento de Correos

Para crear los documentos de respuesta, hay que extraer el contenido relevante de los correos (tanto de clientes como propios). Se debe contemplar un mecanismo para **parsear correos en HTML** y extraer el texto sin importar la estructura de tags.

---

## 2. Momentos de Integración

A continuación se describen los distintos momentos que maneja el servicio de la SFC.

### 2.1. Momento 1: Captura de Información Inicial

Flujo para consumir y guardar en la base de datos las quejas nuevas provenientes de la SFC.

#### 2.1.1. Flujo de Consumo de Información (Quejas)

Se descargan las quejas nuevas desde la SFC y se almacenan localmente para que el CRM pueda accederlas.

- **Endpoint**: `GET https://UrlBase/api/queja/`
- **Paginación**: El servicio entrega los recursos paginados (ej. 100 registros por página). El módulo debe iterar sobre todas las páginas hasta completar el total de recursos pendientes.
- **Headers requeridos**:
  - `Content-Type: application/json`
  - `Authorization: <token>`
  - `X-SFC-Signature: <signature>`
- **Descarga de adjuntos**: Por cada queja procesada, se inician las descargas de archivos adjuntos (de forma simultánea o secuencial).
- **Programación**: El fetch se ejecuta de forma automática y periódica, **tentativamente cada 12 horas** mediante un cron (a revisar).

#### 2.1.2. Mecanismo de Confirmación (ACK)

Tras descargar las quejas y sus archivos, hay que confirmarle a la SFC que ya se descargaron (para que no reaparezcan en el siguiente GET).

- **Endpoint**: `POST https://UrlBase/api/complaint/ack/`
- **Payload**: Array de hasta **100 IDs** de quejas recibidas por petición.
- **Plazo**: Se tienen **48 horas** desde la visualización de la queja para confirmar manualmente; de lo contrario, la SFC marca automáticamente.
- **Dependencia**: El ACK solo puede enviarse después de haber descargado exitosamente la información y los archivos de las quejas.

#### 2.1.3. Gestión de Archivos Anexos

Descarga de archivos adjuntos de cada queja para guardarlos en S3.

- **Endpoint**: `GET https://UrlBase/api/storage/?codigo_queja=<codigo_queja>`
- **Proceso**: La respuesta contiene la información de los archivos; hay que asociarlos con la queja en la base de datos.

> **[OPCIONAL] Antivirus en Momento 1**: No es crítico en este flujo, ya que la SFC garantiza revisar los archivos con su propio antivirus. Queda pendiente para cuando se retome la implementación del antivirus.

---

### 2.2. Momento 2: Envío de Quejas Creadas en Nuestro Lado

Flujo para registrar en el sistema de la SFC las quejas generadas por clientes en nuestro lado.

#### 2.2.1. Creación de la Queja

- **Endpoint**: `POST https://UrlBase/api/queja/`
- **Payload**: Toda la información de la queja (ver documentación de la API).
- **Respuesta**: La SFC retorna el objeto creado en su base de datos; esto se usa como confirmación para continuar con el envío de archivos.
- **Importante**: El ID de la queja no se manda tal cual; hay que **concatenar el código de tipo de entidad + código de entidad + ID** (ver documentación para el detalle exacto).

**Opciones para el flujo de comunicación CRM → microservicio** (hay que testear cuál es mejor según la velocidad/concurrencia):

1. Mandar únicamente el **ID de la queja** y dejar que el microservicio consulte la información directamente en la base de datos.
2. Mandar **toda la información de la queja** en la petición.

#### 2.2.2. Envío de Archivos de la Queja

Posterior a la creación, hay que enviar los archivos adjuntos correspondientes.

- **Endpoint**: `POST https://UrlBase/api/storage/`
- **Respuesta**: ID del archivo almacenado y status de la queja.
- **Qué archivos enviar**: No es conveniente mandar todos los archivos; hay que definir la lógica de selección (ver también Momento 3).

> **[OPCIONAL - No se implementará por el momento] Antivirus en Momento 2**: La SFC requiere escaneo antivirus sobre los archivos salientes. Este punto quedará pendiente hasta que se implemente la funcionalidad de antivirus.

---

### 2.3. Momento 3: Actualización de Quejas

Flujo para actualizar el progreso de una queja existente en la SFC. Se utilizará únicamente `PUT` (actualización completa) al momento de avanzar de etapa/progreso en la resolución.

#### 2.3.1. Actualizaciones de las Quejas

El CRM notifica al microservicio cuando una queja avanza de etapa. El procedimiento es esencialmente igual al Momento 2, pero sobre una queja existente.

- **Cuidado con el ID**: Asegurarse de usar el ID correcto y actualizado para no actualizar la queja equivocada.
- **Respuesta**: La SFC retorna el cuerpo de la queja actualizado.

**Caso especial — Quejas de Fraude**: En el Momento 2 solo se pueden crear quejas genéricas. Para crear una queja de fraude desde cero:
1. Crear primero la queja genérica (Momento 2).
2. Actualizarla inmediatamente con la información de fraude (Momento 3).

**Cierre de caso**: También se realiza mediante este momento, siguiendo las reglas de nomenclatura de archivos y los códigos de estado especificados en la documentación.

#### 2.3.2. Envío de Documentación

Los documentos correspondientes a la actualización del caso deben enviarse **antes** de actualizar la información de la queja (a diferencia del Momento 2, donde van después).

- Aplican las mismas consideraciones de nomenclatura de archivos.
- En casos de fraude o cierre de caso, hay que agregar **afijos específicos** a los nombres de archivo (ver documentación).

> **[OPCIONAL - No se implementará por el momento] Antivirus en Momento 3**: Al igual que en el Momento 2, el escaneo antivirus sobre los archivos salientes quedará pendiente hasta que se implemente la funcionalidad correspondiente.

---

## 3. Consideraciones Adicionales

- **TLS v1.2**: No se necesita VPN para conectarse al servidor de la SFC, pero **sí se requiere TLS v1.2** y un certificado SSL válido durante el handshake.
- **Selección de archivos a enviar**: Hay que definir la lógica para determinar qué archivos se mandan en los Momentos 2 y 3 (no se mandan todos indiscriminadamente).
- **Nomenclatura de archivos**: La SFC tiene reglas de nomenclatura específicas, especialmente para casos de fraude y cierres de caso.
- **Gestión de errores del CRM**: Los mensajes de error que reciba el CRM deben ser comprensibles para los usuarios, no mensajes técnicos internos.
