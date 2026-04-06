# Python 이미지 최적화 성능/운영 테스트 가이드

`imgadvisor`로 생성한 최적화 Dockerfile이 실제로 어떤 이점을 주는지 검증하기 위한 실험 절차입니다.

단순 HTTP 처리 성능만 보는 것이 아니라, 실제 운영에서 체감되는 아래 지표를 함께 비교합니다.

- 이미지 크기
- 빌드 시간
- Docker Hub push/pull 시간
- 컨테이너 시작 후 첫 응답 시간
- 런타임 부하테스트 결과

## 테스트 대상

| 케이스 | 원본 | 최적화 | 앱 | 포트 | readiness endpoint |
|---|---|---|---|---|---|
| pre1 | `Dockerfile.pre1` | `Dockerfile.pre1.optimized` | Flask (gunicorn) | 5000 | `/ready` |
| pre2 | `Dockerfile.pre2` | `Dockerfile.pre2.optimized` | FastAPI (uvicorn) | 8000 | `/ready` |
| pre3 | `Dockerfile.pre3` | `Dockerfile.pre3.optimized` | Flask (gunicorn) | 5000 | `/ready` |

빌드 컨텍스트는 `test/` 디렉터리입니다.

## 사전 준비

Docker Hub에 push할 경우 먼저 로그인합니다.

```bash
docker login -u 0206pdh
```

`imgadvisor` 설치 확인:

```bash
imgadvisor --help
```

## 1. 이미지 빌드

```bash
# pre1
docker build -f test/Dockerfile.pre1            -t pre1-original  test
docker build -f test/Dockerfile.pre1.optimized  -t pre1-optimized test

# pre2
docker build -f test/Dockerfile.pre2            -t pre2-original  test
docker build -f test/Dockerfile.pre2.optimized  -t pre2-optimized test

# pre3
docker build -f test/Dockerfile.pre3            -t pre3-original  test
docker build -f test/Dockerfile.pre3.optimized  -t pre3-optimized test
```

## 2. 이미지 크기 비교

```bash
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" \
  | grep -E "pre[123]-(original|optimized)"
```

더 정확한 바이트 단위 크기:

```bash
for img in pre1-original pre1-optimized pre2-original pre2-optimized pre3-original pre3-optimized; do
  size=$(docker image inspect "$img" --format '{{.Size}}')
  echo "$img: $size bytes"
done
```

## 3. 빌드 시간 비교

### cold build (캐시 없이)

```bash
docker builder prune -af

time docker build --no-cache -f test/Dockerfile.pre1           -t pre1-original  test
time docker build --no-cache -f test/Dockerfile.pre1.optimized -t pre1-optimized test

time docker build --no-cache -f test/Dockerfile.pre2           -t pre2-original  test
time docker build --no-cache -f test/Dockerfile.pre2.optimized -t pre2-optimized test

time docker build --no-cache -f test/Dockerfile.pre3           -t pre3-original  test
time docker build --no-cache -f test/Dockerfile.pre3.optimized -t pre3-optimized test
```

### warm build (캐시 활용)

```bash
time docker build -f test/Dockerfile.pre1           -t pre1-original  test
time docker build -f test/Dockerfile.pre1.optimized -t pre1-optimized test
```

## 4. Docker Hub push

태그 추가 후 push합니다.

```bash
# pre1
docker tag pre1-original  0206pdh/imgadvisor-test:pre1-original
docker tag pre1-optimized 0206pdh/imgadvisor-test:pre1-optimized

# pre2
docker tag pre2-original  0206pdh/imgadvisor-test:pre2-original
docker tag pre2-optimized 0206pdh/imgadvisor-test:pre2-optimized

# pre3
docker tag pre3-original  0206pdh/imgadvisor-test:pre3-original
docker tag pre3-optimized 0206pdh/imgadvisor-test:pre3-optimized
```

push 시간 측정:

```bash
time docker push 0206pdh/imgadvisor-test:pre1-original
time docker push 0206pdh/imgadvisor-test:pre1-optimized

time docker push 0206pdh/imgadvisor-test:pre2-original
time docker push 0206pdh/imgadvisor-test:pre2-optimized

time docker push 0206pdh/imgadvisor-test:pre3-original
time docker push 0206pdh/imgadvisor-test:pre3-optimized
```

## 5. Docker Hub pull 시간

```bash
docker rmi 0206pdh/imgadvisor-test:pre1-original
time docker pull 0206pdh/imgadvisor-test:pre1-original

docker rmi 0206pdh/imgadvisor-test:pre1-optimized
time docker pull 0206pdh/imgadvisor-test:pre1-optimized

docker rmi 0206pdh/imgadvisor-test:pre2-original
time docker pull 0206pdh/imgadvisor-test:pre2-original

docker rmi 0206pdh/imgadvisor-test:pre2-optimized
time docker pull 0206pdh/imgadvisor-test:pre2-optimized

docker rmi 0206pdh/imgadvisor-test:pre3-original
time docker pull 0206pdh/imgadvisor-test:pre3-original

docker rmi 0206pdh/imgadvisor-test:pre3-optimized
time docker pull 0206pdh/imgadvisor-test:pre3-optimized
```

## 6. 컨테이너 시작 후 첫 응답 시간

`docker run` 직후부터 `/ready` 응답까지 걸리는 시간입니다.

