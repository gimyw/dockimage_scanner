"""
빌드 도구가 final stage에 남아 있는지 검사하는 rule.

final stage는 실제 배포되는 런타임 이미지이므로, 여기에 컴파일러나 개발
헤더가 남아 있으면 이미지 크기와 공격 표면이 함께 커진다. 이 rule은
"빌드는 builder stage에서, 실행은 runtime stage에서"라는 기본 원칙을
어긴 흔적을 찾는다.
"""
from __future__ import annotations

import re

from imgadvisor.models import DockerfileIR, Finding, Severity

# final stage에 남아 있으면 보통 멀티스테이지 분리 대상이 되는 도구들
_BUILD_TOOLS: list[str] = [
    "gcc", "g\\+\\+", "clang", "clang\\+\\+", "llvm",
    "make", "cmake", "ninja-build", "automake", "autoconf", "libtool",
    "build-essential", "pkg-config",
    "binutils", "gfortran",
    "maven", "gradle", "ant",
    "cargo", "rustc",
    "python3-dev", "python-dev", "libpython3-dev",
    "libpq-dev", "libssl-dev", "libffi-dev",
    "libblas-dev", "liblapack-dev",
    "wget",
]

# 문자열 검색 성능과 일관성을 위해 rule 로딩 시 정규식을 미리 컴파일한다.
_PATTERNS: list[re.Pattern] = [
    re.compile(rf"\b{tool}\b", re.IGNORECASE) for tool in _BUILD_TOOLS
]


def check(ir: DockerfileIR) -> list[Finding]:
    """
    final stage의 RUN 명령에서 빌드 전용 도구가 남아 있는지 검사한다.

    판단 절차:
    1. final stage의 모든 RUN instruction을 순회한다.
    2. `_BUILD_TOOLS` 패턴이 하나라도 매칭되는지 확인한다.
    3. 발견된 도구명을 중복 없이 수집한다.
    4. 한 개 이상 발견되면 Finding 1건으로 묶어서 반환한다.

    이 함수는 도구별로 Finding을 여러 건 만들지 않는다. 사용자가 보기에
    중요한 것은 "이 runtime 이미지가 아직 빌드 도구를 포함하고 있다"는
    사실이기 때문이다.
    """
    final = ir.final_stage
    if final is None:
        return []

    found: list[str] = []
    first_line_no: int | None = None

    for instr in final.run_instructions:
        for tool_re, tool_name in zip(_PATTERNS, _BUILD_TOOLS):
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
