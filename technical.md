# imgadvisor 기술 문서

Dockerfile 정적 분석 CLI 도구 `imgadvisor`의 구현 방식, 사용 언어·패키지, 핵심 알고리즘을 실제 코드와 함께 설명합니다.

---

## 1. 왜 Python인가

`imgadvisor`는 Dockerfile 텍스트를 파싱하고 분석 결과를 터미널에 출력하는 CLI 도구입니다. Python을 선택한 이유는 세 가지입니다.

**첫째, 정규식 기반 텍스트 파싱 생산성**

Dockerfile은 구조화된 텍스트 파일입니다. 각 줄은 `FROM`, `RUN`, `COPY` 같은 키워드로 시작하고, 내용은 자유 형식 문자열입니다. Python의 `re` 모듈은 이런 패턴 탐지를 간결하게 작성할 수 있고, 도구 설치 없이 바로 사용 가능합니다.

**둘째, 분석 대상이 Python 생태계**

`imgadvisor`는 Python Dockerfile 최적화에 특화되어 있습니다. 분석 중에 `requirements.txt`, `pyproject.toml`, `poetry.lock` 같은 파일을 직접 읽어 패키지 목록을 추론하고, Python의 `ast` 모듈로 Flask 소스 파일을 파싱해 `gunicorn` 엔트리포인트를 자동으로 추론합니다. 분석 도구와 분석 대상이 같은 생태계 안에 있어 자연스럽게 통합됩니다.

**셋째, 패키징과 배포 편의성**

`pip install` 한 줄로 설치되고, `pyproject.toml`로 패키지를 관리합니다. GitHub Release 특정 태그로 직접 설치하는 방식도 pip가 지원합니다. CI 파이프라인에 삽입하는 CLI로는 이 방식이 가장 단순합니다.

---

## 2. 사용 패키지

### 외부 의존성 (pyproject.toml에 명시)

| 패키지 | 버전 | 용도 |
|---|---|---|
| `typer` | >=0.12.0 | CLI 프레임워크 — 서브커맨드, 옵션, 도움말 자동 생성 |
| `rich` | >=13.7.0 | 터미널 컬러 출력, 테이블, 구분선 렌더링 |

### 표준 라이브러리 (별도 설치 불필요)

| 모듈 | 용도 |
|---|---|
| `re` | 정규식 — Dockerfile 파싱, 규칙 탐지 전반 |
| `ast` | Python 소스 파일 파싱 — Flask app 객체/factory 추론 |
| `tomllib` | pyproject.toml 파싱 (Python 3.11 내장) |
| `dataclasses` | 데이터 모델 정의 |
| `enum` | Severity 등급 정의 |
| `pathlib` | 파일 경로 처리 |
| `subprocess` | Docker CLI 호출 |

### 왜 typer인가

`argparse`는 타입 힌트를 직접 지원하지 않아 옵션 정의가 장황합니다. `typer`는 Python 함수의 타입 힌트를 그대로 CLI 옵션 스펙으로 사용합니다.

```python
# argparse 방식 — 타입과 옵션 정의가 분리됨
parser.add_argument("--dockerfile", "-f", type=str, required=True)

# typer 방식 — 타입 힌트 하나로 옵션 정의 완료
def cmd_analyze(
    dockerfile: Path = typer.Option(..., "--dockerfile", "-f"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    ...
```

docstring이 `--help` 텍스트로 자동 변환되고, `Path` 타입을 쓰면 `exists=True`처럼 파일 존재 여부도 자동으로 검증합니다.

### 왜 rich인가

`print()`로 터미널 출력을 하면 finding이 많아질수록 가독성이 떨어집니다. `rich`를 쓰면 컬러, 테이블, 구분선이 자동으로 처리됩니다.

```python
# 실제 display.py의 finding 출력 코드
console.print(f"  {label}  [dim]{line_str}[/dim]  [bold]{f.rule_id}[/bold]")
console.print(f"           [dim]{desc}[/dim]")
console.print(f"           [dim]fix:[/dim] {first}")
```

`[bold red]FAIL[/bold red]` 같은 마크업을 문자열 안에 직접 씁니다. 또한 파이프(`|`)나 리다이렉션 환경에서는 색상을 자동으로 제거하므로 `grep`이나 로그 파일에서도 깨지지 않습니다.

---

## 3. 전체 데이터 흐름

```
Dockerfile 텍스트 파일
         │
         ▼
  ┌─────────────┐
  │  parser.py  │  텍스트 → 구조화된 객체(DockerfileIR)로 변환
  └─────────────┘
         │  DockerfileIR (stages, instructions, raw_lines 포함)
         ▼
  ┌──────────────┐
  │ analyzer.py  │  6개 규칙 함수를 순서대로 실행
  └──────────────┘
         │  list[Finding]  (탐지된 문제 목록)
         ▼
    ┌────┴────┐
    │         │
    ▼         ▼
display.py  recommender.py
(터미널 출력)  (최적화 Dockerfile 생성)
                  │
                  ▼
             최적화된 Dockerfile 텍스트
```