### 포트 정보

| 케이스 | 포트 |
|---|---|
| pre1 | 5000 |
| pre2 | 8000 |
| pre3 | 5000 |

### 측정 스크립트

아래 함수를 사용하면 케이스별로 간편하게 측정할 수 있습니다.

```bash
measure_ready() {
  local name=$1
  local image=$2
  local port=$3

  docker rm -f "$name" 2>/dev/null

  start=$(date +%s%N)
  docker run --rm -d --name "$name" -p "${port}:${port}" "$image" > /dev/null

  until curl -fsS "http://127.0.0.1:${port}/ready" > /dev/null 2>&1; do
    sleep 0.05
  done

  end=$(date +%s%N)
  ms=$(( (end - start) / 1000000 ))
  echo "${image}: ${ms}ms"

  docker rm -f "$name" > /dev/null 2>&1
}

# pre1 (port 5000)
measure_ready pre1-orig pre1-original  5000
measure_ready pre1-opt  pre1-optimized 5000

# pre2 (port 8000)
measure_ready pre2-orig pre2-original  8000
measure_ready pre2-opt  pre2-optimized 8000

# pre3 (port 5000)
measure_ready pre3-orig pre3-original  5000
measure_ready pre3-opt  pre3-optimized 5000
```

3~5회 반복해서 평균과 편차를 함께 기록하는 편이 좋습니다.

## 7. HTTP 부하테스트

경량화 후에도 성능이 유지되는지 검증합니다.

```bash
# pre1 (port 5000)
docker run --rm -d --name pre1-opt -p 5000:5000 pre1-optimized
hey -n 10000 -c 100 http://127.0.0.1:5000/
docker rm -f pre1-opt

# pre2 (port 8000)
docker run --rm -d --name pre2-opt -p 8000:8000 pre2-optimized
hey -n 10000 -c 100 http://127.0.0.1:8000/
docker rm -f pre2-opt

# pre3 (port 5000)
docker run --rm -d --name pre3-opt -p 5000:5000 pre3-optimized
hey -n 10000 -c 100 http://127.0.0.1:5000/
docker rm -f pre3-opt
```

`hey` 대신 `wrk`를 사용하는 경우:

```bash
wrk -t4 -c100 -d30s http://127.0.0.1:5000/
```

기록 항목: RPS, 평균 latency, p95, p99, 에러율

## 8. 메모리/CPU 사용량 비교

```bash
docker run --rm -d --name pre1-orig -p 5000:5000 pre1-original
docker run --rm -d --name pre1-opt  -p 5001:5000 pre1-optimized

# 부하 주입 후 측정
hey -n 5000 -c 50 http://127.0.0.1:5000/ &
hey -n 5000 -c 50 http://127.0.0.1:5001/ &
wait

docker stats --no-stream pre1-orig pre1-opt

docker rm -f pre1-orig pre1-opt
```

## 9. imgadvisor로 분석

빌드 전 분석은 아래처럼 실행합니다.

```bash
# 정적 분석
imgadvisor analyze -f test/Dockerfile.pre1
imgadvisor analyze -f test/Dockerfile.pre2
imgadvisor analyze -f test/Dockerfile.pre3

# 최적화 Dockerfile 생성 (imgadvisor가 직접 생성한 것과 수동 작성본 비교)
imgadvisor recommend -f test/Dockerfile.pre1 -o /tmp/pre1-reco.Dockerfile
imgadvisor recommend -f test/Dockerfile.pre2 -o /tmp/pre2-reco.Dockerfile
imgadvisor recommend -f test/Dockerfile.pre3 -o /tmp/pre3-reco.Dockerfile

# 실제 빌드 비교 (Docker 데몬 필요)
imgadvisor validate -f test/Dockerfile.pre1 --optimized test/Dockerfile.pre1.optimized
imgadvisor validate -f test/Dockerfile.pre2 --optimized test/Dockerfile.pre2.optimized
imgadvisor validate -f test/Dockerfile.pre3 --optimized test/Dockerfile.pre3.optimized

# 레이어 분석
imgadvisor layers -f test/Dockerfile.pre1
imgadvisor layers -f test/Dockerfile.pre1.optimized
```

## 권장 비교 표

| 케이스 | 원본 크기 | 최적화 크기 | 빌드 시간 | push 시간 | pull 시간 | 첫 응답 시간 | p95 latency | 메모리 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| pre1 original |  |  |  |  |  |  |  |  |
| pre1 optimized |  |  |  |  |  |  |  |  |
| pre2 original |  |  |  |  |  |  |  |  |
| pre2 optimized |  |  |  |  |  |  |  |  |
| pre3 original |  |  |  |  |  |  |  |  |
| pre3 optimized |  |  |  |  |  |  |  |  |

## 케이스별 주요 포인트

### pre1
- `flask run` (dev server) → `gunicorn` (prod server) 변경 효과
- 이미지 경량화 + 런타임 서버 개선이 함께 반영됨

### pre2
- `uvicorn` 엔트리포인트 유지, 단일 스테이지 → 멀티 스테이지
- 빌드 도구(gcc, make, libffi-dev) + 캐시 미정리 제거 효과

### pre3
- `requirements.txt` 기반 dependency layer 전략 검증
- builder에서 libpq-dev/git/wget 등 불필요 패키지 제거 + slim 전환 효과
