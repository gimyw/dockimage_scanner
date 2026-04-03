"""
Suggest and generate a concrete Python multi-stage Dockerfile.

This rule is intentionally Python-only. Generic multi-stage templates looked
helpful in the UI but were too weak to produce a reliably smaller runtime
image. The current goal is narrower and more concrete:

1. Detect single-stage Python Dockerfiles that install dependencies or build
   tooling directly into the runtime image.
2. Rebuild the Dockerfile into a builder/runtime split using the original
   instruction stream.
3. Keep only the runtime pieces that are actually needed: the virtualenv,
   the app directory, and selected runtime binaries.
"""
from __future__ import annotations

import re
from pathlib import Path

from imgadvisor.models import DockerfileIR, Finding, Severity, Stage
from imgadvisor.rules.python_runtime import (
    is_python_stage,
    recommended_python_env_lines,
    recommended_python_runtime_command,
)

# Build-only packages that are common in Python images and usually do not
# belong in the final runtime stage.
_BUILD_TOOL_PACKAGES: list[str] = [
    "gcc",
    "g++",
    "make",
    "cmake",
    "build-essential",
    "libpq-dev",
    "libssl-dev",
    "libffi-dev",
    "python3-dev",
]


def check(ir: DockerfileIR) -> list[Finding]:
    """
    single-stage Python Dockerfile이 실제로 multi-stage 전환 대상인지 판정한다.

    이 함수는 Python이 아닌 이미지에는 관여하지 않는다. 범위를 좁히는 대신,
    Python에 대해서는 recommendation 자체를 실제 Dockerfile 본문으로 만들 수
    있을 정도로 구체적인 판단을 수행한다.

    신호로 보는 것:
    - build tool 패키지 설치 흔적
    - apt install 흔적
    - pip install 흔적
    - broad COPY

    이런 신호가 없으면 "멀티스테이지는 가능하지만 실익이 약하다"고 보고
    Finding을 만들지 않는다.
    """
    if ir.is_multi_stage:
        return []

    final = ir.final_stage
    if final is None or not is_python_stage(final):
        return []

    run_text = final.all_run_text
    has_build_pkg = any(
        re.search(rf"\b{re.escape(tool)}\b", run_text, re.IGNORECASE)
        for tool in _BUILD_TOOL_PACKAGES
    )
    has_apt_install = bool(re.search(r"\b(?:apt-get|apt)\s+install\b", run_text, re.IGNORECASE))
    has_pip_install = bool(re.search(r"\bpip(?:3)?\s+install\b", run_text, re.IGNORECASE))
    broad_copy = any(
        instr.instruction == "COPY" and instr.arguments.startswith(". ")
        for instr in final.copy_instructions
    )

    # If the stage does not install anything and does not copy the whole app
    # context, multi-stage often adds complexity without material size benefit.
    if not (has_build_pkg or has_apt_install or has_pip_install or broad_copy):
        return []

    template = _build_python_template(ir, final)
    recommendation = (
        "convert to multi-stage build:\n\n"
        + "\n".join(f"  {line}" for line in template.splitlines())
    )

    return [
        Finding(
            rule_id="SINGLE_STAGE_BUILD",
            severity=Severity.HIGH,
            line_no=_find_first_build_signal_line(final) or 1,
            description="single-stage build — build tools are included in the runtime image",
            recommendation=recommendation,
            saving_min_mb=150,
            saving_max_mb=600,
        )
    ]
