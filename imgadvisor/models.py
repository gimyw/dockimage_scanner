"""
imgadvisor 전체에서 공유하는 데이터 모델 정의.

parser → analyzer → rules → display/recommender 순으로 데이터가 흐르며,
모든 컴포넌트가 이 모듈의 클래스를 공통 인터페이스로 사용한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    """
    탐지 결과의 심각도 등급.

    - HIGH   : 반드시 수정해야 하는 문제 (빌드 도구 잔존, 단일 스테이지 등)
    - MEDIUM : 수정하면 이미지 크기가 줄어드는 문제 (캐시 미정리, 광범위 COPY 등)
    - LOW    : 권고 수준의 개선 사항
    """
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass
class DockerInstruction:
    """
    Dockerfile에서 파싱된 단일 명령어.

    백슬래시 줄 이어쓰기는 파싱 시점에 이미 합쳐지므로,
    `arguments`는 RUN 명령 전체 내용을 하나의 문자열로 담고 있다.

    Attributes:
        line_no     : Dockerfile 원본 파일에서의 줄 번호 (1-based)
        instruction : 명령어 키워드 (FROM, RUN, COPY, ADD, ENV, ...)
        arguments   : 명령어 뒤의 인수 전체 (변수 치환 완료)
        stage_index : 소속 스테이지 인덱스 (0-based)
        raw         : 줄 이어쓰기 합쳐진 원본 텍스트 (디버그용)
    """
    line_no: int
    instruction: str   # FROM, RUN, COPY, ADD, ...
    arguments: str
    stage_index: int
    raw: str


@dataclass
class Stage:
    """
    Dockerfile 내 하나의 빌드 스테이지 (FROM 블록 하나).

    멀티-스테이지 빌드라면 여러 Stage가 존재하며, 마지막 Stage만
    is_final=True 가 된다. 분석 규칙 대부분은 final stage만 검사한다.

    Attributes:
        index        : 스테이지 순서 (0-based)
        base_image   : FROM 뒤의 이미지 이름 (ARG 치환 후)
        alias        : AS 로 지정된 스테이지 이름 (없으면 None)
        is_final     : 마지막(런타임) 스테이지 여부
        instructions : 이 스테이지의 모든 명령어 목록
    """
    index: int
    base_image: str
    alias: Optional[str]
    is_final: bool = False
    instructions: list[DockerInstruction] = field(default_factory=list)

    @property
    def run_instructions(self) -> list[DockerInstruction]:
        """RUN 명령어만 필터링해서 반환."""
        return [i for i in self.instructions if i.instruction == "RUN"]

    @property
    def copy_instructions(self) -> list[DockerInstruction]:
        """COPY 명령어만 필터링해서 반환."""
        return [i for i in self.instructions if i.instruction == "COPY"]

    @property
    def all_run_text(self) -> str:
        """
        이 스테이지의 모든 RUN arguments를 공백으로 이어 붙인 문자열.

        여러 RUN 명령어에 걸쳐 패키지 설치/캐시 정리 여부를 한 번에
        정규식으로 검색할 때 사용한다.
        """
        return " ".join(i.arguments for i in self.run_instructions)


@dataclass
class DockerfileIR:
    """
    Dockerfile 전체를 표현하는 중간 표현(Intermediate Representation).

    parser.parse()가 생성하며, analyzer와 모든 rule 함수에 전달된다.

    Attributes:
        stages          : 파싱된 스테이지 목록 (FROM 순서대로)
        raw_lines       : 원본 파일 줄 목록 (recommender가 패치할 때 사용)
        path            : Dockerfile 파일 경로 (표시용)
        has_dockerignore: Dockerfile 옆에 .dockerignore가 있는지 여부
    """
    stages: list[Stage]
    raw_lines: list[str]
    path: str
    has_dockerignore: bool = False

    @property
    def final_stage(self) -> Optional[Stage]:
        """마지막 스테이지(런타임 이미지)를 반환. 스테이지가 없으면 None."""
        return self.stages[-1] if self.stages else None

    @property
    def is_multi_stage(self) -> bool:
        """멀티-스테이지 빌드 여부 (FROM 블록이 2개 이상이면 True)."""
        return len(self.stages) > 1


@dataclass
class Patch:
    """
    Dockerfile 한 줄을 다른 텍스트로 교체하는 최소 패치.

    recommender가 base_image 교체처럼 단순 줄 치환이 가능한 경우에만
    Finding에 Patch를 첨부한다. 패치가 없는 Finding은 inline 주석으로만 안내한다.

    Attributes:
        line_no  : 교체할 줄 번호 (1-based)
        old_text : 교체 전 원본 텍스트 (일치하는 경우에만 패치 적용)
        new_text : 교체 후 텍스트
    """
    line_no: int      # 1-based
    old_text: str
    new_text: str


@dataclass
class Finding:
    """
    분석 규칙 하나가 탐지한 문제 하나.

    각 rule 함수는 Finding 목록을 반환하며, display 모듈이 이를 출력하고
    recommender 모듈이 이를 참고해 최적화 Dockerfile을 생성한다.

    Attributes:
        rule_id        : 규칙 식별자 (예: BASE_IMAGE_NOT_OPTIMIZED)
        severity       : 심각도 (HIGH / MEDIUM / LOW)
        line_no        : 문제가 발생한 Dockerfile 줄 번호 (없으면 None)
        description    : 문제 요약 (한 줄)
        recommendation : 해결 방법 (멀티라인 가능)
        saving_min_mb  : 예상 절감 용량 최솟값 (MB)
        saving_max_mb  : 예상 절감 용량 최댓값 (MB)
        patch          : 자동 적용 가능한 줄 교체 패치 (없으면 None)
    """
    rule_id: str
    severity: Severity
    line_no: Optional[int]
    description: str
    recommendation: str
    saving_min_mb: int
    saving_max_mb: int
    patch: Optional[Patch] = None

    @property
    def saving_display(self) -> str:
        """절감 용량을 '최소 ~ 최대 MB' 형태 문자열로 반환. 0이면 '-'."""
        if self.saving_min_mb == 0 and self.saving_max_mb == 0:
            return "-"
        return f"{self.saving_min_mb:,} ~ {self.saving_max_mb:,} MB"


@dataclass
class ValidationResult:
    """
    원본 vs 최적화 Dockerfile 실제 빌드 후 비교 결과.

    validator.validate()가 Docker 데몬을 통해 두 이미지를 빌드하고
    크기와 레이어 수를 비교한 값을 담는다.

    Attributes:
        original_size_mb  : 원본 이미지 크기 (MB)
        optimized_size_mb : 최적화 이미지 크기 (MB)
        original_layers   : 원본 이미지 레이어 수
        optimized_layers  : 최적화 이미지 레이어 수
    """
    original_size_mb: float
    optimized_size_mb: float
    original_layers: int
    optimized_layers: int

    @property
    def delta_mb(self) -> float:
        """절감된 용량 (원본 - 최적화, MB)."""
        return self.original_size_mb - self.optimized_size_mb

    @property
    def reduction_pct(self) -> float:
        """절감 비율 (%). 원본 크기가 0이면 0.0 반환."""
        if self.original_size_mb == 0:
            return 0.0
        return (self.delta_mb / self.original_size_mb) * 100


@dataclass
class TrivyFinding:
    """
    Trivy가 탐지한 취약점 또는 Dockerfile 설정 문제 하나.

    scanner 필드로 출처를 구분한다:
    - "config" : `trivy config` 로 발견된 Dockerfile 설정 문제
    - "fs"     : `trivy fs` 로 발견된 의존성 취약점 (CVE)

    Attributes:
        scanner           : 스캐너 종류 ("config" | "fs")
        target            : 스캔 대상 파일 경로 또는 패키지 경로
        severity          : 심각도 문자열 (CRITICAL / HIGH / MEDIUM / LOW / UNKNOWN)
        rule_id           : 취약점 또는 규칙 ID (예: CVE-2024-1234, DS026)
        title             : 문제 제목 (한 줄)
        description       : 상세 설명
        recommendation    : 해결 방법
        primary_url       : 상세 정보 참고 URL (없으면 None)
        pkg_name          : 취약한 패키지 이름 (fs 스캔 시)
        installed_version : 현재 설치된 버전 (fs 스캔 시)
        fixed_version     : 취약점이 수정된 버전 (없으면 None)
        line_no           : Dockerfile 내 문제 줄 번호 (config 스캔 시)
        file_path         : 문제가 발견된 파일 경로 (config 스캔 시)
    """
    # `scanner` distinguishes whether the finding came from `trivy config`
    # or `trivy fs`, which helps the CLI present the result in two clear groups.
    scanner: str
    target: str
    severity: str
    rule_id: str
    title: str
    description: str
    recommendation: str
    primary_url: Optional[str] = None
    pkg_name: Optional[str] = None
    installed_version: Optional[str] = None
    fixed_version: Optional[str] = None
    line_no: Optional[int] = None
    file_path: Optional[str] = None


@dataclass
class TrivyScanResult:
    """
    Trivy 스캔 전체 결과.

    trivy_scanner.scan()이 반환하며, display 모듈이 출력에 사용한다.

    Attributes:
        dockerfile_path : 분석 대상 Dockerfile 경로
        context_dir     : 빌드 컨텍스트 디렉토리 (Dockerfile 위치 기준)
        findings        : 탐지된 모든 TrivyFinding 목록
    """
    dockerfile_path: str
    context_dir: str
    findings: list[TrivyFinding]

    @property
    def config_findings(self) -> list[TrivyFinding]:
        """Dockerfile 설정 문제만 반환 (trivy config 결과)."""
        return [finding for finding in self.findings if finding.scanner == "config"]

    @property
    def fs_findings(self) -> list[TrivyFinding]:
        """의존성 취약점만 반환 (trivy fs 결과)."""
        return [finding for finding in self.findings if finding.scanner == "fs"]

    @property
    def total_findings(self) -> int:
        """전체 탐지 건수."""
        return len(self.findings)
