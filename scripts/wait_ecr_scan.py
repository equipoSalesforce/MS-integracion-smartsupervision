"""Wait for an ECR basic image scan and enforce the CRITICAL findings gate.

Immediately after an image push, ECR can briefly return ScanNotFoundException
even when scan-on-push is enabled. This helper treats only that specific
eventual-consistency window and an IN_PROGRESS scan as retryable conditions.
All other AWS errors and terminal scan states fail closed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any


class ScanError(RuntimeError):
    """The scan could not be validated safely."""


class ScanTimeoutError(ScanError):
    """The scan did not become available and complete before the deadline."""


def _describe_scan(
    repository_name: str,
    image_tag: str,
    region: str,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any] | None:
    command = [
        "aws",
        "ecr",
        "describe-image-scan-findings",
        "--repository-name",
        repository_name,
        "--image-id",
        f"imageTag={image_tag}",
        "--region",
        region,
        "--query",
        "{status:imageScanStatus.status,critical:imageScanFindings.findingSeverityCounts.CRITICAL}",
        "--output",
        "json",
    ]
    result = run_command(command, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        error_output = "\n".join((result.stdout or "", result.stderr or ""))
        if "ScanNotFoundException" in error_output:
            return None
        raise ScanError(
            "AWS rechazó describe-image-scan-findings con un error no transitorio: "
            f"{(result.stderr or result.stdout).strip()}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ScanError("AWS devolvió una respuesta de scan que no es JSON válido.") from exc


def wait_for_scan(
    repository_name: str,
    image_tag: str,
    region: str,
    retry_interval_seconds: float = 5.0,
    timeout_seconds: float = 300.0,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Return the CRITICAL count after a COMPLETE scan, or raise ScanError."""

    if retry_interval_seconds <= 0 or timeout_seconds <= 0:
        raise ValueError("retry_interval_seconds y timeout_seconds deben ser mayores que cero.")

    deadline = clock() + timeout_seconds
    attempt = 0

    while True:
        attempt += 1
        scan = _describe_scan(repository_name, image_tag, region, run_command)

        if scan is None:
            state = "ScanNotFoundException"
        else:
            state = scan.get("status")

            if state == "COMPLETE":
                critical = int(scan.get("critical") or 0)
                print(f"ECR scan COMPLETE. CRITICAL findings: {critical}", flush=True)
                if critical > 0:
                    raise ScanError(
                        f"La imagen contiene {critical} vulnerabilidades CRITICAL."
                    )
                return critical

            if state != "IN_PROGRESS":
                raise ScanError(
                    f"El scan ECR terminó o respondió con estado no aceptable "
                    f"{state!r}."
                )

        remaining = deadline - clock()
        if remaining <= 0:
            raise ScanTimeoutError(
                f"Timeout de {timeout_seconds:g}s esperando que el scan ECR "
                f"aparezca y termine (último estado: {state})."
            )

        delay = min(retry_interval_seconds, remaining)
        print(
            f"ECR scan aún no disponible/completo ({state}); "
            f"retry {attempt} en {delay:g}s.",
            flush=True,
        )
        sleep(delay)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Espera el scan ECR y exige cero findings CRITICAL."
    )
    parser.add_argument("--repository-name", required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--retry-interval-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        wait_for_scan(
            repository_name=args.repository_name,
            image_tag=args.image_tag,
            region=args.region,
            retry_interval_seconds=args.retry_interval_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    except (ScanError, ValueError) as exc:
        print(f"[ECR SCAN] FAIL: {exc}", file=sys.stderr)
        return 1

    print("[ECR SCAN] PASS: scan COMPLETE y CRITICAL=0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
