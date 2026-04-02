"""
광범위한 COPY 범위 탐지.

`COPY . .` 또는 `COPY . /app` 처럼 빌드 컨텍스트 전체를 복사하는 패턴을 탐지한다.
.dockerignore가 없으면 .git, node_modules, .env 등 민감하거나 불필요한 파일까지
이미지에 포함되어 크기가 커지고 보안 위험이 생긴다.

심각도 기준:
  - .dockerignore 없음: HIGH (모든 파일이 포함될 위험)
  - .dockerignore 있음: MEDIUM (일부 파일은 걸러지지만 여전히 위험)
"""
from __future__ import annotations

from imgadvisor.models import DockerfileIR, Finding, Severity

# .dockerignore 예시 — 사용자에게 권장하는 최소 구성
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
    final stage의 COPY 명령에서 컨텍스트 전체 복사 패턴을 탐지한다.

    탐지 조건:
    - `--from=` 없는 COPY 명령 (스테이지 간 복사는 제외)
    - 첫 번째 인수가 "." (현재 컨텍스트 전체)

    Finding 당 하나의 COPY 명령에 대응하므로, 여러 `COPY . X` 가 있으면
    여러 Finding이 생성된다.

    Args:
        ir: Dockerfile 중간 표현

    Returns:
        광범위한 COPY가 탐지된 Finding 목록
    """
    final = ir.final_stage
    if final is None:
        return []

    findings: list[Finding] = []

    for instr in final.copy_instructions:
        args = instr.arguments

        # --from=<stage> 가 있는 COPY는 멀티-스테이지 간 복사이므로 무시
        if "--from=" in args:
            continue

        parts = args.split()
        # 첫 번째 인수가 "." 이어야 컨텍스트 전체 복사 패턴
        if not parts or parts[0] != ".":
            continue

        # .dockerignore 유무에 따라 심각도와 메시지를 다르게 설정
        if not ir.has_dockerignore:
            # .dockerignore가 없으면 .git, node_modules, .env 등 모두 포함될 수 있어 HIGH
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
            # .dockerignore가 있어도 명시적 경로 복사가 더 안전 → MEDIUM
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
