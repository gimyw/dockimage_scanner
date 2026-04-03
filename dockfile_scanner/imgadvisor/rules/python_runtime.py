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

import ast
import re
import tomllib
from pathlib import Path

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

    findings.extend(_check_python_runtime_command(ir, final))
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


def recommended_python_runtime_command(ir: DockerfileIR, final: Stage) -> list[str] | None:
    """
    안전하게 추론 가능한 경우에만 더 나은 runtime command를 생성한다.

    현재 자동 교체 범위는 의도적으로 좁다.
    - `flask run ...` 은 production용 `gunicorn` 명령으로 교체
    - `uvicorn ...` 은 기존 형태를 유지하되 `--workers 2` 추가

    추론이 애매하면 None을 반환한다. 이 경우 caller는 원래 CMD/ENTRYPOINT를
    그대로 유지해야 하며, rule은 경고만 제공한다.
    """
    installed = detect_python_runtime_packages(ir, final)

    for instr in final.instructions:
        if instr.instruction not in {"CMD", "ENTRYPOINT"}:
            continue

        command = instr.arguments.strip()
        lowered = command.lower()

        if "flask run" in lowered:
            if "gunicorn" not in installed:
                return None
            target = _infer_flask_app_target(ir, final)
            if target is None:
                return None
            port = _extract_option_value(command, "--port") or "5000"
            bind = f"0.0.0.0:{port}"
            return [f'CMD ["gunicorn", "-b", "{bind}", "{target}"]']

    return None


