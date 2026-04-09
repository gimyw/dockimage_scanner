# imgadvisor CLI 전체 구조 정리

## 개요

`imgadvisor`는 Dockerfile을 빌드하기 전에 정적으로 분석해서 이미지 비대 원인을 찾고, 최적화된 Dockerfile을 자동으로 생성해주는 CLI 도구입니다.

```
pip install git+https://github.com/0206pdh/dockimage_scanner.git@main#subdirectory=dockfile_scanner
```

---

## 명령어 구조

```
imgadvisor
├── analyze    # Dockerfile 정적 분석 (Docker 불필요)
├── recommend  # 최적화 Dockerfile 생성 (Docker 불필요)
├── validate   # 원본 vs 최적화 이미지 실제 빌드 비교 (Docker 필요)
└── layers     # 레이어별 크기 분석 (Docker 필요)
```

### analyze

```bash
imgadvisor analyze -f Dockerfile
imgadvisor analyze -f Dockerfile --json
```

- Dockerfile을 파싱해서 모든 규칙을 실행
- 문제가 발견되면 exit code 1 반환 (CI 파이프라인 연동 가능)
- `--json` 옵션으로 JSON 출력 가능

### recommend

```bash
imgadvisor recommend -f Dockerfile
imgadvisor recommend -f Dockerfile -o optimized.Dockerfile
```

- `analyze`와 동일하게 파싱 + 분석 후 최적화 Dockerfile 생성
- `-o` 없으면 stdout으로 출력
- Python 단일 스테이지 Dockerfile은 실제 multi-stage 본문으로 재구성

### validate

```bash
imgadvisor validate -f Dockerfile --optimized optimized.Dockerfile
```

- 두 Dockerfile을 실제로 빌드해서 이미지 크기, 레이어 수, 빌드 시간 비교
- UUID 기반 임시 태그 사용, 완료 후 자동 삭제

### layers

```bash
imgadvisor layers -f Dockerfile
```

- Dockerfile을 빌드하고 `docker history`로 레이어별 크기 분석
- 50MB 이상 레이어는 `[!]`로 표시

---

## 데이터 흐름

```
Dockerfile 파일
      │
      ▼
  parser.py          → DockerfileIR (파싱된 중간 표현)
      │
      ▼
  analyzer.py        → Finding[] (탐지된 문제 목록)
      │
      ├──▶ display.py       → 터미널 출력 (Rich 기반)
      │
      ├──▶ recommender.py   → 최적화 Dockerfile 문자열
      │
      ├──▶ validator.py     → ValidationResult (실제 빌드 비교)
      │
      └──▶ layer_analyzer.py → LayerAnalysis (레이어 분석)
```

---

## 핵심 모듈 설명

### `models.py` — 공통 데이터 모델

| 클래스 | 역할 |
|---|---|
| `DockerInstruction` | Dockerfile 명령어 하나 (줄 번호, instruction, arguments) |
| `Stage` | FROM 블록 하나 (base_image, alias, instructions 목록) |
| `DockerfileIR` | Dockerfile 전체 중간 표현 (stages, raw_lines, path) |
| `Finding` | 탐지된 문제 하나 (rule_id, severity, description, recommendation, patch) |
| `Patch` | 줄 직접 교체 패치 (line_no, old_text, new_text) |
| `ValidationResult` | 빌드 비교 결과 (크기, 레이어 수, 빌드 시간) |
| `Severity` | HIGH / MEDIUM / LOW |

### `parser.py` — Dockerfile 파싱

1. 백슬래시 줄 이어쓰기 합치기
2. FROM 이전 전역 ARG 기본값 수집
3. `$VAR` / `${VAR}` 변수 치환
4. FROM 블록 단위로 Stage 생성
5. 마지막 Stage에 `is_final=True` 설정

### `analyzer.py` — 규칙 실행

등록된 모든 규칙을 순서대로 실행하고 Finding 목록을 합쳐서 반환합니다.

```python
_ALL_RULES = [
    base_image.check,
    build_tools.check,
    cache_cleanup.check,
    python_runtime.check,
    copy_scope.check,
    multi_stage.check,
]
```

