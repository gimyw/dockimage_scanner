"""
Base image 최적화 규칙.

패턴 목록을 순서대로 매칭 후 첫 번째 매칭에서 Finding 생성.
이미 slim/alpine/distroless 등 경량 이미지면 skip.
"""
from __future__ import annotations

import re
from typing import Optional

from imgadvisor.models import DockerfileIR, Finding, Patch, Severity

# Runtime base image optimization rule.
# Only the final stage is inspected because it directly determines runtime image
# size and deployment footprint.
#
# The implementation is table-driven:
# - regex pattern for the detected image
# - replacement candidates
# - estimated saving range and caveats per candidate

# (regex_pattern, list of recommendation dicts)
# recommendation dict keys: image, min, max, note
# {v} → regex group(1) 로 치환
_RULES: list[tuple[str, list[dict]]] = [
    # ── Python ──────────────────────────────────────────────────────────────
    (r"^python:(\d+\.\d+(?:\.\d+)?)$", [
        {"image": "python:{v}-slim",                  "min": 250, "max": 420, "note": None},
        {"image": "python:{v}-alpine",                "min": 350, "max": 520, "note": "musl libc compat"},
        {"image": "gcr.io/distroless/python3",        "min": 450, "max": 630, "note": "no shell, recommended for prod"},
    ]),
    (r"^python:(\d+)$", [
        {"image": "python:{v}-slim",                  "min": 250, "max": 420, "note": None},
        {"image": "python:{v}-alpine",                "min": 350, "max": 520, "note": "musl libc compat"},
    ]),
    (r"^python:latest$", [
        {"image": "python:3-slim",                    "min": 250, "max": 420, "note": "pin to a specific tag"},
    ]),

    # ── Node ────────────────────────────────────────────────────────────────
    (r"^node:(\d+)$", [
        {"image": "node:{v}-slim",                    "min": 280, "max": 420, "note": None},
        {"image": "node:{v}-alpine",                  "min": 380, "max": 550, "note": "musl libc compat"},
        {"image": "gcr.io/distroless/nodejs{v}",      "min": 450, "max": 620, "note": "no shell"},
    ]),
    (r"^node:(\d+)-slim$", [
        {"image": "node:{v}-alpine",                  "min": 50,  "max": 150, "note": "musl libc compat"},
    ]),
    (r"^node:lts$", [
        {"image": "node:lts-slim",                    "min": 280, "max": 420, "note": None},
        {"image": "node:lts-alpine",                  "min": 380, "max": 550, "note": "musl libc compat"},
    ]),
    (r"^node:current$", [
        {"image": "node:current-slim",                "min": 280, "max": 420, "note": None},
    ]),
    (r"^node:latest$", [
        {"image": "node:lts-slim",                    "min": 280, "max": 420, "note": "pin to a specific tag"},
    ]),

    # ── Java (OpenJDK — deprecated) ─────────────────────────────────────────
    (r"^openjdk:(\d+)$", [
        {"image": "eclipse-temurin:{v}-jre",              "min": 200, "max": 380, "note": "switch JDK to JRE"},
        {"image": "gcr.io/distroless/java{v}-debian12",   "min": 350, "max": 550, "note": "no shell"},
    ]),
    (r"^openjdk:(\d+)-jdk$", [
        {"image": "eclipse-temurin:{v}-jre",              "min": 200, "max": 380, "note": "switch JDK to JRE"},
    ]),
    (r"^openjdk:(\d+)-slim$", [
        {"image": "eclipse-temurin:{v}-jre-alpine",       "min": 100, "max": 250, "note": None},
    ]),

    # ── Eclipse Temurin ─────────────────────────────────────────────────────
    (r"^eclipse-temurin:(\d+)$", [
        {"image": "eclipse-temurin:{v}-jre",              "min": 150, "max": 300, "note": "defaults to JDK, switch to JRE"},
    ]),
    (r"^eclipse-temurin:(\d+)-jdk$", [
        {"image": "eclipse-temurin:{v}-jre",              "min": 150, "max": 300, "note": "switch JDK to JRE"},
        {"image": "gcr.io/distroless/java{v}-debian12",   "min": 300, "max": 500, "note": "no shell"},
    ]),
    (r"^eclipse-temurin:(\d+)-jdk-alpine$", [
        {"image": "eclipse-temurin:{v}-jre-alpine",       "min": 100, "max": 250, "note": "switch JDK to JRE"},
    ]),

    # ── Go ──────────────────────────────────────────────────────────────────
    (r"^golang:(\d+\.\d+(?:\.\d+)?)$", [
        {"image": "scratch (after multi-stage)",                          "min": 600, "max": 950, "note": "Go binary can be statically linked"},
        {"image": "gcr.io/distroless/static-debian12 (after multi-stage)", "min": 580, "max": 920, "note": "includes CA certs"},
        {"image": "alpine:3.19 (after multi-stage)",                      "min": 540, "max": 880, "note": "use when shell access needed"},
    ]),
    (r"^golang:(\d+\.\d+(?:\.\d+)?)-alpine$", [
        {"image": "scratch (after multi-stage)",             "min": 400, "max": 750, "note": "Go binary can be statically linked"},
        {"image": "alpine:3.19 (after multi-stage)",         "min": 350, "max": 700, "note": None},
    ]),
    (r"^golang:latest$", [
        {"image": "scratch (after multi-stage)",             "min": 600, "max": 950, "note": "pin to a specific tag"},
    ]),

    # ── Rust ────────────────────────────────────────────────────────────────
    (r"^rust:(\d+\.\d+(?:\.\d+)?)$", [
        {"image": "scratch (after multi-stage)",                  "min": 700, "max": 1100, "note": "Rust binary can be statically linked"},
        {"image": "debian:bookworm-slim (after multi-stage)",     "min": 600, "max": 1000, "note": None},
        {"image": "gcr.io/distroless/cc-debian12 (after multi-stage)", "min": 650, "max": 1050, "note": "includes C runtime only"},
    ]),
    (r"^rust:(\d+\.\d+(?:\.\d+)?)-slim$", [
        {"image": "scratch (after multi-stage)",                  "min": 500, "max": 900, "note": "Rust binary can be statically linked"},
    ]),
    (r"^rust:latest$", [
        {"image": "scratch (after multi-stage)",                  "min": 700, "max": 1100, "note": "pin to a specific tag"},
    ]),

    # ── Ubuntu ──────────────────────────────────────────────────────────────
    (r"^ubuntu:(\d+\.\d+)$", [
        {"image": "ubuntu:{v}-minimal",     "min": 30,  "max": 60,  "note": None},
        {"image": "debian:bookworm-slim",   "min": 20,  "max": 80,  "note": None},
        {"image": "alpine:3.19",            "min": 150, "max": 280, "note": "check package compat"},
    ]),
    (r"^ubuntu:latest$", [
        {"image": "ubuntu:22.04",           "min": 0,   "max": 0,   "note": "pin to a specific tag"},
        {"image": "debian:bookworm-slim",   "min": 20,  "max": 80,  "note": None},
    ]),
    (r"^ubuntu:jammy$", [
        {"image": "ubuntu:22.04-minimal",   "min": 30,  "max": 60,  "note": None},
    ]),
    (r"^ubuntu:focal$", [
        {"image": "ubuntu:20.04-minimal",   "min": 30,  "max": 60,  "note": None},
    ]),
    (r"^ubuntu:noble$", [
        {"image": "ubuntu:24.04-minimal",   "min": 30,  "max": 60,  "note": None},
    ]),

    # ── Debian ──────────────────────────────────────────────────────────────
    (r"^debian:(bullseye|bookworm|buster|stretch|trixie)$", [
        {"image": "debian:{v}-slim",        "min": 50,  "max": 120, "note": None},
        {"image": "alpine:3.19",            "min": 150, "max": 280, "note": "check package compat"},
    ]),
    (r"^debian:latest$", [
        {"image": "debian:bookworm-slim",   "min": 50,  "max": 120, "note": "pin to a specific tag"},
    ]),

    # ── Nginx ───────────────────────────────────────────────────────────────
    (r"^nginx:(\d+\.\d+(?:\.\d+)?)$", [
        {"image": "nginx:{v}-alpine",          "min": 90,  "max": 180, "note": None},
        {"image": "nginx:{v}-alpine-slim",     "min": 100, "max": 200, "note": "minimal modules"},
    ]),
    (r"^nginx:latest$", [
        {"image": "nginx:alpine",              "min": 90,  "max": 180, "note": "pin to a specific tag"},
    ]),
    (r"^nginx:stable$", [
        {"image": "nginx:stable-alpine",       "min": 90,  "max": 180, "note": None},
    ]),
    (r"^nginx:mainline$", [
        {"image": "nginx:mainline-alpine",     "min": 90,  "max": 180, "note": None},
    ]),

    # ── Redis ───────────────────────────────────────────────────────────────
    (r"^redis:(\d+(?:\.\d+)*)$", [
        {"image": "redis:{v}-alpine",          "min": 50,  "max": 100, "note": None},
    ]),
    (r"^redis:latest$", [
        {"image": "redis:alpine",              "min": 50,  "max": 100, "note": "pin to a specific tag"},
    ]),

    # ── PostgreSQL ──────────────────────────────────────────────────────────
    (r"^postgres:(\d+(?:\.\d+)*)$", [
        {"image": "postgres:{v}-alpine",       "min": 80,  "max": 150, "note": None},
    ]),
    (r"^postgres:latest$", [
        {"image": "postgres:alpine",           "min": 80,  "max": 150, "note": "pin to a specific tag"},
    ]),

    # ── MySQL ───────────────────────────────────────────────────────────────
    (r"^mysql:(\d+\.\d+)$", [
        {"image": "mysql:{v}-debian",          "min": 20,  "max": 60,  "note": None},
    ]),

    # ── MariaDB ─────────────────────────────────────────────────────────────
    (r"^mariadb:(\d+\.\d+)$", [
        {"image": "mariadb:{v}-focal",         "min": 10,  "max": 40,  "note": None},
    ]),

    # ── PHP ─────────────────────────────────────────────────────────────────
    (r"^php:(\d+\.\d+)$", [
        {"image": "php:{v}-alpine",            "min": 150, "max": 280, "note": "musl libc compat"},
        {"image": "php:{v}-slim",              "min": 80,  "max": 180, "note": None},
    ]),
    (r"^php:(\d+\.\d+)-fpm$", [
        {"image": "php:{v}-fpm-alpine",        "min": 150, "max": 280, "note": "musl libc compat"},
    ]),
    (r"^php:(\d+\.\d+)-apache$", [
        {"image": "php:{v}-fpm-alpine + nginx:alpine", "min": 100, "max": 250,
         "note": "consider switching to Nginx+FPM"},
    ]),

    # ── Ruby ────────────────────────────────────────────────────────────────
    (r"^ruby:(\d+\.\d+(?:\.\d+)?)$", [
        {"image": "ruby:{v}-slim",             "min": 200, "max": 380, "note": None},
        {"image": "ruby:{v}-alpine",           "min": 280, "max": 450, "note": "native gem build may fail"},
    ]),
    (r"^ruby:(\d+\.\d+(?:\.\d+)?)-slim$", [
        {"image": "ruby:{v}-alpine",           "min": 50,  "max": 150, "note": "native gem build may fail"},
    ]),

    # ── .NET ────────────────────────────────────────────────────────────────
    (r"^mcr\.microsoft\.com/dotnet/sdk:(\d+\.\d+)$", [
        {"image": "mcr.microsoft.com/dotnet/runtime:{v}",      "min": 350, "max": 500,
         "note": "switch SDK to Runtime, use multi-stage"},
        {"image": "mcr.microsoft.com/dotnet/aspnet:{v}",       "min": 250, "max": 420,
         "note": "for ASP.NET apps"},
        {"image": "mcr.microsoft.com/dotnet/runtime-deps:{v}", "min": 400, "max": 550,
         "note": "for self-contained apps"},
    ]),
    (r"^mcr\.microsoft\.com/dotnet/aspnet:(\d+\.\d+)$", [
        {"image": "mcr.microsoft.com/dotnet/runtime:{v}",      "min": 50,  "max": 150,
         "note": "if ASP.NET not needed"},
    ]),

    # ── Kafka (Confluent) ────────────────────────────────────────────────────
    (r"^confluentinc/cp-kafka:(\S+)$", [
        {"image": "bitnami/kafka:{v}",         "min": 50,  "max": 200,
         "note": "Bitnami non-root based, better security"},
    ]),

    # ── CentOS (EOL) ─────────────────────────────────────────────────────────
    (r"^centos:(\d+)$", [
        {"image": "almalinux:{v}",             "min": 0,   "max": 0,
         "note": "CentOS EOL, migrate to AlmaLinux/RockyLinux"},
        {"image": "rockylinux:{v}",            "min": 0,   "max": 0,   "note": None},
    ]),
    (r"^centos:latest$", [
        {"image": "almalinux:9",               "min": 0,   "max": 0,
         "note": "CentOS EOL, migrate to AlmaLinux"},
    ]),

    # ── Amazon Linux ─────────────────────────────────────────────────────────
    (r"^amazonlinux:2$", [
        {"image": "amazonlinux:2023",          "min": 0,   "max": 50,
         "note": "AL2 EOL 2025-06-30, migrate to AL2023"},
    ]),
]

