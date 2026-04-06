# imgadvisor 발표 스크립트 (5~6분)

> **총 구성**: 4개 파트 / 각 파트 시간 배분 표시
> 발표 흐름: 문제 제기 → 도구 소개 → 동작 방식 → 실측 결과 → 결론

---

## Part 1 — 문제 제기 (약 1분)

**"Docker 이미지, 얼마나 무겁게 쓰고 계세요?"**

---

Python 웹 서비스를 Docker로 배포할 때 가장 흔하게 쓰는 Dockerfile 패턴을 보면,

```dockerfile
FROM python:3.11
RUN apt-get update && apt-get install -y gcc g++ build-essential
COPY . .
RUN pip install flask gunicorn requests pandas
CMD flask run --host=0.0.0.0 --port=5000
```

이게 얼핏 보면 문제없어 보이지만, 이 이미지는 **483MB**입니다.

문제는 세 가지입니다.

1. `gcc`, `g++` 같은 빌드 도구는 패키지 설치할 때만 필요한데 최종 이미지에 그대로 남습니다.
2. `python:3.11` 풀 이미지는 약 900MB짜리 베이스를 씁니다. 런타임에 필요 없는 컴파일러, 헤더 파일이 모두 포함된 상태입니다.
3. `flask run`은 싱글 스레드 개발 서버입니다. 운영 트래픽을 받으면 동시 요청이 큐에 쌓입니다.

이 세 가지를 사람이 직접 체크하고 고치려면 Dockerfile을 읽고, 어떤 패키지가 빌드 전용인지 판단하고, multi-stage를 직접 짜야 합니다.

그래서 만든 게 **imgadvisor**입니다.

---

## Part 2 — 도구 소개 (약 1분)

**"빌드 전에 Dockerfile을 분석하고, 최적화 Dockerfile을 자동으로 만들어 줍니다."**

---

명령어는 세 가지가 핵심입니다.

```bash
imgadvisor analyze  -f Dockerfile          # 문제 탐지
imgadvisor recommend -f Dockerfile -o Dockerfile.optimized  # 최적화본 생성
imgadvisor validate -f Dockerfile --optimized Dockerfile.optimized  # 실측 비교
```

기존 Dockerfile 린터(`hadolint` 등)와 다른 점은, 경고를 나열하는 데서 끝나지 않고 **실제로 실행 가능한 Dockerfile 본문을 생성**한다는 점입니다.

`analyze`는 finding이 있으면 exit code 1을 반환하기 때문에, CI 파이프라인의 빌드 전 단계에 넣으면 문제 있는 Dockerfile이 실제로 빌드되기 전에 자동 차단됩니다.

---

## Part 3 — 동작 방식 (약 1분 30초)

**"어떻게 탐지하고, 무엇을 바꾸나요?"**

---

탐지 규칙은 9개입니다. 심각도 HIGH 위주로 보면,

| 규칙 | 탐지 내용 |
|---|---|
| `BASE_IMAGE_NOT_OPTIMIZED` | slim/alpine/distroless 미사용 |
| `BUILD_TOOLS_IN_FINAL_STAGE` | gcc 등 빌드 도구가 런타임 이미지에 잔존 |
| `SINGLE_STAGE_BUILD` | 빌드 도구 포함 단일 스테이지 |
| `PYTHON_DEV_SERVER_IN_RUNTIME` | flask run 등 개발 서버를 운영에 그대로 사용 |

`recommend`가 이 finding들을 받아서 하는 일은 크게 세 가지입니다.

**첫째, multi-stage 분리.**
원본 단일 스테이지를 builder / runtime 두 스테이지로 쪼갭니다. gcc는 builder에서만 쓰고, runtime에는 `/opt/venv`(Python 가상환경)와 앱 파일만 복사합니다. 빌드 도구는 최종 이미지에 포함되지 않습니다.

**둘째, 캐시 정리 자동 삽입.**
`apt-get install`이 있는 `RUN` 블록 끝에 `rm -rf /var/lib/apt/lists/*`를 붙이고, `pip install`에 `--no-cache-dir`를 추가합니다. Docker는 `RUN` 하나를 레이어 하나로 저장하기 때문에, 같은 블록 안에서 삭제해야 레이어에 캐시가 포함되지 않습니다.

**셋째, 엔트리포인트 교체 (보수적).**
`flask run`이 CMD에 있고, pip install 목록에 `gunicorn`이 있으면 gunicorn으로 교체합니다. gunicorn이 설치되지 않는데 CMD만 바꾸면 컨테이너가 기동 즉시 실패하기 때문에, 설치 확인 후에만 교체합니다.

