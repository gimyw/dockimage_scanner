"""
광범위한 COPY 범위 탐지.

COPY . . 또는 COPY . /app 처럼 컨텍스트 전체를 복사하는 패턴 감지.
.dockerignore 부재 여부도 함께 고려.
"""
from __future__ import annotations

from imgadvisor.models import DockerfileIR, Finding, Severity

# Broad COPY scope rule.
# final stage에서 `COPY . ...`를 쓰면 소스 외에도 테스트, 문서, 캐시, VCS
# 메타데이터, 심지어 비밀 파일까지 유입될 수 있어 이미지가 쉽게 비대해진다.

_DOCKERIGNORE_EXAMPLE = (
    ".dockerignore 예시:\n"
    "    .git\n"
    "    .github\n"
    "    __pycache__\n"
    "    *.pyc\n"
    "    .env\n"
    "    .env.*\n"
    "    node_modules\n"
    "    dist\n"
    "    build\n"
    "    tests\n"
    "    *.md\n"
    "    Dockerfile*\n"
    "    docker-compose*"
)


def check(ir: DockerfileIR) -> list[Finding]:
    # 이 rule은 shell-form COPY만 다룬다.
    # JSON-array COPY 구문까지 정확히 다루려면 parser가 구조화된 인자를 제공해야 한다.
    final = ir.final_stage
    if final is None:
        return []

    findings: list[Finding] = []

    for instr in final.copy_instructions:
        args = instr.arguments

        # multi-stage artifact copy는 대개 의도된 패턴이므로 제외한다.
        if "--from=" in args:
            continue

        parts = args.split()
        # 현재는 `COPY . <dest>` 형태만 탐지한다.
        if not parts or parts[0] != ".":
            continue

        if not ir.has_dockerignore:
            severity = Severity.HIGH
            recommendation = (
                "no .dockerignore — all files in context will be included\n\n"
                "  option 1: create .dockerignore\n"
                f"    {_DOCKERIGNORE_EXAMPLE}\n\n"
                "  option 2: use explicit COPY paths\n"
                "    COPY src/ /app/src/\n"
                "    COPY pyproject.toml /app/\n"
                "    COPY requirements.txt /app/"
            )
            saving_min, saving_max = 90, 300
        else:
            severity = Severity.MEDIUM
            recommendation = (
                ".dockerignore exists but COPY . . still risks including unwanted files\n\n"
                "  prefer explicit COPY paths\n"
                "    COPY src/ /app/src/\n"
                "    COPY requirements.txt /app/\n"
                "    COPY pyproject.toml /app/"
            )
            saving_min, saving_max = 50, 200

        findings.append(Finding(
            rule_id="BROAD_COPY_SCOPE",
            severity=severity,
            line_no=instr.line_no,
            description=f"broad COPY scope detected: `COPY {args}`",
            recommendation=recommendation,
            saving_min_mb=saving_min,
            saving_max_mb=saving_max,
        ))

    return findings
