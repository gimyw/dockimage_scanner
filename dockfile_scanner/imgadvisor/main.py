"""
imgadvisor CLI 진입점.

Typer를 사용해 네 가지 서브커맨드를 제공한다:
  analyze  : Dockerfile 정적 분석 (이미지 비대 요인 탐지)
  recommend: 최적화 Dockerfile 초안 생성
  validate : 원본 vs 최적화 이미지 실제 빌드 비교 (Docker 데몬 필요)
  layers   : 레이어별 크기 분석 (docker history 기반)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from imgadvisor import display, layer_analyzer, recommender, validator
from imgadvisor.analyzer import analyze
from imgadvisor.parser import parse

# Typer 앱 인스턴스 — 서브커맨드를 등록하는 루트 앱
app = typer.Typer(
    name="imgadvisor",
    help=(
        "Dockerfile pre-build static analyzer and image optimization advisor.\n\n"
        "Predicts image bloat before build and recommends optimizations."
    ),
    add_completion=False,  # 자동완성 스크립트 설치 옵션 비활성화
    no_args_is_help=True,  # 인수 없이 실행하면 도움말 출력
)


@app.command(name="analyze")
def cmd_analyze(
    dockerfile: Path = typer.Option(
        ..., "--dockerfile", "-f",
        help="Path to the Dockerfile to analyze",
        exists=True, readable=True,
    ),
    json_out: bool = typer.Option(
        False, "--json",
        help="Output results as JSON",
    ),
) -> None:
    """
    Analyze a Dockerfile for image bloat and optimization issues.

    Runs all built-in rules against the Dockerfile without building an image:
    - Base image optimization (slim/alpine/distroless)
    - Build tools left in final stage
    - Package manager cache not cleaned
    - Broad COPY scope
    - Single-stage build with build tools

    Exits with code 1 if any findings are detected (CI-friendly).

    \b
    Examples:
        imgadvisor analyze --dockerfile ./Dockerfile
        imgadvisor analyze -f Dockerfile --json
    """
    # 1. Dockerfile 파싱 → DockerfileIR 생성
    ir = parse(str(dockerfile))
    # 2. 모든 규칙 실행 → Finding 목록
    findings = analyze(ir)

    # 3. 결과 출력 (JSON 또는 컴팩트 linter 형식)
    if json_out:
        display.print_json_result(ir, findings)
    else:
        display.print_analysis(ir, findings)

    # 4. Finding이 있으면 exit code 1 (CI 파이프라인에서 빌드 차단 가능)
    if findings:
        raise typer.Exit(code=1)


@app.command(name="recommend")
def cmd_recommend(
    dockerfile: Path = typer.Option(
        ..., "--dockerfile", "-f",
        help="Path to the original Dockerfile",
        exists=True, readable=True,
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Path to save the optimized Dockerfile (stdout if not specified)",
    ),
) -> None:
    """
    Generate an optimized Dockerfile based on analysis findings.

    Applies direct patches (e.g. base image replacement) and inserts
    inline comments for issues that require manual restructuring.

    \b
    Examples:
        imgadvisor recommend -f Dockerfile -o optimized.Dockerfile
        imgadvisor recommend -f Dockerfile          # print to stdout
    """
    # analyze와 동일하게 파싱 + 분석 먼저 수행
    ir = parse(str(dockerfile))
    findings = analyze(ir)

    if not findings:
        display.print_analysis(ir, findings)
        return

    # Compact one-line summary instead of the full analysis output
    display.print_recommend_summary(ir, findings)

    # Finding을 바탕으로 최적화 Dockerfile 생성
    optimized = recommender.recommend(ir, findings)

    if output:
        # 파일로 저장
        output.write_text(optimized, encoding="utf-8")
        typer.echo(f"\n  Optimized Dockerfile saved: {output}")
    else:
        # stdout으로 출력 (파이프 처리 가능)
        display.print_recommended_dockerfile(optimized)


@app.command(name="validate")
def cmd_validate(
    dockerfile: Path = typer.Option(
        ..., "--dockerfile", "-f",
        help="Path to the original Dockerfile",
        exists=True, readable=True,
    ),
    optimized: Path = typer.Option(
        ..., "--optimized",
        help="Path to the optimized Dockerfile",
        exists=True, readable=True,
    ),
) -> None:
    """
    Build both Dockerfiles and compare image size and layer count.

    Requires a running Docker daemon. Both images are built with temporary
    tags and deleted after comparison.

    \b
    Examples:
        imgadvisor validate -f Dockerfile --optimized optimized.Dockerfile
    """
    typer.echo("  Building original image...")
    try:
        # Docker 데몬으로 두 이미지를 빌드하고 결과 비교
        result = validator.validate(str(dockerfile), str(optimized))
    except RuntimeError as e:
        typer.echo(f"[ERROR] {e}", err=True)
        raise typer.Exit(code=1)

    display.print_validation(result)



@app.command(name="layers")
def cmd_layers(
    dockerfile: Path = typer.Option(
        ..., "--dockerfile", "-f",
        help="Path to the Dockerfile to analyze",
        exists=True, readable=True,
    ),
) -> None:
    """
    Build the Dockerfile and show a per-layer size breakdown.

    Builds a temporary image, runs `docker history` to extract each layer's
    size and origin instruction, then removes the temporary image.

    Useful for identifying which RUN/COPY instructions contribute the most
    to image size and prioritizing optimization efforts.

    Requires a running Docker daemon.

    \b
    Examples:
        imgadvisor layers -f Dockerfile
    """
    typer.echo("  Building image for layer analysis...")
    try:
        # Build temp image, collect docker history, cleanup
        analysis = layer_analyzer.analyze(str(dockerfile))
    except RuntimeError as e:
        typer.echo(f"[ERROR] {e}", err=True)
        raise typer.Exit(code=1)

    display.print_layers(analysis)


if __name__ == "__main__":
    app()
