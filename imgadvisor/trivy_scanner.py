"""
Trivy를 이용한 pre-build 취약점 및 설정 문제 스캔.

이미지를 빌드하지 않고 두 가지 방식으로 스캔한다:
  1. `trivy config` — Dockerfile 자체의 설정 문제 탐지
     (예: root 실행, 태그 고정 미사용, 위험한 명령어 패턴)
  2. `trivy fs`     — 빌드 컨텍스트의 의존성 취약점 탐지
     (예: requirements.txt, package-lock.json 등의 CVE)

두 스캔을 분리하는 이유:
  config는 Dockerfile 패턴 문제를, fs는 라이브러리 취약점을 탐지하므로
  신호의 성격이 달라 분리해서 표시하는 것이 사용자에게 더 명확하다.
"""
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
    Trivy pre-build 스캔을 실행하고 TrivyScanResult를 반환한다.

    내부적으로 `trivy config`와 `trivy fs` 두 명령을 순서대로 실행한다.
    Trivy가 설치되어 있지 않으면 즉시 RuntimeError를 발생시킨다.

    Args:
        dockerfile_path : 분석할 Dockerfile 경로
        severity        : 필터링할 심각도 (쉼표 구분, 예: "HIGH,CRITICAL")
        ignore_unfixed  : 수정 버전이 없는 취약점 제외 여부
        timeout_seconds : Trivy 명령 타임아웃 (초)

    Returns:
        TrivyScanResult: config + fs 스캔 결과 합산

    Raises:
        RuntimeError: Trivy가 없거나 실행 실패 시
    """
    dockerfile = Path(dockerfile_path).resolve()
    context_dir = dockerfile.parent  # 빌드 컨텍스트 = Dockerfile 위치 디렉토리

    # Trivy 설치 여부 확인 (PATH에서 trivy 바이너리 탐색)
    if shutil.which("trivy") is None:
        raise RuntimeError(
            "Trivy is not installed or not on PATH. Install Trivy first to use `imgadvisor scan`."
        )

    findings: list[TrivyFinding] = []

    # 1단계: Dockerfile 설정 문제 스캔
    findings.extend(
        _run_config_scan(
            dockerfile=dockerfile,
            severity=severity,
            timeout_seconds=timeout_seconds,
        )
    )

    # 2단계: 빌드 컨텍스트 의존성 취약점 스캔
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
    `trivy config`로 Dockerfile 설정 문제를 스캔한다.

    이미지 빌드 없이 Dockerfile 텍스트만 분석하므로 속도가 빠르다.
    탐지 예시:
      - root 사용자로 실행 (USER 명령 없음)
      - :latest 태그 사용 (버전 고정 미사용)
      - HEALTHCHECK 없음
      - ADD 대신 COPY 사용 권장

    Args:
        dockerfile      : 분석할 Dockerfile Path 객체
        severity        : 심각도 필터
        timeout_seconds : 타임아웃

    Returns:
        TrivyFinding 목록 (scanner="config")
    """
    payload = _run_trivy_command(
        [
            "trivy",
            "config",
            "--format", "json",
            "--severity", severity,
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
    `trivy fs`로 빌드 컨텍스트의 의존성 취약점을 스캔한다.

    빌드 컨텍스트 디렉토리를 직접 탐색해 requirements.txt, package-lock.json,
    Pipfile.lock, go.sum 등의 lockfile에서 알려진 CVE를 찾는다.
    이미지 빌드 없이 소스 파일만으로 취약점을 사전 탐지할 수 있다.

    Args:
        context_dir     : 스캔할 빌드 컨텍스트 디렉토리
        severity        : 심각도 필터
        ignore_unfixed  : 수정 버전 없는 취약점 제외 여부
        timeout_seconds : 타임아웃

    Returns:
        TrivyFinding 목록 (scanner="fs")
    """
    command = [
        "trivy",
        "fs",
        "--format", "json",
        "--severity", severity,
        str(context_dir),
    ]
    # --ignore-unfixed: 아직 패치가 없는 취약점은 노이즈가 많으므로 제외 옵션 제공
    if ignore_unfixed:
        command.append("--ignore-unfixed")

    payload = _run_trivy_command(command, timeout_seconds=timeout_seconds)
    return _parse_fs_findings(payload)


def _run_trivy_command(command: list[str], timeout_seconds: int) -> list[dict]:
    """
    Trivy 명령을 실행하고 JSON 결과를 파싱해서 반환한다.

    Trivy 종료 코드:
      0: 취약점 없음 (정상)
      5: 스캔 대상이 없음 (정상 — 빈 디렉토리 등)
      그 외: 오류

    Trivy JSON 구조:
      - 최상위가 dict이면 {"Results": [...]} 형태에서 Results 추출
      - 최상위가 list이면 그대로 사용 (버전에 따라 다름)

    Args:
        command         : 실행할 trivy 명령 리스트
        timeout_seconds : subprocess 타임아웃

    Returns:
        Results 항목 목록 (각 항목은 target별 결과 dict)

    Raises:
        RuntimeError: 명령 실패 또는 JSON 파싱 오류 시
    """
    env = os.environ.copy()
    env.setdefault("TRIVY_NON_SSL", "false")  # SSL 검증 기본 활성화

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=env,
    )

    # 0(정상), 5(스캔 대상 없음)만 정상 처리
    if result.returncode not in (0, 5):
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"Trivy command failed ({' '.join(command[:2])}):\n{stderr[-2000:]}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        return []  # 출력 없으면 빈 결과

    try:
        decoded = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Trivy returned non-JSON output for {' '.join(command[:2])}: {exc}"
        ) from exc

    # Trivy 버전에 따라 최상위 구조가 다름
    if isinstance(decoded, dict):
        results = decoded.get("Results", [])
        return results if isinstance(results, list) else []
    if isinstance(decoded, list):
        return decoded
    return []


def _parse_config_findings(results: list[dict]) -> list[TrivyFinding]:
    """
    `trivy config` JSON 결과에서 TrivyFinding 목록을 생성한다.

    각 result는 하나의 파일(target)이고, 그 안의 Misconfigurations 목록을
    순회하며 Finding으로 변환한다.

    CauseMetadata.Location에서 줄 번호와 파일 경로를 추출한다.
    """
    findings: list[TrivyFinding] = []

    for result in results:
        target = str(result.get("Target") or "")
        for misconfig in result.get("Misconfigurations", []) or []:
            # CauseMetadata에 줄 번호 정보가 있음 (없으면 None)
            cause_metadata = misconfig.get("CauseMetadata") or {}
            location = cause_metadata.get("Location") or {}
            primary_url = misconfig.get("PrimaryURL") or misconfig.get("PrimaryUrl")

            findings.append(
                TrivyFinding(
                    scanner="config",
                    target=target,
                    severity=str(misconfig.get("Severity") or "UNKNOWN"),
                    # ID 필드명이 Trivy 버전마다 다를 수 있어 여러 키를 순서대로 시도
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
    """
    `trivy fs` JSON 결과에서 TrivyFinding 목록을 생성한다.

    각 result는 스캔된 lockfile/manifest이고, Vulnerabilities 목록을
    순회하며 Finding으로 변환한다.

    패키지 이름, 설치 버전, 수정 버전 정보를 포함해 사용자가
    어떤 패키지를 어떤 버전으로 업그레이드해야 하는지 파악할 수 있게 한다.
    """
    findings: list[TrivyFinding] = []

    for result in results:
        target = str(result.get("Target") or "")

        # `trivy fs`는 여러 카테고리를 반환할 수 있지만
        # pre-build 용도에서는 패키지 취약점(CVE)이 가장 중요한 신호
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
    """
    취약점 dict에서 권고 문구를 생성한다.

    수정 버전이 있으면 구체적인 버전을 안내하고,
    없으면 일반적인 의존성 트리 검토를 권고한다.
    """
    fixed_version = vulnerability.get("FixedVersion")
    if fixed_version:
        return f"Upgrade to a fixed version such as `{fixed_version}` or later."
    return "Review the dependency tree and upgrade or replace the vulnerable package."


def _coerce_line_no(value: object) -> int | None:
    """
    줄 번호 값을 안전하게 int로 변환한다.

    Trivy JSON에서 줄 번호가 int, str, None 등 다양한 타입으로 올 수 있어
    타입 안전하게 변환한다. 변환 실패 시 None을 반환한다.
    """
    if isinstance(value, int):
        return value
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
