# tests/test_idempotency_hash_canonicalization.py
import unittest

from app.services.idempotency_service import IdempotencyService


class TestIdempotencyHashCanonicalization(unittest.TestCase):
    """🟢 FIX P0-11: el hash de idempotencia no debe depender de campos con
    auto-relleno no determinista (CreatedDate/ClosedDate rellenados con la
    fecha/hora ACTUAL cuando el cliente los omite)."""

    def setUp(self):
        self.base = {
            "Smart_Code__c": "SC-1",
            "Status": "New",
            "SuppliedName": "Juan Perez",
        }

    def test_created_date_omitido_con_distinto_auto_relleno_mismo_hash(self):
        """Dos reintentos del MISMO request (CreatedDate omitido) no deben producir
        hashes distintos sólo porque cada uno fue procesado en un segundo distinto."""
        p1 = dict(self.base, CreatedDate="2026-08-14T10:00:00")
        p2 = dict(self.base, CreatedDate="2026-08-14T10:00:05")

        self.assertEqual(
            IdempotencyService.compute_payload_hash(p1),
            IdempotencyService.compute_payload_hash(p2),
        )

    def test_closed_date_omitido_con_distinto_auto_relleno_mismo_hash(self):
        """Igual que CreatedDate, pero para ClosedDate cruzando la medianoche."""
        p1 = dict(self.base, CreatedDate="2026-08-14T10:00:00", ClosedDate="2026-08-14")
        p2 = dict(self.base, CreatedDate="2026-08-14T10:00:00", ClosedDate="2026-08-15")

        self.assertEqual(
            IdempotencyService.compute_payload_hash(p1),
            IdempotencyService.compute_payload_hash(p2),
        )

    def test_cambio_real_de_negocio_si_cambia_el_hash(self):
        """La exclusión de CreatedDate/ClosedDate no debe volver el hash insensible a
        cambios reales del payload (Status, montos, catálogos, etc.)."""
        p1 = dict(self.base, CreatedDate="2026-08-14T10:00:00")
        p2 = dict(self.base, CreatedDate="2026-08-14T10:00:00", Status="Closed")

        self.assertNotEqual(
            IdempotencyService.compute_payload_hash(p1),
            IdempotencyService.compute_payload_hash(p2),
        )

    def test_hash_es_determinista_para_el_mismo_payload(self):
        """El orden de las llaves del dict no debe afectar el hash resultante."""
        p1 = {"a": 1, "b": 2, "CreatedDate": "2026-08-14T10:00:00"}
        p2 = {"CreatedDate": "2026-08-14T23:59:59", "b": 2, "a": 1}

        self.assertEqual(
            IdempotencyService.compute_payload_hash(p1),
            IdempotencyService.compute_payload_hash(p2),
        )


if __name__ == "__main__":
    unittest.main()
