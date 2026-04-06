# imgadvisor 기술 문서

Dockerfile 정적 분석 CLI 도구 `imgadvisor`의 구현 방식, 사용 언어 및 패키지, 핵심 알고리즘을 설명하는 문서입니다.

---

## 1. 왜 Python인가

### CLI 도구에 Python이 적합한 이유

`imgadvisor`는 Dockerfile 텍스트를 파싱하고 분석 결과를 터미널에 출력하는 CLI 도구입니다. Python을 선택한 이유는 세 가지입니다.

**첫째, 정규식 기반 텍스트 파싱 생산성.**
Dockerfile은 구조화된 텍스트 파일입니다. Python의 `re` 모듈은 다양한 패턴 매칭을 빠르게 작성할 수 있고, 기존 CLI 도구들(hadolint, trivy 등)과 동일한 환경에서 쉽게 통합됩니다.

**둘째, 분석 대상이 Python 생태계.**
`imgadvisor`는 Python Dockerfile 최적화에 특화되어 있습니다. 분석 시 `requirements.txt`, `pyproject.toml`, `poetry.lock` 같은 파일을 직접 읽어 패키지 목록을 추론합니다. Python AST(`ast` 모듈)로 Flask 소스 파일을 파싱해 `gunicorn` 엔트리포인트를 자동 추론하는 기능도 포함되어 있습니다. Python이 아닌 언어로는 이런 기능을 구현하는 데 불필요한 복잡도가 생깁니다.

**셋째, 패키징과 배포 편의성.**
`pip install` 한 줄로 설치되고, `pyproject.toml`로 패키지를 관리합니다. GitHub Release에서 특정 태그로 설치하는 방식도 pip가 직접 지원합니다. CI 파이프라인에 삽입하는 CLI로는 이 방식이 가장 단순합니다.

---

## 2. 사용 패키지

### 핵심 의존성

| 패키지 | 용도 |
|---|---|
| `typer` | CLI 프레임워크 — 서브커맨드, 옵션, 도움말 자동 생성 |
| `rich` | 터미널 컬러 출력, 테이블, 패널 렌더링 |

### 표준 라이브러리 (외부 설치 불필요)

| 모듈 | 용도 |
|---|---|
| `re` | 정규식 — Dockerfile 파싱, 패턴 탐지 전반 |
| `ast` | Python 소스 파싱 — Flask app 객체/factory 추론 |
| `tomllib` | pyproject.toml 파싱 (Python 3.11+ 내장) |
| `dataclasses` | 데이터 모델 정의 (Finding, Stage, DockerfileIR 등) |
| `enum` | Severity 등급 정의 (HIGH / MEDIUM / LOW) |
| `pathlib` | 파일 경로 처리 |
| `subprocess` | Docker, Trivy CLI 호출 |

### 왜 typer를 선택했는가

argparse는 타입 힌트를 직접 지원하지 않습니다. click은 데코레이터 중심 설계로 서브커맨드 추가가 번거롭습니다. `typer`는 Python 타입 힌트를 그대로 CLI 옵션 스펙으로 사용하며, `--help` 텍스트도 docstring에서 자동 생성됩니다. 코드와 CLI 스펙이 분리되지 않아 유지보수가 쉽습니다.

### 왜 rich를 선택했는가

터미널 출력이 단순 `print`로 나오면 finding이 많아질수록 가독성이 떨어집니다. `rich`의 Panel, Table, Text 컴포넌트를 쓰면 컬러와 구분선이 자동으로 처리됩니다. 또한 `rich`는 터미널 색 지원 여부를 자동 감지해 파이프(`|`) 환경에서는 색상을 제거합니다.

---

## 3. 데이터 흐름 구조

