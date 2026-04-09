#!/usr/bin/env bash
set -euo pipefail

REPO="gimyw/dockimage_scanner"
SUBDIR="dockfile_scanner"
TOOL="imgadvisor"
VENV_DIR="${HOME}/.imgadvisor"
BIN_DIR="${HOME}/.local/bin"
MIN_PYTHON_MINOR=11

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[imgadvisor]${NC} $*"; }
warn()  { echo -e "${YELLOW}[imgadvisor]${NC} $*"; }
error() { echo -e "${RED}[imgadvisor] 오류:${NC} $*" >&2; exit 1; }

PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$cmd" >/dev/null 2>&1; then
    ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || continue
    major=${ver%%.*}
    minor=${ver##*.}
    if [ "$major" -eq 3 ] && [ "$minor" -ge "$MIN_PYTHON_MINOR" ]; then
      PYTHON="$cmd"
      break
    fi
  fi
done

[ -z "$PYTHON" ] && error "Python 3.${MIN_PYTHON_MINOR}+ 가 필요합니다."
info "사용할 Python: $($PYTHON --version)"

if ! command -v git >/dev/null 2>&1; then
  error "git 이 필요합니다. 먼저 git 을 설치해 주세요."
fi

if ! command -v curl >/dev/null 2>&1; then
  error "curl 이 필요합니다. 먼저 curl 을 설치해 주세요."
fi

LATEST_TAG=$(
  curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" |
    "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['tag_name'])"
) || error "최신 release 정보를 가져오지 못했습니다."

PACKAGE_URL="git+https://github.com/${REPO}.git@${LATEST_TAG}#subdirectory=${SUBDIR}"

info "최신 release: ${LATEST_TAG}"
info "가상환경 생성: ${VENV_DIR}"
"$PYTHON" -m venv "$VENV_DIR"

VENV_PIP="${VENV_DIR}/bin/pip"
VENV_TOOL="${VENV_DIR}/bin/${TOOL}"

info "패키지 설치: ${PACKAGE_URL}"
"$VENV_PIP" install --upgrade pip >/dev/null
"$VENV_PIP" install --force-reinstall "$PACKAGE_URL"

mkdir -p "$BIN_DIR"
ln -sf "$VENV_TOOL" "${BIN_DIR}/${TOOL}"
info "실행 링크 생성: ${BIN_DIR}/${TOOL}"

PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'

add_path_to_rc() {
  local rc_file="$1"
  if [ -f "$rc_file" ] && ! grep -qF "$PATH_LINE" "$rc_file"; then
    {
      echo ""
      echo "# added by imgadvisor installer"
      echo "$PATH_LINE"
    } >> "$rc_file"
    info "PATH 추가: ${rc_file}"
  fi
}

if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
  add_path_to_rc "${HOME}/.bashrc"
  add_path_to_rc "${HOME}/.bash_profile"
  add_path_to_rc "${HOME}/.zshrc"
fi

cat <<EOF

[imgadvisor] 설치가 완료되었습니다.

다음 명령으로 확인할 수 있습니다.
  ${BIN_DIR}/${TOOL} --help

자주 쓰는 명령:
  ${TOOL} analyze   -f Dockerfile
  ${TOOL} recommend -f Dockerfile -o optimized.Dockerfile
  ${TOOL} validate  -f Dockerfile --optimized optimized.Dockerfile
  ${TOOL} scan      -f Dockerfile

현재 셸에서 명령이 바로 안 잡히면 아래를 실행하세요.
  export PATH="${BIN_DIR}:\$PATH"
  hash -r

EOF
