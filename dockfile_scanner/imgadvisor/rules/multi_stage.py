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
    Flag single-stage Python Dockerfiles that should become multi-stage.

    The rule intentionally avoids non-Python images. Once a recommendation is
    emitted, recommender.py can turn the recommendation body into the actual
    optimized Dockerfile content.
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
    Build a concrete Python multi-stage Dockerfile from the current Dockerfile.

    Design constraints:
    - preserve the original instruction stream where possible
    - install Python dependencies into a dedicated venv in the builder
    - avoid copying the builder's full /usr/local tree into the runtime image
    - keep runtime output conservative and buildable
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
    Fallback builder when no manifest-aware dependency strategy is available.
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
    """Choose a conservative runtime base that stays compatible with apt-based builders."""
    if image.endswith("-slim") or image.endswith("-alpine"):
        return image

    match = re.match(r"^python:(\d+(?:\.\d+)?)", image, re.IGNORECASE)
    if match:
        return f"python:{match.group(1)}-slim"
    return "python:3.11-slim"


def _last_instruction_args(final: Stage, instruction: str) -> str | None:
    """Return the arguments of the last matching instruction in the stage."""
    for instr in reversed(final.instructions):
        if instr.instruction == instruction:
            return instr.arguments
    return None


def _find_first_build_signal_line(final: Stage) -> int | None:
    """Return the earliest line that shows the stage is doing build-time work."""
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
    """Return the instruction index of the first Python dependency install step."""
    for idx, instr in enumerate(final.instructions):
        if instr.instruction == "RUN" and _is_dependency_run(instr):
            return idx
    return None


def _detect_python_dependency_strategy(ir: DockerfileIR, final: Stage) -> tuple[str, list[str]]:
    """
    Detect which dependency-file strategy can be used for a generated builder.

    - requirements: requirements*.txt / constraints*.txt present and pip install is used
    - poetry: pyproject.toml present and poetry install is used
    - inline: fall back to preserving the original order more directly
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
    """Emit explicit dependency-file COPY instructions for manifest-aware builders."""
    if not manifest_files:
        return []
    if strategy not in {"requirements", "poetry"}:
        return []
    return [f"COPY {name} ./" for name in manifest_files]


def _build_dependency_run_lines(final: Stage, strategy: str) -> list[str]:
    """
    Reuse the original dependency install commands, but normalize pip/apt flags.
    """
    lines: list[str] = []
    for instr in final.instructions:
        if instr.instruction != "RUN" or not _is_dependency_run(instr):
            continue
        lines.append(_normalize_python_run(instr.raw, instr.arguments))
    return lines


def _is_dependency_run(instr) -> bool:
    """Return True for Python dependency installation commands."""
    if instr.instruction != "RUN":
        return False
    return bool(
        re.search(r"\bpip(?:3)?\s+install\b", instr.arguments, re.IGNORECASE)
        or re.search(r"\bpoetry\s+install\b", instr.arguments, re.IGNORECASE)
    )


def _is_manifest_copy_instruction(instr) -> bool:
    """Return True when a COPY/ADD instruction is only moving dependency manifests."""
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
    """Apply Python-specific normalization to generated builder RUN instructions."""
    if re.search(r"\b(?:apt-get|apt)\s+install\b", run_args, re.IGNORECASE):
        return _normalize_python_apt_run(run_text)
    if re.search(r"\bpip(?:3)?\s+install\b", run_args, re.IGNORECASE):
        return _normalize_python_pip_run(run_text)
    return run_text


def _normalize_python_apt_run(run_text: str) -> str:
    """
    Add conservative apt optimizations inside the generated builder stage.

    The goal is not to be clever; it is to avoid carrying recommended packages
    and apt lists when reconstructing the builder instructions.
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
    Ensure generated pip installs do not leave cache behind.

    The venv path is already injected at the stage level via PATH, so the
    original install command can stay otherwise unchanged.
    """
    updated = run_text
    if "pip install" in updated and "--no-cache-dir" not in updated:
        updated = updated.replace("pip install", "pip install --no-cache-dir", 1)
    if "pip3 install" in updated and "--no-cache-dir" not in updated:
        updated = updated.replace("pip3 install", "pip3 install --no-cache-dir", 1)
    return updated