```
Dockerfile 파일
      │
      ▼
  [parser.py]  ──────────────────────────────► DockerfileIR
      │                                          │
      │  • 백슬래시 줄 합치기                    │  • stages: list[Stage]
      │  • ARG 변수 치환                         │  • raw_lines: list[str]
      │  • Stage 분리 (FROM 단위)                │  • has_dockerignore: bool
      │  • DockerInstruction 생성                │
      │                                          │
      ▼                                          ▼
  [analyzer.py] ◄────────────────────────────── DockerfileIR
      │
      │  rules 순서대로 실행:
      │  1. base_image.check(ir)
      │  2. build_tools.check(ir)
      │  3. cache_cleanup.check(ir)
      │  4. python_runtime.check(ir)
      │  5. copy_scope.check(ir)
      │  6. multi_stage.check(ir)
      │
      ▼
  list[Finding]
      │
      ├──► [display.py]     터미널 출력 (Rich)
      ├──► [recommender.py] 최적화 Dockerfile 텍스트 생성
      └──► exit code 1      (finding > 0 이면)
```

---

## 4. 모듈별 구현 상세

### 4.1 parser.py — Dockerfile → DockerfileIR

파서는 4단계로 Dockerfile을 변환합니다.

**Step 1: 백슬래시 줄 합치기**

```dockerfile
# 원본 (파일 상태)
RUN apt-get update \
    && apt-get install -y gcc

# 합친 후 (파서 내부 표현)
"RUN apt-get update && apt-get install -y gcc"
```

Dockerfile에서 `RUN`은 가독성을 위해 여러 줄로 나눠 쓰는 경우가 많습니다. 각 규칙이 이 형태를 일일이 처리하지 않아도 되도록, 파서 단계에서 이어 붙여 하나의 문자열로 만듭니다.

```python
while joined.endswith("\\") and i + 1 < len(lines):
    joined = joined[:-1].rstrip()
    i += 1
    joined = joined + " " + lines[i].strip()
```

**Step 2: ARG 변수 치환**

```dockerfile
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}
```

`FROM ${BASE_IMAGE}`를 그대로 분석 규칙에 전달하면 패턴 매칭이 실패합니다. 파서는 첫 번째 `FROM` 이전에 선언된 `ARG` 기본값을 수집해서, `FROM` 라인의 변수 참조를 실제 값으로 치환합니다.

```python
re.sub(r"\$\{(\w+)\}|\$(\w+)", replacer, text)
```

**Step 3: Stage 분리**

`FROM`이 등장할 때마다 새로운 `Stage` 객체를 만들고, 이후 명령어들을 해당 스테이지에 추가합니다. multi-stage에서 `FROM builder`처럼 이전 스테이지를 참조하는 경우는 `[stage:builder]`로 마킹해서 base_image 규칙이 이를 외부 이미지로 오해하지 않도록 처리합니다.

**Step 4: is_final 마킹**

`stages` 목록의 마지막 요소에 `is_final = True`를 설정합니다. 대부분의 분석 규칙은 최종 런타임 이미지(마지막 스테이지)만 검사합니다.

---

### 4.2 models.py — 데이터 모델

모든 컴포넌트가 공유하는 데이터 구조를 Python `dataclass`로 정의합니다.

```
DockerfileIR
├── stages: list[Stage]
│   ├── index, base_image, alias, is_final
│   └── instructions: list[DockerInstruction]
│       └── line_no, instruction, arguments, raw
├── raw_lines: list[str]      ← recommender가 줄 교체 시 사용
└── has_dockerignore: bool

Finding
├── rule_id: str              (예: BASE_IMAGE_NOT_OPTIMIZED)
├── severity: Severity        (HIGH / MEDIUM / LOW)
├── line_no: Optional[int]    (Dockerfile 내 문제 위치)
├── description: str          (한 줄 요약)
├── recommendation: str       (해결 방법)
└── patch: Optional[Patch]    (단순 줄 치환이 가능한 경우)
```

