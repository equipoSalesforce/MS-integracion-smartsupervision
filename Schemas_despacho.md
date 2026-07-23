| Campo	| ¿Es Opcional?	| Valor por Defecto	| Descripción / Nota | 
| ------| ---------------| ------------------| --------------------|
| Case_id | Sí	| null	| Al menos este o Smart_Code__c debe estar presente. | 
| Smart_Code__c	| Sí	| null	| Al menos este o Case_id debe estar presente. | 
| CreatedDate	| No	| N/A (Obligatorio)	| Fecha/Hora de creación en formato ISO. | 
| Status	| Sí	| "New"	| Estado inicial dentro del CRM. | 
| SuppliedName	| No	| N/A (Obligatorio)	| Nombre completo del cliente. | 
| SC_id_type__c	| No	| N/A (Obligatorio)	| Tipo de documento de identificación. | 
| id_number__c	| No	| N/A (Obligatorio)	| Número de documento (máx 15 dígitos). | 
| sc_genero__c	| Sí	| null	| Género del cliente. | 
| tipo_de_persona__c	| No	| N/A (Obligatorio)	| Tipo de persona (B2C, B2B). | 
| sc_LGBTIQ__c	| Sí	| null	| Pertenece a comunidad LGBTIQ. | 
| sc_Condicion_especial__c	| Sí	| null	| Condición de vulnerabilidad. | 
| SuppliedPhone	| Sí	| null	| Teléfono de contacto. | 
| SuppliedEmail	| Sí	| null	| Correo electrónico del cliente. | 
| direccion__c	| No	| N/A (Obligatorio)	| Dirección física de correspondencia. | 
| Departamento__c	| Sí	| null	| Departamento de residencia (si aplica). | 
| SC_municipio__c	| Sí	| null	| Municipio de residencia (si aplica). | 
| canal__c	| Sí	| null	| Canal de ingreso de la reclamación. | 
| punto_recepcion	| No	| N/A (Obligatorio)	| Punto de radicación de la queja. | 
| Instancia_de_recepcion__c	| No	| N/A (Obligatorio)	| Instancia de recepción. | 
| admision_col__c	| Sí	| "No Aplica"	| Estado inicial de admisión. | 
| Description	| No	| N/A (Obligatorio)	| Texto de la reclamación (máx 4500 caracteres). | 
| smart_anexo_queja__c	| Sí	| FALSE	| Indica si posee adjuntos. | 
| Tutela__c	| Sí	| "No"	| Indica si posee acción de tutela. | 
| Ente_de_control__c	| Sí	| null	| Ente regulador involucrado. | 
| smart_escalamiento_DCF__c	| No	| N/A (Obligatorio)	| Escalamiento al Defensor del Consumidor Financiero. | 
| marcacion__c	| Sí	| null	| Marcación especial del caso. | 
| Product__c	| No	| N/A (Obligatorio)	| Línea de producto reclamada. | 
| smart_Producto_nombre__c	| Sí	| null	| Nombre del producto digital. | 
| Categorias_COL__c	| No	| N/A (Obligatorio)	| Motivo o macro categoría de la queja. | 
| archivos_s3	| Sí	| []	| Lista con metadatos de archivos subidos a S3. | 
| producto_digital__c	| Sí	| "Si"	| Opcional (Trámite). | 
| tipo_fraude__c	| Sí	| null	| Requerido si es un evento de Fraude. | 
| modalidad_fraude__c	| Sí	| null	| Requerido si es un evento de Fraude. | 
| card_amount__c	| Sí	| null	| Monto reclamado en fraude. | 
| Total_Devuelto_por_Desconocimiento__c	| Sí	| null	| Monto devuelto en fraude. | 
| nombre_archivo_fraude	| Sí	| null	| Requerido en Fraude si hay más de 1 archivo en archivos_s3. | 
| ClosedDate	| Sí	| null	| Obligatorio para Cierre Definitivo (Momento 3). | 
| Favorabilidad__c	| Sí	| null	| Obligatorio para Cierre Definitivo (Momento 3). | 
| a_favor_de__c	| Sí	| null	| Opcional en Cierre. | 
| Aceptacion__c	| Sí	| null	| Obligatorio para Cierre Definitivo (Momento 3). | 
| Rectificacion__c	| Sí	| null	| Opcional en Cierre. | 
| Prorroga__c	| Sí	| null	| Opcional en Cierre. | 
| cuerpo_respuesta_final	| Sí	| null	| Obligatorio para Cierre Definitivo (construcción de PDF). | 

