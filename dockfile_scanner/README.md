# imgadvisor

> **Dockerfile pre-build 정적 분석 및 이미지 경량화 어드바이저**

빌드 전에 Dockerfile을 읽고 이미지 비대 요인을 예측합니다.  
단순 측정기가 아니라, **왜 커지는지 → 어디가 문제인지 → 어떻게 바꿀지 → 얼마나 줄어드는지**를 한 번에 보여주는 도구입니다.

---

## 핵심 가치

```
[Before]                    [Recommendation]              [After]
Dockerfile 분석             왜 큰지 설명                  최적화 Dockerfile 제안
예상 이미지 크기            어떤 instruction이 원인인지   실제 빌드 결과 비교
                            어떻게 바꾸는지               크기/레이어 감소 확인
                            얼마나 줄어드는지
```

---

## 출력 예시

```
  imgadvisor  —  Pre-Build Analyzer

  Dockerfile   :  ./Dockerfile
  Stages       :  1  (single-stage ⚠)
  Final image  :  python:3.11
  .dockerignore:  없음 ✗

  ──────────────────────────────────────────────────

  [FAIL] BASE_IMAGE_NOT_OPTIMIZED  line 1
  ┌──────────────────────────────────────────────────────────┐
  │  베이스 이미지 최적화 필요: `python:3.11`                │
  │  → python:3.11-slim                                      │
  │    다른 후보: python:3.11-alpine, gcr.io/distroless/...  │
  │                                                          │
  │  예상 절감: 250 ~ 420 MB                                 │
  └──────────────────────────────────────────────────────────┘

  [FAIL] BUILD_TOOLS_IN_FINAL_STAGE  line 8
  [WARN] APT_CACHE_NOT_CLEANED       line 8
  [WARN] PIP_CACHE_NOT_DISABLED      line 12
  [FAIL] BROAD_COPY_SCOPE            line 15
  [FAIL] SINGLE_STAGE_BUILD          line 1

  ──────────────────────────────────────────────────

  이슈      2 FAIL  3 WARN
  예상 절감  530 ~ 1,240 MB

  최적화 Dockerfile 생성: imgadvisor recommend --dockerfile <path>
```

---

## 설치

### curl (권장)

```bash
curl -fsSL https://raw.githubusercontent.com/0206pdh/dockimage_scanner/main/install.sh | bash
```

### pip

```bash
pip install git+https://github.com/0206pdh/dockimage_scanner.git
```

### 소스에서 직접

```bash
git clone https://github.com/0206pdh/dockimage_scanner.git
cd dockimage_scanner
pip install -e .
```

**요구사항:** Python 3.11+

---

## 사용법

### 1. `analyze` — 정적 분석

```bash
imgadvisor analyze --dockerfile Dockerfile
imgadvisor analyze -f ./docker/Dockerfile

# JSON 출력 (CI 파이프라인 연동용)
imgadvisor analyze -f Dockerfile --json
```

이슈가 발견되면 exit code 1 반환 → CI gate로 활용 가능.

### 2. `recommend` — 최적화 Dockerfile 생성

```bash
# 최적화 파일로 저장
imgadvisor recommend -f Dockerfile -o optimized.Dockerfile

# stdout 출력
imgadvisor recommend -f Dockerfile
```

자동 적용 항목:
- 베이스 이미지 → slim 대체 (패치 가능한 경우)
- 문제 instruction 위에 `# [imgadvisor:FAIL]` 주석 삽입
- Multi-stage 전환 템플릿 (Go/Rust/Java/Node/일반) 파일 하단 첨부

### 3. `validate` — 실제 빌드 비교

```bash
imgadvisor validate \
  --dockerfile Dockerfile \
  --optimized  optimized.Dockerfile
```

Docker 데몬이 실행 중이어야 합니다.  
원본/최적화 이미지를 각각 빌드 후 크기·레이어 수 비교를 출력합니다.

### 4. `scan` — Trivy pre-build security scan

```bash
imgadvisor scan -f Dockerfile
imgadvisor scan -f Dockerfile --severity HIGH,CRITICAL
imgadvisor scan -f Dockerfile --ignore-unfixed --json
```

This command does not build an image. It combines:
- `trivy config` for Dockerfile misconfiguration checks
- `trivy fs` for dependency vulnerability checks in the build context

