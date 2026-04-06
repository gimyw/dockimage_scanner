from __future__ import annotations

import json
import sys

from rich.console import Console
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich import box

from imgadvisor.models import (
    DockerfileIR,
    Finding,
    Severity,
    ValidationResult,
)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

console = Console()

_LABEL = {
    Severity.HIGH:   ("[bold red]FAIL[/bold red]",   "red"),
    Severity.MEDIUM: ("[bold yellow]WARN[/bold yellow]", "yellow"),
    Severity.LOW:    ("[bold cyan]INFO[/bold cyan]",  "cyan"),
}


def print_analysis(ir: DockerfileIR, findings: list[Finding]) -> None:
    console.print()

    # ── header ───────────────────────────────────────────────────────────────
    stage_info = (
        "[green]multi-stage[/green]" if ir.is_multi_stage
        else "[yellow]single-stage[/yellow]"
    )
    di_info = "[green]yes[/green]" if ir.has_dockerignore else "[red]no[/red]"
    base = ir.final_stage.base_image if ir.final_stage else "unknown"

    console.print(f"  [bold]imgadvisor[/bold]  [dim]{ir.path}[/dim]")
    console.print(
        f"  [dim]base[/dim] [bold]{base}[/bold]  "
        f"[dim]stages[/dim] {len(ir.stages)} ({stage_info})  "
        f"[dim].dockerignore[/dim] {di_info}"
    )
    console.print()

    # ── no issues ────────────────────────────────────────────────────────────
    if not findings:
        console.print("  [bold green]No issues found.[/bold green]")
        console.print()
        return

    # ── findings ─────────────────────────────────────────────────────────────
    console.print(Rule(style="dim"))

    for f in findings:
        _print_finding(f)

    # ── summary ──────────────────────────────────────────────────────────────
    console.print(Rule(style="dim"))

    fail_n = sum(1 for f in findings if f.severity == Severity.HIGH)
    warn_n = sum(1 for f in findings if f.severity == Severity.MEDIUM)

    parts: list[str] = []
    if fail_n:
        parts.append(f"[bold red]{fail_n} failures[/bold red]")
    if warn_n:
        parts.append(f"[bold yellow]{warn_n} warnings[/bold yellow]")

    console.print(f"  {'  '.join(parts)}")
    console.print(
        f"  [dim]run:[/dim] imgadvisor recommend -f {ir.path}"
    )
    console.print()


def _print_finding(f: Finding) -> None:
    label, color = _LABEL.get(f.severity, ("[dim]INFO[/dim]", "dim"))
    line_str = f"line {f.line_no:>3}" if f.line_no else "        "

    # first line: severity + line + rule id
    console.print(f"  {label}  [dim]{line_str}[/dim]  [bold]{f.rule_id}[/bold]")

    # description (one line)
    desc = f.description.replace("`", "")
    console.print(f"           [dim]{desc}[/dim]")

    # recommendation (first meaningful line only — keep it compact)
    rec_lines = [l.strip() for l in f.recommendation.splitlines() if l.strip()]
    if rec_lines:
        first = rec_lines[0].lstrip("-> ").rstrip(" \\").strip()
        if len(first) > 60:
            first = first[:57] + "..."
        console.print(f"           [dim]fix:[/dim] {first}")

    console.print()


def print_recommend_summary(ir: DockerfileIR, findings: list[Finding]) -> None:
    """One-line summary shown before the optimized Dockerfile in recommend mode."""
    fail_n = sum(1 for f in findings if f.severity == Severity.HIGH)
    warn_n = sum(1 for f in findings if f.severity == Severity.MEDIUM)

    parts: list[str] = []
    if fail_n:
        parts.append(f"[bold red]{fail_n} FAIL[/bold red]")
    if warn_n:
        parts.append(f"[bold yellow]{warn_n} WARN[/bold yellow]")

    console.print()
    console.print(
        f"  [bold]imgadvisor[/bold]  [dim]{ir.path}[/dim]  "
        + "  ".join(parts)
    )


def print_recommended_dockerfile(content: str) -> None:
    console.print()
    console.print(Rule("optimized dockerfile", style="dim"))
    console.print(Syntax(content, "dockerfile", theme="monokai", line_numbers=True))
    console.print()


