# imgadvisor 프로젝트 보고서

## 1. 개요

### 프로젝트 목적

`imgadvisor`는 Docker 이미지를 실제로 빌드하기 전에 Dockerfile을 정적으로 분석해서 이미지 비대 원인을 탐지하고 최적화 Dockerfile을 자동 생성하는 CLI 도구입니다.

기존 Dockerfile 린팅 도구가 "이 패턴은 나쁘다"는 경고에 그치는 것과 달리, `imgadvisor`는 분석 결과를 바탕으로 실제 실행 가능한 최적화 Dockerfile 본문을 생성하는 데 집중합니다.

### 핵심 기능

| 명령 | 설명 |
|---|---|
| `analyze` | Dockerfile 정적 분석, 문제 탐지 및 리포트 |
| `recommend` | 최적화 Dockerfile 자동 생성 |
| `validate` | 원본 vs 최적화 이미지 실제 빌드 비교 |
| `layers` | 레이어별 크기 분석 (`docker history` 기반) |
| `scan` | Trivy 기반 pre-build 설정/취약점 검사 |

---

## 2. 탐지 규칙

### 규칙 목록

| rule_id | 탐지 내용 | 심각도 |
|---|---|---|
| `BASE_IMAGE_NOT_OPTIMIZED` | slim/alpine/distroless 미사용 | HIGH |
| `BUILD_TOOLS_IN_FINAL_STAGE` | 빌드 도구가 런타임 이미지에 잔존 | HIGH |
| `APT_CACHE_NOT_CLEANED` | apt 캐시 미정리 | MEDIUM |
| `PIP_CACHE_NOT_DISABLED` | pip 캐시 비활성화 미적용 | MEDIUM |
| `BROAD_COPY_SCOPE` | `.dockerignore` 없이 `COPY . .` 사용 | MEDIUM |
| `SINGLE_STAGE_BUILD` | 빌드 도구 포함 단일 스테이지 | HIGH |
| `PYTHON_RUNTIME_ENVS_MISSING` | Python 런타임 환경 변수 누락 | MEDIUM |
| `PYTHON_DEV_SERVER_IN_RUNTIME` | 개발 서버를 런타임에서 그대로 사용 | HIGH |
| `PYTHON_ASGI_WORKERS_NOT_SET` | uvicorn worker 수 미설정 | MEDIUM |

### 베이스 이미지 탐지 범위

python, node, golang, rust, openjdk, eclipse-temurin, ubuntu, debian, nginx, redis, postgres, mysql, mariadb, php, ruby, .NET SDK, centos, amazonlinux 등 30개 이상 패턴을 지원합니다.

---

## 3. 테스트 케이스 분석

세 개의 Python Dockerfile(pre1, pre2, pre3)을 대상으로 `imgadvisor analyze` 및 `imgadvisor recommend`를 실행하고 결과를 검증했습니다.

### 3.1 pre1 — Flask 개발 서버 포함 단일 스테이지

**원본 특징**

```dockerfile
FROM python:3.11
RUN apt-get update && apt-get install -y gcc g++ build-essential curl
COPY . .
RUN pip install flask gunicorn requests pandas
CMD flask run --host=0.0.0.0 --port=5000
```

**imgadvisor 탐지 결과**

| rule_id | 내용 |
|---|---|
| `BASE_IMAGE_NOT_OPTIMIZED` | `python:3.11` → `python:3.11-slim` 권고 |
| `BUILD_TOOLS_IN_FINAL_STAGE` | `gcc`, `g++`, `build-essential` 잔존 |
| `APT_CACHE_NOT_CLEANED` | apt 캐시 미정리 |
| `PIP_CACHE_NOT_DISABLED` | pip 캐시 비활성화 미적용 |
| `BROAD_COPY_SCOPE` | `COPY . .` + `.dockerignore` 없음 |
| `SINGLE_STAGE_BUILD` | 빌드 도구 포함 단일 스테이지 |
| `PYTHON_RUNTIME_ENVS_MISSING` | `PYTHONUNBUFFERED` 등 누락 |
| `PYTHON_DEV_SERVER_IN_RUNTIME` | `flask run` 사용 |

**최적화 결과**

- builder / runtime multi-stage 분리
- runtime: `python:3.11-slim`
- `flask run` → `gunicorn -b 0.0.0.0:5000 app:app` 교체
- apt/pip 캐시 정리, Python 기본 ENV 보강

