from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from imgadvisor import display, recommender, trivy_scanner, validator
from imgadvisor.analyzer import analyze
from imgadvisor.parser import parse

app = typer.Typer(
    name="imgadvisor",
    help=(
        "Dockerfile pre-build 정적 분석 및 이미지 경량화 어드바이저.\n\n"
        "빌드 전에 이미지 비대 요인을 예측하고 최적화 방안을 추천합니다."
    ),
    add_completion=False,
    no_args_is_help=True,
)


@app.command(name="analyze")
def cmd_analyze(
    dockerfile: Path = typer.Option(
        ..., "--dockerfile", "-f",
        help="분석할 Dockerfile 경로",
        exists=True, readable=True,
    ),
    json_out: bool = typer.Option(
        False, "--json",
        help="결과를 JSON으로 출력",
    ),
) -> None:
    """
    Dockerfile 정적 분석 — 이미지 비대 요인 탐지 및 최적화 추천.

    \b
    예시:
        imgadvisor analyze --dockerfile ./Dockerfile
        imgadvisor analyze -f Dockerfile --json
    """
    ir = parse(str(dockerfile))
    findings = analyze(ir)

    if json_out:
        display.print_json_result(ir, findings)
    else:
        display.print_analysis(ir, findings)

    if findings:
        raise typer.Exit(code=1)


@app.command(name="recommend")
def cmd_recommend(
    dockerfile: Path = typer.Option(
        ..., "--dockerfile", "-f",
        help="원본 Dockerfile 경로",
        exists=True, readable=True,
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="최적화 Dockerfile 저장 경로 (미지정시 stdout 출력)",
    ),
) -> None:
    """
    최적화 Dockerfile 생성.

    분석 결과를 바탕으로 베이스 이미지 교체, 캐시 정리 패턴 추가 등을
    자동 적용한 Dockerfile 초안을 생성합니다.

    \b
    예시:
        imgadvisor recommend -f Dockerfile -o optimized.Dockerfile
        imgadvisor recommend -f Dockerfile          # stdout 출력
    """
    ir = parse(str(dockerfile))
    findings = analyze(ir)

    display.print_analysis(ir, findings)

    if not findings:
        typer.echo("이미 최적화된 Dockerfile입니다. 추천 사항 없음.")
        return

    optimized = recommender.recommend(ir, findings)

    if output:
        output.write_text(optimized, encoding="utf-8")
        typer.echo(f"\n✅  최적화 Dockerfile 저장 완료: {output}")
    else:
        display.print_recommended_dockerfile(optimized)


@app.command(name="validate")
def cmd_validate(
    dockerfile: Path = typer.Option(
        ..., "--dockerfile", "-f",
        help="원본 Dockerfile 경로",
        exists=True, readable=True,
    ),
    optimized: Path = typer.Option(
        ..., "--optimized",
        help="최적화 Dockerfile 경로",
        exists=True, readable=True,
    ),
) -> None:
    """
    원본 vs 최적화 Dockerfile 실제 빌드 후 크기/레이어 비교.

    Docker 데몬이 실행 중이어야 합니다.

    \b
    예시:
        imgadvisor validate -f Dockerfile --optimized optimized.Dockerfile
    """
    typer.echo("🔨  원본 이미지 빌드 중...")
    try:
        result = validator.validate(str(dockerfile), str(optimized))
    except RuntimeError as e:
        typer.echo(f"[ERROR] {e}", err=True)
        raise typer.Exit(code=1)

    display.print_validation(result)


@app.command(name="scan")
def cmd_scan(
    dockerfile: Path = typer.Option(
        ..., "--dockerfile", "-f",
        help="Trivy pre-build 검사를 수행할 Dockerfile 경로",
        exists=True, readable=True,
    ),
    severity: str = typer.Option(
        "MEDIUM,HIGH,CRITICAL", "--severity",
        help="Trivy severity filter. Example: LOW,MEDIUM,HIGH,CRITICAL",
    ),
    ignore_unfixed: bool = typer.Option(
        False, "--ignore-unfixed",
        help="수정 버전이 없는 취약점은 제외",
    ),
    timeout: int = typer.Option(
        300, "--timeout",
        help="Trivy command timeout in seconds",
        min=30,
    ),
    json_out: bool = typer.Option(
        False, "--json",
        help="결과를 JSON으로 출력",
    ),
) -> None:
    """
    Run Trivy pre-build checks for Dockerfile config and build-context dependencies.

    This command intentionally does not build an image. Instead it combines:
    - `trivy config` for Dockerfile misconfigurations
    - `trivy fs` for dependency vulnerabilities in the build context

    \b
    Examples:
        imgadvisor scan -f Dockerfile
        imgadvisor scan -f Dockerfile --severity HIGH,CRITICAL
        imgadvisor scan -f Dockerfile --ignore-unfixed --json
    """
    try:
        result = trivy_scanner.scan(
            dockerfile_path=str(dockerfile),
            severity=severity,
            ignore_unfixed=ignore_unfixed,
            timeout_seconds=timeout,
        )
    except RuntimeError as exc:
        typer.echo(f"[ERROR] {exc}", err=True)
        raise typer.Exit(code=1)

    if json_out:
        display.print_trivy_json_result(result)
    else:
        display.print_trivy_scan(result)

    if result.total_findings:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