Finding이 하나라도 있으면 `exit code 1`을 반환합니다. CI 파이프라인에서 `imgadvisor analyze -f Dockerfile`을 빌드 전 단계에 넣으면 문제 있는 Dockerfile이 실제로 빌드되기 전에 자동 차단됩니다.

---

## 4. 데이터 모델 (models.py)

모든 모듈이 공유하는 데이터 구조를 Python `dataclass`로 정의합니다. `dataclass`를 사용하면 타입 힌트와 필드 구조가 명확해지고, 없는 필드에 접근하면 즉시 오류가 납니다.

### DockerfileIR — 파싱 결과 전체

```python
@dataclass
class DockerfileIR:
    stages: list[Stage]        # FROM 블록 하나당 Stage 하나
    raw_lines: list[str]       # 원본 파일 줄 목록 (recommender가 줄 교체 시 사용)
    path: str                  # Dockerfile 파일 경로
    has_dockerignore: bool     # .dockerignore 존재 여부

    @property
    def final_stage(self) -> Stage | None:
        return self.stages[-1] if self.stages else None

    @property
    def is_multi_stage(self) -> bool:
        return len(self.stages) > 1
```

### Stage — FROM 블록 하나

```python
@dataclass
class Stage:
    index: int                          # 순서 (0부터 시작)
    base_image: str                     # FROM 뒤의 이미지 이름
    alias: str | None                   # AS builder 처럼 붙은 이름
    is_final: bool = False              # 마지막 스테이지(런타임 이미지) 여부
    instructions: list[DockerInstruction] = field(default_factory=list)

    @property
    def run_instructions(self):
        return [i for i in self.instructions if i.instruction == "RUN"]

    @property
    def all_run_text(self) -> str:
        # 이 스테이지의 모든 RUN 명령을 이어 붙인 텍스트
        # 규칙들이 "이 스테이지에 gcc가 있는가?"를 검색할 때 사용
        return " ".join(i.arguments for i in self.run_instructions)
```

### DockerInstruction — 명령어 하나

```python
@dataclass
class DockerInstruction:
    line_no: int        # Dockerfile 원본에서의 줄 번호 (1부터 시작)
    instruction: str    # 명령어 키워드: FROM, RUN, COPY, CMD, ENV ...
    arguments: str      # 명령어 뒤의 내용 (백슬래시 줄 합치기 완료 상태)
    stage_index: int    # 소속 스테이지 인덱스
    raw: str            # 합쳐진 원본 텍스트 (recommender가 생성 시 재사용)
```

### Finding — 탐지된 문제 하나

```python
@dataclass
class Finding:
    rule_id: str              # 규칙 식별자: "BASE_IMAGE_NOT_OPTIMIZED" 등
    severity: Severity        # HIGH / MEDIUM / LOW
    line_no: int | None       # Dockerfile 내 문제가 발생한 줄 번호
    description: str          # 한 줄 요약
    recommendation: str       # 해결 방법 (여러 줄 가능)
    saving_min_mb: int        # 예상 절감 용량 하한 (MB)
    saving_max_mb: int        # 예상 절감 용량 상한 (MB)
    patch: Patch | None = None  # 단순 줄 교체로 수정 가능한 경우 첨부
```

### Patch — 줄 하나를 교체하는 최소 수정

```python
@dataclass
class Patch:
    line_no: int    # 교체할 줄 번호
    old_text: str   # 교체 전 원본 텍스트
    new_text: str   # 교체 후 텍스트
```

`Patch`는 `FROM python:3.11` 한 줄을 `FROM python:3.11-slim`으로 바꾸는 것처럼 줄 하나를 교체하는 간단한 경우에만 생성됩니다. multi-stage 분리처럼 구조적인 변환은 Patch로 표현할 수 없어서 recommender가 별도로 처리합니다.

---

## 5. 파서 (parser.py)

Dockerfile 텍스트를 `DockerfileIR`로 변환합니다. 4단계로 진행됩니다.

### Step 1 — 백슬래시 줄 합치기

Dockerfile에서 `RUN`은 가독성을 위해 여러 줄로 나눠 쓰는 경우가 많습니다.

```dockerfile
# 파일 원본
RUN apt-get update \
    && apt-get install -y gcc g++ \
    && rm -rf /var/lib/apt/lists/*
```

각 규칙이 이 형태를 일일이 처리하면 정규식이 복잡해집니다. 파서 단계에서 미리 합쳐서 규칙에는 한 줄로 전달합니다.

