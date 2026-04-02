"""
Single-stage build 탐지 — Multi-stage 전환 권장.

빌드 전용 이미지(Go, Rust, Maven 등)를 single-stage로 사용하거나
빌드 도구가 설치된 single-stage Dockerfile을 탐지한다.

탐지 조건 (둘 중 하나):
  1. 베이스 이미지가 빌드 전용 이미지 (golang:, rust:, maven:, openjdk:-jdk 등)
  2. RUN 명령에 빌드 도구 패키지가 포함 (gcc, g++, make, cmake 등)

이미 멀티-스테이지 빌드를 사용 중이면 탐지하지 않는다.
"""
from __future__ import annotations

import re

from imgadvisor.models import DockerfileIR, Finding, Severity

# 이 이미지가 베이스이면 멀티-스테이지를 강하게 권장
# 이 이미지들은 런타임에 불필요한 빌드 환경을 포함하고 있다
_BUILD_BASE_PATTERNS: list[str] = [
    r"^golang:",                          # Go 빌드 이미지 (수백 MB)
    r"^rust:",                            # Rust 빌드 이미지
    r"^maven:",                           # Maven + JDK 이미지
    r"^gradle:",                          # Gradle + JDK 이미지
    r"^eclipse-temurin:\d+-jdk",         # OpenJDK JDK (JRE만 있으면 충분)
    r"^openjdk:\d+-jdk",                 # OpenJDK JDK
    r"^mcr\.microsoft\.com/dotnet/sdk:", # .NET SDK (ASP.NET 런타임만 필요)
]

# RUN 명령에 이 패키지가 있으면 멀티-스테이지 권장
_BUILD_TOOL_PACKAGES: list[str] = [
    "gcc", "g++", "make", "cmake", "build-essential",
    "maven", "gradle",
    "cargo", "rustc",
]

# 언어별 멀티-스테이지 Dockerfile 템플릿
# recommender가 SINGLE_STAGE_BUILD Finding의 recommendation에 첨부한다
_TEMPLATES: dict[str, str] = {
    "go": (
        "# ── builder stage ──────────────────────────────\n"
        "FROM golang:1.22-alpine AS builder\n"
        "WORKDIR /app\n"
        "COPY go.mod go.sum ./\n"
        "RUN go mod download\n"
        "COPY . .\n"
        "RUN CGO_ENABLED=0 GOOS=linux go build -o /app/server .\n"
        "\n"
        "# ── runtime stage ───────────────────────────────\n"
        "FROM scratch\n"                      # scratch: 가장 작은 베이스 (Go 정적 바이너리에 최적)
        "COPY --from=builder /app/server /server\n"
        "EXPOSE 8080\n"
        'ENTRYPOINT ["/server"]'
    ),
    "rust": (
        "# ── builder stage ──────────────────────────────\n"
        "FROM rust:1.77-slim AS builder\n"
        "WORKDIR /app\n"
        "COPY Cargo.toml Cargo.lock ./\n"
        # 더미 main.rs로 의존성 레이어를 먼저 캐시 (소스 변경 시 재빌드 최소화)
        "RUN mkdir src && echo 'fn main(){}' > src/main.rs\n"
        "RUN cargo build --release\n"
        "COPY src/ ./src/\n"
        "RUN touch src/main.rs && cargo build --release\n"
        "\n"
        "# ── runtime stage ───────────────────────────────\n"
        "FROM debian:bookworm-slim\n"         # Rust 바이너리는 glibc 필요
        "COPY --from=builder /app/target/release/app /app\n"
        'ENTRYPOINT ["/app"]'
    ),
    "java": (
        "# ── builder stage ──────────────────────────────\n"
        "FROM eclipse-temurin:17-jdk AS builder\n"
        "WORKDIR /app\n"
        "COPY pom.xml .\n"
        "RUN mvn dependency:go-offline -B\n"  # 의존성 미리 다운로드 (캐시 최적화)
        "COPY src/ ./src/\n"
        "RUN mvn package -DskipTests\n"
        "\n"
        "# ── runtime stage ───────────────────────────────\n"
        "FROM eclipse-temurin:17-jre\n"       # JDK 대신 JRE만 사용 (크기 절감)
        "COPY --from=builder /app/target/*.jar /app/app.jar\n"
        'ENTRYPOINT ["java", "-jar", "/app/app.jar"]'
    ),
    "dotnet": (
        "# ── builder stage ──────────────────────────────\n"
        "FROM mcr.microsoft.com/dotnet/sdk:8.0 AS builder\n"
        "WORKDIR /app\n"
        "COPY *.csproj .\n"
        "RUN dotnet restore\n"
        "COPY . .\n"
        "RUN dotnet publish -c Release -o /out\n"
        "\n"
        "# ── runtime stage ───────────────────────────────\n"
        "FROM mcr.microsoft.com/dotnet/aspnet:8.0\n"  # SDK 대신 ASP.NET 런타임만 사용
        "COPY --from=builder /out /app\n"
        'ENTRYPOINT ["dotnet", "/app/App.dll"]'
    ),
    "node": (
        "# ── builder stage ──────────────────────────────\n"
        "FROM node:20-alpine AS builder\n"
        "WORKDIR /app\n"
        "COPY package*.json ./\n"
        "RUN npm ci\n"                        # npm install 대신 ci (lockfile 기반 재현성)
        "COPY . .\n"
        "RUN npm run build\n"
        "\n"
        "# ── runtime stage ───────────────────────────────\n"
        "FROM node:20-alpine\n"
        "WORKDIR /app\n"
        "COPY --from=builder /app/dist ./dist\n"
        "COPY --from=builder /app/package*.json ./\n"
        "RUN npm ci --omit=dev && npm cache clean --force\n"  # devDependencies 제외
        'ENTRYPOINT ["node", "dist/index.js"]'
    ),
    "generic": (
        "# ── builder stage ──────────────────────────────\n"
        "FROM <build-image> AS builder\n"
        "WORKDIR /app\n"
        "# ... 빌드 수행 ...\n"
        "\n"
        "# ── runtime stage ───────────────────────────────\n"
        "FROM <runtime-image-slim>\n"
        "COPY --from=builder /app/output /app/\n"
        'ENTRYPOINT ["/app/entrypoint"]'
    ),
}