Notes:
- `trivy` must be installed locally and available on `PATH`
- this is a pre-build scan, so final image OS packages are not covered yet

---

## 탐지 규칙

### BASE_IMAGE_NOT_OPTIMIZED `FAIL`

slim/alpine/distroless 등 경량 이미지로 교체 가능한 베이스 이미지를 감지합니다.

지원 이미지:

| 카테고리 | 감지 패턴 | 주요 추천 후보 |
|----------|-----------|----------------|
| Python | `python:3.x`, `python:3.x.y` | `python:3.x-slim`, `python:3.x-alpine`, `gcr.io/distroless/python3` |
| Node | `node:20`, `node:lts`, `node:current` | `node:20-slim`, `node:20-alpine`, `gcr.io/distroless/nodejs20` |
| Java | `openjdk:17`, `eclipse-temurin:17-jdk` | `eclipse-temurin:17-jre`, `gcr.io/distroless/java17-debian12` |
| Go | `golang:1.22` | `scratch` (multi-stage 후), `gcr.io/distroless/static-debian12` |
| Rust | `rust:1.77` | `scratch` (multi-stage 후), `debian:bookworm-slim` |
| Ubuntu | `ubuntu:22.04`, `ubuntu:jammy` | `ubuntu:22.04-minimal`, `debian:bookworm-slim` |
| Debian | `debian:bookworm`, `debian:bullseye` | `debian:bookworm-slim` |
| Nginx | `nginx:1.25`, `nginx:latest` | `nginx:1.25-alpine`, `nginx:alpine-slim` |
| Redis | `redis:7`, `redis:7.2.1` | `redis:7-alpine` |
| PostgreSQL | `postgres:16`, `postgres:16.1` | `postgres:16-alpine` |
| PHP | `php:8.2`, `php:8.2-fpm` | `php:8.2-alpine`, `php:8.2-fpm-alpine` |
| Ruby | `ruby:3.3` | `ruby:3.3-slim`, `ruby:3.3-alpine` |
| .NET | `mcr.microsoft.com/dotnet/sdk:8.0` | `mcr.microsoft.com/dotnet/runtime:8.0`, `aspnet:8.0` |
| MySQL | `mysql:8.0` | `mysql:8.0-debian` |
| MariaDB | `mariadb:11.0` | `mariadb:11.0-focal` |
| Kafka | `confluentinc/cp-kafka:7.x` | `bitnami/kafka:7.x` |
| CentOS | `centos:7`, `centos:8` | `almalinux:8`, `rockylinux:8` (EOL 경고) |
| Amazon Linux | `amazonlinux:2` | `amazonlinux:2023` (AL2 EOL 경고) |

이미 경량화된 이미지(`-slim`, `-alpine`, `distroless`, `scratch`, `busybox`)는 탐지에서 제외됩니다.

---

### BUILD_TOOLS_IN_FINAL_STAGE `FAIL`

final stage의 RUN 명령에서 런타임에 불필요한 빌드 도구를 감지합니다.

감지 대상:

| 카테고리 | 패키지 |
|----------|--------|
| C/C++ | `gcc`, `g++`, `clang`, `make`, `cmake`, `build-essential`, `binutils` |
| 빌드 시스템 | `automake`, `autoconf`, `libtool`, `pkg-config` |
| Java | `maven`, `gradle`, `ant` |
| Rust | `cargo`, `rustc` |
| 개발 헤더 | `python3-dev`, `libpq-dev`, `libssl-dev`, `libffi-dev`, `libblas-dev` |

권장 대응: Multi-stage build로 builder/runtime 분리  
예상 절감: **100 ~ 400 MB**

---

### APT_CACHE_NOT_CLEANED `WARN`

`apt-get install` / `apt install` 후 `/var/lib/apt/lists/*` 정리 없음을 감지합니다.

```dockerfile
# Before
RUN apt-get update && apt-get install -y libpq-dev

# After
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*
```

예상 절감: **30 ~ 120 MB**

---

### PIP_CACHE_NOT_DISABLED `WARN`

`pip install` / `pip3 install`에 `--no-cache-dir` 플래그 없음을 감지합니다.

