# app/models/quejas_crud.py
from sqlalchemy.orm import Session
from typing import Optional, Dict, Any, List
from app.models.quejas import Queja

class QuejasCRUD:
    # --- Métodos anteriores del Momento 2 ---
    @staticmethod
    def obtener_queja_por_id(db: Session, smart_code: str) -> Optional[Queja]:
        return db.query(Queja).filter(Queja.Smart_Code__c == smart_code).first()

    @staticmethod
    def obtener_datos_consolidados_caso(db: Session, smart_code: str) -> Optional[Dict[str, Any]]:
        # ... (tu query multi-tabla consolidada) ...
        pass

    @staticmethod
    def actualizar_estado_caso(db: Session, smart_code: str, status_smart: str, sfc_status: Optional[str] = None) -> bool:
        queja = db.query(Queja).filter(Queja.Smart_Code__c == smart_code).first()
        if not queja:
            return False
        queja.status_smart = status_smart
        if sfc_status:
            queja.Smart_Status__c = sfc_status
        db.commit()
        return True

    # ======================================================================
    # NUEVOS MÉTODOS AÑADIDOS PARA EL MOMENTO 1
    # ======================================================================

    @staticmethod
    def obtener_quejas_por_estado(db: Session, status_smart: str) -> List[Queja]:
        """
        Obtiene todas las quejas que coinciden con un estado_smart específico.
        """
        return db.query(Queja).filter(Queja.status_smart == status_smart).all()

    @staticmethod
    def guardar_nueva_queja(db: Session, nueva_queja: Queja, commit: bool = True) -> Queja:
        """
        Registra una nueva queja mapeada en la base de datos.
        Permite controlar el commit para inserciones por lotes de alto rendimiento.
        """
        db.add(nueva_queja)
        if commit:
            db.commit()
        return nueva_queja

    @staticmethod
    def ejecutar_commit(db: Session):
        """Fuerza la persistencia de transacciones pendientes (ej. después de procesar una página)."""
        db.commit()

    @staticmethod
    def procesar_actualizacion_ack_lote(
        db: Session, 
        quejas_lote: List[Queja], 
        errores_sfc: List[str], 
        status_ok: str, 
        status_error: str
    ):
        """
        Actualiza en un solo lote relacional los estados de control de las quejas 
        dependiendo de si fueron rechazadas o aceptadas por el ACK de la SFC.
        """
        for queja in quejas_lote:
            if queja.Smart_Code__c in errores_sfc:
                queja.status_smart = status_error
            else:
                queja.status_smart = status_ok
        db.commit()