`Patch`는 특정 줄을 다른 텍스트로 교체하는 최소 단위 수정입니다. base image 교체처럼 `FROM python:3.11` 한 줄을 `FROM python:3.11-slim`으로 바꾸는 경우에만 생성됩니다. 구조적인 변환(multi-stage 분리 등)은 Patch로 표현할 수 없어 recommender가 별도 처리합니다.

---

### 4.3 analyzer.py — 규칙 실행기

구조는 단순합니다. 등록된 규칙 함수를 순서대로 호출하고 결과를 합칩니다.

```python
_ALL_RULES = [
    base_image.check,
    build_tools.check,
    cache_cleanup.check,
    python_runtime.check,
    copy_scope.check,
    multi_stage.check,
]

def analyze(ir: DockerfileIR) -> list[Finding]:
    findings = []
    for rule in _ALL_RULES:
        findings.extend(rule(ir))
    return findings
```

각 규칙 함수는 `(ir: DockerfileIR) -> list[Finding]` 시그니처를 가집니다. 규칙 함수끼리 상태를 공유하지 않으므로 순서에 영향을 받지 않습니다. 규칙을 추가하려면 함수 하나를 작성하고 `_ALL_RULES`에 등록하면 됩니다.

---

### 4.4 rules/base_image.py — 베이스 이미지 최적화

**테이블 드리븐 패턴 매칭**

규칙을 코드에 하드코딩하지 않고 `_RULES` 테이블로 관리합니다.

```python
_RULES = [
    (r"^python:(\d+\.\d+)$", [
        {"image": "python:{v}-slim",   "min": 250, "max": 420},
        {"image": "python:{v}-alpine", "min": 350, "max": 520, "note": "musl libc"},
        ...
    ]),
    (r"^node:(\d+)$", [...]),
    ...
]
```

`FROM python:3.11`이 들어오면 패턴 목록을 순서대로 매칭해서 첫 번째 일치하는 항목의 추천 후보를 가져옵니다. `{v}`는 정규식 캡처 그룹(버전 번호)으로 치환됩니다.

**보수적 추천 필터링 — Shell 감지**

distroless, scratch 같은 이미지는 `/bin/sh`이 없습니다. CMD가 shell form(`CMD npm start`)이거나 `.sh` 파일을 COPY하면 shell이 필요하다고 판단하고, 이런 후보를 추천 목록에서 제거합니다.

```python
def _detect_shell_requirement(stage):
    for instr in stage.instructions:
        if instr.instruction == "SHELL":
            return "needs_shell", "SHELL directive found"
        if instr.instruction in ("CMD", "ENTRYPOINT"):
            if not instr.arguments.strip().startswith("["):
                return "needs_shell", "shell-form CMD detected"
        if instr.instruction == "COPY":
            if re.search(r"\.sh\b", instr.arguments):
                return "needs_shell", "COPY *.sh detected"
    return "unknown", ""
```

**보수적 추천 필터링 — Alpine 패키지 호환성**

Alpine은 glibc 대신 musl libc를 씁니다. apt로 설치하던 패키지를 Alpine의 apk로 그대로 옮길 수 없는 경우가 있습니다. 알려진 안전한 패키지 매핑 테이블 외의 패키지가 있으면 Alpine 추천을 제거하고 slim 계열만 남깁니다.

```python
_APT_TO_APK_PACKAGE_MAP = {
    "build-essential": "build-base",
    "libpq-dev": "postgresql-dev",
    ...
}

def _can_translate_apt_packages_to_alpine(packages):
    for package in packages:
        if package not in _APT_TO_APK_PACKAGE_MAP and package not in _APK_PASSTHROUGH_PACKAGES:
            return False  # 모르는 패키지 → Alpine 추천 포기
    return True
```

**최적 후보 선택**

필터링 후 남은 후보 중 `saving_max`가 가장 큰 항목을 대표 추천으로 선택합니다.

```python
best = max(filtered_recs, key=lambda r: r["max"])
```

---

### 4.5 rules/multi_stage.py — multi-stage Dockerfile 생성