```dockerfile
# Before
RUN pip install fastapi uvicorn

# After
RUN pip install --no-cache-dir fastapi uvicorn
```

예상 절감: **20 ~ 80 MB**

---

### APK_CACHE_NOT_DISABLED `WARN`

`apk add`에 `--no-cache` 플래그 없음을 감지합니다.

```dockerfile
# Before
RUN apk add curl bash

# After
RUN apk add --no-cache curl bash
```

예상 절감: **10 ~ 40 MB**

---

### NPM / YARN / PNPM 캐시 미정리 `WARN`

패키지 설치 후 캐시 정리 또는 dev dependency 제외 없음을 감지합니다.

지원: `npm`, `yarn`, `pnpm`, `gem`, `composer`, `mvn`, `gradle`

예상 절감: **20 ~ 200 MB**

---

### BROAD_COPY_SCOPE `FAIL` / `WARN`

`COPY . .` 또는 `COPY . /app` 패턴으로 컨텍스트 전체를 복사하는 경우를 감지합니다.

- `.dockerignore` **없음** → `FAIL` (90 ~ 300 MB)
- `.dockerignore` **있음** → `WARN` (50 ~ 200 MB)

권장 대응:
```dockerfile
# .dockerignore 생성 + 명시적 COPY
COPY src/ /app/src/
COPY pyproject.toml /app/
COPY requirements.txt /app/
```

---

### SINGLE_STAGE_BUILD `FAIL`

빌드 전용 이미지(Go, Rust, Java SDK 등)를 single-stage로 사용하는 경우를 감지합니다.  
언어별 multi-stage 템플릿을 자동 생성합니다.

| 언어 | builder | runtime |
|------|---------|---------|
| Go | `golang:alpine` | `scratch` |
| Rust | `rust:slim` | `debian:bookworm-slim` |
| Java | `eclipse-temurin:jdk` | `eclipse-temurin:jre` |
| .NET | `dotnet/sdk` | `dotnet/aspnet` |
| Node | `node:alpine` (dev) | `node:alpine` (prod only) |

예상 절감: **150 ~ 600 MB**

---

## CI 연동 예시

```yaml
# GitHub Actions
- name: Dockerfile analysis
  run: |
    pip install -e .
    imgadvisor analyze --dockerfile Dockerfile --json | tee result.json
  # FAIL 시 파이프라인 중단 (exit code 1)
```

---

## 프로젝트 구조

```
dockfile_scanner/
├── imgadvisor/
│   ├── main.py           # CLI 진입점 (typer)
│   ├── parser.py         # Dockerfile 파서 → DockerfileIR
│   ├── analyzer.py       # 규칙 실행기
│   ├── recommender.py    # 최적화 Dockerfile 생성
│   ├── validator.py      # 실제 빌드 비교 (Docker 데몬 필요)
│   ├── display.py        # Rich 터미널 출력
│   ├── models.py         # 공유 데이터 모델
│   └── rules/
│       ├── base_image.py     # 베이스 이미지 규칙 (30+ 패턴)
│       ├── build_tools.py    # 빌드 도구 탐지
│       ├── cache_cleanup.py  # 캐시 정리 탐지 (9개 패키지 매니저)
│       ├── copy_scope.py     # COPY 범위 탐지
│       └── multi_stage.py    # Multi-stage 전환 권장 + 언어별 템플릿
├── tests/
│   └── fixtures/
│       ├── Dockerfile.python_bad       # 나쁜 예시 (5개 이슈)
│       ├── Dockerfile.node_bad         # 나쁜 예시 (3개 이슈)
│       ├── Dockerfile.go_bad           # Go single-stage 예시
│       └── Dockerfile.multistage_good  # 올바른 예시 (이슈 없음)
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

## 향후 개선 계획

- [ ] confidence score (절감량 추정 신뢰도 표시)
- [ ] `--fail-on-severity HIGH` 옵션
- [ ] 언어별 특화 규칙 확장
  - Python: `requirements.txt` 레이어 캐시 최적화 (COPY 순서)
  - Node: `package-lock.json` 레이어 캐시 전략
  - Java: fat jar vs thin jar 감지
- [ ] PR comment 자동 생성 (GitHub Actions output 형식)
- [ ] security surface score (빌드 도구 제거 시 CVE 감소 예상치)
