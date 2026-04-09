#!/bin/bash
# =============================================================================
# verify_full_lifecycle.sh
# Cold Start 기반 긴급 복구(DR) 프로비저닝 검증 스크립트
#
# 사용법: ./verify_full_lifecycle.sh <도커허브_이미지명> [포트번호] [헬스체크_경로]
# 예시  : ./verify_full_lifecycle.sh myrepo/myapp:latest 8080          # 루트(/) 사용, 리다이렉트 자동 추적
# 예시  : ./verify_full_lifecycle.sh myrepo/myapp:latest 8080 /health  # 헬스체크 전용 엔드포인트 지정
# =============================================================================

IMAGE_NAME=$1
PORT=${2:-8080}
HEALTH_PATH=${3:-/}
CONTAINER_NAME="test_container"
TIMEOUT_MS=30000

# [추가] CSV 저장 경로 설정
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
CSV_FILE="${SCRIPT_DIR}/dr_lifecycle_results.csv"

# =============================================================================
# [사전 검증] 인자 누락 시 안내 후 종료
# =============================================================================
if [ -z "$IMAGE_NAME" ]; then
  echo ""
  echo "❌ [오류] 이미지명이 입력되지 않았습니다."
  echo ""
  echo "  사용법: $0 <도커허브_이미지명> [포트번호] [헬스체크_경로]"
  echo "  예시  : $0 myrepo/myapp:latest 8080"
  echo "  예시  : $0 myrepo/myapp:latest 8080 /health"
  echo ""
  exit 1
fi

# [추가] CSV 헤더 생성 (파일이 없을 때만)
if [ ! -f "$CSV_FILE" ]; then
  echo "timestamp,image_name,pull_extract_ms,ready_ms,total_ms,status" > "$CSV_FILE"
  chmod 666 "$CSV_FILE"
fi

echo ""
echo "================================================="
echo "🚀 [테스트 시작] Target Image: $IMAGE_NAME"
echo "   Port       : $PORT"
echo "   Health Path: $HEALTH_PATH"
echo "================================================="

# =============================================================================
# [Phase 0] Cold Start 환경 초기화
# =============================================================================
echo ""
echo ">> [Phase 0] Cold Start 환경 초기화 중..."
docker rm -f "$CONTAINER_NAME" 2>/dev/null
docker rmi -f "$IMAGE_NAME" 2>/dev/null
echo ">> ✅ 초기화 완료 - 캐시 없는 Cold Start 환경 준비됨"

# =============================================================================
# [Phase 1 & 2] Image Pull & Extract 시간 측정
# =============================================================================
echo ""
echo ">> [Phase 1 & 2] Image Pull & Extract 시작..."
echo "   (네트워크 다운로드 + 레이어 압축 해제 합산 측정)"

pull_start=$(date +%s%3N)
docker pull "$IMAGE_NAME"
PULL_EXIT_CODE=$?
pull_end=$(date +%s%3N)

if [ $PULL_EXIT_CODE -ne 0 ]; then
  echo ""
  echo "🚨 [오류] Image Pull 실패 (exit code: $PULL_EXIT_CODE)"
  echo "   이미지명 또는 네트워크 상태를 확인하세요: $IMAGE_NAME"
  # [추가] Pull 실패 시 CSV 기록
  current_time=$(date "+%Y-%m-%d %H:%M:%S")
  echo "$current_time,$IMAGE_NAME,FAILED,FAILED,FAILED,PULL_FAILED" >> "$CSV_FILE"
  exit 1
fi

pull_total_ms=$((pull_end - pull_start))
echo ">> ✅ Pull & Extract 완료: ${pull_total_ms}ms"

# =============================================================================
# [Phase 3] Container Ready Time 측정
# =============================================================================
echo ""
echo ">> [Phase 3] Container 기동 및 Readiness 체크 시작..."

run_start=$(date +%s%3N)
docker run -d --name "$CONTAINER_NAME" -p "${PORT}:${PORT}" "$IMAGE_NAME"
RUN_EXIT_CODE=$?

if [ $RUN_EXIT_CODE -ne 0 ]; then
  echo ""
  echo "🚨 [오류] docker run 실패 (exit code: $RUN_EXIT_CODE)"
  docker rm -f "$CONTAINER_NAME" 2>/dev/null
  # [추가] docker run 실패 시 CSV 기록
  current_time=$(date "+%Y-%m-%d %H:%M:%S")
  echo "$current_time,$IMAGE_NAME,$pull_total_ms,FAILED,FAILED,RUN_FAILED" >> "$CSV_FILE"
  exit 1
fi

IS_TIMEOUT=false
until [ "$(curl -s -L -o /dev/null -w '%{http_code}' http://localhost:${PORT}${HEALTH_PATH})" -eq 200 ]; do
  sleep 0.1
  elapsed_ms=$(( $(date +%s%3N) - run_start ))
  if [ $elapsed_ms -gt $TIMEOUT_MS ]; then
    echo ">> 🚨 Timeout: ${TIMEOUT_MS}ms 내에 기동되지 않음"
    IS_TIMEOUT=true
    break
  fi
done

run_end=$(date +%s%3N)
ready_ms=$((run_end - run_start))

if [ "$IS_TIMEOUT" = false ]; then
  echo ">> ✅ Container Ready 완료: ${ready_ms}ms"
fi

# =============================================================================
# [최종 결과 출력]
# =============================================================================
echo ""
echo "================================================="
echo "📊 [최종 측정 결과 요약]"
echo "   Target Image : $IMAGE_NAME"
echo "   Health Path  : $HEALTH_PATH"
echo "-------------------------------------------------"
echo "- Image Pull & Extract Time : ${pull_total_ms} ms"
echo "  └ Phase 1 (Network Pull) + Phase 2 (Layer Extract) 합산"

if [ "$IS_TIMEOUT" = true ]; then
  echo "- Container Ready Time      : TIMEOUT (>${TIMEOUT_MS}ms)"
  echo "-------------------------------------------------"
  echo "🚨 Total Time to Ready      : FAILED"
  echo "   서비스가 ${TIMEOUT_MS}ms 내에 정상화되지 않았습니다. (RTO 달성 실패)"
  # [추가] 타임아웃 시 CSV 기록
  current_time=$(date "+%Y-%m-%d %H:%M:%S")
  echo "$current_time,$IMAGE_NAME,$pull_total_ms,TIMEOUT,FAILED,TIMEOUT" >> "$CSV_FILE"
else
  total_ms=$((pull_total_ms + ready_ms))
  echo "- Container Ready Time      : ${ready_ms} ms"
  echo "  └ Phase 3 (App Startup + Network Bridge 설정 포함)"
  echo "-------------------------------------------------"
  echo "🏆 Total Time to Ready      : ${total_ms} ms"
  # [추가] 정상 완료 시 CSV 기록
  current_time=$(date "+%Y-%m-%d %H:%M:%S")
  echo "$current_time,$IMAGE_NAME,$pull_total_ms,$ready_ms,$total_ms,SUCCESS" >> "$CSV_FILE"
fi

echo "================================================="

# [추가] CSV 저장 경로 안내
echo ">> 📁 결과 저장 완료: $CSV_FILE"

# =============================================================================
# [정리] 테스트 컨테이너 삭제
# =============================================================================
echo ""
echo ">> [정리] 테스트 컨테이너 삭제 중..."
docker rm -f "$CONTAINER_NAME" 2>/dev/null
echo ">> ✅ 정리 완료"
echo ""