이 모듈이 `imgadvisor`에서 가장 핵심적인 로직입니다. 단순 경고가 아니라 실제 Dockerfile 본문을 생성합니다.

**의존성 전략 탐지 (3가지)**

```python
def _detect_python_dependency_strategy(ir, final):
    # 1. poetry: pyproject.toml + poetry.lock + poetry install
    # 2. requirements: requirements*.txt + pip install
    # 3. inline: 파일 기반 전략 불가 → fallback
```

전략에 따라 builder stage 구성 방식이 달라집니다.

**manifest-first 전략**

`requirements` 전략이 선택되면, 앱 전체 파일보다 `requirements.txt`를 먼저 COPY하고 pip install을 수행합니다. Docker 레이어 캐시 원리상, `requirements.txt`가 바뀌지 않으면 의존성 레이어가 캐시에서 재사용됩니다. 코드만 수정하고 재빌드할 때 pip install을 건너뛸 수 있습니다.

```python
# 생성되는 builder stage 예시
COPY requirements.txt ./          ← 먼저 복사
RUN pip install --no-cache-dir -r requirements.txt   ← 캐시 레이어
COPY . .                          ← 코드 변경 시 여기서부터만 재실행
```

**instruction 스트림 재조립**

원본 Dockerfile의 명령어를 순서대로 읽으면서 builder / runtime 중 어디에 배치할지 분류합니다.

```
원본 instruction 분류:
- WORKDIR, ENV      → builder와 runtime 모두에 적절히 배치
- RUN apt/pip       → builder에만 배치 (정규화 후)
- COPY (manifest)   → builder에 manifest-first로 재배치
- COPY (app)        → builder에 의존성 설치 후 배치
- CMD/ENTRYPOINT    → runtime에만 배치 (python_runtime 추론 결과 적용)
- EXPOSE, USER      → runtime에만 배치
```

**`/opt/venv` 가상환경 패턴**

Python 의존성을 `/opt/venv`에 격리합니다. runtime stage에서는 venv 경로만 COPY하면 됩니다. `usr/local/lib/python3.x/site-packages` 전체를 복사하지 않아도 되므로 런타임 이미지 크기를 줄이고, 복사 대상이 명확해집니다.

```python
# builder stage
RUN python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir ...   # venv 안에 설치됨

# runtime stage
COPY --from=builder /opt/venv /opt/venv   # venv만 복사
COPY --from=builder /app /app
```

**RUN 정규화**

생성된 builder stage에 들어갈 `RUN` 명령에 자동으로 최적화를 적용합니다.

```python
def _normalize_python_apt_run(run_text):
    # apt-get install -y → apt-get install -y --no-install-recommends
    # 마지막에 && rm -rf /var/lib/apt/lists/* 추가

def _normalize_python_pip_run(run_text):
    # pip install → pip install --no-cache-dir
```

---

### 4.6 rules/python_runtime.py — Python 런타임 분석

**Flask app 객체 추론 (AST 사용)**

`flask run`을 `gunicorn`으로 교체하려면 `gunicorn module:app_object` 형식의 타겟이 필요합니다. 이를 위해 Python의 `ast` 모듈로 소스 파일을 파싱합니다.

```python
# 예: app.py
app = Flask(__name__)   # ← AST로 이 패턴을 탐지
```

```python
def _find_flask_app_object_name(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if _is_flask_constructor_call(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        return target.id   # "app" 반환
```

`create_app()` factory 패턴도 지원합니다. 다만 flask import가 실제로 있는 경우에만 factory로 인정합니다.

**gunicorn 설치 여부 확인**

CMD 교체는 gunicorn이 실제로 설치되는 경우에만 수행합니다. 세 가지 경로를 모두 확인합니다.

1. `requirements*.txt` 파일 파싱
2. `pyproject.toml`의 PEP 621 / Poetry 의존성 섹션 파싱 (`tomllib` 사용)
3. Dockerfile의 `pip install ...` inline 명령 파싱

