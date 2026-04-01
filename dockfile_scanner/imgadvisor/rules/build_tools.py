"""
빌드 도구가 final stage에 남아 있는지 탐지.

런타임에 불필요한 컴파일러, 빌드 시스템, 개발 헤더 등을 검사.
"""
from __future__ import annotations

import re

from imgadvisor.models import DockerfileIR, Finding, Severity

# Build-tool detection rule.
# 목적은 final stage에 컴파일러/개발 헤더/빌드 시스템이 남아 있는지를 빠르게
# 찾아서 multi-stage 분리를 유도하는 것이다.

# 런타임에 불필요한 빌드 도구 목록
_BUILD_TOOLS: list[str] = [
    # C/C++ 컴파일러
    "gcc", "g\\+\\+", "clang", "clang\\+\\+", "llvm",
    # 빌드 시스템
    "make", "cmake", "ninja-build", "automake", "autoconf", "libtool",
    "build-essential", "pkg-config",
    # 바이너리 유틸
    "binutils", "gfortran",
    # Java 빌드
    "maven", "gradle", "ant",
    # Rust
    "cargo", "rustc",
    # 개발 헤더 (주요 패턴)
    "python3-dev", "python-dev", "libpython3-dev",
    "libpq-dev", "libssl-dev", "libffi-dev",
    "libblas-dev", "liblapack-dev",
    # 네트워크 다운로드 도구 (런타임 불필요)
    "wget",
]

# 모든 final-stage RUN 명령에서 반복 사용되므로 미리 컴파일한다.
_PATTERNS: list[re.Pattern] = [
    re.compile(rf"\b{tool}\b", re.IGNORECASE) for tool in _BUILD_TOOLS
]


def check(ir: DockerfileIR) -> list[Finding]:
    # shell 문법을 정교하게 해석하지 않고 문자열 기준으로 매칭한다.
    # 구현은 단순하지만 echo, 조건문, 스크립트 문자열 같은 문맥에서는 오탐이
    # 생길 수 있다.
    final = ir.final_stage
    if final is None:
        return []

    found: list[str] = []
    first_line_no: int | None = None

    for instr in final.run_instructions:
        for tool_re, tool_name in zip(_PATTERNS, _BUILD_TOOLS):
            # g\+\+ 같이 regex escape가 들어간 이름을 사용자 메시지용으로 복원.
            clean_name = tool_name.replace("\\+\\+", "++")
            if tool_re.search(instr.arguments) and clean_name not in found:
                found.append(clean_name)
                if first_line_no is None:
                    first_line_no = instr.line_no

    if not found:
        return []

    tools_display = ", ".join(f"`{t}`" for t in found[:6])
    if len(found) > 6:
        tools_display += f" and {len(found) - 6} more"

    recommendation = (
        "Use multi-stage build to remove build tools from runtime:\n"
        "  1. compile/install dependencies in a builder stage\n"
        "  2. COPY only required artifacts into the runtime stage\n\n"
        "  example:\n"
        "    FROM python:3.11 AS builder\n"
        "    RUN apt-get install -y gcc && pip install --no-cache-dir -r requirements.txt\n\n"
        "    FROM python:3.11-slim AS runtime\n"
        "    COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11\n"
        "    COPY --from=builder /app /app"
    )

    return [Finding(
        rule_id="BUILD_TOOLS_IN_FINAL_STAGE",
        severity=Severity.HIGH,
        line_no=first_line_no,
        description=f"build tools found in final stage: {tools_display}",
        recommendation=recommendation,
        saving_min_mb=100,
        saving_max_mb=400,
    )]
