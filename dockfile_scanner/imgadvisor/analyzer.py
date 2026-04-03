"""
Run all analysis rules and collect findings.

The analyzer stays intentionally simple: each rule receives the parsed
Dockerfile IR and returns zero or more findings. Rule order affects how the
CLI presents results, so Python-specific runtime findings are shown before the
larger multi-stage rewrite suggestion.
"""
from __future__ import annotations

from imgadvisor.models import DockerfileIR, Finding
from imgadvisor.rules import (
    base_image,
    build_tools,
    cache_cleanup,
    copy_scope,
    multi_stage,
    python_runtime,
)

_ALL_RULES = [
    base_image.check,
    build_tools.check,
    cache_cleanup.check,
    python_runtime.check,
    copy_scope.check,
    multi_stage.check,
]


def analyze(ir: DockerfileIR) -> list[Finding]:
    """Execute all registered rules in order and return their combined findings."""
    findings: list[Finding] = []
    for rule in _ALL_RULES:
        findings.extend(rule(ir))
    return findings
