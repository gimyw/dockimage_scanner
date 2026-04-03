"""
Python runtime-specific analysis rules.

This module stays narrow on purpose. The project now treats Python as the one
language that gets deeper runtime-aware guidance instead of shallow cross-
language heuristics.

Current scope:
1. Recommend baseline container-friendly Python env vars.
2. Detect conflicting values for those env vars.
3. Warn when the runtime command uses a development-oriented server pattern.
4. Infer a safer runtime command when it can be rewritten conservatively.
"""
from __future__ import annotations

import re

from imgadvisor.models import DockerInstruction, DockerfileIR, Finding, Severity, Stage

# These defaults are intentionally conservative and container-oriented.
_BASELINE_ENVS: dict[str, str] = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}

_PIP_ENVS: dict[str, str] = {
    "PIP_NO_CACHE_DIR": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}


def check(ir: DockerfileIR) -> list[Finding]:
    """
    final stage가 Python 이미지일 때 runtime 관련 Finding을 수집한다.

    이 함수는 Python runtime 정책의 진입점이다. 현재는 크게 세 가지를 본다.
    1. 컨테이너 친화적인 기본 ENV가 빠져 있는지
    2. 그 ENV가 있더라도 권장값과 충돌하는지
    3. CMD/ENTRYPOINT가 개발용 서버 패턴인지

    반환값은 Python final stage에 대해서만 의미가 있다. Python이 아닌 경우에는
    빈 리스트를 바로 반환해서 다른 rule에 판단을 넘긴다.
    """
    final = ir.final_stage
    if final is None or not is_python_stage(final):
        return []

    findings: list[Finding] = []
    env_map, env_lines = collect_python_env_map(final)

    desired = dict(_BASELINE_ENVS)
    if re.search(r"\bpip(?:3)?\s+install\b", final.all_run_text, re.IGNORECASE):
        desired.update(_PIP_ENVS)

    missing = [name for name, value in desired.items() if env_map.get(name) is None]
    conflicts = [
        (name, env_map[name], desired[name], env_lines.get(name))
        for name in desired
        if env_map.get(name) is not None and env_map[name] != desired[name]
    ]

    if missing:
        recommendation = "\n".join(f"ENV {name}={desired[name]}" for name in missing)
        findings.append(
            Finding(
                rule_id="PYTHON_RUNTIME_ENVS_MISSING",
                severity=Severity.MEDIUM,
                line_no=_suggest_env_line(final),
                description="python runtime env defaults are missing: " + ", ".join(missing),
                recommendation=recommendation,
                saving_min_mb=0,
                saving_max_mb=0,
            )
        )

    if conflicts:
        recommendation = "\n".join(f"ENV {name}={expected}" for name, _, expected, _ in conflicts)
        findings.append(
            Finding(
                rule_id="PYTHON_RUNTIME_ENVS_CONFLICT",
                severity=Severity.MEDIUM,
                line_no=min(line_no for _, _, _, line_no in conflicts if line_no is not None),
                description="python runtime env values conflict with container-safe defaults",
                recommendation=recommendation,
                saving_min_mb=0,
                saving_max_mb=0,
            )
        )

    findings.extend(_check_python_runtime_command(final))
    return findings


def is_python_stage(final: Stage) -> bool:
    """
    주어진 stage가 Python 런타임으로 볼 수 있는지 판정한다.

    현재 기준은 보수적이면서도 단순하다.
    - base image가 `python:*` 이면 Python stage로 본다.
    - base image명이 명확하지 않아도 `pip install` 흔적이 있으면 Python으로 본다.
    """
    if re.match(r"^python:", final.base_image, re.IGNORECASE):
        return True
    return bool(re.search(r"\bpip(?:3)?\s+install\b", final.all_run_text, re.IGNORECASE))


