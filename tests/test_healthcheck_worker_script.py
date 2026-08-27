# tests/test_healthcheck_worker_script.py
"""
🔴 FIX (hallazgo propio, 2026-08-27, auditoría final de cobertura): este script
tenía 0% de cobertura de tests pese a ser el comando HEALTHCHECK real del
contenedor worker en el Dockerfile (`CMD python /code/infrastructure/
healthcheck_worker.py || exit 1`, interval=30s/retries=3) -- es decir, el único
código que decide si ECS/Docker considera el worker sano o lo reinicia. Ninguna
de sus 3 ramas (archivo ausente, contenido corrupto, heartbeat vencido) ni el
camino sano tenían una sola prueba directa.
"""
import unittest
from unittest.mock import mock_open, patch

try:
    from infrastructure.healthcheck_worker import main, MAX_AGE_SECONDS
except ImportError:
    from healthcheck_worker import main, MAX_AGE_SECONDS


class TestHealthcheckWorkerScript(unittest.TestCase):

    def test_heartbeat_reciente_retorna_sano(self):
        with patch("infrastructure.healthcheck_worker.open", mock_open(read_data="1000.0")), \
             patch("infrastructure.healthcheck_worker.time.time", return_value=1000.0 + 10):
            self.assertEqual(main(), 0)

    def test_heartbeat_justo_en_el_limite_retorna_sano(self):
        with patch("infrastructure.healthcheck_worker.open", mock_open(read_data="1000.0")), \
             patch("infrastructure.healthcheck_worker.time.time", return_value=1000.0 + MAX_AGE_SECONDS):
            self.assertEqual(main(), 0)

    def test_heartbeat_vencido_retorna_no_sano(self):
        with patch("infrastructure.healthcheck_worker.open", mock_open(read_data="1000.0")), \
             patch("infrastructure.healthcheck_worker.time.time", return_value=1000.0 + MAX_AGE_SECONDS + 1):
            self.assertEqual(main(), 1)

    def test_archivo_de_heartbeat_ausente_retorna_no_sano(self):
        with patch("infrastructure.healthcheck_worker.open", side_effect=FileNotFoundError()):
            self.assertEqual(main(), 1)

    def test_contenido_corrupto_retorna_no_sano(self):
        with patch("infrastructure.healthcheck_worker.open", mock_open(read_data="no-es-un-timestamp")):
            self.assertEqual(main(), 1)

    def test_contenido_vacio_retorna_no_sano(self):
        with patch("infrastructure.healthcheck_worker.open", mock_open(read_data="")):
            self.assertEqual(main(), 1)


if __name__ == "__main__":
    unittest.main()