### `recommender.py` — 최적화 Dockerfile 생성

1. `SINGLE_STAGE_BUILD` Finding이 있으면 → multi-stage 본문 직접 생성
2. `Patch`가 있는 Finding → 해당 줄 직접 교체 (내림차순으로 처리해 오프셋 드리프트 방지)
3. `Patch` 없는 Finding → `# [imgadvisor:FAIL]` 인라인 주석 삽입
4. Alpine으로 base image가 바뀌는 경우 → apt 명령을 apk로 자동 변환

### `validator.py` — 실제 빌드 비교

- UUID 기반 임시 태그로 두 이미지 빌드
- `docker image inspect`로 크기와 레이어 수 조회
- `finally` 블록에서 임시 이미지 항상 삭제

### `layer_analyzer.py` — 레이어 분석

- `docker history --no-trunc`로 레이어별 크기 파싱
- BuildKit 형식과 Legacy 형식 모두 지원
- SI 단위 파싱 (kB=1000, MB=1000000)

### `display.py` — 터미널 출력

Rich 라이브러리 기반으로 컬러 출력을 담당합니다.

| 함수 | 용도 |
|---|---|
| `print_analysis` | analyze 결과 출력 |
| `print_recommend_summary` | recommend 요약 한 줄 출력 |
| `print_recommended_dockerfile` | 최적화 Dockerfile 출력 (Syntax highlighting) |
| `print_validation` | 빌드 비교 테이블 출력 |
| `print_layers` | 레이어 분석 결과 출력 |
| `print_json_result` | JSON 형식 출력 |

---

## 탐지 규칙 목록

### `rules/base_image.py` — BASE_IMAGE_NOT_OPTIMIZED

- severity: **HIGH**
- final stage의 base image가 slim/alpine/distroless 없는 풀 이미지인 경우
- 지원 이미지: python, node, golang, rust, openjdk, ubuntu, debian, nginx, redis, postgres, mysql, php, ruby, .NET 등 30개 이상
- shell 필요 여부와 apt 패키지 호환성을 고려해서 추천 후보를 필터링
- 단순 치환 가능한 경우 `Patch` 자동 생성

### `rules/build_tools.py` — BUILD_TOOLS_IN_FINAL_STAGE

- severity: **HIGH**
- final stage의 RUN 명령에 빌드 전용 도구가 남아있는 경우
- 탐지 대상: `gcc`, `g++`, `make`, `cmake`, `build-essential`, `libpq-dev`, `libssl-dev`, `libffi-dev`, `cargo`, `rustc`, `maven`, `gradle` 등

### `rules/cache_cleanup.py` — 캐시 미정리

- severity: **MEDIUM**
- 설치 명령이 있는데 같은 RUN 블록 안에서 캐시를 정리하지 않은 경우

| rule_id | 패키지 매니저 |
|---|---|
| APT_CACHE_NOT_CLEANED | apt-get / apt |
| PIP_CACHE_NOT_DISABLED | pip / pip3 |
| APK_CACHE_NOT_DISABLED | apk |
| NPM_CACHE_NOT_CLEANED | npm |
| YARN_CACHE_NOT_CLEANED | yarn |
| PNPM_CACHE_NOT_CLEANED | pnpm |
| YUM_CACHE_NOT_CLEANED | yum |
| DNF_CACHE_NOT_CLEANED | dnf |
| GEM_CACHE_NOT_CLEANED | gem / bundle |
| COMPOSER_CACHE_NOT_CLEANED | composer |
| MAVEN_CACHE_IN_FINAL_STAGE | mvn |
| GRADLE_CACHE_IN_FINAL_STAGE | gradle |

### `rules/copy_scope.py` — BROAD_COPY_SCOPE

- severity: **HIGH** (`.dockerignore` 없을 때) / **MEDIUM** (있을 때)
- `COPY . .` 패턴이 있고 `.dockerignore`가 없는 경우
- `--from=` 이 있는 multi-stage COPY는 제외