worker 수나 포트 같은 값은 운영 환경에 따라 달라지므로 자동으로 고정하지 않습니다.

---

## Part 4 — 실측 결과 (약 1분 30초)

**"실제로 얼마나 달라지나요?"**

---

세 개의 Python Dockerfile(Flask, FastAPI, requirements.txt 기반 Flask)로 Cold Start DR 시나리오를 측정했습니다.

> Cold Start DR이란: 로컬 캐시가 전혀 없는 상태에서 Docker Hub에서 이미지를 받아 서비스 가능한 상태가 될 때까지 걸리는 시간입니다. 새 노드에 배포하거나 장애 후 복구할 때의 실제 상황과 같습니다.

**이미지 크기**

| 케이스 | 원본 | 최적화 | 절감률 |
|---|---:|---:|---:|
| pre1 (Flask) | 483 MB | 95 MB | **80%** |
| pre2 (FastAPI) | 438 MB | 70 MB | **84%** |
| pre3 (Flask + requirements.txt) | 422 MB | 61 MB | **85%** |

**서비스 가능 상태까지 걸린 시간 (Pull + Ready)**

| 케이스 | 원본 | 최적화 | 단축률 |
|---|---:|---:|---:|
| pre1 | 134초 | 32초 | **76%** |
| pre2 | 106초 | 15초 | **86%** |
| pre3 | 13초 | 8초 | **38%** |

핵심은 이미지가 무거울수록 효과가 크다는 점입니다. pandas, sqlalchemy 같은 대형 패키지를 포함한 pre1/pre2는 80% 이상 단축됩니다.

컨테이너가 뜬 이후 응답 시간(Ready Time)은 원본/최적화 모두 2초 이내로 동일했습니다. 이미지 경량화는 Pull 속도에 영향을 주지, 런타임 처리 속도에는 영향을 주지 않습니다.

부하 테스트(`hey -n 10000 -c 100`)에서 pre1은 RPS 872 → 2,392로 약 2.7배 향상됐는데, 이건 이미지 최적화 효과가 아니라 `flask run` → `gunicorn` 교체 효과입니다. pre2/pre3는 서버 변경이 없어 RPS가 거의 동일했습니다. 세 케이스 모두 에러율 0%로 최적화 후에도 서비스 안정성에 문제가 없었습니다.

---

## 마무리 (약 30초)

`imgadvisor`가 하는 일을 한 줄로 요약하면,

> **"Dockerfile을 빌드하기 전에 읽고, 문제를 찾고, 직접 고친 Dockerfile을 내줍니다."**

경량화는 이미지 크기만의 문제가 아닙니다. 장애 시 복구 시간, 오토스케일 반응 속도, CI/CD 배포 시간이 모두 이미지 크기에 비례합니다. 이미지가 무겁다는 건 복구가 느리다는 뜻입니다.

감사합니다.

---

## 예상 질문 & 답변

**Q. 다른 언어(Node, Go 등)는 지원하나요?**

탐지 규칙 자체는 30개 이상 베이스 이미지 패턴에 적용됩니다. 다만 `recommend`의 multi-stage Dockerfile 자동 생성은 현재 Python 전용입니다. 언어마다 의존성 관리 방식이 달라서, Python 최적화를 충분히 검증한 뒤 확장할 계획입니다.

**Q. 기존 hadolint 같은 도구와 차이가 뭔가요?**

hadolint는 규칙 위반을 경고로 알려줍니다. imgadvisor는 경고에서 끝나지 않고 실제로 실행 가능한 최적화 Dockerfile 본문을 생성합니다. 사용자가 경고를 읽고 직접 고치는 게 아니라, 결과물을 바로 사용하거나 검토할 수 있습니다.

**Q. worker 수를 자동으로 안 넣는 이유가 뭔가요?**

CPU 코어 수, 메모리 크기, 요청 패턴(CPU-bound vs I/O-bound)에 따라 최적 worker 수가 달라집니다. 운영 환경을 모르는 상태에서 값을 임의로 넣으면 오히려 OOM이나 리소스 경합을 유발할 수 있습니다. 이 부분은 경고로 안내하고 직접 설정하도록 설계했습니다.

**Q. 실측값이 환경마다 다를 수 있지 않나요?**

맞습니다. 네트워크 속도, Docker Hub 상태에 따라 Pull 시간은 달라집니다. 다만 이미지 크기 절감률은 환경과 무관하게 일정합니다. Pull 시간은 이미지 크기에 비례하기 때문에, 크기가 80% 줄면 Pull 시간도 비례해서 줄어드는 경향이 있습니다.
