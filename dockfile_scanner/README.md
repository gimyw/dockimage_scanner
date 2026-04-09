# imgadvisor

`imgadvisor`는 Dockerfile을 빌드 전에 정적으로 분석해서 이미지 비대 원인과 최적화 방향을 보여주는 CLI입니다.

현재 프로젝트는 범위를 의도적으로 좁혀서 Python Dockerfile 최적화에 가장 깊게 집중하고 있습니다. 단순히 예시 템플릿을 붙이는 수준이 아니라, Python 단일 스테이지 Dockerfile을 읽고 실제 multi-stage Dockerfile 본문을 생성하는 흐름까지 포함합니다.

## 저장소 구조

최상단 Git 저장소는 `0206pdh/dockimage_scanner` 이고, 실제 `imgadvisor` 패키지와 문서는 그 아래 `dockfile_scanner/` 하위 프로젝트에 있습니다.

즉 이 문서가 가리키는 실제 프로젝트 루트는 다음 경로입니다.

- `dockfile_scanner/`

## 설치

### 가장 쉬운 방법

최신 release를 기준으로 전용 가상환경 `~/.imgadvisor`에 설치하려면 아래 명령을 사용합니다.

```bash
curl -fsSL https://raw.githubusercontent.com/0206pdh/dockimage_scanner/main/dockfile_scanner/install.sh | bash
```

이 스크립트는 다음을 처리합니다.

- Python 3.11 이상 탐색
- 최신 GitHub release 태그 조회
- `dockfile_scanner` 하위 프로젝트만 설치
- `~/.local/bin/imgadvisor` 실행 링크 생성

### 수동 설치

하위 프로젝트를 정확히 지정해서 직접 설치하려면 아래처럼 `subdirectory`를 포함해야 합니다.

```bash
python -m pip install --no-cache-dir --force-reinstall \
  "git+https://github.com/0206pdh/dockimage_scanner.git@main#subdirectory=dockfile_scanner"
```

특정 릴리스를 설치하려면:

```bash
python -m pip install --no-cache-dir --force-reinstall \
  "git+https://github.com/0206pdh/dockimage_scanner.git@v0.3.10#subdirectory=dockfile_scanner"
```

필수 조건:

- Python 3.11 이상
- `validate`, `layers` 사용 시 Docker daemon

## 명령

| 명령 | Docker 필요 | 설명 |
|---|---|---|
| `analyze` | 아니오 | Dockerfile 규칙 분석 |
| `recommend` | 아니오 | 최적화 Dockerfile 생성 |
| `layers` | 예 | 실제 빌드 후 레이어 크기 분석 |
| `validate` | 예 | 원본과 최적화본을 실제로 빌드해 비교 |

## 현재 최적화 범위

현재 구현은 Python 중심입니다.

- Python 단일 스테이지 Dockerfile을 multi-stage 전환 대상으로 판단
- 실제 instruction 흐름을 읽어서 builder/runtime 재구성
- `/opt/venv` 기반 의존성 분리
- runtime 이미지를 보수적으로 `python:*‑slim` 계열로 축소
- `apt` / `pip` 설치 명령 정규화
- `requirements*.txt`, `constraints*.txt`, `pyproject.toml`, `poetry.lock` 기반 manifest-first 복사 전략
- Python runtime 기본 `ENV` 보강
- `flask run`, `uvicorn` 같은 엔트리포인트 보정

자동으로 보강하는 Python 기본 `ENV`:

- `PYTHONUNBUFFERED=1`
- `PYTHONDONTWRITEBYTECODE=1`
- `PIP_NO_CACHE_DIR=1`
- `PIP_DISABLE_PIP_VERSION_CHECK=1`

자동으로 보수적으로 보정하는 엔트리포인트:

- `flask run`은 가능할 때 `gunicorn`으로 교체
- `uvicorn`에 `--workers`가 없으면 경고만 표시하고 자동 고정은 하지 않음

실제 전후 비교와 성능/배포 테스트 방법은 아래 문서를 참고합니다.

- [comparison.md](./comparison.md)
- [benchmark.md](./benchmark.md)

## 빠른 사용 예시

```bash
imgadvisor analyze -f Dockerfile
imgadvisor recommend -f Dockerfile -o optimized.Dockerfile
imgadvisor validate -f Dockerfile --optimized optimized.Dockerfile
```

레이어별 크기를 먼저 보고 싶다면:

```bash
imgadvisor layers -f Dockerfile
```

## 최적화 흐름

`imgadvisor`의 최적화는 세 단계로 진행됩니다.

```
[1] analyze   → Dockerfile을 파싱해 문제(finding) 탐지
[2] recommend → finding을 기반으로 최적화 Dockerfile 자동 생성
[3] validate  → 원본/최적화 이미지 실제 빌드 후 크기 비교 (실측)
```

빌드 없이 문제를 먼저 드러내고(`analyze`), 실행 가능한 결과물을 즉시 만들어(`recommend`), 실제로 얼마나 줄었는지 확인(`validate`)하는 구조입니다.

---

## 탐지 규칙 상세

### `BASE_IMAGE_NOT_OPTIMIZED`

**탐지**: `FROM` 라인의 이미지 태그에 `-slim`, `-alpine`, `-distroless` 등이 없는 경우

```dockerfile
# 탐지됨
FROM python:3.11

# 권고
FROM python:3.11-slim
```

`python:3.11` 풀 이미지는 약 900MB입니다. 런타임에서 불필요한 컴파일러, 헤더 파일, 문서 등이 모두 포함된 상태입니다. `python:3.11-slim`은 약 130MB로, 표준 라이브러리와 pip만 포함합니다.

Python multi-stage 생성 경로에서는 Alpine 대신 slim 계열을 기본으로 선택합니다. Alpine은 musl libc 기반이라 일부 C 확장 패키지(pandas, numpy, psycopg2 등)와 호환 문제가 생길 수 있습니다.

지원하는 베이스 이미지 패턴은 python, node, golang, rust, openjdk, ubuntu, debian, nginx, redis, postgres, mysql 등 30개 이상입니다.

---

### `BUILD_TOOLS_IN_FINAL_STAGE`

**탐지**: 마지막 `FROM` 스테이지의 `RUN apt-get install`에 빌드 도구가 포함된 경우

탐지 대상 패키지: `gcc`, `g++`, `make`, `build-essential`, `cmake`, `libffi-dev`, `libpq-dev`, `git`, `wget`, `curl` 등

```dockerfile
# 탐지됨 — gcc가 런타임 이미지에 남음
FROM python:3.11
RUN apt-get install -y gcc libffi-dev
RUN pip install cryptography
```

`gcc`는 패키지 빌드 시점에만 필요합니다. 빌드가 끝난 뒤에도 이미지에 남으면 수백 MB가 낭비됩니다. `SINGLE_STAGE_BUILD`와 함께 탐지되면 `recommend`가 multi-stage 구조로 변환합니다.

---

### `APT_CACHE_NOT_CLEANED`

**탐지**: `apt-get install`이 있는 `RUN` 블록에 `rm -rf /var/lib/apt/lists/*`가 없는 경우

```dockerfile
# 탐지됨 — apt 캐시가 레이어에 남음
RUN apt-get update && apt-get install -y gcc

# 자동 수정
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*
```

Docker는 `RUN` 명령 하나를 레이어 하나로 저장합니다. apt 캐시를 같은 `RUN` 안에서 삭제해야 해당 레이어에 캐시가 포함되지 않습니다. 별도 `RUN rm -rf ...`으로 나누면 이전 레이어에 캐시가 이미 기록되어 있어 삭제 효과가 없습니다.

`--no-install-recommends`도 함께 삽입해 권고 패키지 설치를 막습니다.

---

### `PIP_CACHE_NOT_DISABLED`

**탐지**: `pip install`에 `--no-cache-dir` 플래그가 없는 경우

```dockerfile
# 탐지됨 — pip 캐시가 이미지에 남음
RUN pip install flask gunicorn pandas

# 자동 수정
RUN pip install --no-cache-dir flask gunicorn pandas
```

pip는 기본적으로 `~/.cache/pip`에 다운로드 캐시를 저장합니다. 컨테이너 이미지에서는 이 캐시를 재사용할 일이 없으므로 레이어 크기만 늘어납니다. `--no-cache-dir`로 캐시 생성 자체를 막습니다.

---

### `BROAD_COPY_SCOPE`

**탐지**: `COPY . .` 또는 `ADD . .` 패턴이 있고 프로젝트 루트에 `.dockerignore`가 없는 경우

```dockerfile
# 탐지됨
COPY . .
```