```python
# parser.py — _join_continuations()
while joined.endswith("\\") and i + 1 < len(lines):
    joined = joined[:-1].rstrip()   # 끝의 \ 제거
    i += 1
    next_part = lines[i].strip()
    if next_part and not next_part.startswith("#"):
        joined = joined + " " + next_part

# 결과: "RUN apt-get update && apt-get install -y gcc g++ && rm -rf /var/lib/apt/lists/*"
```

### Step 2 — ARG 변수 치환

```dockerfile
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}   # ← 이 상태로 규칙에 전달하면 패턴 매칭 실패
```

파서는 첫 번째 `FROM` 이전에 선언된 `ARG` 기본값을 수집해서 변수 참조를 실제 값으로 치환합니다.

```python
# parser.py — _collect_arg_defaults() + _substitute_vars()
def _collect_arg_defaults(joined):
    args = {}
    for _, line in joined:
        if re.match(r"^FROM\s+", line, re.IGNORECASE):
            break  # FROM 만나면 수집 중단
        m = re.match(r"^ARG\s+(\w+)(?:=(.+))?$", line, re.IGNORECASE)
        if m:
            args[m.group(1)] = (m.group(2) or "").strip().strip('"')
    return args

def _substitute_vars(text, args):
    def replacer(m):
        name = m.group(1) or m.group(2)
        return args.get(name, m.group(0))  # 모르는 변수는 원본 유지

    return re.sub(r"\$\{(\w+)\}|\$(\w+)", replacer, text)

# ${BASE_IMAGE} → "python:3.11-slim" 으로 치환됨
```

### Step 3 — Stage 분리

`FROM`이 등장할 때마다 새로운 `Stage` 객체를 만들고, 이후 명령어들을 해당 스테이지에 추가합니다.

```python
# parser.py — parse() 내부
for line_no, line in joined:
    cmd = line.split()[0].upper()   # 첫 단어가 명령어

    if cmd == "FROM":
        # FROM python:3.11 AS builder 파싱
        from_m = re.match(r"^(\S+)(?:\s+AS\s+(\S+))?", args, re.IGNORECASE)
        base_image = from_m.group(1)   # "python:3.11"
        alias = from_m.group(2)         # "builder" (없으면 None)

        # FROM builder 처럼 이전 스테이지를 참조하면 [stage:builder]로 마킹
        # → base_image 규칙이 이걸 외부 이미지로 오해하지 않도록
        if base_image.lower() in stage_aliases:
            base_image = f"[stage:{base_image}]"

        stages.append(Stage(index=current_idx, base_image=base_image, alias=alias))

    else:
        # FROM 이후의 명령어는 현재 스테이지에 추가
        instr = DockerInstruction(
            line_no=line_no,
            instruction=cmd,
            arguments=args,
            ...
        )
        stages[current_idx].instructions.append(instr)
```

### Step 4 — is_final 마킹

```python
# 가장 마지막 스테이지가 런타임 이미지
if stages:
    stages[-1].is_final = True
```

대부분의 분석 규칙은 `ir.final_stage`만 검사합니다. 빌드 도구가 builder 스테이지에 있는 건 정상이지만, 마지막 runtime 스테이지에 있으면 문제이기 때문입니다.

---

## 6. 규칙 실행기 (analyzer.py)

구조는 단순합니다. 등록된 규칙 함수를 순서대로 호출하고 결과를 합칩니다.

```python
# analyzer.py
_ALL_RULES = [
    base_image.check,
    build_tools.check,
    cache_cleanup.check,
    python_runtime.check,
    copy_scope.check,
    multi_stage.check,
]

def analyze(ir: DockerfileIR) -> list[Finding]:
    findings: list[Finding] = []
    for rule in _ALL_RULES:
        findings.extend(rule(ir))
    return findings
```

각 규칙 함수는 `(ir: DockerfileIR) -> list[Finding]` 시그니처를 가집니다. 규칙끼리 상태를 공유하지 않으므로 독립적입니다. 새 규칙을 추가하려면 함수 하나를 작성하고 `_ALL_RULES`에 등록하면 됩니다.

---

## 7. 규칙별 구현 상세

### 7.1 BASE_IMAGE_NOT_OPTIMIZED (base_image.py)

**테이블 드리븐 패턴 매칭**

30개 이상의 이미지 패턴을 if-elif로 쓰면 새 이미지 추가 시 전체 흐름을 읽어야 합니다. `_RULES` 테이블은 패턴과 추천값을 데이터로 분리해서, 새 이미지 추가가 테이블 한 줄 추가로 끝납니다.

