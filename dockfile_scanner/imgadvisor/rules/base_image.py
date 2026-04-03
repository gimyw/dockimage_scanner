"""
Base image 최적화 규칙.

패턴 목록을 순서대로 매칭 후 첫 번째 매칭에서 Finding 생성.
이미 slim/alpine/distroless 등 경량 이미지면 skip.
"""
from __future__ import annotations

import re
from typing import Optional

from imgadvisor.models import DockerfileIR, Finding, Patch, Severity, Stage

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

# Alpine rewrites are only safe when apt-installed packages can be translated
# with high confidence. Otherwise the rule should prefer a Debian/slim variant
# and avoid generating a Dockerfile that fails during validate/build.
_APT_TO_APK_PACKAGE_MAP: dict[str, str] = {
    "build-essential": "build-base",
    "libpq-dev": "postgresql-dev",
    "libssl-dev": "openssl-dev",
    "pkg-config": "pkgconf",
}

_APK_PASSTHROUGH_PACKAGES: set[str] = {
    "bash",
    "ca-certificates",
    "coreutils",
    "curl",
    "gcc",
    "g++",
    "git",
    "grep",
    "libffi-dev",
    "make",
    "musl-dev",
    "openssl",
    "python3-dev",
    "sed",
    "tar",
    "unzip",
    "zip",
}

def _is_no_shell_image(image_template: str) -> bool:
    """Return True if the image requires no shell (distroless or scratch)."""
    return "distroless" in image_template or image_template.startswith("scratch")


def _is_alpine_image(image_template: str) -> bool:
    """Return True if the image template targets Alpine Linux."""
    return "-alpine" in image_template or image_template.startswith("alpine:")


def _filter_recs_by_shell(recs: list[dict], shell_status: str) -> list[dict]:
    """
    Filter recommendation candidates based on shell requirement.

    - no_shell:    all options OK (exec-form only → distroless is safe)
    - needs_shell: exclude distroless/scratch (they have no /bin/sh)
    - unknown:     exclude distroless/scratch as a safe default
    """
    if shell_status == "no_shell":
        return recs
    filtered = [r for r in recs if not _is_no_shell_image(r["image"])]
    return filtered if filtered else recs  # fallback if every candidate was excluded


def _extract_apt_packages(run_text: str) -> Optional[list[str]]:
    """
    Extract package names from a straightforward apt install command.

    Supported shape:
      apt-get update && apt-get install -y <packages...>

    More complex shell logic deliberately returns None so the caller can avoid
    unsafe Alpine rewrites.
    """
    normalized = re.sub(r"\s+", " ", run_text.strip())
    match = re.search(r"(?:apt-get|apt)\s+install\s+(.+)", normalized, re.IGNORECASE)
    if not match:
        return None

    tail = re.split(r"\s*(?:&&|;|\|\|)\s*", match.group(1), maxsplit=1)[0].strip()
    if not tail:
        return None

    packages = [token for token in tail.split() if not token.startswith("-")]
    return packages or None


def _can_translate_apt_packages_to_alpine(packages: list[str]) -> bool:
    """Return True if every package has a confident Alpine equivalent."""
    for package in packages:
        if package in _APT_TO_APK_PACKAGE_MAP:
            continue
        if package in _APK_PASSTHROUGH_PACKAGES:
            continue
        return False
    return True


def _filter_recs_by_pkg_manager(stage: Stage, recs: list[dict]) -> tuple[list[dict], str]:
    """
    Remove Alpine candidates when the final stage uses apt packages that cannot
    be translated safely.
    """
    apt_packages: list[str] = []
    for instr in stage.run_instructions:
        if not re.search(r"\b(?:apt-get|apt)\b", instr.arguments, re.IGNORECASE):
            continue

        packages = _extract_apt_packages(instr.arguments)
        if not packages:
            filtered = [r for r in recs if not _is_alpine_image(r["image"])]
            return (filtered if filtered else recs), "complex apt command detected"
        apt_packages.extend(packages)

    if not apt_packages:
        return recs, ""

    if _can_translate_apt_packages_to_alpine(apt_packages):
        return recs, "apt packages can be translated to alpine"

    filtered = [r for r in recs if not _is_alpine_image(r["image"])]
    return (filtered if filtered else recs), "apt packages are not safely translatable to alpine"


def _detect_shell_requirement(stage: Stage) -> tuple[str, str]:
    """
    Inspect the stage's instructions to determine if a shell is needed at runtime.

    Detection signals (in priority order):
      needs_shell:
        - SHELL directive present
        - CMD or ENTRYPOINT in shell form (argument does not start with '[')
        - COPY *.sh  (shell scripts copied into the image)
      no_shell:
        - exec-form ENTRYPOINT ["binary", ...] and none of the above signals
      unknown:
        - no CMD / ENTRYPOINT found → default to slim (safe choice)

    Returns:
        (status, signal_description)
        status: "needs_shell" | "no_shell" | "unknown"
    """
    has_exec_form_entrypoint = False

    for instr in stage.instructions:
        if instr.instruction == "SHELL":
            return "needs_shell", "SHELL directive found"

        if instr.instruction in ("CMD", "ENTRYPOINT"):
            args = instr.arguments.strip()
            if not args.startswith("["):
                # Shell form: e.g.  CMD npm start  or  ENTRYPOINT /start.sh
                return "needs_shell", f"shell-form {instr.instruction} detected"
            # Exec form: ["binary", "arg", ...]
            if instr.instruction == "ENTRYPOINT":
                has_exec_form_entrypoint = True

        if instr.instruction == "COPY":
            # Shell scripts copied into the image → /bin/sh will be needed
            if re.search(r"\.sh\b", instr.arguments):
                return "needs_shell", "COPY *.sh detected"

    if has_exec_form_entrypoint:
        return "no_shell", "exec-form ENTRYPOINT only → distroless safe"

    return "unknown", "no CMD/ENTRYPOINT found"


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

        # Detect whether the Dockerfile needs a shell at runtime, then filter
        # candidates accordingly before picking the one with maximum savings.
        shell_status, shell_signal = _detect_shell_requirement(final)
        filtered_recs = _filter_recs_by_shell(recs, shell_status)
        filtered_recs, pkg_signal = _filter_recs_by_pkg_manager(final, filtered_recs)

        best = max(filtered_recs, key=lambda r: r["max"])
        best_image = best["image"].replace("{v}", version)
        note_str = f" ({best['note']})" if best.get("note") else ""

        # Show alternatives from the same filtered pool (excluded no-shell images
        # are intentionally hidden when shell is required)
        alternatives = [
            r["image"].replace("{v}", version)
            for r in filtered_recs
            if r is not best
        ]
        alt_str = ""
        if alternatives:
            alt_str = "\n  alternatives: " + ", ".join(alternatives)

        # Only surface the signal when it actually changed the recommendation
        signal_str = ""
        if shell_status != "unknown":
            signal_str = f"\n  signal: {shell_signal}"
        if pkg_signal:
            signal_str += f"\n  package-manager: {pkg_signal}"

        recommendation = f"→ {best_image}{note_str}{signal_str}{alt_str}"

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