`.dockerignore`가 없으면 `.git/`, `__pycache__/`, `*.pyc`, 로컬 가상환경(`venv/`, `.venv/`), 테스트 파일, IDE 설정 파일 등이 모두 빌드 컨텍스트에 포함됩니다. 이미지 크기 증가뿐 아니라, 빌드 컨텍스트 전송 시간도 길어집니다.

---

### `SINGLE_STAGE_BUILD`

**탐지**: `FROM`이 하나뿐이고 빌드 도구(`gcc` 등) 또는 개발 의존성이 포함된 경우

이 규칙이 탐지되면 `recommend`가 원본 Dockerfile을 분석해 builder / runtime 두 스테이지로 재구성합니다.

```
[원본 단일 스테이지]               [변환 후 multi-stage]
FROM python:3.11                   FROM python:3.11 AS builder
RUN apt-get install gcc ...   →    RUN apt-get install gcc ...  ← 빌드 도구: builder에만
RUN pip install ...                RUN pip install --no-cache-dir ...
COPY . .                           
CMD [...]                          FROM python:3.11-slim         ← 경량 runtime
                                   COPY --from=builder /opt/venv /opt/venv
                                   COPY --from=builder /app /app
                                   CMD [...]
```

의존성은 builder에서 `/opt/venv`(Python 가상환경)에 설치하고, runtime에는 venv 디렉토리와 앱 파일만 복사합니다. gcc 등 빌드 도구는 최종 이미지에 포함되지 않습니다.

---

### `PYTHON_RUNTIME_ENVS_MISSING`

**탐지**: Python 컨테이너에서 권장 환경 변수가 누락된 경우

자동으로 추가되는 ENV:

| 변수 | 효과 |
|---|---|
| `PYTHONUNBUFFERED=1` | stdout/stderr 버퍼링 비활성화. 로그가 즉시 출력됨 |
| `PYTHONDONTWRITEBYTECODE=1` | `.pyc` 파일 생성 안 함. 이미지 크기 소폭 절감 |
| `PIP_NO_CACHE_DIR=1` | pip 캐시 전역 비활성화 |
| `PIP_DISABLE_PIP_VERSION_CHECK=1` | pip 버전 체크 요청 제거 |

`PYTHONUNBUFFERED=1`이 없으면 컨테이너 로그가 버퍼에 쌓여 지연 출력되거나, 비정상 종료 시 마지막 로그가 유실될 수 있습니다.

---

### `PYTHON_DEV_SERVER_IN_RUNTIME`

**탐지**: `CMD` 또는 `ENTRYPOINT`에 개발 서버 패턴이 포함된 경우

탐지 패턴: `flask run`, `python manage.py runserver`, `python -m flask`, `bottle.run` 등

```dockerfile
# 탐지됨
CMD flask run --host=0.0.0.0 --port=5000

# 자동 교체 (gunicorn이 설치 목록에 있을 때만)
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
```

`flask run`은 싱글 스레드 개발 서버입니다. 동시 요청이 들어오면 큐에 쌓여 직렬 처리됩니다. `gunicorn`은 멀티 워커 프로세스 기반으로 동시 처리가 가능합니다. 실측 결과 pre1 케이스에서 RPS 872 → 2,392 (약 2.7배) 향상을 확인했습니다.

단, gunicorn이 `pip install` 목록에 없으면 교체하지 않습니다. CMD만 바꾸고 gunicorn이 설치되지 않으면 컨테이너 기동 즉시 실패하기 때문입니다.

---

### `PYTHON_ASGI_WORKERS_NOT_SET`

**탐지**: `uvicorn`을 CMD에서 사용하는데 `--workers` 플래그가 없는 경우

