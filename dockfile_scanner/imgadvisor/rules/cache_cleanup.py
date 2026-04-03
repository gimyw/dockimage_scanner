"""
패키지 매니저 캐시 정리 누락 탐지.

RUN 명령에서 패키지를 설치했지만 캐시를 정리하지 않은 경우를 탐지한다.
캐시가 이미지에 남아 있으면 불필요하게 용량을 차지하므로 설치와 캐시 정리를
같은 RUN 명령에서 && 로 연결하는 것이 권장된다.

지원 패키지 매니저:
  apt, pip, apk, npm, yarn, pnpm, yum, dnf, gem, composer, maven, gradle
"""
from __future__ import annotations

import re

from imgadvisor.models import DockerfileIR, Finding, Severity

# 각 패키지 매니저별 검사 규칙 정의
# - id      : Finding rule_id (고유 식별자)
# - pm      : 패키지 매니저 이름 (출력용)
# - install : 설치 명령 탐지용 정규식
# - cleanup : 캐시 정리 여부 확인용 정규식 목록 (하나라도 있으면 정리된 것으로 판단)
# - recommended: 권장 명령어 예시
# - min/max : 예상 절감 용량 범위 (MB)
_CHECKS: list[dict] = [
    {
        "id": "APT_CACHE_NOT_CLEANED",
        "pm": "apt-get",
        # apt-get install 또는 apt install 패턴
        "install": r"apt-get\s+install|apt\s+install",
        # 캐시 정리 방법: rm -rf /var/lib/apt/lists, apt-get clean, apt-get autoremove
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
        # pip install 또는 pip3 install
        "install": r"pip\s+install|pip3\s+install",
        # 캐시 비활성화: --no-cache-dir 플래그 또는 pip cache purge
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
        # Alpine의 apk add
        "install": r"apk\s+add",
        # 캐시 비활성화: --no-cache 플래그 또는 /var/cache/apk 삭제
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
        # npm install 또는 npm ci (clean install)
        "install": r"npm\s+install|npm\s+ci",
        # 캐시 정리 방법: npm cache clean, --omit=dev, --production, NODE_ENV=production
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
        # yarn install 또는 yarn add
        "install": r"yarn\s+install|yarn\s+add",
        # 캐시 정리: yarn cache clean, --production 플래그
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
        # pnpm install 또는 pnpm add
        "install": r"pnpm\s+install|pnpm\s+add",
        # 캐시 정리: pnpm store prune, --prod 플래그
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
        # yum install (CentOS/RHEL)
        "install": r"yum\s+install|yum\s+-y\s+install",
        # 캐시 정리: yum clean all 또는 /var/cache/yum 삭제
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
        # dnf install (Fedora/RHEL 8+)
        "install": r"dnf\s+install",
        # 캐시 정리: dnf clean all
        "cleanup": [
            r"dnf\s+clean\s+all",
        ],
        "recommended": "RUN dnf install -y <pkg> && dnf clean all",
        "min": 20, "max": 80,
    },
    {
        "id": "GEM_CACHE_NOT_CLEANED",
        "pm": "gem",
        # gem install 또는 bundle install (Ruby)
        "install": r"gem\s+install|bundle\s+install",
        # 캐시 정리: --no-document(문서 제외), --without development, gem cleanup
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
        # composer install 또는 composer require (PHP)
        "install": r"composer\s+install|composer\s+require",
        # 캐시 정리: --no-dev(개발 의존성 제외), composer clear-cache
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
        # mvn 또는 maven 명령 (Java 빌드)
        "install": r"\bmvn\b|\bmaven\b",
        # ~/.m2 캐시 삭제로 정리 여부 확인
        "cleanup": [
            r"rm\s+-rf\s+.*\.m2",
            r"rm\s+-rf\s+\$HOME/\.m2",
            r"rm\s+-rf\s+/root/\.m2",
        ],
        # Maven은 멀티-스테이지로 빌드하는 것이 근본적인 해결책
        "recommended": (
            "Use multi-stage build: run mvn package in builder stage,\n"
            "  COPY only the JAR to runtime stage (excludes ~/.m2 cache)"
        ),
        "min": 50, "max": 200,
    },
    {
        "id": "GRADLE_CACHE_IN_FINAL_STAGE",
        "pm": "gradle",
        # gradle 또는 gradlew/gradleW 명령 (Java/Kotlin 빌드)
        "install": r"\bgradle\b|\bgradle[wW]\b",
        # .gradle 캐시 삭제로 정리 여부 확인
        "cleanup": [
            r"rm\s+-rf\s+.*\.gradle",
            r"rm\s+-rf\s+/root/\.gradle",
        ],
        # Gradle도 멀티-스테이지가 근본적인 해결책
        "recommended": (
            "Use multi-stage build: run gradle build in builder stage,\n"
            "  COPY only JAR/WAR to runtime stage (excludes .gradle cache)"
        ),
        "min": 50, "max": 200,
    },
]


def check(ir: DockerfileIR) -> list[Finding]:
    """
    final stage의 RUN 명령을 순회하며 캐시 미정리 패턴을 탐지한다.

    탐지 로직:
    1. RUN 명령 텍스트에서 install 패턴이 있는지 확인
    2. 있으면 같은 RUN 명령 내에 cleanup 패턴이 하나라도 있는지 확인
    3. cleanup이 없으면 Finding 생성

    동일한 규칙은 seen_ids로 중복 탐지를 방지한다
    (여러 RUN 명령에 같은 패키지 매니저가 나와도 한 번만 리포트).

    Args:
        ir: Dockerfile 중간 표현

    Returns:
        캐시 미정리가 탐지된 Finding 목록
    """
    final = ir.final_stage
    if final is None:
        return []

    findings: list[Finding] = []
    seen_ids: set[str] = set()  # 이미 탐지된 규칙 ID (중복 방지)

    for instr in final.run_instructions:
        run_text = instr.arguments  # 합쳐진 RUN 명령 전체 텍스트

        for rule in _CHECKS:
            # 이미 탐지된 규칙은 건너뜀
            if rule["id"] in seen_ids:
                continue
            # 설치 명령이 없으면 이 RUN은 해당 패키지 매니저와 무관
            if not re.search(rule["install"], run_text, re.IGNORECASE):
                continue
            # 캐시 정리 패턴이 하나라도 있으면 정상
            cleaned = any(
                re.search(p, run_text, re.IGNORECASE) for p in rule["cleanup"]
            )
            if cleaned:
                continue

            # 탐지: 캐시 정리 없이 설치만 함
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