```python
# base_image.py — _RULES 테이블 (일부)
_RULES = [
    # (정규식 패턴, 추천 후보 목록)
    (r"^python:(\d+\.\d+)$", [
        {"image": "python:{v}-slim",   "min": 250, "max": 420, "note": None},
        {"image": "python:{v}-alpine", "min": 350, "max": 520, "note": "musl libc compat"},
        {"image": "gcr.io/distroless/python3", "min": 450, "max": 630, "note": "no shell"},
    ]),
    (r"^node:(\d+)$", [
        {"image": "node:{v}-slim",  "min": 280, "max": 420},
        {"image": "node:{v}-alpine","min": 380, "max": 550, "note": "musl libc compat"},
    ]),
    (r"^golang:(\d+\.\d+)$", [
        {"image": "scratch (after multi-stage)", "min": 600, "max": 950},
    ]),
    # ... 30개 이상 계속
]
```

`FROM python:3.11`이 들어오면 패턴을 순서대로 매칭합니다. `(\d+\.\d+)` 캡처 그룹이 `3.11`을 잡고, 추천 이미지의 `{v}`를 `3.11`로 치환합니다.

```python
for pattern, recs in _RULES:
    m = re.match(pattern, image, re.IGNORECASE)
    if not m:
        continue
    version = m.group(1) if m.lastindex else ""
    best_image = best["image"].replace("{v}", version)
    # python:{v}-slim → python:3.11-slim
```

**Shell 감지 필터 — distroless/scratch 추천 안전장치**

distroless, scratch 이미지는 `/bin/sh`이 없습니다. CMD가 shell form이거나 `.sh` 파일을 COPY하면 shell이 필요하다고 판단해서 이런 후보를 목록에서 제거합니다.

```python
# base_image.py — _detect_shell_requirement()
def _detect_shell_requirement(stage):
    for instr in stage.instructions:
        if instr.instruction == "SHELL":
            return "needs_shell", "SHELL directive found"

        if instr.instruction in ("CMD", "ENTRYPOINT"):
            args = instr.arguments.strip()
            if not args.startswith("["):
                # shell form: CMD npm start  (대괄호 없음 → 쉘 필요)
                return "needs_shell", f"shell-form {instr.instruction} detected"

        if instr.instruction == "COPY":
            if re.search(r"\.sh\b", instr.arguments):
                # .sh 파일 복사 → 실행 시 /bin/sh 필요
                return "needs_shell", "COPY *.sh detected"

    return "unknown", "no CMD/ENTRYPOINT found"
```

**Alpine 패키지 호환성 필터**

Alpine은 glibc 대신 musl libc를 씁니다. Debian/Ubuntu 계열에서 쓰던 apt 패키지를 Alpine의 apk로 그대로 옮길 수 없는 경우가 있습니다. 안전하게 변환 가능한 패키지 매핑 테이블 외의 패키지가 있으면 Alpine 추천을 제거합니다.

```python
# base_image.py
_APT_TO_APK_PACKAGE_MAP = {
    "build-essential": "build-base",
    "libpq-dev": "postgresql-dev",
    "libssl-dev": "openssl-dev",
    "pkg-config": "pkgconf",
}
_APK_PASSTHROUGH_PACKAGES = {"gcc", "g++", "make", "curl", "git", ...}

def _can_translate_apt_packages_to_alpine(packages):
    for package in packages:
        in_map = package in _APT_TO_APK_PACKAGE_MAP
        in_passthrough = package in _APK_PASSTHROUGH_PACKAGES
        if not in_map and not in_passthrough:
            return False   # 모르는 패키지 → Alpine 추천 포기
    return True
```

**최적 후보 선택**

필터링 후 남은 후보 중 절감 상한값(`max`)이 가장 큰 항목을 대표 추천으로 선택합니다.

```python
best = max(filtered_recs, key=lambda r: r["max"])
```

---

### 7.2 BUILD_TOOLS_IN_FINAL_STAGE (build_tools.py)

final stage의 `RUN` 명령에서 빌드 전용 도구가 남아있는지 검사합니다.

```python
# build_tools.py
_BUILD_TOOLS = [
    "gcc", "g\\+\\+", "clang", "make", "cmake",
    "build-essential", "pkg-config",
    "libpq-dev", "libssl-dev", "libffi-dev",
    "maven", "gradle", "cargo", "rustc",
    "python3-dev", "wget",
    # ...
]

# 로딩 시 미리 컴파일 (검색마다 컴파일하면 느림)
_PATTERNS = [
    re.compile(rf"\b{tool}\b", re.IGNORECASE) for tool in _BUILD_TOOLS
]

def check(ir):
    final = ir.final_stage
    found = []

    for instr in final.run_instructions:
        for tool_re, tool_name in zip(_PATTERNS, _BUILD_TOOLS):
            clean_name = tool_name.replace("\\+\\+", "++")  # g\+\+ → g++
            if tool_re.search(instr.arguments) and clean_name not in found:
                found.append(clean_name)

    # 발견된 도구 목록: ["gcc", "g++", "build-essential"]
    # 도구별로 Finding을 여러 개 만들지 않고 하나로 묶어서 반환
    if found:
        return [Finding(rule_id="BUILD_TOOLS_IN_FINAL_STAGE", ...)]
```

