# Python 테스트 Dockerfile 비교

이 문서는 `test/` 아래에서 실제로 최적화한 세 개의 Python Dockerfile을 기준으로, 원본과 최적화 결과를 비교 정리한 문서입니다.

대상 파일:

- `test/Dockerfile.pre1` / `test/Dockerfile.pre1.optimized`
- `test/Dockerfile.pre2` / `test/Dockerfile.pre2.optimized`
- `test/Dockerfile.pre3` / `test/Dockerfile.pre3.optimized`

## 비교 요약

| 케이스 | 앱 성격 | 원본 엔트리포인트 | 최적화 후 엔트리포인트 | 핵심 변화 |
|---|---|---|---|---|
| `pre1` | Flask + `pip install` inline | `flask run` | `gunicorn -b 0.0.0.0:5000 app:app` | multi-stage, slim runtime, apt/pip 정리, 개발 서버 제거 |
| `pre2` | FastAPI + Uvicorn | `uvicorn main:app` | `uvicorn main:app` (유지) | multi-stage, slim runtime, apt/pip 정리 |
| `pre3` | Flask + `requirements.txt` | `gunicorn -b 0.0.0.0:5000 app:app` | `gunicorn -b 0.0.0.0:5000 app:app` (유지) | multi-stage, slim runtime, manifest-first install, 불필요 apt 패키지 제거 |

## 공통적으로 적용된 최적화

세 케이스 모두 아래 원칙이 공통으로 반영됩니다.

- 단일 스테이지에서 builder / runtime multi-stage 구조로 분리
- runtime 이미지를 `python:3.11-slim` 계열로 축소
- `/opt/venv` 가상환경을 만들어 런타임 의존성만 복사
- `apt-get install`에 `--no-install-recommends` 추가
- 같은 `RUN`에서 `rm -rf /var/lib/apt/lists/*` 수행
- `pip install`에 `--no-cache-dir` 적용
- Python 컨테이너 기본 `ENV` 보강
  - `PYTHONUNBUFFERED=1`
  - `PYTHONDONTWRITEBYTECODE=1`
  - `PIP_NO_CACHE_DIR=1`
  - `PIP_DISABLE_PIP_VERSION_CHECK=1`

## 케이스 1: Flask 개발 서버를 Gunicorn으로 교체

대상:

- 원본: `test/Dockerfile.pre1`
- 결과: `test/Dockerfile.pre1.optimized`

원본 특징:

- `python:3.11` 단일 스테이지
- `gcc`, `g++`, `build-essential`이 최종 이미지에 남음
- `COPY . .`
- `pip install flask gunicorn requests pandas`
- 엔트리포인트가 `flask run`

최적화 결과 핵심:

- builder stage에서 빌드 도구와 의존성 설치
- runtime stage에서는 `/opt/venv`와 `/app`만 복사
- `python:3.11-slim` 사용
- `app.py` 안의 `app = Flask(__name__)`를 확인한 뒤 `gunicorn`으로 엔트리포인트 교체

최적화 결과의 핵심 부분:

```dockerfile
# -- runtime stage --
FROM python:3.11-slim
WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
EXPOSE 5000
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
```

포인트:

- `-w 2` 같은 고정 worker 값은 넣지 않음
- `gunicorn`이 실제 설치되는 경우에만 자동 교체

## 케이스 2: FastAPI / Uvicorn은 유지하고 런타임만 경량화

대상:

- 원본: `test/Dockerfile.pre2`
- 결과: `test/Dockerfile.pre2.optimized` (imgadvisor 자동 생성)

원본 특징:

- `python:3.11` 단일 스테이지
- `gcc`, `make`, `libffi-dev`가 최종 이미지에 남음
- `pip install fastapi uvicorn sqlalchemy` (캐시 미정리)
- `COPY . .` (광범위 복사)
- 엔트리포인트: `uvicorn main:app --host 0.0.0.0 --port 8000`

최적화 결과 핵심:

- builder / runtime 분리
- builder에서 gcc/make/libffi-dev 사용 후 `rm -rf /var/lib/apt/lists/*`로 캐시 정리
- runtime은 `python:3.11-slim`만 사용 — 빌드 도구 없음
- `pip install --no-cache-dir`로 pip 캐시 제거
- `uvicorn` 엔트리포인트는 유지

최적화 결과의 핵심 부분:

