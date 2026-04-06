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
| `PIP_CACHE_NOT_DISABLED` | pip 캐시 미비활성화 | MEDIUM |
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

## 4. 측정 결과 요약

전체 측정 결과는 [result.md](./result.md)를 참고합니다.

### Docker Hub Pull 시간 (Cold Start 실측)

Cold Start 환경(`docker rmi -f` 후 pull)에서 측정한 이미지 다운로드 시간입니다.

| 케이스 | 원본 | 최적화 | 단축률 |
|---|---:|---:|---:|
| pre3 | 71,105ms | 13,239ms | **81.4%** |

pre3 기준, `python:3.11` 풀 이미지(13개 레이어) 대비 `python:3.11-slim` 기반 multi-stage(8개 레이어)로 전환 시 Docker Hub에서 이미지를 내려받는 시간이 약 5.4배 단축되었습니다.

이 수치는 단순 성능 수치를 넘어 아래 운영 시나리오에 직결됩니다.

- **배포 속도**: 새 버전 배포 시 이미지 pull 시간 단축
- **오토스케일**: 트래픽 급증 시 신규 인스턴스 준비 시간 단축
- **DR(재해 복구)**: 장애 후 Cold Start로 서비스 복구까지 걸리는 시간 단축

---

## 5. 설계 원칙

### 보수적 자동화

`imgadvisor`는 자동 수정 범위를 의도적으로 제한합니다.

- **자동 수정**: 구조 최적화(multi-stage 분리), 캐시 정리, base image 교체, Python ENV 보강
- **보수적 수정**: 엔트리포인트 변경 (gunicorn 설치 확인 후에만 교체)
- **수정 안 함**: worker 수, 포트, 운영 환경 의존적 설정

운영 환경을 모르는 상태에서 CPU/메모리 기반의 worker 수나 타임아웃 같은 값을 자동으로 고정하면 오히려 장애를 유발할 수 있기 때문입니다.

### 예측값 미표시

이미지 최적화로 절감되는 용량과 시간은 이미지를 직접 빌드하기 전까지 정확히 알 수 없습니다. `imgadvisor`는 예측 기반 절감량을 출력하지 않으며, 실측값은 `validate` 명령으로 직접 확인하도록 설계되어 있습니다.

---

## 6. 결론

`imgadvisor`의 핵심 가치는 빌드 전 단계에서 이미지 비대 요인을 탐지하고 실행 가능한 최적화 결과물을 즉시 생성하는 것입니다.

테스트 결과를 통해 확인된 주요 효과:

1. **이미지 경량화**: `python:3.11` → `python:3.11-slim` + multi-stage 전환으로 이미지 크기가 대폭 감소
2. **배포/복구 시간 단축**: Cold Start 기준 Docker Hub pull 시간이 최대 81% 단축 (pre3 실측)
3. **런타임 안정성 향상**: 개발 서버 제거, 캐시 정리, Python 기본 ENV 보강으로 운영 환경에 적합한 구조로 전환
4. **CI 파이프라인 연동 가능**: `analyze` 명령이 finding 발견 시 exit code 1을 반환하여 빌드 차단 가능

전체 벤치마크 수치는 [result.md](./result.md)에서 확인할 수 있습니다.
