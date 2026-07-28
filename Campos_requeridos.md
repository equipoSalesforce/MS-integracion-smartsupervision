# Campos Obligatorios por Escenario


## Campos Base Obligatorios (Comunes a TODOS los momentos y casos)

Cualquier petición enviada al endpoint de despacho debe incluir obligatoriamente estos 11 campos (sin valor predeterminado):

1. **`Case_id`** o **`Smart_Code__c`** *(Al menos uno de los dos debe estar presente)*
2. **`SuppliedName`**
3. **`SC_id_type__c`**
4. **`id_number__c`** *(Solo dígitos, máx 15 caracteres)*
5. **`tipo_de_persona__c`**
6. **`direccion__c`**
7. **`punto_recepcion`**
8. **`Description`** *(Máx 4500 caracteres)*
9. **`smart_escalamiento_DCF__c`**
10. **`Product__c`**
11. **`Categorias_COL__c`**

---

## Detalle de Campos por Escenario de Negocio

### 1. Momento 2 Base (Radicación Inicial / Queja Nueva)
* **Campos Base Obligatorios:** Los 11 campos base comunes.
* **Campos Opcionales/Predeterminados:**
  * `Status` (default: `"New"`)
  * `Tutela__c` (default: `"No"`)
  * `admision_col__c` (default: `"No Aplica"`)
  * `Instancia_de_recepcion__c` (default: `"Entidad vigilada"`)
  * `smart_anexo_queja__c` (default: `False`)
  * `archivos_s3` (default: `[]`)

---

### 2. Momento 3 Actualización Normal Base (Trámite)
* **Campos Base Obligatorios:** Los 11 campos base comunes.stand by
* **Condición:** No incluir campos de Cierre ni de Fraude.
* **Campos adicionales obligatorios:**
    * `Status` (default: `"In Progress o Stand by"`)

---

### 3. Momento 3 Actualización y Creación a la vez (Self-Healing)
* **Campos Base Obligatorios:** Los 11 campos base comunes.
* **Lógica:** Idéntico a la actualización normal. Si la SFC devuelve `404`, el orquestador usa los mismos 12 campos base para radicar primero la queja en Momento 2 y luego aplicar la actualización.

---

### 4. Momento 3 Fraude Base
* **Campos Base Obligatorios:** Los 11 campos base comunes.
* **Gatillo de Activación:** Al menos uno entre `tipo_fraude__c` o `modalidad_fraude__c`.
* **Campos Condicionales Obligatorios:**
  * **`archivos_s3`**: Debe contener **al menos 1 archivo**.
  * **`nombre_archivo_fraude`**: Obligatorio únicamente si se envían **2 o más archivos** en `archivos_s3` (si viene 1 solo archivo, se deduce automáticamente).

* **Campos opcionales pero importantes:**
  ***`card_amount__c`**: Valor reclamado por el fraude
  ***`Total_Devuelto_por_Desconocimiento__c`**: Valor financiero devuelto o reconocido

    Ambos tienen valor por defecto de 0.0 por compatibilidad con la api, pero es importante mandarlos si se aplica adecuadamente

---

### 5. Momento 3 Fraude y Creación a la vez (Self-Healing)
* **Campos Base Obligatorios:** Los 11 campos base comunes.
* **Gatillo de Activación:** `tipo_fraude__c` o `modalidad_fraude__c`.
* **Campos Condicionales Obligatorios:**
  * `archivos_s3` (mínimo 1 elemento).
  * `nombre_archivo_fraude` (si `len(archivos_s3) > 1`).
* **Lógica:** Al fallar M3 con `404`, crea el caso en M2 con los datos base y sus anexos, y posteriormente aplica la gestión de fraude, por lo que también aplica lo de `card_amount__c` y `Total_Devuelto_por_Desconocimiento__c`

---

### 6. Momento 3 Cierre Base
* **Campos Base Obligatorios:** Los 11 campos base comunes.
* **Gatillo de Activación:** `Status = "Closed"` (o incluir `ClosedDate` / `Favorabilidad__c`).
* **Campos Obligatorios de Cierre:**
  1. **`Favorabilidad__c`**
  2. **`Aceptacion__c`**
* **Nota de `cuerpo_respuesta_final`:** Opcional. Si no se provee, se asigna automáticamente una plantilla de respuesta genérica.
* **Nota de `ClosedDate`:** Opcional. Si no se provee, se asigna automáticamente la fecha y hora actual de Bogotá.

---

### 7. Momento 3 Cierre y Creación a la vez (Self-Healing)
* **Campos Base Obligatorios:** Los 11 campos base comunes.
* **Gatillo de Activación:** `Status = "Closed"`.
* **Campos Obligatorios de Cierre:**
  1. **`Favorabilidad__c`**
  2. **`Aceptacion__c`**
* **Lógica:** Al fallar con `404`, el orquestador radicará la queja base en Momento 2, generará el PDF de respuesta final y efectuará de inmediato el cierre definitivo.