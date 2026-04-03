"""
원본 vs 최적화 Dockerfile 실제 빌드 후 크기/레이어 비교.

Docker 데몬이 실행 중이어야 사용 가능.
두 Dockerfile을 임시 태그로 빌드하고, `docker image inspect`로
크기와 레이어 수를 비교한 뒤 임시 이미지를 정리한다.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid

from imgadvisor.models import ValidationResult


def validate(original_path: str, optimized_path: str) -> ValidationResult:
    """
    원본과 최적화 Dockerfile을 각각 빌드해 크기와 레이어 수를 비교한다.

    임시 태그를 UUID 기반으로 생성해 기존 이미지와 충돌을 방지한다.
    빌드 성공/실패에 관계없이 finally 블록에서 임시 이미지를 항상 삭제한다.

    Args:
        original_path : 원본 Dockerfile 경로
        optimized_path: 최적화 Dockerfile 경로

    Returns:
        ValidationResult: 크기 및 레이어 비교 결과

    Raises:
        RuntimeError: Docker 빌드 실패 시
    """
    # 충돌 방지를 위해 8자리 UUID 기반 임시 태그 생성
    orig_tag = f"imgadvisor-orig-{uuid.uuid4().hex[:8]}"
    opt_tag = f"imgadvisor-opt-{uuid.uuid4().hex[:8]}"

    try:
        t0 = time.monotonic()
        _build(original_path, orig_tag)
        orig_build_time_s = time.monotonic() - t0

        t0 = time.monotonic()
        _build(optimized_path, opt_tag)
        opt_build_time_s = time.monotonic() - t0

        orig = _inspect(orig_tag)
        opt = _inspect(opt_tag)

        return ValidationResult(
            original_size_mb=orig["size"] / (1024 * 1024),   # bytes → MB
            optimized_size_mb=opt["size"] / (1024 * 1024),
            original_layers=orig["layers"],
            optimized_layers=opt["layers"],
            original_build_time_s=orig_build_time_s,
            optimized_build_time_s=opt_build_time_s,
        )
    finally:
        # 성공/실패 모두 임시 이미지 삭제
        _cleanup(orig_tag)
        _cleanup(opt_tag)


def _build(dockerfile_path: str, tag: str) -> None:
    """
    지정한 Dockerfile을 Docker 데몬으로 빌드하고 tag를 붙인다.

    빌드 컨텍스트는 Dockerfile이 위치한 디렉토리로 설정한다.
    빌드 실패 시 stderr 마지막 2000자를 포함한 RuntimeError를 발생시킨다.

    Args:
        dockerfile_path: 빌드할 Dockerfile 경로
        tag            : 빌드 결과에 붙일 이미지 태그
    """
    context_dir = os.path.dirname(os.path.abspath(dockerfile_path))
    result = subprocess.run(
        ["docker", "build", "-f", os.path.abspath(dockerfile_path), "-t", tag, context_dir],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Docker build 실패 (tag={tag}):\n{result.stderr[-2000:]}"
        )


def _inspect(tag: str) -> dict:
    """
    `docker image inspect`로 이미지 크기와 레이어 수를 조회한다.

    Returns:
        {"size": bytes, "layers": int}
    """
    result = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)[0]
    return {
        "size": data["Size"],                       # 전체 이미지 크기 (bytes)
        "layers": len(data["RootFS"]["Layers"]),    # 레이어 SHA 목록 수
    }


def _cleanup(tag: str) -> None:
    """
    임시 빌드 이미지를 강제 삭제한다.

    실패해도 예외를 전파하지 않는다 (이미 삭제됐거나 빌드 자체가 실패한 경우).
    """
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