```python
def detect_python_runtime_packages(ir, final):
    # requirements.txt, pyproject.toml, inline pip install 전부 확인
    interesting = {"flask", "gunicorn", "uvicorn"}
    return detected & interesting
```

**ENV 충돌 탐지**

같은 ENV 키가 여러 번 선언될 수 있습니다. 마지막 선언이 실제로 효력을 갖습니다. `collect_python_env_map`은 이 특성에 맞게 마지막으로 본 값을 유지합니다.

```python
def collect_python_env_map(final):
    env_map = {}
    for instr in final.instructions:
        if instr.instruction == "ENV":
            for key, value in _parse_env_assignments(instr.arguments):
                env_map[key] = value   # 마지막 선언 우선
    return env_map
```

---

## 5. 설계 결정

### 왜 dataclass를 사용했는가

finding 하나를 dict로 표현하면 `finding["rule_id"]`처럼 문자열 키로 접근해야 합니다. 오타가 런타임에서야 발견되고, IDE 자동완성도 동작하지 않습니다. `dataclass`를 쓰면 타입 힌트와 필드 구조가 명확해지고, 존재하지 않는 필드에 접근하면 즉시 AttributeError가 납니다.

### 왜 규칙을 테이블로 관리하는가

base_image 규칙에서 30개 이상의 이미지 패턴을 if-elif 체인으로 작성하면 새 이미지를 추가할 때 로직 흐름을 전부 읽어야 합니다. `_RULES` 테이블은 패턴과 추천값을 데이터로 분리해서, 새 이미지 추가가 테이블에 한 줄 추가하는 수준으로 단순해집니다.

### 왜 예측 절감량을 출력하지 않는가

빌드하기 전까지 정확한 이미지 크기는 알 수 없습니다. 같은 `python:3.11`이라도 설치하는 패키지에 따라 최종 크기가 크게 달라집니다. `imgadvisor`는 빌드 전 단계에서 동작하므로 예측값 대신 실측값을 `validate` 명령으로 직접 확인하도록 설계했습니다.

### 왜 worker 수를 자동으로 고정하지 않는가

gunicorn / uvicorn의 `--workers` 값은 CPU 코어 수, 메모리 크기, 요청 패턴(CPU-bound vs I/O-bound)에 따라 최적값이 다릅니다. 운영 환경 정보 없이 임의 값을 넣으면 리소스 경합이나 OOM을 유발할 수 있습니다. 이 부분은 경고(Finding)만 생성하고 값은 사용자가 직접 결정하도록 설계했습니다.

---

## 6. 전체 패키지 구조

```
imgadvisor/
├── main.py           CLI 진입점 (Typer 서브커맨드 등록)
├── parser.py         Dockerfile 텍스트 → DockerfileIR 변환
├── models.py         공유 데이터 모델 (dataclass)
├── analyzer.py       규칙 실행기 (모든 rule 호출)
├── recommender.py    Finding → 최적화 Dockerfile 텍스트 생성
├── display.py        Rich 기반 터미널 출력
├── validator.py      원본/최적화 이미지 실제 빌드 비교
├── layer_analyzer.py docker history 기반 레이어 크기 분석
├── trivy_scanner.py  Trivy CLI 호출 및 결과 파싱
└── rules/
    ├── base_image.py       BASE_IMAGE_NOT_OPTIMIZED
    ├── build_tools.py      BUILD_TOOLS_IN_FINAL_STAGE
    ├── cache_cleanup.py    APT_CACHE_NOT_CLEANED, PIP_CACHE_NOT_DISABLED
    ├── copy_scope.py       BROAD_COPY_SCOPE
    ├── multi_stage.py      SINGLE_STAGE_BUILD + multi-stage Dockerfile 생성
    └── python_runtime.py   PYTHON_RUNTIME_ENVS_*, PYTHON_DEV_SERVER_IN_RUNTIME, PYTHON_ASGI_WORKERS_NOT_SET
```