def _check_python_runtime_command(ir: DockerfileIR, final: Stage) -> list[Finding]:
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
            inferred = recommended_python_runtime_command(ir, final)
            recommendation = (
                "Use a production WSGI server. Auto-rewrite only happens when the Flask entry module can be inferred from the source tree."
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
            recommendation = (
                "Set an explicit worker count for deployment. Do not auto-fix this blindly because the right value depends on CPU, memory, and the server model."
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


def detect_python_runtime_packages(ir: DockerfileIR, final: Stage) -> set[str]:
    """
    Dockerfile 과 프로젝트 의존성 파일을 함께 읽어서 런타임 관련 Python 패키지를 수집한다.

    현재 자동 엔트리포인트 최적화에 직접 쓰는 패키지는 아래 세 개뿐이다.
    - flask
    - gunicorn
    - uvicorn

    판단 근거:
    - requirements/constraints 파일
    - pyproject.toml 의 dependencies / optional-dependencies
    - Dockerfile 의 `pip install ...` inline 명령
    """
    context_dir = Path(ir.path).resolve().parent
    detected: set[str] = set()

    for pattern in ("requirements*.txt", "constraints*.txt"):
        for path in context_dir.glob(pattern):
            detected.update(_read_requirement_like_file(path))

    pyproject_path = context_dir / "pyproject.toml"
    if pyproject_path.is_file():
        detected.update(_read_pyproject_dependencies(pyproject_path))

    for instr in final.run_instructions:
        detected.update(_read_inline_pip_install(instr.arguments))

    interesting = {"flask", "gunicorn", "uvicorn"}
    return detected & interesting


def _read_requirement_like_file(path: Path) -> set[str]:
    """
    requirements 계열 파일에서 패키지 이름만 느슨하게 추출한다.
    """
    packages: set[str] = set()
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return packages

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        line = line.split("#", 1)[0].strip()
        name = re.split(r"[<>=!~\[\s;]", line, maxsplit=1)[0].strip()
        if name:
            packages.add(name.lower().replace("_", "-"))
    return packages


def _read_pyproject_dependencies(path: Path) -> set[str]:
    """
    pyproject.toml 에서 PEP 621 / Poetry 스타일 dependency 이름을 읽는다.
    """
    packages: set[str] = set()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return packages

    project = data.get("project", {})
    for item in project.get("dependencies", []) or []:
        packages.update(_extract_dependency_name(item))

    optional_deps = project.get("optional-dependencies", {}) or {}
    for items in optional_deps.values():
        for item in items or []:
            packages.update(_extract_dependency_name(item))

    poetry = (((data.get("tool", {}) or {}).get("poetry", {})) or {})
    poetry_deps = poetry.get("dependencies", {}) or {}
    for name in poetry_deps.keys():
        if name != "python":
            packages.add(str(name).lower().replace("_", "-"))

    poetry_groups = poetry.get("group", {}) or {}
    for group in poetry_groups.values():
        deps = (group or {}).get("dependencies", {}) or {}
        for name in deps.keys():
            if name != "python":
                packages.add(str(name).lower().replace("_", "-"))

    return packages


def _extract_dependency_name(spec: str) -> set[str]:
    """
    `flask>=3.0`, `uvicorn[standard]>=0.30` 같은 dependency spec 에서 이름만 뽑는다.
    """
    item = spec.strip()
    if not item:
        return set()
    name = re.split(r"[<>=!~\[\s;]", item, maxsplit=1)[0].strip()
    return {name.lower().replace("_", "-")} if name else set()


def _read_inline_pip_install(run_arguments: str) -> set[str]:
    """
    `RUN pip install ...` 형태의 inline 설치 명령에서 패키지 이름을 뽑는다.

    아주 공격적으로 파싱하지는 않는다. URL, 옵션, `-r requirements.txt` 같은 값은 제외한다.
    """
    packages: set[str] = set()
    match = re.search(r"\bpip(?:3)?\s+install\b(.+)", run_arguments, re.IGNORECASE)
    if not match:
        return packages

    tail = re.split(r"\s*(?:&&|;|\|\|)\s*", match.group(1), maxsplit=1)[0]
    for token in tail.split():
        token = token.strip()
        if not token or token.startswith("-") or "://" in token or token.startswith("."):
            continue
        name = re.split(r"[<>=!~\[\s;]", token, maxsplit=1)[0].strip()
        if name:
            packages.add(name.lower().replace("_", "-"))
    return packages


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


def _infer_flask_app_target(ir: DockerfileIR, final: Stage) -> str | None:
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
    context_dir = Path(ir.path).resolve().parent
    env_map, _ = collect_python_env_map(final)
    flask_app = env_map.get("FLASK_APP")

    for module_hint, file_path in _candidate_flask_files(context_dir, flask_app):
        target = _infer_flask_target_from_file(file_path, module_hint)
        if target is not None:
            return target

    return None


def _candidate_flask_files(context_dir: Path, flask_app: str | None) -> list[tuple[str, Path]]:
    """
    FLASK_APP 와 일반적인 Flask 엔트리 파일 이름을 기준으로 검사 후보를 만든다.

    반환 형식:
    - module hint: gunicorn 에 넣을 module 부분 후보
    - file path: 실제로 읽어볼 Python 파일 경로

    자동 변환은 이 후보 파일을 실제로 읽어서 app 객체나 factory 가 확인될 때만 허용한다.
    """
    candidates: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    if flask_app:
        cleaned = flask_app.strip().strip('"').strip("'")
        if cleaned.endswith(".py"):
            path = (context_dir / cleaned).resolve()
            module_hint = cleaned[:-3].replace("/", ".").replace("\\", ".")
            if path not in seen:
                candidates.append((module_hint, path))
                seen.add(path)
        elif ":" in cleaned:
            module_hint = cleaned.split(":", 1)[0]
            path = (context_dir / (module_hint.replace(".", "/") + ".py")).resolve()
            if path not in seen:
                candidates.append((module_hint, path))
                seen.add(path)
        elif re.fullmatch(r"[A-Za-z_][\w\.]*", cleaned):
            module_hint = cleaned
            path = (context_dir / (module_hint.replace(".", "/") + ".py")).resolve()
            if path not in seen:
                candidates.append((module_hint, path))
                seen.add(path)

    for module_hint in ("app", "main", "wsgi"):
        path = (context_dir / f"{module_hint}.py").resolve()
        if path not in seen:
            candidates.append((module_hint, path))
            seen.add(path)

    return candidates


def _infer_flask_target_from_file(file_path: Path, module_hint: str) -> str | None:
    """
    Python 소스 파일을 실제로 읽어서 gunicorn target 을 추론한다.

    허용하는 경우:
    - `name = Flask(...)` 형태의 직접 app 객체
    - `def create_app(...): ...` 형태의 app factory

    둘 다 확인되지 않으면 자동 변환하지 않는다.
    """
    if not file_path.exists() or file_path.suffix != ".py":
        return None

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (OSError, SyntaxError, ValueError):
        return None

    object_name = _find_flask_app_object_name(tree)
    if object_name is not None:
        return f"{module_hint}:{object_name}"

    factory_name = _find_flask_app_factory_name(tree)
    if factory_name is not None:
        return f"{module_hint}:{factory_name}()"

    return None


def _find_flask_app_object_name(tree: ast.AST) -> str | None:
    """
    `app = Flask(__name__)` 처럼 직접 생성된 Flask 객체 이름을 찾는다.
    """
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if not isinstance(node, ast.Assign):
            continue
        if not _is_flask_constructor_call(node.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                return target.id
    return None


def _find_flask_app_factory_name(tree: ast.AST) -> str | None:
    """
    `create_app()` 패턴을 아주 보수적으로 찾아 gunicorn factory target 으로 쓸지 판단한다.

    함수명만 보는 것이 아니라, 파일 안에 Flask 관련 import 가 있을 때만 허용한다.
    """
    has_flask_import = any(
        (isinstance(node, ast.ImportFrom) and node.module == "flask")
        or (
            isinstance(node, ast.Import)
            and any(alias.name == "flask" for alias in node.names)
        )
        for node in ast.walk(tree)
    )
    if not has_flask_import:
        return None

    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.FunctionDef) and node.name == "create_app":
            return node.name
    return None


def _is_flask_constructor_call(node: ast.AST) -> bool:
    """
    AST 노드가 `Flask(...)` 생성 호출인지 확인한다.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "Flask"
    if isinstance(func, ast.Attribute):
        return func.attr == "Flask"
    return False


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
