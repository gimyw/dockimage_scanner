from __future__ import annotations

import json
import sys

from rich.console import Console
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich import box

from imgadvisor.models import DockerfileIR, Finding, Severity, ValidationResult

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
    total_min = sum(f.saving_min_mb for f in findings)
    total_max = sum(f.saving_max_mb for f in findings)

    parts: list[str] = []
    if fail_n:
        parts.append(f"[bold red]{fail_n} failures[/bold red]")
    if warn_n:
        parts.append(f"[bold yellow]{warn_n} warnings[/bold yellow]")

    console.print(
        f"  {'  '.join(parts)}  "
        f"[dim]|[/dim]  est. savings [green]{total_min:,} ~ {total_max:,} MB[/green]"
    )
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

    # savings
    if f.saving_min_mb > 0 or f.saving_max_mb > 0:
        console.print(
            f"           [dim]est.[/dim] [green]{f.saving_display}[/green]"
        )

    console.print()


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
    console.print(tbl)
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
                "saving_min_mb": f.saving_min_mb,
                "saving_max_mb": f.saving_max_mb,
            }
            for f in findings
        ],
        "total_saving_min_mb": sum(f.saving_min_mb for f in findings),
        "total_saving_max_mb": sum(f.saving_max_mb for f in findings),
    }
    console.print_json(json.dumps(data, ensure_ascii=False, indent=2))
