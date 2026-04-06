# 벤치마크 측정 결과

테스트 환경 및 측정 결과를 정리한 문서입니다.
측정 방법 및 절차는 [benchmark.md](./benchmark.md)를 참고합니다.

## 테스트 환경

| 항목 | 내용 |
|---|---|
| VM | Docker Swarm manager node |
| Docker | Docker Engine (linux/amd64) |
| 이미지 레지스트리 | Docker Hub (`0206pdh/imgadvisor-test`) |
| 측정 스크립트 | `verify_full_lifecycle.sh` (Cold Start 기반 DR 프로비저닝 검증) |
| readiness endpoint | `/ready` (HTTP 200 기준) |
| timeout | 30,000ms |

---

## 1. 이미지 크기

| 케이스 | 원본 크기 | 최적화 크기 | 절감량 | 절감률 |
|---|---:|---:|---:|---:|
| pre1 | | | | |
| pre2 | | | | |
| pre3 | | | | |

---

## 2. 레이어 수

Docker Hub pull 로그 기준 레이어 수입니다.

| 케이스 | 원본 | 최적화 | 감소 |
|---|---:|---:|---:|
| pre1 | | | |
| pre2 | | | |
| pre3 | 13 | 8 | -5 |

---

## 3. 빌드 시간 (cold build)

`docker builder prune -af` 후 `--no-cache` 기준입니다.

| 케이스 | 원본 | 최적화 |
|---|---:|---:|
| pre1 | | |
| pre2 | | |
| pre3 | | |

---

## 4. Docker Hub Push 시간

| 케이스 | 원본 | 최적화 |
|---|---:|---:|
| pre1 | | |
| pre2 | | |
| pre3 | | |

---

## 5. Docker Hub Pull 시간 (Cold Start)

이미지를 로컬에서 완전히 삭제한 뒤(`docker rmi -f`) pull 시간을 측정합니다.

| 케이스 | 원본 | 최적화 | 단축량 | 단축률 |
|---|---:|---:|---:|---:|
| pre1 | | | | |
| pre2 | | | | |
| pre3 | 71,105ms | 13,239ms | 57,866ms | **81.4%** |

> pre3 pull 시간은 `verify_full_lifecycle.sh` 실행 결과 실측값입니다.

---

## 6. 컨테이너 첫 응답 시간 (Ready Time)

`docker run` 직후부터 `/ready` 엔드포인트 HTTP 200 응답까지 걸린 시간입니다.

| 케이스 | 원본 | 최적화 | 단축량 |
|---|---:|---:|---:|
| pre1 | | | |
| pre2 | | | |
| pre3 | | | |

---

## 7. Total Time to Ready (Pull + Ready)

Pull & Extract + Container Ready 합산입니다. Cold Start DR 시나리오에서 실제 서비스 복구까지 걸리는 총 시간입니다.

| 케이스 | 원본 | 최적화 | 단축량 | 단축률 |
|---|---:|---:|---:|---:|
| pre1 | | | | |
| pre2 | | | | |
| pre3 | | | | |

---

## 8. HTTP 부하테스트 (hey)

`hey -n 10000 -c 100` 기준입니다.

| 케이스 | RPS (원본) | RPS (최적화) | p95 (원본) | p95 (최적화) | 에러율 |
|---|---:|---:|---:|---:|---:|
| pre1 | | | | | |
| pre2 | | | | | |
| pre3 | | | | | |

---

## 주요 확인 결과

- **pre3 Docker Hub pull 시간**: 원본 71,105ms → 최적화 13,239ms (81.4% 단축)
  - 원본: `python:3.11` 풀 이미지 기반, 13개 레이어
  - 최적화: `python:3.11-slim` 기반 multi-stage, 8개 레이어
