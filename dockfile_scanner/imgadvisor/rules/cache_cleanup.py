"""
패키지 매니저 캐시 정리 누락 탐지.

apt, pip, apk, npm, yum, dnf, gem, composer, maven 지원.
"""
from __future__ import annotations

import re

from imgadvisor.models import DockerfileIR, Finding, Severity

# Final-stage cache cleanup rule.
# `_CHECKS`의 각 항목은 하나의 패키지 매니저 또는 빌드 도구 계열을 설명한다.
# - install: 설치/사용 흔적을 찾는 패턴
# - cleanup: 같은 RUN 안에서 캐시 제거 또는 캐시 비활성화로 인정할 패턴
# - recommended/min/max: 사용자에게 보여줄 개선안과 예상 절감 범위

_CHECKS: list[dict] = [
    {
        "id": "APT_CACHE_NOT_CLEANED",
        "pm": "apt-get",
        "install": r"apt-get\s+install|apt\s+install",
        "cleanup": [
            r"rm\s+-rf\s+/var/lib/apt/lists",
            r"apt-get\s+clean",
            r"apt-get\s+autoremove",
        ],
        "recommended": (
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "        <pkg> \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        ),
        "min": 30, "max": 120,
    },
    {
        "id": "PIP_CACHE_NOT_DISABLED",
        "pm": "pip",
        "install": r"pip\s+install|pip3\s+install",
        "cleanup": [
            r"--no-cache-dir",
            r"pip\s+cache\s+purge",
        ],
        "recommended": "RUN pip install --no-cache-dir <pkg>",
        "min": 20, "max": 80,
    },
    {
        "id": "APK_CACHE_NOT_DISABLED",
        "pm": "apk",
        "install": r"apk\s+add",
        "cleanup": [
            r"--no-cache",
            r"rm\s+-rf\s+/var/cache/apk",
        ],
        "recommended": "RUN apk add --no-cache <pkg>",
        "min": 10, "max": 40,
    },
    {
        "id": "NPM_CACHE_NOT_CLEANED",
        "pm": "npm",
        "install": r"npm\s+install|npm\s+ci",
        "cleanup": [
            r"npm\s+cache\s+clean",
            r"--omit=dev",
            r"--production",
            r"NODE_ENV\s*=\s*production",
        ],
        "recommended": (
            "RUN npm ci --omit=dev \\\n"
            "    && npm cache clean --force"
        ),
        "min": 20, "max": 100,
    },
    {
        "id": "YARN_CACHE_NOT_CLEANED",
        "pm": "yarn",
        "install": r"yarn\s+install|yarn\s+add",
        "cleanup": [
            r"yarn\s+cache\s+clean",
            r"--production",
            r"--frozen-lockfile.*--production",
        ],
        "recommended": (
            "RUN yarn install --frozen-lockfile --production \\\n"
            "    && yarn cache clean"
        ),
        "min": 20, "max": 100,
    },
    {
        "id": "PNPM_CACHE_NOT_CLEANED",
        "pm": "pnpm",
        "install": r"pnpm\s+install|pnpm\s+add",
        "cleanup": [
            r"pnpm\s+store\s+prune",
            r"--prod",
        ],
        "recommended": (
            "RUN pnpm install --prod \\\n"
            "    && pnpm store prune"
        ),
        "min": 20, "max": 80,
    },
    {
        "id": "YUM_CACHE_NOT_CLEANED",
        "pm": "yum",
        "install": r"yum\s+install|yum\s+-y\s+install",
        "cleanup": [
            r"yum\s+clean\s+all",
            r"rm\s+-rf\s+/var/cache/yum",
        ],
        "recommended": (
            "RUN yum install -y <pkg> \\\n"
            "    && yum clean all \\\n"
            "    && rm -rf /var/cache/yum"
        ),
        "min": 20, "max": 80,
    },
    {
        "id": "DNF_CACHE_NOT_CLEANED",
        "pm": "dnf",
        "install": r"dnf\s+install",
        "cleanup": [
            r"dnf\s+clean\s+all",
        ],
        "recommended": "RUN dnf install -y <pkg> && dnf clean all",
        "min": 20, "max": 80,
    },
    {
        "id": "GEM_CACHE_NOT_CLEANED",
        "pm": "gem",
        "install": r"gem\s+install|bundle\s+install",
        "cleanup": [
            r"--no-document",
            r"--without\s+development",
            r"gem\s+cleanup",
        ],
        "recommended": (
            "RUN gem install --no-document <gem> \\\n"
            "    && gem cleanup"
        ),
        "min": 20, "max": 80,
    },
    {
        "id": "COMPOSER_CACHE_NOT_CLEANED",
        "pm": "composer",
        "install": r"composer\s+install|composer\s+require",
        "cleanup": [
            r"--no-dev",
            r"composer\s+clear-cache",
        ],
        "recommended": (
            "RUN composer install --no-dev --optimize-autoloader \\\n"
            "    && composer clear-cache"
        ),
        "min": 20, "max": 80,
    },
    {
        "id": "MAVEN_CACHE_IN_FINAL_STAGE",
        "pm": "mvn",
        "install": r"\bmvn\b|\bmaven\b",
        "cleanup": [
            r"rm\s+-rf\s+.*\.m2",
            r"rm\s+-rf\s+\$HOME/\.m2",
            r"rm\s+-rf\s+/root/\.m2",
        ],
        "recommended": (
            "Use multi-stage build: run mvn package in builder stage,\n"
            "  COPY only the JAR to runtime stage (excludes ~/.m2 cache)"
        ),
        "min": 50, "max": 200,
    },
    {
        "id": "GRADLE_CACHE_IN_FINAL_STAGE",
        "pm": "gradle",
        "install": r"\bgradle\b|\bgradle[wW]\b",
        "cleanup": [
            r"rm\s+-rf\s+.*\.gradle",
            r"rm\s+-rf\s+/root/\.gradle",
        ],
        "recommended": (
            "Use multi-stage build: run gradle build in builder stage,\n"
            "  COPY only JAR/WAR to runtime stage (excludes .gradle cache)"
        ),
        "min": 50, "max": 200,
    },
]


def check(ir: DockerfileIR) -> list[Finding]:
    # 같은 규칙이 여러 RUN에서 반복 매칭되어도 한 번만 보고한다.
    # 출력은 깔끔해지지만, 두 번째 이후의 발생 위치는 의도적으로 생략된다.
    final = ir.final_stage
    if final is None:
        return []

    findings: list[Finding] = []
    seen_ids: set[str] = set()

    for instr in final.run_instructions:
        run_text = instr.arguments

        for rule in _CHECKS:
            if rule["id"] in seen_ids:
                continue
            if not re.search(rule["install"], run_text, re.IGNORECASE):
                continue
            # cleanup은 같은 RUN 안에 있어야만 실제 이미지 크기 절감에 의미가 있다.
            # 이후 RUN에서 지워도 이전 layer의 용량은 그대로 남기 때문이다.
            cleaned = any(
                re.search(p, run_text, re.IGNORECASE) for p in rule["cleanup"]
            )
            if cleaned:
                continue

            seen_ids.add(rule["id"])
            findings.append(Finding(
                rule_id=rule["id"],
                severity=Severity.MEDIUM,
                line_no=instr.line_no,
                description=f"`{rule['pm']}` cache not cleaned",
                recommendation=rule["recommended"],
                saving_min_mb=rule["min"],
                saving_max_mb=rule["max"],
            ))

    return findings