`\b`는 단어 경계(word boundary)를 의미합니다. `gcc`가 `ngcc`나 `gcc-dev` 같은 단어의 일부로 포함된 경우는 탐지하지 않고, 독립적인 단어로 쓰인 경우만 탐지합니다.

---

### 7.3 APT_CACHE_NOT_CLEANED / PIP_CACHE_NOT_DISABLED (cache_cleanup.py)

apt, pip, npm, yarn, gem 등 12개 패키지 매니저를 지원합니다. 규칙 로직은 동일하고 데이터만 다르므로 `_CHECKS` 테이블로 관리합니다.

```python
# cache_cleanup.py — _CHECKS 테이블 (일부)
_CHECKS = [
    {
        "id": "APT_CACHE_NOT_CLEANED",
        "pm": "apt-get",
        "install": r"apt-get\s+install|apt\s+install",   # 설치 명령 패턴
        "cleanup": [                                       # 정리 방법 (하나라도 있으면 OK)
            r"rm\s+-rf\s+/var/lib/apt/lists",
            r"apt-get\s+clean",
            r"apt-get\s+autoremove",
        ],
        "recommended": "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
                       "        <pkg> \\\n"
                       "    && rm -rf /var/lib/apt/lists/*",
        "min": 30, "max": 120,
    },
    {
        "id": "PIP_CACHE_NOT_DISABLED",
        "pm": "pip",
        "install": r"pip\s+install|pip3\s+install",
        "cleanup": [r"--no-cache-dir", r"pip\s+cache\s+purge"],
        "recommended": "RUN pip install --no-cache-dir <pkg>",
        "min": 20, "max": 80,
    },
    # npm, yarn, pnpm, yum, dnf, gem, composer, maven, gradle ...
]
```

탐지 로직은 세 단계입니다.

```python
def check(ir):
    final = ir.final_stage
    findings = []
    seen_ids = set()  # 같은 규칙이 여러 RUN에서 중복 탐지되지 않도록

    for instr in final.run_instructions:
        run_text = instr.arguments  # 합쳐진 RUN 전체 텍스트

        for rule in _CHECKS:
            if rule["id"] in seen_ids:
                continue

            # 1. 이 RUN에 설치 명령이 있는가?
            if not re.search(rule["install"], run_text, re.IGNORECASE):
                continue

            # 2. 같은 RUN 안에 캐시 정리 명령이 있는가?
            cleaned = any(
                re.search(p, run_text, re.IGNORECASE) for p in rule["cleanup"]
            )
            if cleaned:
                continue

            # 3. 정리 없이 설치만 → Finding 생성
            seen_ids.add(rule["id"])
            findings.append(Finding(rule_id=rule["id"], ...))

    return findings
```

**핵심: 왜 같은 RUN 블록 안에서 정리해야 하는가**

Docker는 `RUN` 명령 하나를 레이어 하나로 저장합니다. 아래 두 경우는 완전히 다릅니다.

```dockerfile
# 잘못된 방식 — 캐시가 layer 1에 이미 포함됨
RUN apt-get install -y gcc            # layer 1: gcc + apt 캐시 포함
RUN rm -rf /var/lib/apt/lists/*       # layer 2: 캐시 삭제 (하지만 layer 1은 이미 확정)

# 올바른 방식 — 같은 레이어에서 설치 + 삭제
RUN apt-get install -y gcc \
    && rm -rf /var/lib/apt/lists/*    # layer 1: gcc만 포함, 캐시 없음
```

---

### 7.4 BROAD_COPY_SCOPE (copy_scope.py)

`.dockerignore` 없이 `COPY . .`를 쓰면 `.git/`, `__pycache__/`, `.env`, 테스트 파일 등이 전부 이미지에 포함됩니다.

```python
# copy_scope.py
def check(ir):
    final = ir.final_stage
    findings = []

    for instr in final.copy_instructions:
        args = instr.arguments

        # --from=builder 같은 multi-stage COPY는 제외 (정상 패턴)
        if "--from=" in args:
            continue

        # COPY의 첫 인수가 . 인지 확인 (COPY . /app, COPY . . 등)
        parts = args.split()
        if not parts or parts[0] != ".":
            continue

        # .dockerignore 유무에 따라 심각도 다르게 부여
        if not ir.has_dockerignore:
            severity = Severity.HIGH      # dockerignore 없음 → 매우 위험
            saving_min, saving_max = 90, 300
        else:
            severity = Severity.MEDIUM    # dockerignore 있어도 명시적 경로가 더 안전
            saving_min, saving_max = 50, 200

        findings.append(Finding(rule_id="BROAD_COPY_SCOPE", severity=severity, ...))
```

---

### 7.5 SINGLE_STAGE_BUILD + multi-stage 생성 (multi_stage.py)

`imgadvisor`에서 가장 복잡한 모듈입니다. 단순 경고가 아니라 실행 가능한 Dockerfile 본문을 생성합니다.