def _build_python_template(ir: DockerfileIR, final: Stage) -> str:
    """
    현재 single-stage Dockerfile을 기반으로 실제 Python multi-stage 본문을 생성한다.

    이 함수는 단순 예시 템플릿을 붙이는 것이 아니라, 파싱된 instruction 목록을
    읽어서 builder/runtime를 다시 조립한다.

    핵심 원칙:
    - 가능한 한 원본 instruction 순서를 보존한다.
    - Python 의존성은 `/opt/venv` 에 격리한다.
    - runtime stage에는 builder의 전체 `/usr/local` 을 복사하지 않는다.
    - runtime command는 python_runtime rule의 추론 결과를 반영한다.
    """
    image = final.base_image
    runtime_image = _python_runtime_image(image)
    workdir = _last_instruction_args(final, "WORKDIR") or "/app"
    strategy, manifest_files = _detect_python_dependency_strategy(ir, final)

    env_lines = [instr.raw for instr in final.instructions if instr.instruction == "ENV"]
    recommended_env_lines = recommended_python_env_lines(final)
    runtime_only_lines = [
        instr.raw
        for instr in final.instructions
        if instr.instruction in {"EXPOSE", "CMD", "ENTRYPOINT", "USER", "HEALTHCHECK", "STOPSIGNAL"}
    ]
    runtime_command_override = recommended_python_runtime_command(final)
    if runtime_command_override is not None:
        runtime_only_lines = [
            instr.raw
            for instr in final.instructions
            if instr.instruction in {"EXPOSE", "USER", "HEALTHCHECK", "STOPSIGNAL"}
        ] + runtime_command_override

    if strategy == "inline":
        return _build_inline_python_template(
            final=final,
            image=image,
            runtime_image=runtime_image,
            workdir=workdir,
            env_lines=env_lines,
            runtime_only_lines=runtime_only_lines,
        )

    first_dep_idx = _find_first_dependency_run_index(final)
    pre_dep_lines: list[str] = []
    post_dep_lines: list[str] = []
    copy_usr_local_bin = False

    for idx, instr in enumerate(final.instructions):
        if instr.instruction in {"WORKDIR", "ENV", "EXPOSE", "CMD", "ENTRYPOINT", "USER", "HEALTHCHECK", "STOPSIGNAL"}:
            continue

        if instr.instruction in {"COPY", "ADD"}:
            if _is_manifest_copy_instruction(instr):
                continue

            # For manifest-aware strategies, broad/app source copies are delayed
            # until after dependency installation so the generated builder better
            # reflects the standard Python layer split.
            target = post_dep_lines if first_dep_idx is not None else pre_dep_lines
            target.append(instr.raw)
            continue

        if instr.instruction != "RUN":
            target = pre_dep_lines if first_dep_idx is None or idx < first_dep_idx else post_dep_lines
            target.append(instr.raw)
            continue

        if _is_dependency_run(instr):
            continue

        normalized = _normalize_python_run(instr.raw, instr.arguments)
        if "/usr/local/bin" in normalized or "tar -C /usr/local/bin" in normalized:
            copy_usr_local_bin = True

        target = pre_dep_lines if first_dep_idx is None or idx < first_dep_idx else post_dep_lines
        target.append(normalized)

    builder_lines = [
        "# -- builder stage --",
        f"FROM {image} AS builder",
        f"WORKDIR {workdir}",
        "ENV VIRTUAL_ENV=/opt/venv",
        'ENV PATH="/opt/venv/bin:$PATH"',
        "RUN python -m venv $VIRTUAL_ENV",
    ]
    builder_lines.extend(env_lines)
    builder_lines.extend(recommended_env_lines)
    builder_lines.extend(pre_dep_lines)
    builder_lines.extend(_build_manifest_copy_lines(strategy, manifest_files))
    builder_lines.extend(_build_dependency_run_lines(final, strategy))
    builder_lines.extend(post_dep_lines)

    runtime_lines = [
        "",
        "# -- runtime stage --",
        f"FROM {runtime_image}",
        f"WORKDIR {workdir}",
        "ENV VIRTUAL_ENV=/opt/venv",
        'ENV PATH="/opt/venv/bin:$PATH"',
    ]
    runtime_lines.extend(env_lines)
    runtime_lines.extend(recommended_env_lines)
    runtime_lines.append("COPY --from=builder /opt/venv /opt/venv")
    if copy_usr_local_bin:
        runtime_lines.append("COPY --from=builder /usr/local/bin /usr/local/bin")
    runtime_lines.append(f"COPY --from=builder {workdir} {workdir}")
    runtime_lines.extend(runtime_only_lines or ['CMD ["python", "app.py"]'])

    return "\n".join(builder_lines + runtime_lines)


