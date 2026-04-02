#!/usr/bin/env bash
set -e

REPO="0206pdh/dockimage_scanner"
TOOL="imgadvisor"
VENV_DIR="${HOME}/.imgadvisor"
BIN_DIR="${HOME}/.local/bin"
MIN_PYTHON_MINOR=11

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[imgadvisor]${NC} $*"; }
warn()  { echo -e "${YELLOW}[imgadvisor]${NC} $*"; }
error() { echo -e "${RED}[imgadvisor] ERROR:${NC} $*" >&2; exit 1; }

# ── Python 3.11+ 탐색 ──────────────────────────────────────────────────────
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
[ -z "$PYTHON" ] && error "Python 3.${MIN_PYTHON_MINOR}+ is required."
info "Python: $($PYTHON --version)"

# ── git 확인 ───────────────────────────────────────────────────────────────
if ! command -v git &>/dev/null; then
  info "Installing git..."
  if command -v apt-get &>/dev/null; then
    apt-get install -y git 2>/dev/null || sudo apt-get install -y git
  elif command -v yum &>/dev/null; then
    yum install -y git 2>/dev/null || sudo yum install -y git
  else
    error "git not found. Please install it manually."
  fi
fi

# ── venv 생성 ──────────────────────────────────────────────────────────────
info "Creating virtualenv: ${VENV_DIR}"
"$PYTHON" -m venv "$VENV_DIR"

VENV_PIP="${VENV_DIR}/bin/pip"

# ── 설치 ───────────────────────────────────────────────────────────────────
info "Installing from github.com/${REPO} ..."
"$VENV_PIP" install --quiet --upgrade pip
"$VENV_PIP" install --quiet --force-reinstall "git+https://github.com/${REPO}.git"

# ── ~/.local/bin 심볼릭 링크 ───────────────────────────────────────────────
mkdir -p "$BIN_DIR"
ln -sf "${VENV_DIR}/bin/${TOOL}" "${BIN_DIR}/${TOOL}"
info "Symlink: ${BIN_DIR}/${TOOL}"

# ── PATH 자동 등록 ─────────────────────────────────────────────────────────
# ~/.local/bin 이 PATH에 없으면 셸 RC 파일에 자동으로 추가
PATH_LINE="export PATH=\"\$HOME/.local/bin:\$PATH\""

_add_to_rc() {
  local rc="$1"
  if [ -f "$rc" ] && ! grep -qF '.local/bin' "$rc"; then
    echo "" >> "$rc"
    echo "# added by imgadvisor installer" >> "$rc"
    echo "$PATH_LINE" >> "$rc"
    info "Added PATH to $rc"
  fi
}

if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
  # bash
  _add_to_rc "${HOME}/.bashrc"
  _add_to_rc "${HOME}/.bash_profile"
  # zsh
  _add_to_rc "${HOME}/.zshrc"
  # 현재 세션에도 즉시 적용
  export PATH="${BIN_DIR}:$PATH"
fi

# ── 완료 ───────────────────────────────────────────────────────────────────
info "Done! Run: imgadvisor --help"
echo ""
echo "  imgadvisor analyze   -f Dockerfile"
echo "  imgadvisor recommend -f Dockerfile -o optimized.Dockerfile"
echo "  imgadvisor scan      -f Dockerfile"
echo ""

if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
  warn "Reload your shell to use the command:"
  warn "  source ~/.bashrc   (or open a new terminal)"
else
  warn "If 'imgadvisor' is not found, run: source ~/.bashrc"
fi