**탐지 조건 판정**

단순히 single-stage라고 무조건 탐지하지 않습니다. 실제로 multi-stage 전환이 의미 있는 경우만 탐지합니다.

```python
# multi_stage.py — check()
def check(ir):
    if ir.is_multi_stage:
        return []   # 이미 multi-stage → 패스

    final = ir.final_stage
    if not is_python_stage(final):
        return []   # Python이 아니면 생성 경로 없음 → 패스

    run_text = final.all_run_text

    # 빌드 신호가 하나라도 있어야 탐지
    has_build_pkg = any(
        re.search(rf"\b{re.escape(tool)}\b", run_text, re.IGNORECASE)
        for tool in ["gcc", "g++", "make", "libpq-dev", ...]
    )
    has_apt_install = bool(re.search(r"\b(?:apt-get|apt)\s+install\b", run_text))
    has_pip_install = bool(re.search(r"\bpip(?:3)?\s+install\b", run_text))
    broad_copy = any(
        instr.instruction == "COPY" and instr.arguments.startswith(". ")
        for instr in final.copy_instructions
    )

    # 아무 신호도 없으면 multi-stage 전환 실익이 없음 → 탐지 안 함
    if not (has_build_pkg or has_apt_install or has_pip_install or broad_copy):
        return []
```

**의존성 전략 탐지 (3가지)**

원본 Dockerfile이 어떤 방식으로 의존성을 관리하는지 감지해서 생성 전략을 결정합니다.

```python
# multi_stage.py — _detect_python_dependency_strategy()
def _detect_python_dependency_strategy(ir, final):
    context_dir = Path(ir.path).parent

    # 1. requirements.txt / constraints.txt 가 있고 pip install 이 있으면 → requirements 전략
    requirement_files = list(context_dir.glob("requirements*.txt"))
    has_pip = any(
        re.search(r"\bpip\s+install\b", instr.arguments)
        for instr in final.run_instructions
    )
    if requirement_files and has_pip:
        return "requirements", [f.name for f in requirement_files]

    # 2. pyproject.toml + poetry.lock 이 있고 poetry install 이 있으면 → poetry 전략
    poetry_files = [name for name in ("pyproject.toml", "poetry.lock")
                    if (context_dir / name).exists()]
    has_poetry = any(
        re.search(r"\bpoetry\s+install\b", instr.arguments)
        for instr in final.run_instructions
    )
    if poetry_files and has_poetry:
        return "poetry", poetry_files

    # 3. 파일 기반 전략 불가 → inline fallback
    return "inline", []
```

**manifest-first 전략**

`requirements` 전략이 선택되면 `requirements.txt`를 앱 코드보다 먼저 COPY합니다. Docker 레이어 캐시 원리상, `requirements.txt`가 바뀌지 않으면 pip install 레이어가 캐시에서 재사용됩니다.

```dockerfile
# 생성되는 builder stage 구조
FROM python:3.11 AS builder
WORKDIR /app
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN python -m venv $VIRTUAL_ENV

COPY requirements.txt ./           ← requirements.txt만 먼저 복사
RUN pip install --no-cache-dir -r requirements.txt  ← 캐시 레이어
COPY . .                           ← 코드가 바뀌어도 위 레이어는 재사용

FROM python:3.11-slim              ← 경량 runtime
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
CMD [...]
```

**RUN 명령 정규화**

생성된 builder stage에 들어갈 `RUN` 명령에 최적화를 자동 삽입합니다.

```python
# multi_stage.py — _normalize_python_apt_run()
def _normalize_python_apt_run(run_text):
    updated = run_text

    # --no-install-recommends 추가 (권고 패키지 설치 방지)
    if "apt-get install -y" in updated and "--no-install-recommends" not in updated:
        updated = updated.replace(
            "apt-get install -y",
            "apt-get install -y --no-install-recommends",
            1
        )

    # 같은 RUN 안에 apt 캐시 삭제 추가
    if "/var/lib/apt/lists" not in updated:
        updated = updated.rstrip() + " \\\n    && rm -rf /var/lib/apt/lists/*"

    return updated


def _normalize_python_pip_run(run_text):
    updated = run_text

    # --no-cache-dir 추가
    if "pip install" in updated and "--no-cache-dir" not in updated:
        updated = updated.replace("pip install", "pip install --no-cache-dir", 1)

    return updated
```

---

### 7.6 Python 런타임 분석 (python_runtime.py)

**Flask app 객체 추론 — Python AST 활용**

`flask run`을 `gunicorn`으로 교체하려면 `gunicorn module:app_object` 형식의 타겟이 필요합니다. 이를 위해 Python의 `ast` 모듈로 소스 파일을 직접 파싱합니다.

```python
# app.py 예시
app = Flask(__name__)   # ← 이 패턴을 AST로 찾아야 함
```