# 이미 경량 이미지로 보이거나 stage alias를 참조하는 경우는 skip.
_ALREADY_OPTIMAL = re.compile(
    r"^("
    r"scratch"
    r"|gcr\.io/distroless/"
    r"|.*-slim"
    r"|.*-alpine"
    r"|.*-minimal"
    r"|alpine:"
    r"|busybox:"
    r"|\[stage:.*\]"
    r")",
    re.IGNORECASE,
)


def check(ir: DockerfileIR) -> list[Finding]:
    # Builder stage가 크더라도 runtime stage가 가볍다면 이 rule의 목적에는
    # 부합하므로, 최종 stage의 base image만 본다.
    final = ir.final_stage
    if final is None:
        return []

    image = final.base_image

    if _ALREADY_OPTIMAL.search(image):
        return []

    for pattern, recs in _RULES:
        m = re.match(pattern, image, re.IGNORECASE)
        if not m:
            continue

        version = m.group(1) if m.lastindex else ""
        # 절감폭 최대치를 기준으로 대표 추천안을 고른다. 단순하고 일관적이지만,
        # 운영 난이도까지 반영한 "가장 무난한" 선택과는 다를 수 있다.
        best = max(recs, key=lambda r: r["max"])
        best_image = best["image"].replace("{v}", version)
        note_str = f" ({best['note']})" if best.get("note") else ""

        alternatives = [r["image"].replace("{v}", version) for r in recs[1:]]
        alt_str = ""
        if alternatives:
            alt_str = "\n  alternatives: " + ", ".join(alternatives)

        recommendation = f"→ {best_image}{note_str}{alt_str}"

        # Patch는 단순 이미지 교체가 가능한 경우에만 만든다.
        # 예: "scratch (after multi-stage)"는 안내 문구이지 즉시 치환 가능한
        # 이미지명이 아니므로 patch를 만들지 않는다.
        patch = None
        from_line_no = _find_final_from_line(ir)
        if from_line_no and "(" not in best_image and "[" not in best_image:
            old = ir.raw_lines[from_line_no - 1]
            new = old.replace(image, best_image, 1)
            patch = Patch(line_no=from_line_no, old_text=old, new_text=new)

        return [Finding(
            rule_id="BASE_IMAGE_NOT_OPTIMIZED",
            severity=Severity.HIGH,
            line_no=from_line_no,
            description=f"base image not optimized: `{image}`",
            recommendation=recommendation,
            saving_min_mb=best["min"],
            saving_max_mb=best["max"],
            patch=patch,
        )]

    return []


def _find_final_from_line(ir: DockerfileIR) -> Optional[int]:
    """return 1-based line number of the final stage FROM instruction."""
    target = len(ir.stages)
    count = 0
    for i, line in enumerate(ir.raw_lines):
        if re.match(r"^\s*FROM\s+", line, re.IGNORECASE):
            count += 1
            if count == target:
                return i + 1
    return None