```dockerfile
# -- builder stage --
FROM python:3.11 AS builder
WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN python -m venv $VIRTUAL_ENV
RUN apt-get update && apt-get install -y --no-install-recommends gcc make libffi-dev \
    && rm -rf /var/lib/apt/lists/*
COPY . .
RUN pip install --no-cache-dir fastapi uvicorn sqlalchemy

# -- runtime stage --
FROM python:3.11-slim
WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

포인트:

- `uvicorn`에 worker 수를 자동으로 고정하지 않음
- CPU/메모리/배포 환경을 모르는 상태에서 `--workers` 자동 삽입은 하지 않음

## 케이스 3: requirements 기반 Flask 앱 구조 최적화

대상:

- 원본: `test/Dockerfile.pre3`
- 결과: `test/Dockerfile.pre3.optimized` (imgadvisor 자동 생성)

원본 특징:

- `python:3.11` 단일 스테이지
- `gcc`, `libpq-dev`, `git`, `wget`이 최종 이미지에 남음 (빌드 불필요 패키지 포함)
- apt 캐시 미정리
- `COPY requirements.txt` → `pip install` → `COPY . .` 순서는 올바름 (manifest-first)
- pip 캐시 미정리
- 엔트리포인트: `gunicorn -b 0.0.0.0:5000 app:app`

최적화 결과 핵심:

- builder에서 `gcc`, `libpq-dev`, `git`, `wget` 사용 후 apt 캐시 정리
- runtime은 `python:3.11-slim`만 사용 — 빌드 도구 없음
- manifest-first 전략(`requirements.txt` 먼저 복사) 유지
- `pip install --no-cache-dir -r requirements.txt`로 pip 캐시 제거
- 엔트리포인트 `gunicorn` 유지

최적화 결과의 핵심 부분:

```dockerfile
# -- builder stage --
FROM python:3.11 AS builder
WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN python -m venv $VIRTUAL_ENV
ENV FLASK_APP=app.py
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev git wget \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# -- runtime stage --
FROM python:3.11-slim
WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV FLASK_APP=app.py
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
EXPOSE 5000
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
```

포인트:

- 이 케이스의 핵심은 manifest-first + multi-stage 전환
- `gcc`, `libpq-dev`, `git`, `wget` 같은 패키지는 builder에만 남고 최종 이미지에는 포함되지 않음
- 엔트리포인트는 원본과 동일하게 유지

## 케이스별 차이

### `pre1`

- inline `pip install`
- `flask run` → `gunicorn` 교체로 이미지 경량화 + 서버 개선 동시 달성

### `pre2`

- FastAPI / Uvicorn 케이스
- 엔트리포인트 유지, 구조 최적화에 집중
- worker 수는 자동 수정하지 않음

### `pre3`

- `requirements.txt` 기반 manifest-first 설치 전략 유지
- 불필요한 시스템 패키지(gcc, libpq-dev, git, wget)를 builder에만 격리
- 엔트리포인트 변경 없이 순수 구조 최적화

## 테스트 방법

각 케이스를 디렉토리 별로 분리한 경우:

```bash
# pre2 디렉토리에서
docker build -f Dockerfile.pre2           -t pre2-original  .
docker build -f Dockerfile.pre2.optimized -t pre2-optimized .

# pre3 디렉토리에서
docker build -f Dockerfile.pre3           -t pre3-original  .
docker build -f Dockerfile.pre3.optimized -t pre3-optimized .
```

imgadvisor로 분석 및 validate:

```bash
imgadvisor analyze  -f Dockerfile.pre2
imgadvisor validate -f Dockerfile.pre2 --optimized Dockerfile.pre2.optimized

imgadvisor analyze  -f Dockerfile.pre3
imgadvisor validate -f Dockerfile.pre3 --optimized Dockerfile.pre3.optimized
```

전체 검증 절차는 아래 문서를 참고합니다.

- [benchmark.md](./benchmark.md)

## 정리

현재 `imgadvisor`의 Python 최적화는 다음 방향으로 정리됩니다.

- 구조 최적화 (multi-stage, venv 분리): 자동
- 캐시/기본 ENV 정리: 자동
- base image 축소 (slim): 자동
- 엔트리포인트 변경: 보수적 자동화 (gunicorn 설치 확인 후에만 교체)
- worker 수 등 운영 환경 의존 설정: 자동 수정 안 함
