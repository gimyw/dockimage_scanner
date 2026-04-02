"""
Docker layer analysis via `docker history`.

Builds the target Dockerfile with a temporary tag, runs `docker history`
to extract per-layer sizes, then cleans up the temporary image.

Requires a running Docker daemon.
Flow:
  analyze(path) → build temp image → docker history → parse → LayerAnalysis
                → docker rmi temp image (cleanup in finally)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field


@dataclass
class LayerEntry:
    """
    One layer from `docker history`.

    Attributes:
        size_bytes  : Layer delta size in bytes (0 for metadata-only layers)
        instruction : Dockerfile keyword — RUN / COPY / ADD / ENV / CMD / ...
        display_cmd : Human-readable command text (truncated to 72 chars)
        raw         : Original CreatedBy string from docker history (debug)
    """
    size_bytes: int
    instruction: str
    display_cmd: str
    raw: str


@dataclass
class LayerAnalysis:
    """
    Full layer breakdown of a built image.

    Attributes:
        image_tag       : Temporary tag used to build the image
        dockerfile_path : Path to the analyzed Dockerfile
        total_bytes     : Total image size from `docker inspect` (bytes)
        layers          : All layers, newest-first (same order as docker history)
    """
    image_tag: str
    dockerfile_path: str
    total_bytes: int
    layers: list[LayerEntry] = field(default_factory=list)

    @property
    def total_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)

    @property
    def layer_count(self) -> int:
        return len(self.layers)

    @property
    def nonempty_layers(self) -> list[LayerEntry]:
        """Layers with non-zero size (actual filesystem changes)."""
        return [l for l in self.layers if l.size_bytes > 0]

    def size_pct(self, layer: LayerEntry) -> float:
        """Percentage of total image size this layer contributes."""
        # Use sum of history sizes as denominator for consistency
        total = sum(l.size_bytes for l in self.layers)
        if total == 0:
            return 0.0
        return layer.size_bytes / total * 100


def analyze(dockerfile_path: str) -> LayerAnalysis:
    """
    Build the Dockerfile and analyze its layers.

    Builds a temporary image, collects layer data, then always removes the
    temporary image in a finally block regardless of success or failure.

    Args:
        dockerfile_path: Path to the Dockerfile to analyze

    Returns:
        LayerAnalysis with per-layer breakdown

    Raises:
        RuntimeError: If Docker build fails or docker daemon is not running
    """
    tag = f"imgadvisor-layers-{uuid.uuid4().hex[:8]}"
    try:
        _build(dockerfile_path, tag)
        total_bytes = _inspect_total_size(tag)
        layers = _parse_history(tag)
        return LayerAnalysis(
            image_tag=tag,
            dockerfile_path=dockerfile_path,
            total_bytes=total_bytes,
            layers=layers,
        )
    finally:
        _cleanup(tag)


# ── internal helpers ────────────────────────────────────────────────────────

def _build(dockerfile_path: str, tag: str) -> None:
    """Build a Dockerfile and tag the result."""
    context_dir = os.path.dirname(os.path.abspath(dockerfile_path))
    result = subprocess.run(
        ["docker", "build", "-f", os.path.abspath(dockerfile_path), "-t", tag, context_dir],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Docker build failed:\n{result.stderr[-2000:]}")


def _inspect_total_size(tag: str) -> int:
    """Get total image size in bytes via `docker image inspect`."""
    result = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)[0]
    return data["Size"]


def _parse_history(tag: str) -> list[LayerEntry]:
    """
    Run `docker history --no-trunc` and parse each layer.

    Returns layers in docker history order (newest first).
    """
    result = subprocess.run(
        ["docker", "history", "--no-trunc",
         "--format", "{{.Size}}\t{{.CreatedBy}}", tag],
        capture_output=True, text=True, check=True,
    )
    entries: list[LayerEntry] = []
    for line in result.stdout.strip().splitlines():
        if "\t" not in line:
            continue
        size_str, created_by = line.split("\t", 1)
        size_bytes = _parse_size(size_str.strip())
        instruction, display_cmd = _clean_created_by(created_by.strip())
        entries.append(LayerEntry(
            size_bytes=size_bytes,
            instruction=instruction,
            display_cmd=display_cmd,
            raw=created_by,
        ))
    return entries


def _parse_size(size_str: str) -> int:
    """
    Convert docker history size string to bytes.

    Docker uses SI units in history output:
    "0B" → 0, "4.96kB" → 4960, "54.9MB" → 54900000, "1.23GB" → 1230000000

    Returns 0 for unparseable strings.
    """
    s = size_str.strip().upper().replace(" ", "")
    if s in ("0", "0B", ""):
        return 0
    m = re.match(r"^([\d.]+)([KMGT]?I?B?)$", s)
    if not m:
        return 0
    value = float(m.group(1))
    unit = m.group(2)
    # Docker history uses SI (1 kB = 1000 B), not IEC (1024)
    _mult: dict[str, int] = {
        "B": 1, "": 1,
        "KB": 1_000,       "MB": 1_000_000,       "GB": 1_000_000_000,
        "KIB": 1_024,      "MIB": 1_048_576,      "GIB": 1_073_741_824,
    }
    return int(value * _mult.get(unit, 1))


def _clean_created_by(raw: str) -> tuple[str, str]:
    """
    Parse the CreatedBy field from docker history into (instruction, display_cmd).

    Docker produces two formats depending on builder version:

    BuildKit (newer):
        "RUN /bin/sh -c pip install flask # buildkit"
        "COPY src /app # buildkit"

    Legacy:
        "/bin/sh -c apt-get install -y gcc"          → RUN
        "/bin/sh -c #(nop)  CMD [\"python\", \"app\"]"  → CMD
        "/bin/sh -c #(nop) WORKDIR /app"             → WORKDIR
    """
    raw = raw.strip()

    # BuildKit: instruction keyword leads, optional "# buildkit" trailer
    _KEYWORDS = (
        "RUN", "COPY", "ADD", "ENV", "WORKDIR", "CMD", "ENTRYPOINT",
        "USER", "ARG", "LABEL", "EXPOSE", "HEALTHCHECK", "SHELL", "VOLUME",
    )
    for kw in _KEYWORDS:
        if raw.upper().startswith(kw + " ") or raw.upper().startswith(kw + "\t"):
            cmd = raw[len(kw):].strip()
            cmd = re.sub(r"\s*#\s*buildkit\s*$", "", cmd, flags=re.IGNORECASE).strip()
            # BuildKit RUN still prefixes /bin/sh -c
            cmd = re.sub(r"^/bin/sh\s+-c\s+", "", cmd)
            return kw, _truncate(cmd)

    # Legacy: /bin/sh -c #(nop) INSTRUCTION args
    nop = re.match(r"/bin/sh\s+-c\s+#\(nop\)\s+(\w+)\s+(.*)", raw, re.DOTALL)
    if nop:
        instr = nop.group(1).upper()
        return instr, _truncate(nop.group(2).strip())

    # Legacy RUN: /bin/sh -c <command>
    run = re.match(r"/bin/sh\s+-c\s+(.*)", raw, re.DOTALL)
    if run:
        return "RUN", _truncate(run.group(1).strip())

    return "LAYER", _truncate(raw)


def _truncate(text: str, max_len: int = 72) -> str:
    """Collapse internal whitespace and truncate to max_len characters."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _cleanup(tag: str) -> None:
    """Silently remove the temporary image (ignores errors)."""
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