def collect_python_env_map(final: Stage) -> tuple[dict[str, str], dict[str, int]]:
    """
    final stage의 ENV instruction을 파싱해서 최종 key/value 맵을 만든다.

    Dockerfile에서는 같은 ENV 키가 여러 번 다시 선언될 수 있다.
    이 함수는 "마지막 선언이 실제 효력"이라는 Dockerfile 특성에 맞춰
    마지막으로 본 값을 유지한다.

    반환값:
    - env_map: 최종 ENV 값
    - env_lines: 각 ENV 키가 마지막으로 선언된 line number
    """
    env_map: dict[str, str] = {}
    env_lines: dict[str, int] = {}
    for instr in final.instructions:
        if instr.instruction != "ENV":
            continue
        for key, value in _parse_env_assignments(instr.arguments):
            env_map[key] = value
            env_lines[key] = instr.line_no
    return env_map, env_lines


def recommended_python_env_lines(final: Stage) -> list[str]:
    """
    generated Dockerfile에 추가할 Python 권장 ENV 라인을 계산한다.

    이미 같은 값이 정확히 선언돼 있으면 다시 추가하지 않는다.
    반대로 값이 없거나 다른 값이면 권장값을 다시 넣어서 generated output에서
    runtime 기본값이 일관되게 맞춰지도록 한다.
    """
    desired = dict(_BASELINE_ENVS)
    if re.search(r"\bpip(?:3)?\s+install\b", final.all_run_text, re.IGNORECASE):
        desired.update(_PIP_ENVS)
    current_envs, _ = collect_python_env_map(final)
    return [
        f"ENV {name}={value}"
        for name, value in desired.items()
        if current_envs.get(name) != value
    ]


def recommended_python_runtime_command(final: Stage) -> list[str] | None:
    """
    안전하게 추론 가능한 경우에만 더 나은 runtime command를 생성한다.

    현재 자동 교체 범위는 의도적으로 좁다.
    - `flask run ...` 은 production용 `gunicorn` 명령으로 교체
    - `uvicorn ...` 은 기존 형태를 유지하되 `--workers 2` 추가

    추론이 애매하면 None을 반환한다. 이 경우 caller는 원래 CMD/ENTRYPOINT를
    그대로 유지해야 하며, rule은 경고만 제공한다.
    """
    for instr in final.instructions:
        if instr.instruction not in {"CMD", "ENTRYPOINT"}:
            continue

        command = instr.arguments.strip()
        lowered = command.lower()

        if "flask run" in lowered:
            target = _infer_flask_app_target(final)
            if target is None:
                return None
            port = _extract_option_value(command, "--port") or "5000"
            bind = f"0.0.0.0:{port}"
            return [f'CMD ["gunicorn", "-w", "2", "-b", "{bind}", "{target}"]']

        if "uvicorn" in lowered and "--workers" not in lowered and "gunicorn" not in lowered:
            rewritten = _append_json_or_shell_flag(command, "--workers", "2")
            return [f"{instr.instruction} {rewritten}"]

    return None


def _check_python_runtime_command(final: Stage) -> list[Finding]:
    """
    Python 컨테이너에서 흔한 runtime command 문제를 Finding으로 변환한다.

    이 함수는 실제 command rewrite를 수행하지 않는다.
    대신 어떤 패턴이 왜 문제인지와, 자동 추론이 가능하다면 어떤 command가
    더 적절한지를 recommendation에 담아서 반환한다.
    """
    findings: list[Finding] = []
    for instr in final.instructions:
        if instr.instruction not in {"CMD", "ENTRYPOINT"}:
            continue

        command = instr.arguments.strip()
        lowered = command.lower()

        if "flask run" in lowered:
            inferred = recommended_python_runtime_command(final)
            recommendation = (
                "Use a production WSGI server."
                if inferred is None
                else "Use a production WSGI server, for example:\n" + "\n".join(inferred)
            )
            findings.append(
                Finding(
                    rule_id="PYTHON_DEV_SERVER_IN_RUNTIME",
                    severity=Severity.HIGH,
                    line_no=instr.line_no,
                    description="flask development server is configured as the container entrypoint",
                    recommendation=recommendation,
                    saving_min_mb=0,
                    saving_max_mb=0,
                )
            )
            continue

        if "uvicorn" in lowered and "--workers" not in lowered and "gunicorn" not in lowered:
            inferred = recommended_python_runtime_command(final)
            recommendation = (
                'Set an explicit worker count for CPU-bound deployment.'
                if inferred is None
                else "Set an explicit worker count for CPU-bound deployment, for example:\n" + "\n".join(inferred)
            )
            findings.append(
                Finding(
                    rule_id="PYTHON_ASGI_WORKERS_NOT_SET",
                    severity=Severity.MEDIUM,
                    line_no=instr.line_no,
                    description="uvicorn is running without an explicit worker setting",
                    recommendation=recommendation,
                    saving_min_mb=0,
                    saving_max_mb=0,
                )
            )

    return findings


