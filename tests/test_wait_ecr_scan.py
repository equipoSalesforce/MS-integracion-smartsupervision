import json
import os
import subprocess
import unittest

from scripts.wait_ecr_scan import ScanError, ScanTimeoutError, wait_for_scan


WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".github", "workflows", "deploy-aws.yml"
)


def _result(payload=None, *, error=None):
    if error is not None:
        return subprocess.CompletedProcess(
            args=[], returncode=254, stdout="", stderr=error
        )
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(payload), stderr=""
    )


def _scan(status="COMPLETE", critical=0):
    return {"status": status, "critical": critical}


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class SequenceRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if not self.results:
            raise AssertionError("El test agotó la secuencia de respuestas AWS.")
        return self.results.pop(0)


class TestWaitEcrScan(unittest.TestCase):
    def run_wait(self, results, timeout=30):
        runner = SequenceRunner(results)
        fake_time = FakeTime()
        critical = wait_for_scan(
            "repo-configurable",
            "sha-tag",
            "us-east-1",
            retry_interval_seconds=5,
            timeout_seconds=timeout,
            run_command=runner,
            clock=fake_time.clock,
            sleep=fake_time.sleep,
        )
        return critical, runner, fake_time

    def test_scan_disponible_inmediatamente_pasa(self):
        critical, runner, fake_time = self.run_wait([_result(_scan())])
        self.assertEqual(critical, 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(fake_time.sleeps, [])

    def test_scan_not_found_una_vez_reintenta_y_pasa(self):
        critical, runner, fake_time = self.run_wait(
            [
                _result(error="ScanNotFoundException: scan does not exist"),
                _result(_scan()),
            ]
        )
        self.assertEqual(critical, 0)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(fake_time.sleeps, [5])

    def test_scan_not_found_multiples_veces_reintenta_y_pasa(self):
        critical, runner, fake_time = self.run_wait(
            [
                _result(error="ScanNotFoundException"),
                _result(error="ScanNotFoundException"),
                _result(_scan("IN_PROGRESS")),
                _result(_scan()),
            ]
        )
        self.assertEqual(critical, 0)
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(fake_time.sleeps, [5, 5, 5])

    def test_timeout_esperando_scan_falla(self):
        runner = SequenceRunner(
            [
                _result(error="ScanNotFoundException"),
                _result(error="ScanNotFoundException"),
                _result(error="ScanNotFoundException"),
            ]
        )
        fake_time = FakeTime()
        with self.assertRaises(ScanTimeoutError):
            wait_for_scan(
                "repo",
                "tag",
                "us-east-1",
                retry_interval_seconds=5,
                timeout_seconds=10,
                run_command=runner,
                clock=fake_time.clock,
                sleep=fake_time.sleep,
            )
        self.assertEqual(fake_time.sleeps, [5, 5])

    def test_complete_con_critical_cero_pasa(self):
        critical, _, _ = self.run_wait([_result(_scan(critical=0))])
        self.assertEqual(critical, 0)

    def test_complete_con_critical_mayor_a_cero_falla(self):
        with self.assertRaisesRegex(ScanError, "8 vulnerabilidades CRITICAL"):
            self.run_wait([_result(_scan(critical=8))])

    def test_error_aws_no_transitorio_falla_sin_retry(self):
        runner = SequenceRunner(
            [_result(error="AccessDeniedException: not authorized")]
        )
        fake_time = FakeTime()
        with self.assertRaisesRegex(ScanError, "error no transitorio"):
            wait_for_scan(
                "repo",
                "tag",
                "us-east-1",
                run_command=runner,
                clock=fake_time.clock,
                sleep=fake_time.sleep,
            )
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(fake_time.sleeps, [])

    def test_estado_failed_falla_sin_retry(self):
        with self.assertRaisesRegex(ScanError, "'FAILED'"):
            self.run_wait([_result(_scan("FAILED"))])

    def test_nombre_de_repositorio_se_pasa_al_aws_cli(self):
        _, runner, _ = self.run_wait([_result(_scan())])
        command = runner.calls[0][0]
        index = command.index("--repository-name")
        self.assertEqual(command[index + 1], "repo-configurable")


class TestEcrScanWorkflowGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW_PATH, "r", encoding="utf-8") as workflow_file:
            cls.workflow = workflow_file.read()

    def test_workflow_usa_helper_con_repo_y_tag_configurables(self):
        self.assertIn("python scripts/wait_ecr_scan.py", self.workflow)
        self.assertIn("${{ vars.ECR_REPOSITORY_NAME }}", self.workflow)
        self.assertIn("${{ steps.vars.outputs.image_tag }}", self.workflow)
        self.assertIn("--retry-interval-seconds 5", self.workflow)
        self.assertIn("--timeout-seconds 300", self.workflow)

    def test_deploy_depende_del_build_y_scan_sin_continuar_en_error(self):
        deploy_start = self.workflow.index("\n  deploy:\n")
        deploy_header = self.workflow[deploy_start : deploy_start + 300]
        self.assertIn("needs: build-and-push", deploy_header)
        scan_step_start = self.workflow.index(
            "- name: Verificar hallazgos críticos del scan de ECR"
        )
        scan_step = self.workflow[scan_step_start:deploy_start]
        self.assertNotIn("continue-on-error", scan_step)


if __name__ == "__main__":
    unittest.main()