def check(ir: DockerfileIR) -> list[Finding]:
    """
    단일 스테이지 빌드에서 빌드 전용 이미지 또는 빌드 도구 사용을 탐지한다.

    탐지 조건 (이미 멀티-스테이지면 탐지하지 않음):
    1. 베이스 이미지가 _BUILD_BASE_PATTERNS 중 하나와 일치
    2. RUN 명령에 _BUILD_TOOL_PACKAGES 중 하나가 포함

    언어를 자동 감지해 적합한 멀티-스테이지 템플릿을 권고에 포함한다.

    Args:
        ir: Dockerfile 중간 표현

    Returns:
        단일 스테이지 빌드가 탐지되면 Finding 하나를 담은 리스트, 없으면 빈 리스트
    """
    # 이미 멀티-스테이지이면 검사 불필요
    if ir.is_multi_stage:
        return []

    final = ir.final_stage
    if final is None:
        return []

    image = final.base_image
    run_text = final.all_run_text  # 모든 RUN 명령 텍스트를 합친 문자열

    # 탐지 조건 1: 빌드 전용 베이스 이미지 사용
    is_build_base = any(re.match(p, image, re.IGNORECASE) for p in _BUILD_BASE_PATTERNS)
    # 탐지 조건 2: RUN 명령에 빌드 도구 패키지 포함
    has_build_pkg = any(re.search(rf"\b{re.escape(t)}\b", run_text, re.IGNORECASE)
                        for t in _BUILD_TOOL_PACKAGES)

    # 둘 다 해당 없으면 탐지하지 않음
    if not (is_build_base or has_build_pkg):
        return []

    # 언어 감지 후 적합한 멀티-스테이지 템플릿 선택
    lang = _detect_lang(image, run_text)
    template = _TEMPLATES.get(lang, _TEMPLATES["generic"])

    recommendation = (
        f"convert to multi-stage build:\n\n"
        + "\n".join(f"  {line}" for line in template.splitlines())
    )

    return [Finding(
        rule_id="SINGLE_STAGE_BUILD",
        severity=Severity.HIGH,
        line_no=1,  # FROM 명령은 항상 파일 초반에 있으므로 줄 번호 1 사용
        description="single-stage build — build tools are included in the runtime image",
        recommendation=recommendation,
        saving_min_mb=150,
        saving_max_mb=600,
    )]


def _detect_lang(image: str, run_text: str) -> str:
    """
    베이스 이미지 이름과 RUN 명령 텍스트를 분석해 언어를 감지한다.

    탐지 우선순위:
    1. 이미지 이름 기반 (golang:, rust:, dotnet/sdk:, eclipse-temurin:, openjdk:, node:)
    2. RUN 명령 기반 (mvn, gradle 키워드)
    3. 위에 해당하지 않으면 "generic"

    Returns:
        "go" | "rust" | "dotnet" | "java" | "node" | "generic"
    """
    if re.match(r"^golang:", image, re.IGNORECASE):
        return "go"
    if re.match(r"^rust:", image, re.IGNORECASE):
        return "rust"
    if re.match(r"^mcr\.microsoft\.com/dotnet/sdk:", image, re.IGNORECASE):
        return "dotnet"
    if re.match(r"^(eclipse-temurin|openjdk):", image, re.IGNORECASE):
        return "java"
    # 이미지 이름만으로 판단 안 될 때 RUN 명령의 mvn/gradle로 Java 감지
    if re.search(r"\bmvn\b|\bgradle\b", run_text, re.IGNORECASE):
        return "java"
    if re.match(r"^node:", image, re.IGNORECASE):
        return "node"
    return "generic"
