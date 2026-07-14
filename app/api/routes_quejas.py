from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from app.api.dependencies import get_db, get_sfc_client
from app.integrations.sfc_client import SfcClient
from app.services.momento_1_sync import SincronizacionService
from app.models.quejas import Queja

router = APIRouter()

@router.post(
    "/sync/momento-1", 
    status_code=status.HTTP_200_OK, 
    summary="Ejecutar Sincronización Completa del Momento 1 (Para Cron Job)"
)
async def ejecutar_sync_momento_1(
    db: Session = Depends(get_db),
    sfc_client: SfcClient = Depends(get_sfc_client)
):
    """
    Inicia el flujo automatizado secuencial para capturar la información de la SFC.
    
    1. Trae quejas nuevas de la nube de la SFC.
    2. Descarga de forma asíncrona sus archivos adjuntos y los envía a S3.
    3. Envía el reporte de confirmación recibido (ACK) en lotes para liberar el backlog.
    """
    try:
        # Instanciamos el servicio utilizando las dependencias de la petición
        servicio = SincronizacionService(sfc_client=sfc_client, db=db)
        
        # Ejecutamos el pipeline unificado de la máquina de estados
        resultado = await servicio.ejecutar_flujo_completo_momento_1()
        return resultado
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fallo crítico en el proceso de sincronización automática de la SFC: {str(e)}"
        )

@router.get(
    "/", 
    status_code=status.HTTP_200_OK,
    summary="Listar quejas locales para el CRM"
)
def listar_quejas(
    status_smart: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """
    Endpoint para el CRM.
    Retorna la lista de quejas persistidas localmente en el microservicio.
    Permite filtrar por Smart Status (ej. 'reportACK-OK' o 'FileDownload-ERROR').
    """
    query = db.query(Queja)
    
    if status_smart:
        query = query.filter(Queja.status_smart == status_smart)
        
    total = query.count()
    quejas = query.offset(offset).limit(limit).all()
    
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [
            {
                "codigo_queja": q.codigo_queja,
                "status_smart": q.status_smart,
                "tipo_entidad": q.tipo_entidad,
                "entidad_cod": q.entidad_cod,
                "fecha_creacion": q.fecha_creacion.isoformat() if q.fecha_creacion else None,
                "nombres": q.nombres,
                "numero_id_CF": q.numero_id_CF,
                "texto_queja": q.texto_queja,
                "anexo_queja": q.anexo_queja
            }
            for q in quejas
        ]
    }

@router.get(
    "/{codigo_queja}", 
    status_code=status.HTTP_200_OK,
    summary="Obtener detalle de una queja por ID"
)
def obtener_queja(
    codigo_queja: str,
    db: Session = Depends(get_db)
):
    """
    Endpoint para el CRM.
    Busca una queja específica en la base de datos local y retorna su información completa.
    """
    queja = db.query(Queja).filter(Queja.codigo_queja == codigo_queja).first()
    
    if not queja:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No se encontró ninguna queja local con el código {codigo_queja}."
        )
        
    return {
        "codigo_queja": queja.codigo_queja,
        "status_smart": queja.status_smart,
        "tipo_entidad": queja.tipo_entidad,
        "entidad_cod": queja.entidad_cod,
        "fecha_creacion": queja.fecha_creacion.isoformat() if queja.fecha_creacion else None,
        "nombres": queja.nombres,
        "numero_id_CF": queja.numero_id_CF,
        "texto_queja": queja.texto_queja,
        "anexo_queja": queja.anexo_queja,
        "macro_motivo_cod": queja.macro_motivo_cod,
        "producto_cod": queja.producto_cod,
        "canal_cod": queja.canal_cod
    }