# imgadvisor 내부 구조 학습 문서

---

## 1. 전체 데이터 흐름

```
Dockerfile (텍스트)
      │
      ▼
 parser.parse()
      │  줄 이어쓰기 합치기
      │  ARG 기본값 수집
      │  변수 치환
      │  FROM 단위로 Stage 분리
      │
      ▼
 DockerfileIR  ◄── 이 구조체가 모든 규칙에 전달됨
  ├── stages: list[Stage]        # FROM 블록 목록
  │    ├── base_image            # FROM 뒤의 이미지명
  │    ├── alias                 # AS builder 같은 이름
  │    ├── is_final              # 마지막 스테이지 여부
  │    └── instructions          # COPY, RUN, CMD 등 목록
  ├── raw_lines                  # 원본 줄 (패치용)
  ├── path                       # 파일 경로
  └── has_dockerignore           # .dockerignore 존재 여부
      │
      ▼
 analyzer.analyze()
      │  규칙 5개를 순서대로 실행
      │  각 규칙은 독립적 (서로 영향 없음)
      │
      ▼
 list[Finding]
  ├── rule_id        # BASE_IMAGE_NOT_OPTIMIZED 등
  ├── severity       # HIGH / MEDIUM
  ├── line_no        # Dockerfile 줄 번호
  ├── description    # 한 줄 설명
  ├── recommendation # 수정 방법 (여러 줄 가능)
  ├── saving_min_mb  # 예상 절감 최소 (MB)
  ├── saving_max_mb  # 예상 절감 최대 (MB)
  └── patch          # 자동 줄 교체 패치 (있을 때만)
      │
      ├── display.print_analysis()   → 터미널 출력
      ├── recommender.recommend()    → 최적화 Dockerfile 생성
      └── display.print_json_result() → JSON 출력
```

---

## 2. 파서 (parser.py)

Dockerfile을 읽어서 `DockerfileIR`로 변환하는 단계. 규칙이 직접 텍스트를 파싱하지 않아도 되도록 구조화된 데이터를 제공한다.

### 2-1. 줄 이어쓰기 처리

