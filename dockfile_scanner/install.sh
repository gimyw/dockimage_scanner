#!/usr/bin/env bash
set -e

REPO="0206pdh/dockimage_scanner"
TOOL="imgadvisor"
MIN_PYTHON="3.11"

# ── 색상 ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[imgadvisor]${NC} $*"; }
warn()  { echo -e "${YELLOW}[imgadvisor]${NC} $*"; }
error() { echo -e "${RED}[imgadvisor] ERROR:${NC} $*" >&2; exit 1; }

# 설치 스크립트 자체는 raw `main` 브랜치에서 내려받더라도, 실제 패키지 설치는
# 항상 최신 GitHub release tag를 기준으로 수행한다. 이렇게 해야 사용자가
# 같은 curl 명령을 실행해도 개발 중인 main HEAD가 아니라 가장 최근 배포본을 받는다.
fetch_latest_release_tag() {
  "$PYTHON" - <<'PY'
import json
from urllib.request import Request, urlopen

repo = "0206pdh/dockimage_scanner"
url = f"https://api.github.com/repos/{repo}/releases/latest"
request = Request(
    url,
    headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "imgadvisor-install-script",
    },
)

with urlopen(request, timeout=15) as response:
    payload = json.load(response)

tag = str(payload.get("tag_name") or "").strip()
if not tag:
    raise SystemExit("latest release tag not found")

print(tag)
PY
}

# ── Python 확인 ─────────────────────────────────────────────────────────────
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" &>/dev/null; then
    ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    major=${ver%%.*}; minor=${ver##*.}
    if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
      PYTHON="$cmd"
      break
    fi
  fi
done

[ -z "$PYTHON" ] && error "Python ${MIN_PYTHON}+ 이 필요합니다. https://python.org 에서 설치하세요."
info "Python 확인: $($PYTHON --version)"

# ── pip 확인 ────────────────────────────────────────────────────────────────
if ! "$PYTHON" -m pip --version &>/dev/null; then
  error "pip 가 없습니다. 'python -m ensurepip --upgrade' 로 설치하세요."
fi

# ── 최신 release 조회 ──────────────────────────────────────────────────────
LATEST_TAG=""
if ! LATEST_TAG="$(fetch_latest_release_tag 2>/dev/null)"; then
  error "최신 GitHub release 조회에 실패했습니다. 네트워크 상태 또는 release 설정을 확인하세요."
fi

# git clone 대신 release tarball을 직접 설치하면 사용자가 항상 최신 배포본을
# 받게 되고, 설치 시점의 main 브랜치 상태에 영향을 받지 않는다.
PACKAGE_URL="https://github.com/${REPO}/archive/refs/tags/${LATEST_TAG}.tar.gz"

# ── 설치 ────────────────────────────────────────────────────────────────────
info "설치 중... (github.com/${REPO} @ ${LATEST_TAG})"
"$PYTHON" -m pip install --quiet --upgrade \
  "${PACKAGE_URL}"

# ── 확인 ────────────────────────────────────────────────────────────────────
if command -v "$TOOL" &>/dev/null; then
  info "설치 완료!"
  echo ""
  echo "  사용법:"
  echo "    imgadvisor analyze   --dockerfile Dockerfile"
  echo "    imgadvisor recommend --dockerfile Dockerfile --output optimized.Dockerfile"
  echo "    imgadvisor scan      --dockerfile Dockerfile"
  echo "    imgadvisor validate  --dockerfile Dockerfile --optimized optimized.Dockerfile"
  echo ""
  echo "  자세한 도움말:"
  echo "    imgadvisor --help"
else
  warn "'imgadvisor' 명령어를 찾을 수 없습니다."
  warn "pip install 경로가 PATH에 있는지 확인하세요."
  warn "  예: export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
