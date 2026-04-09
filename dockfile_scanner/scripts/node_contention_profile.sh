#!/bin/bash
# =====================================================================
# Docker Node Contention Performance Profiling Script
# Metrics: startup_ms / kernel_wait_ms / ctx_switches
# =====================================================================

# -- Phase 0. Input validation
if [ -z "$1" ]; then
  echo "Usage: $0 <image_name> [run_id]"
  echo "Example: $0 myapp:baseline 1"
  exit 1
fi

# -- Variables
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
LOG_FILE="${SCRIPT_DIR}/node_contention_results.csv"
IMAGE_NAME=$1
RUN_ID=${2:-1}
PORT=$((8080 + RUN_ID))
STRESS_CPU=$(nproc)
STRESS_TIMEOUT="60s"

# -- stress-ng check
if ! command -v stress-ng &>/dev/null; then
  echo ">> [ERROR] stress-ng not found. Run: apt-get install -y stress-ng"
  exit 1
fi

# -- CSV header
if [ ! -f "$LOG_FILE" ]; then
  echo "timestamp,run_id,image_name,startup_ms,kernel_wait_ms,ctx_switches" > "$LOG_FILE"
  chmod 666 "$LOG_FILE"
fi

echo ">> [Run $RUN_ID] Node contention test start ($IMAGE_NAME)"

# -- Phase 1. Inject host CPU load
docker rm -f contention_test 2>/dev/null
stress-ng --cpu $STRESS_CPU --cpu-method matrixprod --timeout $STRESS_TIMEOUT > /dev/null 2>&1 &
STRESS_PID=$!
sleep 3

# -- Phase 2. Start container and timer
start_time=$(date +%s%3N)
CONTAINER_ID=$(docker run -d --name contention_test -p $PORT:5000 "$IMAGE_NAME")

if [ -z "$CONTAINER_ID" ]; then
  echo ">> [ERROR] docker run failed. Check image name: $IMAGE_NAME"
  kill -9 $STRESS_PID 2>/dev/null
  exit 1
fi

# PID 확정 후 ctx_switches 시작값 스냅샷 (누적값 제거 목적)
PID=$(docker inspect -f '{{.State.Pid}}' contention_test 2>/dev/null)
CTX_START=$(grep "nr_switches" "/proc/$PID/sched" 2>/dev/null | awk '{print $3}')
[ -z "$CTX_START" ] && CTX_START=0

# -- Phase 2. Poll /ready (0.1s interval, 40s timeout)
IS_TIMEOUT=false
until [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:$PORT/ready)" -eq 200 ]; do
  sleep 0.1
  if [ $(( $(date +%s%3N) - start_time )) -gt 40000 ]; then
    echo ">> Timeout!"
    IS_TIMEOUT=true
    break
  fi
done

end_time=$(date +%s%3N)

if [ "$IS_TIMEOUT" = true ]; then
  startup_ms="FAILED"
else
  startup_ms=$((end_time - start_time))
fi

# -- Phase 3. Extract kernel metrics
CGROUP_PATH=$(find /sys/fs/cgroup -path "*docker*${CONTAINER_ID:0:12}*" -name "cpu.stat" 2>/dev/null | head -n 1 | xargs dirname 2>/dev/null)

if [ -n "$PID" ] && [ "$IS_TIMEOUT" = false ]; then
  # PSI cpu.pressure some total (usec) -> ms
  WAIT_USEC=$(grep "some" "$CGROUP_PATH/cpu.pressure" 2>/dev/null | awk -F'total=' '{print $2}' | awk '{print $1}')
  [ -z "$WAIT_USEC" ] && WAIT_MS=0 || WAIT_MS=$((WAIT_USEC / 1000))

  # ctx_switches: 시작 후 ready까지의 순수 증가분만 측정
  CTX_END=$(grep "nr_switches" "/proc/$PID/sched" 2>/dev/null | awk '{print $3}')
  [ -z "$CTX_END" ] && CTX_END=0
  CTX_SWITCHES=$((CTX_END - CTX_START))
else
  WAIT_MS="FAILED"
  CTX_SWITCHES="FAILED"
fi

# -- Phase 4. Release load
kill -9 $STRESS_PID 2>/dev/null
sleep 2

# -- Phase 5. Record results
current_time=$(date "+%Y-%m-%d %H:%M:%S")
echo "$current_time,$RUN_ID,$IMAGE_NAME,$startup_ms,$WAIT_MS,$CTX_SWITCHES" >> "$LOG_FILE"
docker rm -f contention_test 2>/dev/null

echo ">> [Done] Log: $LOG_FILE"
echo ">> Startup: ${startup_ms}ms | Wait: ${WAIT_MS}ms | Switches: ${CTX_SWITCHES}"
echo "---------------------------------------------------"