Dockerfile에서 RUN은 가독성을 위해 `\`로 줄을 이어 쓰는 게 일반적이다.

```dockerfile
RUN apt-get update \
    && apt-get install -y gcc \
    && rm -rf /var/lib/apt/lists/*
```

파서는 이걸 **하나의 문자열**로 합쳐서 처리한다:
```
"apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*"
```

줄 번호는 **첫 번째 줄**을 보존한다. 규칙이 `line_no`를 보고할 때 정확한 위치를 찾을 수 있다.

### 2-2. ARG 변수 치환

```dockerfile
ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}
```

파서는 FROM 이전에 선언된 ARG의 기본값을 수집해서 `${PYTHON_VERSION}` → `3.11`로 치환한다. 치환이 안 되는 변수는 원본 그대로 남긴다. base_image 규칙이 `python:${PYTHON_VERSION}`이 아닌 `python:3.11`을 보게 된다.

### 2-3. 멀티스테이지 스테이지 참조 마킹

```dockerfile
FROM golang:1.22 AS builder
FROM builder          ← 이전 스테이지를 베이스로 사용
```

두 번째 FROM의 base_image가 이전 스테이지 이름(`builder`)이면, base_image를 `[stage:builder]`로 마킹한다. base_image 규칙이 `_ALREADY_OPTIMAL` 정규식에서 `\[stage:.*\]` 패턴으로 이를 감지하고 스킵한다.

---

## 3. 규칙 엔진 (analyzer.py)

```python
_ALL_RULES = [
    base_image.check,
    build_tools.check,
    cache_cleanup.check,
    copy_scope.check,
    multi_stage.check,
]

def analyze(ir):
    findings = []
    for rule in _ALL_RULES:
        findings.extend(rule(ir))   # 각 규칙 독립 실행
    return findings
```

구조가 단순한 게 의도적이다. 규칙을 추가하려면 `rules/` 아래에 `check(ir) -> list[Finding]` 함수를 만들고 `_ALL_RULES`에 등록하면 끝난다.

---

## 4. 규칙 상세

### 4-1. BASE_IMAGE_NOT_OPTIMIZED (base_image.py)

**목적**: 베이스 이미지가 slim/alpine/distroless 대안이 있는 경우 탐지

#### 전체 흐름

```
final_stage.base_image 추출
        │
        ▼
_ALREADY_OPTIMAL 정규식 검사
 (-slim, -alpine, distroless, scratch, busybox, [stage:...])
        │ 해당하면 → return [] (스킵)
        │
        ▼
_RULES 테이블을 순서대로 정규식 매칭
 "python:3.11" → python:(\d+\.\d+) 패턴 매칭
        │ 매칭 안 되면 다음 규칙으로
        │ 매칭되면 →
        ▼
_detect_shell_requirement(final_stage)
 CMD/ENTRYPOINT/COPY/SHELL 명령 분석
 → "needs_shell" / "no_shell" / "unknown"
        │
        ▼
_filter_recs_by_shell(recs, shell_status)
 needs_shell/unknown → distroless, scratch 제외
 no_shell → 전체 후보 유지
        │
        ▼
_filter_recs_by_pkg_manager(stage, recs)
 apt-get install 패키지 추출
 → 모르는 패키지 있으면 alpine 제외
        │
        ▼
best = max(filtered_recs, key=lambda r: r["max"])
 절감량 최대 후보 선택
        │
        ▼
Patch 생성 (즉시 줄 교체 가능한 경우만)
 "scratch (after multi-stage)" 같은 설명문은 Patch 없음
        │
        ▼
Finding 반환
```

#### shell 탐지 로직

```
SHELL [/bin/bash, -c]              → needs_shell (SHELL 지시자)
CMD python app.py                  → needs_shell (shell form: [로 시작 안 함)
ENTRYPOINT /start.sh               → needs_shell (shell form)
COPY entrypoint.sh /app/           → needs_shell (*.sh 파일 복사)
ENTRYPOINT ["/server"]             → no_shell    (exec form + no other signals)
CMD ["node", "app.js"]만 있고      → unknown     (exec form CMD지만 ENTRYPOINT 없음)
아무 CMD/ENTRYPOINT 없음           → unknown
```

`unknown`은 distroless를 제외하고 slim을 권장한다. 실수로 shell 없는 이미지 추천해서 빌드가 깨지는 것보다 안전한 기본값이 낫기 때문이다.

#### apt 패키지 번역 안전 필터

Alpine을 추천하기 전에 Dockerfile의 apt 패키지들이 Alpine에서도 동작하는지 확인한다.

```python
_APT_TO_APK_PACKAGE_MAP = {
    "build-essential": "build-base",   # debian → alpine 이름이 다름
    "libpq-dev": "postgresql-dev",
    "libssl-dev": "openssl-dev",
    "pkg-config": "pkgconf",
}

_APK_PASSTHROUGH_PACKAGES = {
    "bash", "curl", "gcc", "git", "make", "tar", ...  # 이름 그대로 apk에도 있음
}
```

- 두 목록에 **모두 있는** 패키지만 있으면 → Alpine 추천 유지
- 하나라도 모르는 패키지가 있으면 → Alpine 제외, slim으로 fallback
- `apt-get install`이 아예 없으면 → 체크 없이 Alpine 유지

#### _RULES 테이블 구조

```python
(r"^python:(\d+\.\d+(?:\.\d+)?)$", [
    {"image": "python:{v}-slim",           "min": 250, "max": 420, "note": None},
    {"image": "python:{v}-alpine",         "min": 350, "max": 520, "note": "musl libc compat"},
    {"image": "gcr.io/distroless/python3", "min": 450, "max": 630, "note": "no shell, recommended for prod"},
]),
```

- 패턴의 캡처 그룹 `(\d+\.\d+)` → `{v}` 자리표시자에 치환됨
- `min`/`max` = 예상 절감 MB (경험적 수치)
- `max` 기준으로 best 선택 → 절감 효과 최대 후보 우선

---

### 4-2. BUILD_TOOLS_IN_FINAL_STAGE (build_tools.py)

**목적**: 런타임에 불필요한 빌드 도구가 final stage RUN에 남아있는지 탐지

#### 탐지 방식

```python
_BUILD_TOOLS = [
    "gcc", "g\\+\\+", "clang",          # C/C++ 컴파일러 (++ 이스케이프 필요)
    "make", "cmake", "build-essential",  # 빌드 시스템
    "maven", "gradle", "ant",            # Java 빌드
    "cargo", "rustc",                    # Rust
    "python3-dev", "libpq-dev", ...      # 개발 헤더
    "wget",                              # 다운로드 도구
]

_PATTERNS = [re.compile(rf"\b{tool}\b", re.IGNORECASE) for tool in _BUILD_TOOLS]
```

`\b` 단어 경계를 사용하는 이유: `libgcc`가 `gcc`로 잘못 탐지되는 걸 방지한다.

`g\\+\\+`처럼 이스케이프하는 이유: `+`는 정규식에서 특수문자(`1개 이상`)이므로 리터럴 `+`를 표현하려면 `\+`로 이스케이프해야 한다.

#### 결과 포맷

여러 빌드 도구가 발견돼도 Finding 하나로 묶어서 반환한다. 최대 6개를 나열하고 나머지는 "and N more"로 표시한다.

```
build tools found in final stage: `gcc`, `make`, `python3-dev` and 2 more
```

줄 번호는 **처음 발견된** 위치만 기록한다.

---

### 4-3. 캐시 정리 규칙 (cache_cleanup.py)

**목적**: 패키지 설치 후 캐시를 정리하지 않은 경우 탐지 (12가지 패키지 매니저)

#### 공통 탐지 구조

모든 패키지 매니저 규칙이 동일한 `_CHECKS` 테이블 구조를 사용한다:

```python
{
    "id":      "APT_CACHE_NOT_CLEANED",
    "install": r"apt-get\s+install|apt\s+install",  # 설치 명령 정규식
    "cleanup": [                                     # 정리 방법 (하나라도 있으면 OK)
        r"rm\s+-rf\s+/var/lib/apt/lists",
        r"apt-get\s+clean",
    ],
    ...
}
```

탐지 알고리즘:
```
RUN 명령 텍스트에서 install 패턴 검색
        │ 없으면 → skip
        │ 있으면 →
        ▼
같은 RUN 텍스트에서 cleanup 패턴 하나라도 있는지 검색
        │ 있으면 → skip (정리됨)
        │ 없으면 → Finding 생성
```

**왜 같은 RUN 명령에서 정리해야 하는가?**

```dockerfile
# 잘못된 예 (캐시가 레이어에 남음)
RUN apt-get install -y gcc
RUN rm -rf /var/lib/apt/lists/*   ← 이미 이전 레이어에 기록됨

# 올바른 예 (같은 레이어에서 설치+정리)
RUN apt-get install -y gcc \
    && rm -rf /var/lib/apt/lists/*
```

Docker 레이어는 union filesystem에서 쌓인다. 설치와 정리가 **다른 RUN**에 있으면, 설치 레이어에 이미 캐시 파일들이 기록되어 이후에 삭제해도 전체 이미지 크기에는 영향이 없다.

#### 중복 방지

`seen_ids` set으로 같은 규칙이 여러 번 리포트되지 않도록 한다. 예: 동일 Dockerfile에 `pip install`이 두 번 있어도 `PIP_CACHE_NOT_DISABLED`는 한 번만 나온다.

#### 패키지 매니저별 정리 방법 요약

| PM | 설치 탐지 | 정리 방법 |
|---|---|---|
| apt-get | `apt-get install` | `&& rm -rf /var/lib/apt/lists/*` |
| pip | `pip install` | `--no-cache-dir` 플래그 |
| apk | `apk add` | `--no-cache` 플래그 |
| npm | `npm install/ci` | `npm cache clean --force`, `--omit=dev` |
| yarn | `yarn install/add` | `yarn cache clean`, `--production` |
| pnpm | `pnpm install/add` | `pnpm store prune`, `--prod` |
| yum | `yum install` | `yum clean all` |
| dnf | `dnf install` | `dnf clean all` |
| gem | `gem install`, `bundle install` | `--no-document`, `gem cleanup` |
| composer | `composer install/require` | `--no-dev`, `composer clear-cache` |
| mvn | `mvn` | `rm -rf ~/.m2` (or multi-stage 권장) |
| gradle | `gradle`, `gradlew` | `rm -rf ~/.gradle` (or multi-stage 권장) |

Maven/Gradle은 근본적으로 multi-stage가 해결책이라 권고 메시지가 다르다.

---

### 4-4. BROAD_COPY_SCOPE (copy_scope.py)

**목적**: `COPY . .` 처럼 빌드 컨텍스트 전체를 복사하는 패턴 탐지

#### 탐지 조건

```python
for instr in final.copy_instructions:
    args = instr.arguments

    # --from=<stage> COPY는 멀티스테이지 간 복사 → 무시
    if "--from=" in args:
        continue

    parts = args.split()
    # 첫 번째 인수가 "." 인 경우만 탐지
    if not parts or parts[0] != ".":
        continue
```

`COPY . .` 또는 `COPY . /app` 처럼 첫 번째 인수가 `.`(현재 디렉토리 전체)인 경우만 탐지한다. `COPY src/ /app/src/` 같은 명시적 경로는 탐지하지 않는다.

#### 심각도 분기

```
.dockerignore 없음 → HIGH
  위험: .git, node_modules, .env, secrets 등이 모두 이미지에 포함될 수 있음

.dockerignore 있음 → MEDIUM
  위험은 줄었지만 명시적 경로가 더 안전하고 의도를 명확히 함
```

#### .dockerignore 위치 확인

파서에서 `path.parent / ".dockerignore"` 경로를 확인한다. Dockerfile이 `/app/docker/Dockerfile`이면 `/app/docker/.dockerignore`를 본다.

---

### 4-5. SINGLE_STAGE_BUILD (multi_stage.py)

**목적**: 빌드 전용 이미지나 빌드 도구를 single-stage로 사용하는 경우 탐지

#### 탐지 조건 (둘 중 하나)

```python
# 조건 1: 베이스 이미지가 빌드 전용
_BUILD_BASE_PATTERNS = [
    r"^golang:",
    r"^rust:",
    r"^maven:",
    r"^gradle:",
    r"^eclipse-temurin:\d+-jdk",
    r"^openjdk:\d+-jdk",
    r"^mcr\.microsoft\.com/dotnet/sdk:",
]

# 조건 2: RUN에 빌드 도구 패키지 포함
_BUILD_TOOL_PACKAGES = [
    "gcc", "g++", "make", "cmake", "build-essential",
    "maven", "gradle", "cargo", "rustc",
]
```

`is_multi_stage`가 True면 이 규칙은 실행하지 않는다. 이미 multi-stage를 쓰고 있으면 해당 없음.

#### 언어 감지 후 템플릿 선택

```
이미지명 기반 (우선순위 높음):
  golang:  → "go"
  rust:    → "rust"
  dotnet/sdk: → "dotnet"
  eclipse-temurin/openjdk: → "java"
  node:    → "node"

RUN 텍스트 기반 (이미지명만으로 모를 때):
  mvn/gradle 키워드 → "java"

모두 해당 없음 → "generic"
```

#### 언어별 템플릿 최적화 포인트

**Go** → `scratch` (가장 작은 런타임):
```dockerfile
FROM golang:1.22-alpine AS builder
RUN CGO_ENABLED=0 GOOS=linux go build -o /app/server .
FROM scratch                          # 바이너리만 복사, OS 불필요
COPY --from=builder /app/server /server
```
Go는 정적 링킹 컴파일이 가능하므로 OS가 전혀 없는 `scratch`를 쓸 수 있다.

**Rust** → `debian:bookworm-slim`:
```dockerfile
FROM rust:slim AS builder
FROM debian:bookworm-slim             # glibc 필요 (Rust 기본은 동적 링킹)
```
Rust는 기본 빌드가 glibc 동적 링킹을 사용하므로 glibc가 있는 debian-slim이 필요하다. 완전 정적 빌드(`RUSTFLAGS=-C target-feature=+crt-static`)하면 scratch도 가능하다.

**Java** → `eclipse-temurin:jre` (JDK → JRE):
```dockerfile
FROM eclipse-temurin:17-jdk AS builder
RUN mvn package -DskipTests
FROM eclipse-temurin:17-jre          # JDK(컴파일러)→JRE(런타임만), ~150MB 절감
COPY --from=builder /app/target/*.jar /app/app.jar
```

**Node** → devDependencies 제거:
```dockerfile
FROM node:20-alpine AS builder
RUN npm ci                            # lockfile 기반 재현성
RUN npm run build
FROM node:20-alpine
RUN npm ci --omit=dev                 # devDependencies 제외
```

---

## 5. 추천 엔진 (recommender.py)

`Finding` 목록을 받아서 수정된 Dockerfile 문자열을 반환한다.

### 처리 단계

#### 1단계: Patch 적용 (줄 직접 교체)

```python
patches.sort(key=lambda p: p.line_no, reverse=True)  # 내림차순!
for patch in patches:
    lines[patch.line_no - 1] = patch.new_text
```

**내림차순으로 처리하는 이유** (offset drift 방지):

```
원본:
  1: FROM python:3.11
  2: RUN pip install flask
  3: COPY . .

줄 1을 먼저 교체하면 → 라인 수 그대로
줄 3에 주석 2줄 삽입하면 → 줄 번호 shift 없음

반면 줄 1에 주석 삽입 먼저 하면:
  원래 2번이 4번으로 밀림 → 그 다음 처리할 때 잘못된 위치
```

#### 2단계: apt → apk 번역

Alpine 베이스 이미지로 교체되면, 단순한 `apt-get install` 명령을 `apk add --no-cache`로 자동 변환한다.

```python
# build-base가 있으면 중복 제거 (build-base가 gcc/g++/make를 포함)
if "build-base" in packages:
    packages = [pkg for pkg in packages if pkg not in {"gcc", "g++", "make"}]
```

복잡한 shell 로직이 있거나 모르는 패키지가 있으면 변환하지 않고 원본 유지.

#### 3단계: 인라인 주석 삽입

Patch 없는 Finding(자동 수정 불가)은 해당 줄 위에 주석을 삽입한다:

```dockerfile
# [imgadvisor:FAIL] BUILD_TOOLS_IN_FINAL_STAGE
#   build tools found in final stage: `gcc`
#   Use multi-stage build to remove build tools from runtime:
RUN apt-get install -y gcc
```

주석도 내림차순으로 삽입 (같은 이유: offset drift 방지).

#### 4단계: multi-stage 템플릿 추가

`SINGLE_STAGE_BUILD` Finding이 있을 때만 파일 하단에 언어별 템플릿을 주석 블록으로 추가한다.

---

## 6. 레이어 분석기 (layer_analyzer.py)

### docker history 출력 형식

Docker 버전에 따라 두 가지 형식이 있다:

**Legacy 형식:**
```
/bin/sh -c apt-get install -y gcc          → RUN
/bin/sh -c #(nop)  CMD ["python"]          → CMD (nop = no operation, 파일시스템 변경 없음)
/bin/sh -c #(nop) WORKDIR /app             → WORKDIR
```

**BuildKit 형식 (newer):**
```
RUN /bin/sh -c apt-get install -y gcc # buildkit   → RUN
COPY ./src /app # buildkit                          → COPY
```

`_clean_created_by()` 함수가 두 형식을 모두 처리해서 `(instruction_type, display_text)`로 반환한다.

### image size vs uncompressed layer content

```
  image size 150.7 MB  uncompressed layer content 453.8 MB
```

두 숫자가 다른 이유:

- **image size** (`docker inspect Size`): union filesystem 적용 후 실제 이미지 크기. 레이어 간 중복 파일 제거, whiteout(삭제 마커) 처리 후 값.
- **uncompressed layer content** (docker history 합산): 각 레이어가 파일시스템에 **추가한** 비압축 데이터의 합. 나중에 다른 레이어에서 삭제되는 파일도 포함됨.

예: `apk add build-base`(290 MB 추가) → `apk del build-base`(0 MB 추가, whiteout만 기록) 패턴이면 history 합산에는 290 MB가 잡히지만 최종 이미지에는 남지 않는다.

### 레이어 퍼센트 계산

```python
def size_pct(self, layer):
    total = self.history_total_bytes   # history 합산을 분모로 사용
    return layer.size_bytes / total * 100
```

`docker inspect`를 분모로 쓰면 단일 레이어 퍼센트가 100%를 초과하는 경우가 생기므로, history 합산을 분모로 써서 퍼센트가 일관되게 나오도록 한다.

---

## 7. FAIL vs WARN 기준

### 핵심 논리

```
구조적 문제 (설계를 바꿔야 해결됨)   →  FAIL (HIGH)
습관적 문제 (명령어 하나 추가하면 됨) →  WARN (MEDIUM)
```

### FAIL로 분류하는 조건

| 규칙 | FAIL인 이유 |
|---|---|
| `BASE_IMAGE_NOT_OPTIMIZED` | 베이스 이미지 자체가 수백 MB 낭비. FROM 한 줄 교체가 필요한 구조적 결정 |
| `BUILD_TOOLS_IN_FINAL_STAGE` | 런타임에 전혀 불필요한 도구가 포함됨. 멀티스테이지 분리가 필요 |
| `BROAD_COPY_SCOPE` (.dockerignore 없음) | `.git`, `node_modules`, `.env`, secrets가 이미지에 포함될 수 있음. 보안 위험 |
| `SINGLE_STAGE_BUILD` | 빌드 전용 이미지를 런타임까지 그대로 사용. 멀티스테이지 재설계 필요 |

### WARN으로 분류하는 조건

| 규칙 | WARN인 이유 |
|---|---|
| `APT_CACHE_NOT_CLEANED` 등 캐시 규칙 12개 | 캐시가 레이어에 남는 건 실수지만, 설계 자체는 문제없음. `&& rm -rf ...` 한 줄 추가로 해결 |
| `BROAD_COPY_SCOPE` (.dockerignore 있음) | 보안 위험은 줄었음. 명시적 경로가 더 나은 습관이지만 구조적 문제는 아님 |

### CI 동작과의 연결

```
FAIL 존재  →  exit code 1  →  CI 파이프라인 차단
WARN만 존재 →  exit code 1  →  동일하게 차단 (모든 finding이 차단)
finding 없음 →  exit code 0  →  통과
```

> 현재는 FAIL/WARN 모두 exit 1을 반환한다. WARN을 통과시키고 싶으면
> `imgadvisor analyze -f Dockerfile --json`으로 출력 후 직접 파싱하거나,
> 향후 `--fail-on FAIL` 옵션 추가를 고려할 수 있다.

### 새 규칙 심각도 결정 기준

새 규칙을 추가할 때 심각도를 결정하는 기준:

- **FAIL**: 수정하려면 Dockerfile 구조를 바꿔야 하거나, 보안 위험이 있거나, 절감량이 100 MB 이상
- **WARN**: 명령어 플래그 하나 추가/수정으로 해결되고, 보안 위험 없고, 절감량이 수십 MB 수준

---

## 8. Finding이 없을 때 (이미 최적화된 경우)

각 규칙이 스킵하는 조건 요약:

| 규칙 | 스킵 조건 |
|---|---|
| BASE_IMAGE_NOT_OPTIMIZED | `-slim`, `-alpine`, `distroless`, `scratch`, `busybox`, `[stage:*]` |
| BUILD_TOOLS_IN_FINAL_STAGE | final stage RUN에 `_BUILD_TOOLS` 패턴 없음 |
| 캐시 규칙들 | install 명령 없거나 같은 RUN에 cleanup 패턴 있음 |
| BROAD_COPY_SCOPE | `COPY . .` 패턴 없거나 `--from=` COPY만 있음 |
| SINGLE_STAGE_BUILD | 이미 multi-stage(`is_multi_stage=True`) 이거나 빌드 도구/이미지 없음 |

---

## 8. 규칙 추가하는 법

1. `imgadvisor/rules/my_rule.py` 생성
2. `check(ir: DockerfileIR) -> list[Finding]` 함수 구현
3. `analyzer.py`의 `_ALL_RULES`에 `my_rule.check` 추가

```python
# my_rule.py 예시
def check(ir: DockerfileIR) -> list[Finding]:
    final = ir.final_stage
    if final is None:
        return []

    # final stage RUN 텍스트에서 패턴 탐지
    for instr in final.run_instructions:
        if re.search(r"some_pattern", instr.arguments):
            return [Finding(
                rule_id="MY_RULE",
                severity=Severity.MEDIUM,
                line_no=instr.line_no,
                description="설명",
                recommendation="수정 방법",
                saving_min_mb=10,
                saving_max_mb=50,
            )]
    return []
```