```dockerfile
# 탐지됨
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

uvicorn 기본값은 단일 워커(싱글 프로세스)입니다. 운영 환경에서는 `--workers N`으로 CPU 코어 수에 맞게 워커 수를 설정해야 합니다.

다만 `imgadvisor`는 worker 수를 자동으로 고정하지 않습니다. CPU 코어 수, 메모리, I/O 비중, 배포 환경 등을 모르는 상태에서 값을 임의로 넣으면 오히려 리소스 경합이나 OOM을 유발할 수 있습니다. 이 규칙은 경고(WARNING) 수준으로 탐지하고 직접 설정하도록 안내합니다.

---

## Python에서 `recommend`가 생성하는 결과 예시

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
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

중요한 점은 이 결과가 단순 템플릿 붙이기가 아니라는 점입니다. 실제 Dockerfile instruction을 읽고, 가능한 범위에서 원래 의도를 유지하면서 builder/runtime를 다시 조립합니다.

주의:

- 현재 구현은 `uvicorn`에 worker 수를 자동으로 넣지 않습니다.
- 위 예시는 구조 설명용이며, 실제 생성 결과는 프로젝트 의존성과 엔트리포인트 추론 결과에 따라 달라집니다.

## 프로젝트 구조

```text
dockfile_scanner/
├─ README.md
├─ comparison.md
├─ install.sh
├─ pyproject.toml
├─ imgadvisor/
│  ├─ main.py
│  ├─ parser.py
│  ├─ analyzer.py
│  ├─ recommender.py
│  ├─ validator.py
│  ├─ layer_analyzer.py
│  ├─ display.py
│  ├─ models.py
│  └─ rules/
│     ├─ base_image.py
│     ├─ build_tools.py
│     ├─ cache_cleanup.py
│     ├─ copy_scope.py
│     ├─ multi_stage.py
│     └─ python_runtime.py
└─ test/
   ├─ Dockerfile.bloated
   └─ app.py
```

## 현재 범위 정리

이 프로젝트는 넓은 언어 지원보다 Python 최적화 깊이를 우선합니다. 다른 언어도 일부 범용 rule로 분석은 하지만, 실제 Dockerfile 본문을 재구성하는 multi-stage 생성 경로는 현재 Python 전용입니다.


---

## 검증 스크립트

### `verify_full_lifecycle.sh` — Cold Start DR 검증

Docker 이미지를 캐시 없이 처음부터 pull해서 서비스가 정상 응답할 때까지의 전체 시간을 측정합니다. 긴급 복구(DR) 시나리오에서 RTO(Recovery Time Objective) 달성 여부를 검증하는 용도입니다.

```bash
./verify_full_lifecycle.sh <이미지명> [포트] [헬스체크_경로]

# 예시
./verify_full_lifecycle.sh myrepo/myapp:latest 8080
./verify_full_lifecycle.sh myrepo/myapp:latest 8080 /health
```

측정 단계:
- Phase 0: 기존 컨테이너/이미지 삭제 (Cold Start 환경 초기화)
- Phase 1 & 2: `docker pull` 시간 측정 (네트워크 다운로드 + 레이어 압축 해제)
- Phase 3: 컨테이너 기동 후 HTTP 200 응답까지의 시간 측정

결과는 `dr_lifecycle_results.csv`에 자동 저장됩니다.

| 컬럼 | 설명 |
|---|---|
| pull_extract_ms | 이미지 pull + 레이어 추출 시간 (ms) |
| ready_ms | 컨테이너 기동 후 서비스 준비 시간 (ms) |
| total_ms | 전체 소요 시간 (ms) |
| status | SUCCESS / PULL_FAILED / RUN_FAILED / TIMEOUT |

---

### `node_contention_profile.sh` — CPU 경합 환경 성능 프로파일링

`stress-ng`로 호스트 CPU에 인위적인 부하를 주면서 컨테이너 기동 성능을 측정합니다. 실제 운영 환경처럼 노드 자원이 경합 중인 상황에서 이미지 최적화 전후의 성능 차이를 비교하는 용도입니다.

```bash
./node_contention_profile.sh <이미지명> [run_id]

# 예시
./node_contention_profile.sh myapp:baseline 1
./node_contention_profile.sh myapp:optimized 2
```

사전 요구사항:
```bash
apt-get install -y stress-ng
```

측정 지표:
- Phase 1: 호스트 전체 CPU 코어에 행렬 연산 부하 주입
- Phase 2: 컨테이너 기동 후 `/ready` 엔드포인트 폴링 (0.1s 간격, 40s 타임아웃)
- Phase 3: cgroup PSI(`cpu.pressure`)와 `/proc/<pid>/sched`에서 커널 메트릭 추출

결과는 `node_contention_results.csv`에 자동 저장됩니다.

| 컬럼 | 설명 |
|---|---|
| startup_ms | 컨테이너 기동 후 /ready 응답까지 시간 (ms) |
| kernel_wait_ms | CPU 경합으로 인한 커널 대기 시간 (PSI 기반, ms) |
| ctx_switches | 기동 구간 동안 발생한 컨텍스트 스위치 횟수 |
