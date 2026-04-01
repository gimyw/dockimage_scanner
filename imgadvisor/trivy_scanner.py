from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from imgadvisor.models import TrivyFinding, TrivyScanResult


def scan(
    dockerfile_path: str,
    severity: str = "MEDIUM,HIGH,CRITICAL",
    ignore_unfixed: bool = False,
    timeout_seconds: int = 300,
) -> TrivyScanResult:
    """
    Run Trivy pre-build checks against:
    - the Dockerfile/config itself via `trivy config`
    - the build context filesystem via `trivy fs`

    The command is intentionally split in two because the signal is different:
    config findings describe insecure Dockerfile patterns, while fs findings
    describe vulnerable application dependencies discovered from lockfiles and
    manifest files in the build context.
    """
    dockerfile = Path(dockerfile_path).resolve()
    context_dir = dockerfile.parent

    if shutil.which("trivy") is None:
        raise RuntimeError(
            "Trivy is not installed or not on PATH. Install Trivy first to use `imgadvisor scan`."
        )

    findings: list[TrivyFinding] = []
    findings.extend(
        _run_config_scan(
            dockerfile=dockerfile,
            severity=severity,
            timeout_seconds=timeout_seconds,
        )
    )
    findings.extend(
        _run_fs_scan(
            context_dir=context_dir,
            severity=severity,
            ignore_unfixed=ignore_unfixed,
            timeout_seconds=timeout_seconds,
        )
    )

    return TrivyScanResult(
        dockerfile_path=str(dockerfile),
        context_dir=str(context_dir),
        findings=findings,
    )


def _run_config_scan(
    dockerfile: Path,
    severity: str,
    timeout_seconds: int,
) -> list[TrivyFinding]:
    """
    Scan the Dockerfile as configuration, not as a built image.

    `trivy config` can report misconfigurations such as unpinned tags, root
    user usage, or insecure instructions before any image build happens.
    """
    payload = _run_trivy_command(
        [
            "trivy",
            "config",
            "--format",
            "json",
            "--severity",
            severity,
            str(dockerfile),
        ],
        timeout_seconds=timeout_seconds,
    )
    return _parse_config_findings(payload)


def _run_fs_scan(
    context_dir: Path,
    severity: str,
    ignore_unfixed: bool,
    timeout_seconds: int,
) -> list[TrivyFinding]:
    """
    Scan the Docker build context filesystem for dependency vulnerabilities.

    This is the pre-build approximation for package risk: Trivy inspects files
    such as `requirements.txt`, lockfiles, and manifest files directly from the
    source tree without needing a built container image.
    """
    command = [
        "trivy",
        "fs",
        "--format",
        "json",
        "--severity",
        severity,
        str(context_dir),
    ]
    if ignore_unfixed:
        command.append("--ignore-unfixed")

    payload = _run_trivy_command(command, timeout_seconds=timeout_seconds)
    return _parse_fs_findings(payload)


def _run_trivy_command(command: list[str], timeout_seconds: int) -> list[dict]:
    """
    Execute Trivy and return the decoded JSON payload.

    Trivy typically returns JSON with a top-level `Results` list, but some
    versions wrap differently. The parser therefore accepts both a raw list and
    a dict with a `Results` key.
    """
    env = os.environ.copy()
    env.setdefault("TRIVY_NON_SSL", "false")

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=env,
    )
    if result.returncode not in (0, 5):
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"Trivy command failed ({' '.join(command[:2])}):\n{stderr[-2000:]}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        return []

    try:
        decoded = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Trivy returned non-JSON output for {' '.join(command[:2])}: {exc}"
        ) from exc

    if isinstance(decoded, dict):
        results = decoded.get("Results", [])
        return results if isinstance(results, list) else []
    if isinstance(decoded, list):
        return decoded
    return []


def _parse_config_findings(results: list[dict]) -> list[TrivyFinding]:
    findings: list[TrivyFinding] = []

    for result in results:
        target = str(result.get("Target") or "")
        for misconfig in result.get("Misconfigurations", []) or []:
            cause_metadata = misconfig.get("CauseMetadata") or {}
            location = cause_metadata.get("Location") or {}
            primary_url = misconfig.get("PrimaryURL") or misconfig.get("PrimaryUrl")

            findings.append(
                TrivyFinding(
                    scanner="config",
                    target=target,
                    severity=str(misconfig.get("Severity") or "UNKNOWN"),
                    rule_id=str(
                        misconfig.get("ID")
                        or misconfig.get("AVDID")
                        or misconfig.get("Type")
                        or "TRIVY_CONFIG"
                    ),
                    title=str(misconfig.get("Title") or misconfig.get("Message") or "Misconfiguration"),
                    description=str(misconfig.get("Description") or misconfig.get("Message") or ""),
                    recommendation=str(misconfig.get("Resolution") or "Review and harden this Dockerfile setting."),
                    primary_url=str(primary_url) if primary_url else None,
                    line_no=_coerce_line_no(location.get("StartLine") or location.get("EndLine")),
                    file_path=str(location.get("File")) if location.get("File") else None,
                )
            )

    return findings


def _parse_fs_findings(results: list[dict]) -> list[TrivyFinding]:
    findings: list[TrivyFinding] = []

    for result in results:
        target = str(result.get("Target") or "")

        # `trivy fs` can return multiple finding categories. For pre-build use we
        # prioritize package vulnerabilities because they are the clearest signal
        # for dependency risk before a container image exists.
        for vulnerability in result.get("Vulnerabilities", []) or []:
            primary_url = vulnerability.get("PrimaryURL") or vulnerability.get("PrimaryUrl")
            findings.append(
                TrivyFinding(
                    scanner="fs",
                    target=target,
                    severity=str(vulnerability.get("Severity") or "UNKNOWN"),
                    rule_id=str(vulnerability.get("VulnerabilityID") or "TRIVY_VULN"),
                    title=str(vulnerability.get("Title") or vulnerability.get("PkgName") or "Dependency vulnerability"),
                    description=str(vulnerability.get("Description") or ""),
                    recommendation=_format_vulnerability_recommendation(vulnerability),
                    primary_url=str(primary_url) if primary_url else None,
                    pkg_name=str(vulnerability.get("PkgName")) if vulnerability.get("PkgName") else None,
                    installed_version=str(vulnerability.get("InstalledVersion")) if vulnerability.get("InstalledVersion") else None,
                    fixed_version=str(vulnerability.get("FixedVersion")) if vulnerability.get("FixedVersion") else None,
                    file_path=target or None,
                )
            )

    return findings


def _format_vulnerability_recommendation(vulnerability: dict) -> str:
    fixed_version = vulnerability.get("FixedVersion")
    if fixed_version:
        return f"Upgrade to a fixed version such as `{fixed_version}` or later."
    return "Review the dependency tree and upgrade or replace the vulnerable package."


def _coerce_line_no(value: object) -> int | None:
    if isinstance(value, int):
        return value
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
