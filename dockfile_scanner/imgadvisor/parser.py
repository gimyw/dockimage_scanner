"""
Dockerfile 파서 — 텍스트를 DockerfileIR로 변환.

처리 순서:
  1. 백슬래시 줄 이어쓰기(continuation) 합치기
  2. FROM 이전 ARG 기본값 수집
  3. ARG 변수 치환 ($VAR / ${VAR})
  4. FROM 블록 단위로 Stage 생성
  5. 각 명령어를 DockerInstruction으로 변환해 Stage에 추가
"""
from __future__ import annotations

import re
from pathlib import Path

from imgadvisor.models import DockerfileIR, DockerInstruction, Stage


def _join_continuations(lines: list[str]) -> list[tuple[int, str]]:
    """
    백슬래시 줄 이어쓰기를 합치고 (원본 줄 번호, 합쳐진 내용) 목록을 반환.

    Dockerfile에서 RUN 명령은 가독성을 위해 줄 끝에 \를 붙여 여러 줄로 쓰는 경우가 많다:
        RUN apt-get update \\
            && apt-get install -y curl

    이 함수는 위와 같은 연속 줄을 하나의 문자열로 합쳐서 반환한다.
    반환되는 줄 번호는 해당 명령이 시작되는 원본 파일의 첫 번째 줄 번호다.

    빈 줄과 주석(#)은 건너뛴다.
    """
    result: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        # 빈 줄 또는 주석은 무시
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        original_line_no = i + 1  # 1-based 줄 번호 보존
        joined = stripped

        # 줄 끝이 \로 끝나는 동안 다음 줄을 계속 이어 붙임
        while joined.endswith("\\") and i + 1 < len(lines):
            joined = joined[:-1].rstrip()  # 끝의 \ 제거
            i += 1
            next_part = lines[i].strip()
            if next_part and not next_part.startswith("#"):
                joined = joined + " " + next_part

        result.append((original_line_no, joined))
        i += 1

    return result


def _collect_arg_defaults(joined: list[tuple[int, str]]) -> dict[str, str]:
    """
    첫 번째 FROM 이전에 선언된 ARG의 기본값을 수집한다.

    Dockerfile에서 ARG는 FROM 앞에 전역으로 선언할 수 있으며,
    FROM의 이미지 이름에 변수로 사용될 수 있다:
        ARG BASE_IMAGE=python:3.11-slim
        FROM ${BASE_IMAGE}

    이 함수는 그런 ARG 기본값을 {변수명: 기본값} 딕셔너리로 반환한다.
    FROM을 만나면 수집을 중단한다 (FROM 이후 ARG는 스테이지 내부 변수이므로 별도 처리).
    """
    args: dict[str, str] = {}
    for _, line in joined:
        # 첫 번째 FROM을 만나면 전역 ARG 수집 종료
        if re.match(r"^FROM\s+", line, re.IGNORECASE):
            break
        # ARG NAME=default_value 패턴 파싱
        m = re.match(r"^ARG\s+(\w+)(?:=(.+))?$", line, re.IGNORECASE)
        if m:
            name = m.group(1)
            default = (m.group(2) or "").strip().strip('"').strip("'")
            args[name] = default
    return args


def _substitute_vars(text: str, args: dict[str, str]) -> str:
    """
    텍스트에서 ${VAR} 및 $VAR 형태를 ARG 기본값으로 치환한다.

    치환 대상이 없는 변수는 원본 표현 그대로 남긴다.
    예: $UNKNOWN → $UNKNOWN (변경 없음)
    """
    def replacer(m: re.Match) -> str:
        # group(1): ${VAR} 형태, group(2): $VAR 형태
        name = m.group(1) or m.group(2)
        return args.get(name, m.group(0))  # 없으면 원본 유지

    return re.sub(r"\$\{(\w+)\}|\$(\w+)", replacer, text)


def parse(dockerfile_path: str) -> DockerfileIR:
    """
    Dockerfile 파일을 읽어 DockerfileIR로 변환한다.

    처리 단계:
    1. 파일 읽기 (UTF-8, 인코딩 오류는 replace)
    2. .dockerignore 존재 여부 확인 (Dockerfile 옆 디렉토리 기준)
    3. 줄 이어쓰기 합치기
    4. 전역 ARG 기본값 수집
    5. 명령어 파싱:
       - FROM: 새 Stage 생성, 이전 스테이지 참조인 경우 [stage:alias] 로 마킹
       - 그 외: DockerInstruction 생성 후 현재 Stage에 추가
    6. 마지막 Stage에 is_final=True 설정

    Args:
        dockerfile_path: Dockerfile 파일 경로 (문자열)

    Returns:
        DockerfileIR: 파싱된 중간 표현
    """
    path = Path(dockerfile_path)
    content = path.read_text(encoding="utf-8", errors="replace")
    raw_lines = content.splitlines()

    # Dockerfile과 같은 디렉토리에 .dockerignore가 있는지 확인
    has_dockerignore = (path.parent / ".dockerignore").exists()

    joined = _join_continuations(raw_lines)
    arg_defaults = _collect_arg_defaults(joined)

    stages: list[Stage] = []
    stage_aliases: set[str] = set()  # 멀티-스테이지에서 AS로 붙인 이름 모음
    current_idx = -1  # 현재 스테이지 인덱스 (FROM을 만날 때마다 증가)

    for line_no, line in joined:
        m = re.match(r"^(\w+)\s*(.*)", line, re.IGNORECASE)
        if not m:
            continue

        cmd = m.group(1).upper()
        args_raw = m.group(2).strip()
        args = _substitute_vars(args_raw, arg_defaults)  # 변수 치환

        if cmd == "FROM":
            current_idx += 1
            # FROM <image> [AS <alias>] 파싱
            from_m = re.match(r"^(\S+)(?:\s+AS\s+(\S+))?", args, re.IGNORECASE)
            if from_m:
                base_image = from_m.group(1)
                alias = from_m.group(2)
            else:
                base_image = args
                alias = None

            # AS alias가 있으면 스테이지 이름으로 등록
            if alias:
                stage_aliases.add(alias.lower())

            # COPY --from=builder 등에서 참조되는 이미지 이름이 아닌
            # FROM builder 처럼 이전 스테이지를 직접 베이스로 쓰는 경우:
            # base_image를 [stage:alias] 로 마킹해서 base_image 규칙이 무시하도록 한다.
            if base_image.lower() in stage_aliases:
                base_image = f"[stage:{base_image}]"

            stage = Stage(
                index=current_idx,
                base_image=base_image,
                alias=alias,
            )
            stages.append(stage)

        elif current_idx >= 0 and cmd != "ARG":
            # FROM 이후의 명령어는 현재 스테이지에 추가 (ARG는 제외)
            instr = DockerInstruction(
                line_no=line_no,
                instruction=cmd,
                arguments=args,
                stage_index=current_idx,
                raw=line,
            )
            stages[current_idx].instructions.append(instr)

    # 가장 마지막 스테이지가 런타임 이미지 (final stage)
    if stages:
        stages[-1].is_final = True

    return DockerfileIR(
        stages=stages,
        raw_lines=raw_lines,
        path=dockerfile_path,
        has_dockerignore=has_dockerignore,
    )
