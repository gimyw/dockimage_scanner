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
    """Return Python runtime findings for the final stage only."""
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
    """Return True when the stage is recognizably Python-based."""
    if re.match(r"^python:", final.base_image, re.IGNORECASE):
        return True
    return bool(re.search(r"\bpip(?:3)?\s+install\b", final.all_run_text, re.IGNORECASE))


def collect_python_env_map(final: Stage) -> tuple[dict[str, str], dict[str, int]]:
    """
    Parse final-stage ENV instructions into the last seen key/value map.

    Dockerfile supports both:
    - ENV KEY=value
    - ENV KEY=value OTHER=value
    This helper covers those common forms. Non key=value forms are ignored.
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
    Return ordered ENV lines for generated Python Dockerfiles.

    Existing app-specific ENV lines are preserved as-is by multi_stage.py.
    These lines are appended later so container-safe defaults override any
    conflicting earlier values.
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
    Return a safer runtime CMD/ENTRYPOINT block when inference is reliable enough.

    Current conservative rewrites:
    - `flask run ...` -> `gunicorn ... module:app` if the Flask app target can be inferred
    - `uvicorn ...` without `--workers` -> same command with `--workers 2`

    If inference is ambiguous, return None and let the caller preserve the
    original runtime command.
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
    """Inspect CMD/ENTRYPOINT patterns that are common runtime footguns."""
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
    """Extract KEY=value tokens from an ENV instruction."""
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
    """Choose a stable line number near existing ENV/CMD configuration."""
    for instr in final.instructions:
        if instr.instruction == "ENV":
            return instr.line_no
    for instr in final.instructions:
        if instr.instruction in {"CMD", "ENTRYPOINT"}:
            return instr.line_no
    return 1


def _infer_flask_app_target(final: Stage) -> str | None:
    """
    Infer the `<module>:<app>` target for gunicorn from FLASK_APP or known defaults.
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
    """Extract a flag value from either JSON-form or shell-form command text."""
    json_match = re.search(rf'"{re.escape(option)}"\s*,\s*"([^"]+)"', command)
    if json_match:
        return json_match.group(1)
    shell_match = re.search(rf"{re.escape(option)}(?:=|\s+)([^\s\"\]]+)", command)
    if shell_match:
        return shell_match.group(1)
    return None


def _append_json_or_shell_flag(command: str, option: str, value: str) -> str:
    """Append a CLI flag while preserving JSON-form commands when possible."""
    stripped = command.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        body = stripped[:-1].rstrip()
        if body.endswith("["):
            return f'["{option}", "{value}"]'
        return f'{body}, "{option}", "{value}"]'
    return f"{stripped} {option} {value}"
