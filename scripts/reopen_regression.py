"""Run the relevant regressions with external network access forbidden."""
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["TEST_REDIS_URL"] = "redis://127.0.0.1:56379/15"
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "56379"

original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex


def local_only(original):
    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError("External network forbidden during REOPEN QA")
        return original(sock, address)
    return connect


if __name__ == "__main__":
    prefixes = ("test_momento_", "test_integration_momento_", "test_crm_payloads_",
                "test_crm_storage_", "test_despacho_", "test_routes_quejas_despacho",
                "test_queue_", "test_scheduler_", "test_idempotency_", "test_reopen_")
    modules = ["tests." + path.stem for path in sorted((ROOT / "tests").glob("test_*.py"))
               if path.stem.startswith(prefixes)]
    with patch("socket.socket.connect", local_only(original_connect)), \
         patch("socket.socket.connect_ex", local_only(original_connect_ex)), \
         contextlib.redirect_stdout(io.StringIO()):
        suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
        # Do not disable loggers: several regressions assert the audit events.
        logging.getLogger().handlers = [logging.NullHandler()]
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    summary = {"run": result.testsRun, "failures": len(result.failures),
               "errors": len(result.errors), "skipped": len(result.skipped),
               "failed_ids": [test.id() for test, _ in result.failures + result.errors],
               "skip_reasons": [(test.id(), reason) for test, reason in result.skipped],
               "real_sfc_calls": False, "external_network": "FORBIDDEN", "modules": modules}
    print(json.dumps(summary, indent=2))
    sys.exit(not result.wasSuccessful())