def print_validation(result: ValidationResult) -> None:
    console.print()
    tbl = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    tbl.add_column("", style="dim")
    tbl.add_column("original",  justify="right")
    tbl.add_column("optimized", justify="right", style="green")
    tbl.add_column("saved",     justify="right")

    size_delta  = result.original_size_mb - result.optimized_size_mb
    layer_delta = result.original_layers  - result.optimized_layers
    time_delta  = result.original_build_time_s - result.optimized_build_time_s

    tbl.add_row(
        "image size",
        f"{result.original_size_mb:.1f} MB",
        f"{result.optimized_size_mb:.1f} MB",
        f"[bold green]-{size_delta:.1f} MB ({result.reduction_pct:.1f}%)[/bold green]",
    )
    tbl.add_row(
        "layers",
        str(result.original_layers),
        str(result.optimized_layers),
        (f"[bold green]-{layer_delta}[/bold green]" if layer_delta > 0
         else f"[yellow]{layer_delta:+}[/yellow]"),
    )
    tbl.add_row(
        "build time",
        f"{result.original_build_time_s:.1f}s",
        f"{result.optimized_build_time_s:.1f}s",
        (f"[bold green]-{time_delta:.1f}s[/bold green]" if time_delta > 0
         else f"[yellow]{time_delta:+.1f}s[/yellow]"),
    )
    console.print(tbl)
    console.print()



def print_layers(analysis: "LayerAnalysis") -> None:  # type: ignore[name-defined]
    """
    Compact layer breakdown from docker history.

    Shows each layer's size, percentage, instruction type, and command.
    Layers >= 50 MB are flagged with [!]. Zero-size metadata layers
    (ENV, CMD, WORKDIR, etc.) are grouped into a footer count.
    """
    from imgadvisor.layer_analyzer import LayerAnalysis  # local import avoids circular

    _LARGE_THRESHOLD = 50 * 1_000_000  # 50 MB

    nonempty = analysis.nonempty_layers
    zero_count = analysis.layer_count - len(nonempty)
    history_mb = analysis.history_total_bytes / 1_000_000

    console.print()
    console.print(f"  [bold]imgadvisor[/bold]  [dim]{analysis.dockerfile_path}[/dim]")
    console.print(
        f"  [dim]image size[/dim] [bold]{analysis.total_mb:.1f} MB[/bold]  "
        f"[dim]layers[/dim] {analysis.layer_count}  "
        f"[dim]uncompressed layer content[/dim] {history_mb:.1f} MB  "
        f"[dim]build[/dim] [bold]{analysis.build_time_s:.1f}s[/bold]"
    )
    # Note: docker history sizes are uncompressed layer deltas and may sum to more
    # than the final image size due to union filesystem deduplication/whiteouts.
    console.print()
    console.print(Rule(style="dim"))

    for layer in nonempty:
        pct = analysis.size_pct(layer)
        mb = layer.size_bytes / 1_000_000
        large_flag = "  [bold red][!][/bold red]" if layer.size_bytes >= _LARGE_THRESHOLD else ""

        console.print(
            f"  [green]{pct:5.1f}%[/green]  "
            f"[bold]{mb:7.1f} MB[/bold]  "
            f"[dim]{layer.instruction:<12}[/dim]  "
            f"{layer.display_cmd}"
            + large_flag
        )

    if zero_count:
        console.print(
            f"  [dim]  0.0%      0.0 MB  "
            f"({zero_count} metadata layers: ENV / CMD / WORKDIR / EXPOSE / ...)[/dim]"
        )

    console.print(Rule(style="dim"))

    large_layers = [l for l in nonempty if l.size_bytes >= _LARGE_THRESHOLD]
    if large_layers:
        large_mb = sum(l.size_bytes for l in large_layers) / 1_000_000
        large_pct = sum(analysis.size_pct(l) for l in large_layers)
        console.print(
            f"  [bold red]{len(large_layers)} large layer{'s' if len(large_layers) > 1 else ''}[/bold red]"
            f"  [dim](>50 MB)[/dim]  [bold]{large_mb:.1f} MB[/bold]"
            f"  [dim]({large_pct:.1f}% of uncompressed layers)[/dim]"
        )
    else:
        console.print("  [bold green]No large layers detected.[/bold green]")

    console.print(f"  [dim]run:[/dim] imgadvisor recommend -f {analysis.dockerfile_path}")
    console.print()


def print_json_result(ir: DockerfileIR, findings: list[Finding]) -> None:
    data = {
        "dockerfile": ir.path,
        "stages": len(ir.stages),
        "is_multi_stage": ir.is_multi_stage,
        "final_image": ir.final_stage.base_image if ir.final_stage else None,
        "has_dockerignore": ir.has_dockerignore,
        "findings": [
            {
                "rule_id": f.rule_id,
                "severity": f.severity.value,
                "line_no": f.line_no,
                "description": f.description,
                "recommendation": f.recommendation,
            }
            for f in findings
        ],
    }
    console.print_json(json.dumps(data, ensure_ascii=False, indent=2))