def _build_inline_python_template(
    final: Stage,
    image: str,
    runtime_image: str,
    workdir: str,
    env_lines: list[str],
    runtime_only_lines: list[str],
) -> str:
    """
    manifest 파일 전략을 쓸 수 없을 때 사용하는 fallback 경로.

    `requirements.txt` 나 `pyproject.toml` 같은 명확한 dependency 파일이
    없으면, copy/install 순서를 과도하게 재배열하는 것이 오히려 위험할 수 있다.
    이 경우에는 원본 instruction 흐름을 최대한 보존하면서 venv 기반의
    builder/runtime 분리만 적용한다.
    """
    copy_usr_local_bin = False
    recommended_env_lines = recommended_python_env_lines(final)

    builder_lines = [
        "# -- builder stage --",
        f"FROM {image} AS builder",
        f"WORKDIR {workdir}",
        "ENV VIRTUAL_ENV=/opt/venv",
        'ENV PATH="/opt/venv/bin:$PATH"',
        "RUN python -m venv $VIRTUAL_ENV",
    ]
    builder_lines.extend(env_lines)
    builder_lines.extend(recommended_env_lines)

    for instr in final.instructions:
        if instr.instruction == "WORKDIR":
            continue
        if instr.instruction == "ENV":
            continue
        if instr.instruction in {"EXPOSE", "CMD", "ENTRYPOINT", "USER", "HEALTHCHECK", "STOPSIGNAL"}:
            continue
        if instr.instruction != "RUN":
            builder_lines.append(instr.raw)
            continue

        normalized = _normalize_python_run(instr.raw, instr.arguments)
        if "/usr/local/bin" in normalized or "tar -C /usr/local/bin" in normalized:
            copy_usr_local_bin = True
        builder_lines.append(normalized)

    runtime_lines = [
        "",
        "# -- runtime stage --",
        f"FROM {runtime_image}",
        f"WORKDIR {workdir}",
        "ENV VIRTUAL_ENV=/opt/venv",
        'ENV PATH="/opt/venv/bin:$PATH"',
    ]
    runtime_lines.extend(env_lines)
    runtime_lines.extend(recommended_env_lines)
    runtime_lines.append("COPY --from=builder /opt/venv /opt/venv")
    if copy_usr_local_bin:
        runtime_lines.append("COPY --from=builder /usr/local/bin /usr/local/bin")
    runtime_lines.append(f"COPY --from=builder {workdir} {workdir}")
    runtime_lines.extend(runtime_only_lines or ['CMD ["python", "app.py"]'])

    return "\n".join(builder_lines + runtime_lines)


def _python_runtime_image(image: str) -> str:
    """
    generated runtime stage에 사용할 보수적인 Python base image를 고른다.

    현재는 Alpine 같은 공격적인 전환보다 `-slim` 쪽을 우선한다.
    이유는 builder에서 apt 계열 도구를 쓰는 Python 프로젝트가 많고,
    무리한 base 교체는 validate 단계에서 바로 깨질 가능성이 높기 때문이다.
    """
    if image.endswith("-slim") or image.endswith("-alpine"):
        return image

    match = re.match(r"^python:(\d+(?:\.\d+)?)", image, re.IGNORECASE)
    if match:
        return f"python:{match.group(1)}-slim"
    return "python:3.11-slim"


