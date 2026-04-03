"""
build context 전체를 과하게 복사하는 COPY 패턴을 검사하는 rule.

`COPY . .` 같은 명령은 편하지만, `.git`, 테스트 파일, 로컬 캐시, 비밀값
파일까지 함께 이미지로 들어갈 수 있다. 이 rule은 final stage에서 그런
과도한 복사 범위를 찾아서 `.dockerignore` 추가나 명시적 COPY 경로 분리를
권장한다.
"""
from __future__ import annotations

from imgadvisor.models import DockerfileIR, Finding, Severity

_DOCKERIGNORE_EXAMPLE = (
    ".dockerignore example:\n"
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
    """
    final stage의 COPY 명령 중 build context 전체를 복사하는 패턴을 찾는다.

    탐지 조건:
    1. COPY instruction이어야 한다.
    2. `--from=` 이 없어야 한다.
       즉, 다른 stage에서 산출물만 가져오는 정상적인 multi-stage COPY는 제외한다.
    3. 첫 번째 source 인수가 `.` 이어야 한다.

    `.dockerignore` 유무에 따라 심각도를 다르게 준다.
    - 없으면 HIGH: 거의 모든 불필요 파일이 들어갈 수 있음
    - 있으면 MEDIUM: 그래도 명시적 경로 복사보다 위험함
    """
    final = ir.final_stage
    if final is None:
        return []

    findings: list[Finding] = []

    for instr in final.copy_instructions:
        args = instr.arguments
        if "--from=" in args:
            continue

        parts = args.split()
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