`ast.parse(source)`는 Python 소스 코드를 AST(Abstract Syntax Tree, 추상 구문 트리)로 변환합니다. 문자열 매칭이 아니라 코드 구조 자체를 분석하므로, 공백이나 따옴표 형식이 달라도 정확하게 탐지합니다.

```python
# python_runtime.py — _find_flask_app_object_name()
def _find_flask_app_object_name(tree):
    for node in tree.body:               # 모듈 최상위 문장들 순회
        if not isinstance(node, ast.Assign):
            continue                      # 대입문이 아니면 건너뜀

        if not _is_flask_constructor_call(node.value):
            continue                      # 오른쪽이 Flask(...) 호출이 아니면 건너뜀

        for target in node.targets:
            if isinstance(target, ast.Name):
                return target.id          # "app" 반환

    return None


def _is_flask_constructor_call(node):
    # Flask(...) 또는 flask.Flask(...) 형태 확인
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "Flask"
    if isinstance(func, ast.Attribute):
        return func.attr == "Flask"
    return False
```

`create_app()` factory 패턴도 지원합니다. 다만 `from flask import Flask` import가 실제로 있는 파일에서만 factory로 인정합니다.

```python
def _find_flask_app_factory_name(tree):
    # flask import가 없으면 create_app이 있어도 Flask factory로 보지 않음
    has_flask_import = any(
        isinstance(node, ast.ImportFrom) and node.module == "flask"
        for node in ast.walk(tree)
    )
    if not has_flask_import:
        return None

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "create_app":
            return node.name   # "create_app" 반환 → CMD gunicorn module:create_app()
```

**gunicorn 설치 여부 확인 — 3가지 경로**

CMD 교체는 gunicorn이 실제로 설치되는 경우에만 수행합니다. 설치되지 않는데 CMD만 바꾸면 컨테이너 기동 즉시 실패하기 때문입니다.

```python
# python_runtime.py — detect_python_runtime_packages()
def detect_python_runtime_packages(ir, final):
    context_dir = Path(ir.path).resolve().parent
    detected = set()

    # 경로 1: requirements*.txt 파싱
    for pattern in ("requirements*.txt", "constraints*.txt"):
        for path in context_dir.glob(pattern):
            for raw_line in path.read_text().splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                # "flask>=3.0,<4" → "flask"
                name = re.split(r"[<>=!~\[\s;]", line)[0].strip()
                detected.add(name.lower())

    # 경로 2: pyproject.toml 파싱 (PEP 621 / Poetry 스타일)
    pyproject = context_dir / "pyproject.toml"
    if pyproject.exists():
        data = tomllib.loads(pyproject.read_text())
        for item in data.get("project", {}).get("dependencies", []):
            name = re.split(r"[<>=!~\[\s;]", item)[0].strip()
            detected.add(name.lower())

    # 경로 3: Dockerfile inline pip install 파싱
    for instr in final.run_instructions:
        match = re.search(r"\bpip\s+install\b(.+)", instr.arguments)
        if match:
            for token in match.group(1).split():
                if not token.startswith("-"):
                    name = re.split(r"[<>=!~\[]", token)[0]
                    detected.add(name.lower())

    # flask, gunicorn, uvicorn 중 실제로 설치되는 것만 반환
    return detected & {"flask", "gunicorn", "uvicorn"}
```

**ENV 충돌 탐지**

같은 ENV 키가 Dockerfile에 여러 번 나올 수 있습니다. 마지막 선언이 실제로 효력을 갖습니다. 기존에 선언된 값이 권장값과 다르면 충돌로 탐지합니다.

```python
# python_runtime.py — collect_python_env_map()
def collect_python_env_map(final):
    env_map = {}    # 최종 ENV 값
    env_lines = {}  # 각 키의 마지막 선언 줄 번호

    for instr in final.instructions:
        if instr.instruction != "ENV":
            continue
        for key, value in _parse_env_assignments(instr.arguments):
            env_map[key] = value           # 덮어쓰기 → 마지막 선언 유지
            env_lines[key] = instr.line_no

    return env_map, env_lines

# 권장 ENV가 없거나 다른 값이면 Finding 생성
_BASELINE_ENVS = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PIP_NO_CACHE_DIR": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}

missing = [name for name in _BASELINE_ENVS if env_map.get(name) is None]
conflicts = [(name, env_map[name], expected)
             for name, expected in _BASELINE_ENVS.items()
             if env_map.get(name) not in (None, expected)]
```

---

## 8. recommender.py — 최적화 Dockerfile 생성

Finding 목록을 받아 최적화된 Dockerfile 텍스트를 생성합니다. 세 가지 방식으로 동작합니다.

**방식 1: SINGLE_STAGE_BUILD가 있으면 → multi-stage 본문 전체 출력**