def _last_instruction_args(final: Stage, instruction: str) -> str | None:
    """
    stage 안에서 특정 instruction이 마지막으로 등장한 인수를 반환한다.

    예를 들어 WORKDIR은 여러 번 바뀔 수 있으므로, 최종적으로 효력이 있는
    마지막 WORKDIR 값을 잡기 위해 사용한다.
    """
    for instr in reversed(final.instructions):
        if instr.instruction == instruction:
            return instr.arguments
    return None


def _find_first_build_signal_line(final: Stage) -> int | None:
    """
    '이 stage는 빌드성 작업을 한다'는 가장 이른 line number를 찾는다.

    CLI 출력에서 SINGLE_STAGE_BUILD Finding이 Dockerfile 어디를 가리킬지 정하는
    용도다. apt install, pip install, build tool 키워드 순으로 검사한다.
    """
    for instr in final.run_instructions:
        if re.search(r"\b(?:apt-get|apt)\s+install\b", instr.arguments, re.IGNORECASE):
            return instr.line_no
        if re.search(r"\bpip(?:3)?\s+install\b", instr.arguments, re.IGNORECASE):
            return instr.line_no
        if any(
            re.search(rf"\b{re.escape(tool)}\b", instr.arguments, re.IGNORECASE)
            for tool in _BUILD_TOOL_PACKAGES
        ):
            return instr.line_no
    return None


def _find_first_dependency_run_index(final: Stage) -> int | None:
    """
    첫 번째 Python dependency install RUN의 instruction index를 찾는다.

    manifest-first 전략에서는 이 지점을 기준으로
    - 의존성 설치 전에 둘 instruction
    - 의존성 설치 후에 둘 instruction
    을 나눠서 builder를 재구성한다.
    """
    for idx, instr in enumerate(final.instructions):
        if instr.instruction == "RUN" and _is_dependency_run(instr):
            return idx
    return None


def _detect_python_dependency_strategy(ir: DockerfileIR, final: Stage) -> tuple[str, list[str]]:
    """
    builder stage에서 어떤 dependency-layer 전략을 쓸지 결정한다.

    반환 전략:
    - `requirements`: requirements/constraints 파일 기반 pip install
    - `poetry`: pyproject.toml + poetry.lock 기반 install
    - `inline`: 파일 기반 전략을 안전하게 쓸 수 없을 때의 fallback

    이 분기는 "의존성 파일만 먼저 COPY하고 install한 뒤 앱 전체를 COPY"하는
    최적화가 가능한지 판단하는 핵심 단계다.
    """
    context_dir = Path(ir.path).parent
    requirement_files = sorted(
        file.name
        for pattern in ("requirements*.txt", "constraints*.txt")
        for file in context_dir.glob(pattern)
        if file.is_file()
    )
    poetry_files = [
        name for name in ("pyproject.toml", "poetry.lock") if (context_dir / name).is_file()
    ]

    has_pip = any(
        instr.instruction == "RUN" and re.search(r"\bpip(?:3)?\s+install\b", instr.arguments, re.IGNORECASE)
        for instr in final.instructions
    )
    has_poetry = any(
        instr.instruction == "RUN" and re.search(r"\bpoetry\s+install\b", instr.arguments, re.IGNORECASE)
        for instr in final.instructions
    )

    if poetry_files and has_poetry:
        return "poetry", poetry_files
    if requirement_files and has_pip:
        return "requirements", requirement_files
    return "inline", []


def _build_manifest_copy_lines(strategy: str, manifest_files: list[str]) -> list[str]:
    """
    dependency manifest만 먼저 복사하는 COPY 라인을 생성한다.

    manifest-first 전략의 목적은 앱 소스 코드가 조금 바뀌어도 의존성 layer가
    불필요하게 다시 빌드되지 않게 만드는 데 있다.
    """
    if not manifest_files:
        return []
    if strategy not in {"requirements", "poetry"}:
        return []
    return [f"COPY {name} ./" for name in manifest_files]


