"""
빌드 도구가 final stage에 잔존하는지 탐지.

런타임에는 불필요한 컴파일러, 빌드 시스템, 개발 헤더 등이
final stage의 RUN 명령에서 설치되면 이미지 크기가 불필요하게 커진다.

올바른 해결책: 멀티-스테이지 빌드를 사용해 builder stage에서만
빌드 도구를 설치하고, runtime stage에는 빌드 결과물만 COPY한다.
"""
from __future__ import annotations

import re

from imgadvisor.models import DockerfileIR, Finding, Severity

# 런타임에 불필요한 빌드 도구 목록
# 정규식에서 + 같은 특수문자는 이스케이프 처리 (g++, clang++ 등)
_BUILD_TOOLS: list[str] = [
    # C/C++ 컴파일러 — 컴파일 후에는 불필요
    "gcc", "g\\+\\+", "clang", "clang\\+\\+", "llvm",
    # 빌드 시스템 — 소스 컴파일 후에는 불필요
    "make", "cmake", "ninja-build", "automake", "autoconf", "libtool",
    "build-essential", "pkg-config",
    # 바이너리 유틸 — 개발/디버그용
    "binutils", "gfortran",
    # Java 빌드 도구 — JAR 생성 후에는 불필요
    "maven", "gradle", "ant",
    # Rust 빌드 도구 — 바이너리 컴파일 후에는 불필요
    "cargo", "rustc",
    # 개발 헤더 — C 확장 컴파일 후에는 불필요
    "python3-dev", "python-dev", "libpython3-dev",
    "libpq-dev", "libssl-dev", "libffi-dev",
    "libblas-dev", "liblapack-dev",
    # 네트워크 다운로드 도구 — 설치 후에는 불필요
    "wget",
]

# 각 도구 이름을 \b 단어 경계를 가진 정규식 패턴으로 컴파일
# 예: "gcc" → re.compile(r"\bgcc\b", re.IGNORECASE)
_PATTERNS: list[re.Pattern] = [
    re.compile(rf"\b{tool}\b", re.IGNORECASE) for tool in _BUILD_TOOLS
]


def check(ir: DockerfileIR) -> list[Finding]:
    """
    final stage의 RUN 명령에서 빌드 도구 설치 패턴을 탐지한다.

    탐지 로직:
    1. final stage의 모든 RUN 명령을 순회
    2. 각 RUN arguments에서 빌드 도구 패턴을 검색
    3. 발견된 도구 이름을 수집하고 첫 번째 발견 줄 번호를 기록
    4. 발견된 도구가 있으면 Finding 하나를 반환 (도구별 개별 Finding이 아님)

    여러 빌드 도구가 있어도 하나의 Finding으로 묶어서 반환한다.
    최대 6개까지 나열하고 나머지는 "and N more"로 표시한다.

    Args:
        ir: Dockerfile 중간 표현

    Returns:
        빌드 도구가 탐지되면 Finding 하나를 담은 리스트, 없으면 빈 리스트
    """
    final = ir.final_stage
    if final is None:
        return []

    found: list[str] = []             # 발견된 빌드 도구 이름 목록
    first_line_no: int | None = None  # 처음 발견된 줄 번호

    for instr in final.run_instructions:
        for tool_re, tool_name in zip(_PATTERNS, _BUILD_TOOLS):
            # 출력용 이름: 이스케이프된 \+\+ 를 실제 ++ 로 복원
            clean_name = tool_name.replace("\\+\\+", "++")
            # 이미 발견된 도구는 중복 추가하지 않음
            if tool_re.search(instr.arguments) and clean_name not in found:
                found.append(clean_name)
                # 첫 번째 발견 줄 번호만 기록 (대표 위치)
                if first_line_no is None:
                    first_line_no = instr.line_no

    if not found:
        return []

    # 출력용 도구 목록 (최대 6개 + "and N more")
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