```python
# recommender.py — recommend()
def recommend(ir, findings):
    multi_finding = next(
        (f for f in findings if f.rule_id == "SINGLE_STAGE_BUILD"), None
    )

    if multi_finding:
        # multi_stage.py가 Finding의 recommendation 필드에
        # 실제 Dockerfile 본문을 넣어두었음
        template = _extract_multistage_template(multi_finding)
        if template:
            return _HEADER + "\n" + template + "\n"
```

**방식 2: Patch가 있는 Finding → 해당 줄 직접 교체**

```python
    # 내림차순으로 적용 — 위쪽 줄부터 교체하면 아래 줄 번호가 밀림
    patches = [f.patch for f in findings if f.patch is not None]
    patches.sort(key=lambda p: p.line_no, reverse=True)

    lines = list(ir.raw_lines)
    for patch in patches:
        idx = patch.line_no - 1
        if lines[idx] == patch.old_text:   # 안전 장치: 줄 내용이 일치할 때만 교체
            lines[idx] = patch.new_text
```

예: `FROM python:3.11` → `FROM python:3.11-slim` (base_image.py가 Patch 생성)

**방식 3: Patch 없는 Finding → inline 주석 삽입**

구조적 변환이 필요하지만 Patch로 표현할 수 없는 경우, 해당 줄 위에 주석을 삽입합니다.

```python
def _insert_comments(lines, findings):
    no_patch = [f for f in findings if f.patch is None and f.line_no is not None]
    no_patch.sort(key=lambda f: f.line_no, reverse=True)  # 내림차순

    for f in no_patch:
        idx = f.line_no - 1
        severity_tag = "FAIL" if f.severity == Severity.HIGH else "WARN"
        comment_lines = [
            f"# [imgadvisor:{severity_tag}] {f.rule_id}",
            f"#   {f.description}",
        ]
        for rec_line in f.recommendation.splitlines()[:3]:
            comment_lines.append(f"#   {rec_line}")

        lines[idx:idx] = comment_lines   # 해당 줄 앞에 주석 삽입
```

삽입을 내림차순(아래 줄부터)으로 하는 이유: 위쪽 줄에 먼저 줄을 삽입하면 아래 줄들의 인덱스가 밀려서 줄 번호가 틀어집니다.

---

## 9. 설계 결정 정리

| 결정 | 이유 |
|---|---|
| `dataclass` 사용 | dict 키 오타는 런타임에 발견되지만, dataclass 필드 오타는 즉시 AttributeError 발생 |
| 규칙을 테이블로 관리 | if-elif 체인은 새 이미지 추가 시 전체를 읽어야 함. 테이블은 한 줄 추가로 끝남 |
| 예측 절감량 미표시 | 빌드 전에는 정확한 크기를 알 수 없음. `validate`로 실측값 확인 유도 |
| worker 수 자동 고정 안 함 | CPU 코어·메모리·요청 패턴에 따라 최적값이 다름. 임의 값은 OOM 유발 가능 |
| Alpine 추천 보수적 | musl libc 호환 문제로 패키지 변환이 불확실하면 slim 계열로 후퇴 |
| AST로 Flask 앱 파싱 | 정규식은 공백·따옴표 형식에 취약. AST는 코드 구조를 직접 분석해서 정확함 |
| gunicorn 설치 확인 후 CMD 교체 | 설치 없이 CMD만 바꾸면 컨테이너 기동 즉시 실패 |

---

## 10. 전체 파일 구조

```
imgadvisor/
├── main.py           CLI 진입점 — Typer 서브커맨드 등록 (analyze/recommend/validate/layers)
├── parser.py         Dockerfile 텍스트 → DockerfileIR 변환 (4단계 파이프라인)
├── models.py         공유 데이터 모델 — DockerfileIR, Stage, Finding, Patch 등
├── analyzer.py       규칙 실행기 — _ALL_RULES를 순서대로 호출
├── recommender.py    Finding → 최적화 Dockerfile 텍스트 생성
├── display.py        Rich 기반 터미널 출력
├── validator.py      원본/최적화 이미지 실제 빌드 비교 (docker build 호출)
├── layer_analyzer.py docker history 기반 레이어 크기 분석
└── rules/
    ├── base_image.py       BASE_IMAGE_NOT_OPTIMIZED — 테이블 드리븐 패턴 매칭
    ├── build_tools.py      BUILD_TOOLS_IN_FINAL_STAGE — 정규식 단어 경계 탐지
    ├── cache_cleanup.py    APT/PIP/NPM 등 12개 패키지 매니저 캐시 탐지
    ├── copy_scope.py       BROAD_COPY_SCOPE — .dockerignore 유무 연계
    ├── multi_stage.py      SINGLE_STAGE_BUILD + 실제 Dockerfile 본문 생성
    └── python_runtime.py   Python ENV/CMD 분석 — AST로 Flask app 객체 추론
```