### `rules/multi_stage.py` — SINGLE_STAGE_BUILD

- severity: **HIGH**
- Python 전용 규칙
- 단일 스테이지인데 빌드 도구, apt install, pip install, broad COPY 흔적이 있는 경우
- recommendation에 실제 실행 가능한 multi-stage Dockerfile 본문을 생성

**multi-stage 생성 전략:**
1. `requirements*.txt` / `constraints*.txt` 파일이 있으면 → manifest-first 전략
2. `pyproject.toml` + `poetry.lock` 이 있으면 → poetry 전략
3. 둘 다 없으면 → inline fallback 전략

**생성 구조:**
```dockerfile
# -- builder stage --
FROM python:3.11 AS builder
WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN python -m venv $VIRTUAL_ENV
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# -- runtime stage --
FROM python:3.11-slim
WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
CMD [...]
```

### `rules/python_runtime.py` — Python 런타임 규칙

| rule_id | severity | 탐지 조건 |
|---|---|---|
| PYTHON_RUNTIME_ENVS_MISSING | MEDIUM | `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE` 등 권장 ENV 누락 |
| PYTHON_RUNTIME_ENVS_CONFLICT | MEDIUM | 권장 ENV가 있지만 값이 다른 경우 |
| PYTHON_DEV_SERVER_IN_RUNTIME | HIGH | CMD/ENTRYPOINT에 `flask run` 패턴 |
| PYTHON_ASGI_WORKERS_NOT_SET | MEDIUM | `uvicorn` 사용 시 `--workers` 없는 경우 |

**자동 추가되는 Python 권장 ENV:**

| 변수 | 값 | 효과 |
|---|---|---|
| PYTHONUNBUFFERED | 1 | stdout/stderr 버퍼링 비활성화 |
| PYTHONDONTWRITEBYTECODE | 1 | `.pyc` 파일 생성 안 함 |
| PIP_NO_CACHE_DIR | 1 | pip 캐시 전역 비활성화 |
| PIP_DISABLE_PIP_VERSION_CHECK | 1 | pip 버전 체크 요청 제거 |

**flask run 자동 교체 조건:**
- `gunicorn`이 requirements 파일 또는 pip install 명령에 있어야 함
- Flask 앱 객체(`app = Flask(...)`) 또는 factory(`create_app()`)가 소스에서 확인되어야 함
- 조건 미충족 시 경고만 표시하고 자동 교체 안 함

---

## 프로젝트 파일 구조

```
dockfile_scanner/
├── pyproject.toml          # 패키지 설정 (v0.4.1)
├── requirements.txt        # 의존성 (typer, rich)
├── install.sh              # 자동 설치 스크립트
├── imgadvisor/
│   ├── main.py             # CLI 진입점 (Typer 앱)
│   ├── models.py           # 공통 데이터 모델
│   ├── parser.py           # Dockerfile 파서
│   ├── analyzer.py         # 규칙 실행기
│   ├── recommender.py      # 최적화 Dockerfile 생성기
│   ├── validator.py        # 실제 빌드 비교
│   ├── layer_analyzer.py   # 레이어 분석
│   ├── display.py          # 터미널 출력 (Rich)
│   └── rules/
│       ├── base_image.py   # BASE_IMAGE_NOT_OPTIMIZED
│       ├── build_tools.py  # BUILD_TOOLS_IN_FINAL_STAGE
│       ├── cache_cleanup.py # 캐시 미정리 규칙 12종
│       ├── copy_scope.py   # BROAD_COPY_SCOPE
│       ├── multi_stage.py  # SINGLE_STAGE_BUILD (Python 전용)
│       └── python_runtime.py # Python 런타임 규칙 4종
└── tests/
    └── fixtures/           # 테스트용 Dockerfile 샘플
```

---

## 의존성

```toml
requires-python = ">=3.11"
dependencies = [
    "typer>=0.12.0",   # CLI 프레임워크
    "rich>=13.7.0",    # 터미널 컬러 출력
]
```

`validate`, `layers` 명령은 Docker daemon이 실행 중이어야 합니다.