def _build_dependency_run_lines(final: Stage, strategy: str) -> list[str]:
    """
    원본 dependency install RUN을 재사용하되 Python 친화적으로 정규화한다.

    예:
    - pip install -> --no-cache-dir 추가
    - apt install -> --no-install-recommends 와 cleanup 추가
    """
    lines: list[str] = []
    for instr in final.instructions:
        if instr.instruction != "RUN" or not _is_dependency_run(instr):
            continue
        lines.append(_normalize_python_run(instr.raw, instr.arguments))
    return lines


def _is_dependency_run(instr) -> bool:
    """
    주어진 RUN이 Python dependency 설치 단계인지 판정한다.

    현재는 `pip install` 과 `poetry install` 을 dependency install로 본다.
    """
    if instr.instruction != "RUN":
        return False
    return bool(
        re.search(r"\bpip(?:3)?\s+install\b", instr.arguments, re.IGNORECASE)
        or re.search(r"\bpoetry\s+install\b", instr.arguments, re.IGNORECASE)
    )


def _is_manifest_copy_instruction(instr) -> bool:
    """
    COPY/ADD instruction이 dependency manifest만 옮기는지 판정한다.

    manifest-first builder를 만들 때는 이런 COPY를 일반 app source COPY와
    구분해야 하므로 별도 helper로 분리했다.
    """
    if instr.instruction not in {"COPY", "ADD"}:
        return False
    lowered = instr.arguments.lower()
    manifest_markers = (
        "requirements",
        "constraints",
        "pyproject.toml",
        "poetry.lock",
        "setup.py",
        "setup.cfg",
    )
    return any(marker in lowered for marker in manifest_markers)


def _normalize_python_run(run_text: str, run_args: str) -> str:
    """
    generated builder에 들어갈 RUN 문자열에 Python 전용 정규화를 적용한다.

    현재는 apt install과 pip install을 감지해서 각 전용 helper로 넘긴다.
    다른 RUN은 원문을 그대로 유지한다.
    """
    if re.search(r"\b(?:apt-get|apt)\s+install\b", run_args, re.IGNORECASE):
        return _normalize_python_apt_run(run_text)
    if re.search(r"\bpip(?:3)?\s+install\b", run_args, re.IGNORECASE):
        return _normalize_python_pip_run(run_text)
    return run_text


def _normalize_python_apt_run(run_text: str) -> str:
    """
    generated builder 안의 apt install 명령에 보수적인 최적화를 추가한다.

    이 함수는 공격적인 재작성보다 안전한 정규화에 집중한다.
    - `--no-install-recommends` 추가
    - apt lists cleanup 추가

    즉, apt 명령의 의미를 크게 바꾸지 않으면서 불필요한 layer 낭비를 줄인다.
    """
    updated = run_text
    if "apt-get install -y" in updated and "--no-install-recommends" not in updated:
        updated = updated.replace("apt-get install -y", "apt-get install -y --no-install-recommends", 1)
    if "apt install -y" in updated and "--no-install-recommends" not in updated:
        updated = updated.replace("apt install -y", "apt install -y --no-install-recommends", 1)
    if "/var/lib/apt/lists" not in updated:
        updated = updated.rstrip() + " \\\n    && rm -rf /var/lib/apt/lists/*"
    return updated


def _normalize_python_pip_run(run_text: str) -> str:
    """
    generated builder 안의 pip install에 `--no-cache-dir` 를 보장한다.

    venv 경로는 stage 수준에서 이미 PATH에 주입해 두었으므로,
    여기서는 install 대상이나 패키지 목록은 건드리지 않고 pip cache만 막는다.
    """
    updated = run_text
    if "pip install" in updated and "--no-cache-dir" not in updated:
        updated = updated.replace("pip install", "pip install --no-cache-dir", 1)
    if "pip3 install" in updated and "--no-cache-dir" not in updated:
        updated = updated.replace("pip3 install", "pip3 install --no-cache-dir", 1)
    return updated
