from enum import Enum

class SmartStatus(str, Enum):
    # --- MOMENTO I ---
    CREATED = "Created"                                 # Caso obtenido desde SFC y creado en CRM
    FILE_DOWNLOAD_OK = "FileDownload-OK"               # Éxito al descargar adjuntos desde la SFC
    FILE_DOWNLOAD_ERROR = "FileDownload-ERROR"         # Error al descargar archivos desde la SFC
    REPORT_ACK_OK = "reportACK-OK"                     # Éxito al reportar como recibido a la SFC
    REPORT_ACK_ERROR = "reportACK-ERROR"               # Error al reportar como recibido

    # --- MOMENTO II ---
    SEND_TO_SMART_OK = "sendToSmart-OK"                 # Éxito al reportar queja con la SFC
    SEND_TO_SMART_ERROR = "sendToSmart-Error"           # Error al reportar queja con la SFC

    # --- MOMENTO III ---
    SEND_UPDATE_SMART_OK = "SendUpdateSmart-OK"         # Éxito al actualizar información a la SFC
    SEND_UPDATE_SMART_ERROR = "SendUpdateSmart-Error"   # Error al actualizar información a la SFC
    FINAL_DOCUMENT_UPLOAD_OK = "FinalDocumentUpload-OK" # Éxito al subir documento de cierre
    FINAL_DOCUMENT_UPLOAD_ERROR = "FinalDocumentUpload-Error" # Error al subir documento de cierre
    

class SfcEndpoints(str, Enum):
    QUEJA = "/api/queja/"
    STORAGE = "/api/storage/"
    ACK_COMPLAINT = "/api/complaint/ack"
    USUARIOS = "/api/usuarios/info/"
    USUARIOS_ACK = "/api/usuarios/ack/"