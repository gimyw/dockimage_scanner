"""
Single-stage build 탐지 — Multi-stage 전환 권장.

빌드 전용 이미지(Go, Rust, Maven 등)를 single stage로 사용하거나
빌드 도구가 설치된 single-stage Dockerfile을 탐지.
"""
from __future__ import annotations

import re

from imgadvisor.models import DockerfileIR, Finding, Severity

# Single-stage to multi-stage conversion rule.
# final image가 "빌드도 하고 실행도 하는" 상태인지 휴리스틱하게 판별한다.

# 이 이미지가 base면 multi-stage를 강하게 권장
_BUILD_BASE_PATTERNS: list[str] = [
    r"^golang:",
    r"^rust:",
    r"^maven:",
    r"^gradle:",
    r"^eclipse-temurin:\d+-jdk",
    r"^openjdk:\d+-jdk",
    r"^mcr\.microsoft\.com/dotnet/sdk:",
]

# final stage에서 보이면 build 성격이 강한 패키지들
_BUILD_TOOL_PACKAGES: list[str] = [
    "gcc", "g++", "make", "cmake", "build-essential",
    "maven", "gradle",
    "cargo", "rustc",
]

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
        "FROM scratch\n"
        "COPY --from=builder /app/server /server\n"
        "EXPOSE 8080\n"
        'ENTRYPOINT ["/server"]'
    ),
    "rust": (
        "# ── builder stage ──────────────────────────────\n"
        "FROM rust:1.77-slim AS builder\n"
        "WORKDIR /app\n"
        "COPY Cargo.toml Cargo.lock ./\n"
        "RUN mkdir src && echo 'fn main(){}' > src/main.rs\n"
        "RUN cargo build --release\n"
        "COPY src/ ./src/\n"
        "RUN touch src/main.rs && cargo build --release\n"
        "\n"
        "# ── runtime stage ───────────────────────────────\n"
        "FROM debian:bookworm-slim\n"
        "COPY --from=builder /app/target/release/app /app\n"
        'ENTRYPOINT ["/app"]'
    ),
    "java": (
        "# ── builder stage ──────────────────────────────\n"
        "FROM eclipse-temurin:17-jdk AS builder\n"
        "WORKDIR /app\n"
        "COPY pom.xml .\n"
        "RUN mvn dependency:go-offline -B\n"
        "COPY src/ ./src/\n"
        "RUN mvn package -DskipTests\n"
        "\n"
        "# ── runtime stage ───────────────────────────────\n"
        "FROM eclipse-temurin:17-jre\n"
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
        "FROM mcr.microsoft.com/dotnet/aspnet:8.0\n"
        "COPY --from=builder /out /app\n"
        'ENTRYPOINT ["dotnet", "/app/App.dll"]'
    ),
    "node": (
        "# ── builder stage ──────────────────────────────\n"
        "FROM node:20-alpine AS builder\n"
        "WORKDIR /app\n"
        "COPY package*.json ./\n"
        "RUN npm ci\n"
        "COPY . .\n"
        "RUN npm run build\n"
        "\n"
        "# ── runtime stage ───────────────────────────────\n"
        "FROM node:20-alpine\n"
        "WORKDIR /app\n"
        "COPY --from=builder /app/dist ./dist\n"
        "COPY --from=builder /app/package*.json ./\n"
        "RUN npm ci --omit=dev && npm cache clean --force\n"
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
    # 이미 multi-stage면 이 rule은 종료한다. 세부 비효율은 다른 rule이 본다.
    if ir.is_multi_stage:
        return []

    final = ir.final_stage
    if final is None:
        return []

    image = final.base_image
    run_text = final.all_run_text

    # 두 축 중 하나만 만족해도 탐지한다.
    # - final base image 자체가 builder 지향
    # - final stage RUN 내용에 build package 흔적이 있음
    is_build_base = any(re.match(p, image, re.IGNORECASE) for p in _BUILD_BASE_PATTERNS)
    has_build_pkg = any(re.search(rf"\b{re.escape(t)}\b", run_text, re.IGNORECASE)
                        for t in _BUILD_TOOL_PACKAGES)

    if not (is_build_base or has_build_pkg):
        return []

    lang = _detect_lang(image, run_text)
    template = _TEMPLATES.get(lang, _TEMPLATES["generic"])

    recommendation = (
        f"convert to multi-stage build:\n\n"
        + "\n".join(f"  {line}" for line in template.splitlines())
    )

    return [Finding(
        rule_id="SINGLE_STAGE_BUILD",
        severity=Severity.HIGH,
        line_no=1,
        description="single-stage build — build tools are included in the runtime image",
        recommendation=recommendation,
        saving_min_mb=150,
        saving_max_mb=600,
    )]


def _detect_lang(image: str, run_text: str) -> str:
    # 추천 템플릿을 고르기 위한 가벼운 생태계 추정 로직.
    # 정확성보다 설명 가능성과 유지보수성을 우선한다.
    if re.match(r"^golang:", image, re.IGNORECASE):
        return "go"
    if re.match(r"^rust:", image, re.IGNORECASE):
        return "rust"
    if re.match(r"^mcr\.microsoft\.com/dotnet/sdk:", image, re.IGNORECASE):
        return "dotnet"
    if re.match(r"^(eclipse-temurin|openjdk):", image, re.IGNORECASE):
        return "java"
    if re.search(r"\bmvn\b|\bgradle\b", run_text, re.IGNORECASE):
        return "java"
    if re.match(r"^node:", image, re.IGNORECASE):
        return "node"
    return "generic"
