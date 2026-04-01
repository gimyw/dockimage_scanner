from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass
class DockerInstruction:
    line_no: int
    instruction: str   # FROM, RUN, COPY, ADD, ...
    arguments: str
    stage_index: int
    raw: str


@dataclass
class Stage:
    index: int
    base_image: str
    alias: Optional[str]
    is_final: bool = False
    instructions: list[DockerInstruction] = field(default_factory=list)

    @property
    def run_instructions(self) -> list[DockerInstruction]:
        return [i for i in self.instructions if i.instruction == "RUN"]

    @property
    def copy_instructions(self) -> list[DockerInstruction]:
        return [i for i in self.instructions if i.instruction == "COPY"]

    @property
    def all_run_text(self) -> str:
        """모든 RUN args를 공백으로 이어 붙인 문자열 (패턴 매칭용)."""
        return " ".join(i.arguments for i in self.run_instructions)


@dataclass
class DockerfileIR:
    stages: list[Stage]
    raw_lines: list[str]
    path: str
    has_dockerignore: bool = False

    @property
    def final_stage(self) -> Optional[Stage]:
        return self.stages[-1] if self.stages else None

    @property
    def is_multi_stage(self) -> bool:
        return len(self.stages) > 1


@dataclass
class Patch:
    """단순 텍스트 치환 패치 — recommender가 Dockerfile 수정에 사용."""
    line_no: int      # 1-based
    old_text: str
    new_text: str


@dataclass
class Finding:
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
        if self.saving_min_mb == 0 and self.saving_max_mb == 0:
            return "-"
        return f"{self.saving_min_mb:,} ~ {self.saving_max_mb:,} MB"


@dataclass
class ValidationResult:
    original_size_mb: float
    optimized_size_mb: float
    original_layers: int
    optimized_layers: int

    @property
    def delta_mb(self) -> float:
        return self.original_size_mb - self.optimized_size_mb

    @property
    def reduction_pct(self) -> float:
        if self.original_size_mb == 0:
            return 0.0
        return (self.delta_mb / self.original_size_mb) * 100


@dataclass
class TrivyFinding:
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
    dockerfile_path: str
    context_dir: str
    findings: list[TrivyFinding]

    @property
    def config_findings(self) -> list[TrivyFinding]:
        return [finding for finding in self.findings if finding.scanner == "config"]

    @property
    def fs_findings(self) -> list[TrivyFinding]:
        return [finding for finding in self.findings if finding.scanner == "fs"]

    @property
    def total_findings(self) -> int:
        return len(self.findings)