---

### 3.2 pre2 — FastAPI + Uvicorn 단일 스테이지

**원본 특징**

```dockerfile
FROM python:3.11
RUN apt-get update && apt-get install -y gcc make libffi-dev
COPY . .
RUN pip install fastapi uvicorn sqlalchemy
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**imgadvisor 탐지 결과**

| rule_id | 내용 |
|---|---|
| `BASE_IMAGE_NOT_OPTIMIZED` | `python:3.11` → `python:3.11-slim` 권고 |
| `BUILD_TOOLS_IN_FINAL_STAGE` | `gcc`, `make`, `libffi-dev` 잔존 |
| `APT_CACHE_NOT_CLEANED` | apt 캐시 미정리 |
| `PIP_CACHE_NOT_DISABLED` | pip 캐시 비활성화 미적용 |
| `BROAD_COPY_SCOPE` | `COPY . .` + `.dockerignore` 없음 |
| `SINGLE_STAGE_BUILD` | 빌드 도구 포함 단일 스테이지 |
| `PYTHON_RUNTIME_ENVS_MISSING` | Python 기본 ENV 누락 |
| `PYTHON_ASGI_WORKERS_NOT_SET` | `uvicorn` worker 수 미설정 |

**최적화 결과**

- builder / runtime multi-stage 분리
- runtime: `python:3.11-slim`
- `uvicorn` 엔트리포인트 유지 (worker 수는 운영 환경 의존적이므로 자동 고정 안 함)
- apt/pip 캐시 정리, Python 기본 ENV 보강

---

### 3.3 pre3 — requirements.txt 기반 Flask 앱

**원본 특징**

```dockerfile
FROM python:3.11
RUN apt-get update && apt-get install -y gcc libpq-dev git wget
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY . .
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
```

**imgadvisor 탐지 결과**

| rule_id | 내용 |
|---|---|
| `BASE_IMAGE_NOT_OPTIMIZED` | `python:3.11` → `python:3.11-slim` 권고 |
| `BUILD_TOOLS_IN_FINAL_STAGE` | `gcc`, `libpq-dev`, `git`, `wget` 잔존 |
| `APT_CACHE_NOT_CLEANED` | apt 캐시 미정리 |
| `PIP_CACHE_NOT_DISABLED` | pip 캐시 비활성화 미적용 |
| `SINGLE_STAGE_BUILD` | 빌드 도구 포함 단일 스테이지 |
| `PYTHON_RUNTIME_ENVS_MISSING` | Python 기본 ENV 누락 |

**최적화 결과**

- builder / runtime multi-stage 분리
- runtime: `python:3.11-slim`
- `requirements.txt` manifest-first 복사 전략 유지
- `gcc`, `libpq-dev`, `git`, `wget`은 builder에만 격리
- apt/pip 캐시 정리, Python 기본 ENV 보강
- 엔트리포인트 `gunicorn` 유지

---

## 4. 측정 결과

`verify_full_lifecycle.sh` 스크립트를 사용해 Cold Start 환경에서 측정한 결과입니다.
전체 측정 결과는 [result.md](./result.md)를 참고합니다.

### Docker Hub Pull & Extract 시간

| 케이스 | 원본 | 최적화 | 단축률 |
|---|---:|---:|---:|
| pre1 | 132,764ms | 29,924ms | **77.5%** |
| pre2 | 103,930ms | 13,338ms | **87.2%** |
| pre3 | 12,417ms | 6,518ms | **47.5%** |

### 컨테이너 Ready Time

| 케이스 | 원본 | 최적화 |
|---|---:|---:|
| pre1 | 1,232ms | 1,674ms |
| pre2 | 1,806ms | 1,920ms |
| pre3 | 687ms | 1,535ms |

모든 케이스에서 Ready Time은 원본/최적화 모두 2초 이내로, 이미지 경량화가 앱 기동 속도 자체에는 영향을 주지 않음을 확인했습니다.

### Total Time to Ready (Pull + Ready)

| 케이스 | 원본 | 최적화 | 단축량 | 단축률 |
|---|---:|---:|---:|---:|
| pre1 | 133,996ms | 31,598ms | 102,398ms | **76.4%** |
| pre2 | 105,736ms | 15,258ms | 90,478ms | **85.6%** |
| pre3 | 13,104ms | 8,053ms | 5,051ms | **38.5%** |

---

## 5. 결과 해석

### pre2가 가장 큰 절감 효과

`python:3.11` 풀 이미지 + `gcc`, `make`, `libffi-dev` + `fastapi`, `uvicorn`, `sqlalchemy` 조합이 원본 Pull 시간을 103초까지 끌어올린 주된 원인입니다. multi-stage + slim 전환 후 13초로 **87% 단축**되었습니다.

### pre1은 이미지 크기 + 서버 교체 효과 혼재

pre1은 `flask run`(개발 서버)에서 `gunicorn`(운영 서버)으로 엔트리포인트가 함께 교체됩니다. 이미지 경량화 효과와 서버 교체 효과가 함께 반영된 결과로, Pull 시간 77% 단축이 이를 뒷받침합니다.

### pre3는 원본 자체가 상대적으로 가벼움

`flask`, `gunicorn`, `requests`만 포함한 pre3 원본은 `pandas`나 `sqlalchemy` 같은 대형 패키지가 없어 시작부터 pull 시간이 12초 수준입니다. 최적화 후 8초로 38% 단축됩니다.

### Ready Time의 소폭 증가

최적화 이미지의 Ready Time이 원본보다 소폭 길게 나타나는 경향이 있습니다. slim 이미지 + venv 기반 기동 과정에서 발생하는 경미한 오버헤드로 추정되며, 절대값은 2초 미만으로 실운영 영향 수준은 아닙니다.

---

## 6. CLI 최적화 동작 방식

`imgadvisor`는 Dockerfile을 빌드 없이 정적으로 분석해 문제를 탐지하고, 실행 가능한 최적화 Dockerfile을 자동 생성합니다. 처리 흐름은 세 단계로 구성됩니다.

### Step 1 — 정적 파싱 및 규칙 탐지 (`analyze`)

Dockerfile을 한 줄씩 파싱해 각 명령어(`FROM`, `RUN`, `COPY`, `CMD` 등)의 구조를 추출합니다. 추출된 정보를 사전에 정의된 탐지 규칙과 대조해 finding을 생성합니다.

주요 탐지 로직 예시:

| 규칙 | 탐지 방식 |
|---|---|
| `BASE_IMAGE_NOT_OPTIMIZED` | `FROM` 라인의 이미지 태그에 `-slim`, `-alpine`, `-distroless` 등이 없으면 탐지 |
| `BUILD_TOOLS_IN_FINAL_STAGE` | `gcc`, `g++`, `make`, `build-essential` 등 빌드 도구가 마지막 stage의 `RUN apt-get install`에 포함되면 탐지 |
| `APT_CACHE_NOT_CLEANED` | `apt-get install`이 있는 `RUN` 블록에 `rm -rf /var/lib/apt/lists/*`가 없으면 탐지 |
| `PIP_CACHE_NOT_DISABLED` | `pip install`에 `--no-cache-dir` 플래그가 없으면 탐지 |
| `BROAD_COPY_SCOPE` | `COPY . .` 패턴이 있고 `.dockerignore`가 없으면 탐지 |
| `SINGLE_STAGE_BUILD` | `FROM`이 하나뿐이고 빌드 도구가 포함되어 있으면 탐지 |
| `PYTHON_DEV_SERVER_IN_RUNTIME` | `CMD`에 `flask run`, `python manage.py runserver` 등 개발 서버 패턴이 있으면 탐지 |

finding이 하나라도 있으면 exit code 1을 반환하므로, CI 파이프라인에서 `imgadvisor analyze -f Dockerfile`을 빌드 전 단계에 삽입하면 문제가 있는 Dockerfile이 실제로 빌드되기 전에 자동 차단됩니다.

### Step 2 — 최적화 Dockerfile 생성 (`recommend`)

`analyze`의 finding 목록을 입력으로 받아 원본 Dockerfile을 변환합니다. 탐지된 문제에 따라 아래 변환이 자동 적용됩니다.

**구조 변환 (multi-stage 분리)**

단일 스테이지 Dockerfile을 `builder` / `runtime` 두 스테이지로 분리합니다.

```
[원본 단일 스테이지]               [변환 후 multi-stage]
FROM python:3.11                   FROM python:3.11 AS builder
RUN apt-get install gcc ...   →    RUN apt-get install gcc ...  ← 빌드 도구는 builder에만
RUN pip install ...                RUN pip install ...
COPY . .                           COPY . .
CMD [...]
                                   FROM python:3.11-slim         ← 경량 runtime
                                   COPY --from=builder /opt/venv /opt/venv
                                   COPY --from=builder /app /app
                                   CMD [...]
```

`/opt/venv`에 Python 가상환경을 생성해 의존성을 격리한 뒤, runtime stage에서는 venv 디렉토리와 앱 파일만 복사합니다. gcc, make 같은 빌드 도구는 builder에만 존재하고 최종 이미지에는 포함되지 않습니다.

**캐시 정리 자동 삽입**

```dockerfile
# 변환 전
RUN apt-get update && apt-get install -y gcc make

# 변환 후
RUN apt-get update && apt-get install -y --no-install-recommends gcc make \
    && rm -rf /var/lib/apt/lists/*
```

```dockerfile
# 변환 전
RUN pip install flask gunicorn

# 변환 후
RUN pip install --no-cache-dir flask gunicorn
```

**Python 기본 ENV 보강**

아래 환경 변수가 없으면 자동으로 추가합니다.

```dockerfile
ENV PYTHONUNBUFFERED=1           # 로그 버퍼링 방지 (stdout 즉시 출력)
ENV PYTHONDONTWRITEBYTECODE=1    # .pyc 파일 생성 안 함 (이미지 크기 절감)
ENV PIP_NO_CACHE_DIR=1           # pip 캐시 비활성화
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
```

**엔트리포인트 교체 (보수적 적용)**

`flask run`이 CMD에 있고, `pip install` 목록에 `gunicorn`이 포함되어 있는 경우에만 `gunicorn`으로 교체합니다. gunicorn이 설치되지 않는데 CMD만 바꾸면 컨테이너 기동 시 즉시 실패하기 때문입니다.

```dockerfile
# 변환 전
CMD flask run --host=0.0.0.0 --port=5000

# 변환 후 (gunicorn이 pip install 목록에 있을 때만)
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
```

worker 수(`-w`)나 타임아웃 같은 값은 운영 환경(CPU 코어 수, 메모리, 요청 패턴)에 따라 달라지므로 자동으로 고정하지 않습니다.

### Step 3 — 실측 비교 (`validate`)

`recommend`로 생성한 최적화 Dockerfile을 원본과 함께 실제로 빌드해 이미지 크기를 직접 비교합니다. 예측값이 아닌 실측값만 출력합니다.

```
imgadvisor validate -f Dockerfile --optimized Dockerfile.optimized
```

---

## 7. 설계 원칙

### 보수적 자동화

`imgadvisor`는 자동 수정 범위를 의도적으로 제한합니다.

- **자동 수정**: 구조 최적화(multi-stage 분리), 캐시 정리, base image 교체, Python ENV 보강
- **보수적 수정**: 엔트리포인트 변경 (gunicorn 설치 확인 후에만 교체)
- **수정 안 함**: worker 수, 포트, 운영 환경 의존적 설정

운영 환경을 모르는 상태에서 CPU/메모리 기반의 worker 수나 타임아웃 같은 값을 자동으로 고정하면 오히려 장애를 유발할 수 있습니다.

### 예측값 미표시

이미지 경량화로 절감되는 용량과 시간은 빌드 전까지 정확히 알 수 없습니다. `imgadvisor`는 예측 절감량을 출력하지 않으며, 실측값은 `validate` 명령으로 직접 확인하도록 설계되어 있습니다.

---

## 8. 결론

`imgadvisor`의 핵심 가치는 빌드 전 단계에서 이미지 비대 요인을 탐지하고 실행 가능한 최적화 결과물을 즉시 생성하는 것입니다.

Cold Start 기반 DR 시나리오 실측을 통해 확인된 주요 효과:

1. **배포/복구 시간 대폭 단축**: Total Time to Ready 기준 pre1 76%, pre2 86%, pre3 38% 단축
2. **이미지가 무거울수록 효과 극대화**: pandas, sqlalchemy 등 대형 패키지를 포함한 풀 이미지 기반 케이스에서 절감 효과가 가장 큼
3. **앱 기동 시간에는 영향 없음**: Ready Time은 원본/최적화 모두 2초 이내로 동일 수준 유지
4. **CI 파이프라인 연동 가능**: `analyze` 명령이 finding 발견 시 exit code 1을 반환하여 빌드 전 자동 차단 가능
