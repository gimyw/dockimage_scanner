"""
분석 엔진 — 모든 규칙을 순서대로 실행하고 Finding 목록을 합산.

규칙을 추가하려면 rules/ 아래에 check(ir) -> list[Finding] 함수를 구현하고
_ALL_RULES 리스트에 등록하면 된다.
"""
from __future__ import annotations

from imgadvisor.models import DockerfileIR, Finding
from imgadvisor.rules import base_image, build_tools, cache_cleanup, copy_scope, multi_stage

# 실행할 규칙 함수 목록 (순서는 출력 순서에 영향을 줌)
_ALL_RULES = [
    base_image.check,    # 베이스 이미지 최적화 여부 (slim/alpine/distroless 미사용)
    build_tools.check,   # 빌드 도구가 final stage에 잔존 여부
    cache_cleanup.check, # apt/pip/npm 등 패키지 매니저 캐시 미정리 여부
    copy_scope.check,    # COPY . . 처럼 컨텍스트 전체 복사 여부
    multi_stage.check,   # 단일 스테이지 빌드를 멀티-스테이지로 전환 필요 여부
]


def analyze(ir: DockerfileIR) -> list[Finding]:
    """
    DockerfileIR을 모든 규칙에 통과시켜 Finding 목록을 반환한다.

    각 규칙은 독립적으로 실행되며, 한 규칙이 Finding을 반환해도
    다른 규칙의 실행에는 영향을 주지 않는다.

    Args:
        ir: parser.parse()가 반환한 Dockerfile 중간 표현

    Returns:
        탐지된 모든 Finding의 합산 목록 (규칙 등록 순서대로)
    """
    findings: list[Finding] = []
    for rule in _ALL_RULES:
        findings.extend(rule(ir))
    return findings