def _parse_env_assignments(arguments: str) -> list[tuple[str, str]]:
    """
    ENV instruction 문자열에서 `KEY=value` 쌍만 추출한다.

    Dockerfile의 ENV는 여러 개의 key=value를 한 줄에 둘 수 있으므로,
    이 helper는 공백 기준으로 토큰을 나눈 뒤 `=` 를 가진 항목만 골라낸다.
    """
    pairs: list[tuple[str, str]] = []
    for token in re.split(r"\s+", arguments.strip()):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            pairs.append((key, value))
    return pairs


def _suggest_env_line(final: Stage) -> int | None:
    """
    ENV 관련 Finding을 어디 줄에 표시할지 정한다.

    가능한 한 기존 ENV 근처에 붙이고, ENV가 없으면 CMD/ENTRYPOINT 근처를 택한다.
    그래도 없으면 Dockerfile 첫 줄을 fallback으로 사용한다.
    """
    for instr in final.instructions:
        if instr.instruction == "ENV":
            return instr.line_no
    for instr in final.instructions:
        if instr.instruction in {"CMD", "ENTRYPOINT"}:
            return instr.line_no
    return 1


def _infer_flask_app_target(final: Stage) -> str | None:
    """
    `gunicorn module:app` 형식의 target을 Flask 설정에서 추론한다.

    우선순위:
    1. `FLASK_APP` ENV
    2. `app.py` 형태의 보수적 기본값

    예:
    - `FLASK_APP=app.py`   -> `app:app`
    - `FLASK_APP=main`     -> `main:app`
    - `FLASK_APP=src.app:app` -> 그대로 사용
    """
    env_map, _ = collect_python_env_map(final)
    flask_app = env_map.get("FLASK_APP")
    if flask_app:
        cleaned = flask_app.strip().strip('"').strip("'")
        if ":" in cleaned:
            return cleaned
        if cleaned.endswith(".py"):
            return f"{cleaned[:-3]}:app"
        if re.fullmatch(r"[A-Za-z_][\w\.]*", cleaned):
            return f"{cleaned}:app"

    # Conservative default only when the project is already using the common app.py form.
    return "app:app"


def _extract_option_value(command: str, option: str) -> str | None:
    """
    JSON form과 shell form command 양쪽에서 특정 옵션 값을 추출한다.

    예를 들어 `--port 8000` 또는 `"--port", "8000"` 같은 형태를 모두
    받아서 같은 방식으로 읽어내기 위해 사용한다.
    """
    json_match = re.search(rf'"{re.escape(option)}"\s*,\s*"([^"]+)"', command)
    if json_match:
        return json_match.group(1)
    shell_match = re.search(rf"{re.escape(option)}(?:=|\s+)([^\s\"\]]+)", command)
    if shell_match:
        return shell_match.group(1)
    return None


def _append_json_or_shell_flag(command: str, option: str, value: str) -> str:
    """
    기존 command 형식을 유지한 채 옵션 하나를 뒤에 덧붙인다.

    - JSON 배열 형태면 JSON 배열을 유지한다.
    - shell form이면 문자열 뒤에 그대로 이어 붙인다.

    generated Dockerfile에서 원래 command 스타일을 최대한 보존하려는 목적이다.
    """
    stripped = command.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        body = stripped[:-1].rstrip()
        if body.endswith("["):
            return f'["{option}", "{value}"]'
        return f'{body}, "{option}", "{value}"]'
    return f"{stripped} {option} {value}"
