#!/usr/bin/env bash
set -e

REPO="0206pdh/dockimage_scanner"
TOOL="imgadvisor"
MIN_PYTHON_MINOR=11

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[imgadvisor]${NC} $*"; }
warn()  { echo -e "${YELLOW}[imgadvisor]${NC} $*"; }
error() { echo -e "${RED}[imgadvisor] ERROR:${NC} $*" >&2; exit 1; }

# ── Python 3.11+ 탐색 ────────────────────────────────────────────────────────
PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$cmd" &>/dev/null; then
    ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || continue
    major=${ver%%.*}; minor=${ver##*.}
    if [ "$major" -ge 3 ] && [ "$minor" -ge "$MIN_PYTHON_MINOR" ]; then
      PYTHON="$cmd"; break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  error "Python 3.${MIN_PYTHON_MINOR}+ 이 필요합니다.
  Ubuntu/Debian: sudo apt update && sudo apt install -y python3.11
  macOS:         brew install python@3.11"
fi
info "Python: $($PYTHON --version)"

# ── git 확인 ────────────────────────────────────────────────────────────────
if ! command -v git &>/dev/null; then
  warn "git 없음 — 자동 설치 시도..."
  if command -v apt-get &>/dev/null; then
    apt-get install -y git 2>/dev/null || sudo apt-get install -y git || error "git 설치 실패"
  elif command -v yum &>/dev/null; then
    yum install -y git 2>/dev/null || sudo yum install -y git || error "git 설치 실패"
  else
    error "git 이 없습니다. 수동으로 설치하세요."
  fi
fi

# ── pipx 우선 시도 → pip fallback ───────────────────────────────────────────
install_with_pipx() {
  if ! command -v pipx &>/dev/null; then
    info "pipx 설치 중..."
    if command -v apt-get &>/dev/null; then
      apt-get install -y pipx 2>/dev/null || sudo apt-get install -y pipx || return 1
    else
      "$PYTHON" -m pip install --quiet pipx 2>/dev/null || return 1
    fi
  fi
  info "pipx로 설치 중..."
  pipx install "git+https://github.com/${REPO}.git" --force
  pipx ensurepath
}

install_with_pip() {
  if ! "$PYTHON" -m pip --version &>/dev/null 2>&1; then
    if command -v apt-get &>/dev/null; then
      apt-get install -y python3-pip 2>/dev/null || sudo apt-get install -y python3-pip
    fi
  fi
  info "pip으로 설치 중..."
  # externally-managed-environment (PEP 668) 대응: --break-system-packages
  "$PYTHON" -m pip install --quiet --upgrade \
    --break-system-packages \
    "git+https://github.com/${REPO}.git" 2>/dev/null \
  || "$PYTHON" -m pip install --quiet --upgrade \
    "git+https://github.com/${REPO}.git"
}

info "설치 중... (github.com/${REPO})"
if ! install_with_pipx 2>/dev/null; then
  warn "pipx 실패, pip으로 재시도..."
  install_with_pip
fi

# ── PATH 처리 ────────────────────────────────────────────────────────────────
for extra_path in "$HOME/.local/bin" "$HOME/.local/pipx/venvs/imgadvisor/bin"; do
  if [ -f "$extra_path/$TOOL" ] && [[ ":$PATH:" != *":$extra_path:"* ]]; then
    export PATH="$extra_path:$PATH"
    warn "PATH에 $extra_path 추가됨. 영구 적용:"
    warn "  echo 'export PATH=\"$extra_path:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
  fi
done

# ── 확인 ─────────────────────────────────────────────────────────────────────
if command -v "$TOOL" &>/dev/null; then
  info "설치 완료!"
  echo ""
  echo "  사용법:"
  echo "    imgadvisor analyze   --dockerfile Dockerfile"
  echo "    imgadvisor recommend --dockerfile Dockerfile --output optimized.Dockerfile"
  echo "    imgadvisor validate  --dockerfile Dockerfile --optimized optimized.Dockerfile"
  echo ""
  echo "  도움말: imgadvisor --help"
else
  warn "설치는 완료됐으나 명령어를 찾을 수 없습니다. 새 터미널을 열거나:"
  warn "  source ~/.bashrc"
  warn "  또는: export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
