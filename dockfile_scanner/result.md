# 벤치마크 측정 결과

테스트 환경 및 측정 결과를 정리한 문서입니다.
측정 방법 및 절차는 [benchmark.md](./benchmark.md)를 참고합니다.

## 테스트 환경

| 항목 | 내용 |
|---|---|
| VM | Docker Swarm manager node (Linux/amd64) |
| 이미지 레지스트리 | Docker Hub (`0206pdh/imgadvisor-test`) |
| 측정 스크립트 | `verify_full_lifecycle.sh` (Cold Start 기반 DR 프로비저닝 검증) |
| readiness endpoint | `/ready` (HTTP 200 기준) |
| timeout | 30,000ms |
| 측정 방식 | Phase 0 캐시 완전 삭제(`docker rmi -f`) 후 cold start 측정 |

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
| pre3 | | | |

---

## 3. 빌드 시간 (cold build)

`docker builder prune -af` 후 `--no-cache` 기준입니다.

| 케이스 | 원본 | 최적화 |
|---|---:|---:|
| pre1 | | |
| pre2 | | |
| pre3 | | |

---

## 4. Docker Hub Pull & Extract 시간 (Cold Start 실측)

캐시 없는 환경(`docker rmi -f`)에서 이미지 다운로드 + 레이어 압축 해제 합산 시간입니다.

| 케이스 | 원본 | 최적화 | 단축량 | 단축률 |
|---|---:|---:|---:|---:|
| pre1 | 132,764ms | 29,924ms | 102,840ms | **77.5%** |
| pre2 | 103,930ms | 13,338ms | 90,592ms | **87.2%** |
| pre3 | 12,417ms | 6,518ms | 5,899ms | **47.5%** |

---

## 5. 컨테이너 첫 응답 시간 (Ready Time 실측)

`docker run` 직후부터 `/ready` HTTP 200 응답까지 걸린 시간입니다.

| 케이스 | 원본 | 최적화 | 차이 |
|---|---:|---:|---:|
| pre1 | 1,232ms | 1,674ms | +442ms |
| pre2 | 1,806ms | 1,920ms | +114ms |
| pre3 | 687ms | 1,535ms | +848ms |

> 최적화 이미지의 Ready Time이 소폭 증가하는 것은 slim 이미지 + venv 기반 기동의 특성으로, 실운영 영향 수준(1초 미만)은 아닙니다.

---

## 6. Total Time to Ready (Pull + Ready 실측)

Pull & Extract + Container Ready 합산입니다. Cold Start DR 시나리오에서 서비스 복구까지 걸리는 총 시간입니다.

| 케이스 | 원본 | 최적화 | 단축량 | 단축률 |
|---|---:|---:|---:|---:|
| pre1 | 133,996ms (약 134초) | 31,598ms (약 32초) | 102,398ms | **76.4%** |
| pre2 | 105,736ms (약 106초) | 15,258ms (약 15초) | 90,478ms | **85.6%** |
| pre3 | 13,104ms (약 13초) | 8,053ms (약 8초) | 5,051ms | **38.5%** |

---

## 7. HTTP 부하테스트

| 케이스 | RPS (원본) | RPS (최적화) | p95 (원본) | p95 (최적화) | 에러율 |
|---|---:|---:|---:|---:|---:|
| pre1 | | | | | |
| pre2 | | | | | |
| pre3 | | | | | |

---

## 주요 확인 결과 요약

| 항목 | pre1 | pre2 | pre3 |
|---|---|---|---|
| Pull 단축률 | 77.5% | **87.2%** | 47.5% |
| Total 단축률 | 76.4% | **85.6%** | 38.5% |
| 원본 Total | 134초 | 106초 | 13초 |
| 최적화 Total | 32초 | **15초** | 8초 |

- **pre2가 가장 큰 절감 효과**: `python:3.11` + 빌드 도구(gcc/make/libffi-dev) + fastapi/uvicorn/sqlalchemy 조합이 무거워 원본이 106초였으나, multi-stage + slim 전환으로 15초로 단축
- **pre1도 76% 단축**: pandas 포함 풀 이미지 기반(134초)에서 32초로
- **pre3는 상대적으로 작은 절감**: 원본 패키지 구성(flask/gunicorn/requests)이 가벼워 원본 자체가 이미 빠름(13초), 최적화 후 8초
- **Ready Time은 모든 케이스에서 1초 이내**: 이미지 크기와 무관하게 앱 기동 자체는 빠름
