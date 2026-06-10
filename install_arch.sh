#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  install_arch.sh  —  Tally Extractor installer for Arch Linux
#
#  Supports Standalone Mode:
#    If shared as a single file, it will automatically clone the
#    codebase from GitHub and install it to ~/.local/share/tally-extractor.
# ════════════════════════════════════════════════════════════════

set -euo pipefail
IFS=$'\n\t'

# ── Colours ─────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERR ]${NC}  $*" >&2; }
die()     { error "$*"; exit 1; }

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║      Tally Extractor — Arch Linux Installer      ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ─── Determine Installation Mode ───────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "${REPO_ROOT}/tally_web/requirements.txt" ]; then
    info "Detected local codebase. Installing in local directory..."
    INSTALL_DIR="${REPO_ROOT}"
    STANDALONE=false
else
    info "Standalone mode: Codebase files not found."
    INSTALL_DIR="${HOME}/.local/share/tally-extractor"
    STANDALONE=true
fi

WEB_DIR="${INSTALL_DIR}/tally_web"
VENV_DIR="${WEB_DIR}/venv"
BIN_DIR="${HOME}/.local/bin"
DESKTOP_DIR="${HOME}/.local/share/applications"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
SERVICE_NAME="tally-extractor"
CLI_SCRIPT="${BIN_DIR}/tallyextractor"
DESKTOP_FILE="${DESKTOP_DIR}/tally-extractor.desktop"
SERVICE_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}.service"

# ─── Step 1: System package checks ────────────────────────────
echo -e "\n${BOLD}[1/6] Checking system dependencies...${NC}"

MISSING_PKGS=()

check_pkg() {
    local cmd="$1"
    local pkg="$2"
    if ! command -v "$cmd" &>/dev/null; then
        warn "$cmd not found — will install package: $pkg"
        MISSING_PKGS+=("$pkg")
    else
        ok "$cmd is available"
    fi
}

check_pkg python3     "python"
check_pkg pip         "python-pip"
check_pkg git         "git"

if ! python3 -c "import venv" &>/dev/null; then
    warn "python venv module missing — will install python"
    MISSING_PKGS+=("python")
fi

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    mapfile -t UNIQUE_PKGS < <(printf '%s\n' "${MISSING_PKGS[@]}" | sort -u)
    info "Installing missing packages via pacman: ${UNIQUE_PKGS[*]}"
    if ! sudo pacman -Sy --noconfirm "${UNIQUE_PKGS[@]}"; then
        die "pacman failed. Please install dependencies manually and run this script again."
    fi
fi

# ─── Standalone Download ───────────────────────────────────────
if [ "$STANDALONE" = true ]; then
    echo -e "\n${BOLD}[*] Standalone Setup: Cloning Tally Extractor from GitHub...${NC}"
    if [ -d "${INSTALL_DIR}" ]; then
        warn "Target installation folder ${INSTALL_DIR} already exists. Updating..."
        cd "${INSTALL_DIR}"
        git fetch --all --quiet
        git reset --hard origin/main --quiet
    else
        git clone https://github.com/Malviya-Mayur/Tally-Extractor.git "${INSTALL_DIR}" --quiet
        ok "Repository cloned to ${INSTALL_DIR}"
    fi
fi

# ─── Step 2: Verify Python version ────────────────────────────
echo -e "\n${BOLD}[2/6] Verifying Python version...${NC}"
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    die "Python ${PY_VER} is too old. Tally Extractor requires Python 3.10+."
fi
ok "Python ${PY_VER} — compatible"

# ─── Step 3: Virtual environment ───────────────────────────────
echo -e "\n${BOLD}[3/6] Setting up virtual environment in tally_web/venv/...${NC}"
if [ -f "${VENV_DIR}/bin/activate" ]; then
    info "Virtual environment already exists — skipping creation."
else
    python3 -m venv "${VENV_DIR}"
    ok "Virtual environment created at ${VENV_DIR}"
fi

# Activate venv
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

# ─── Step 4: Install Python dependencies ─────────────────────
echo -e "\n${BOLD}[4/6] Installing Python dependencies...${NC}"
pip install --upgrade pip --quiet
pip install -r "${WEB_DIR}/requirements.txt" --quiet

info "Installing optional lxml (3-5x faster XML parsing)..."
pip install lxml --quiet || warn "lxml not installed — falling back to stdlib XML parser."
deactivate
ok "Dependencies setup complete."

# ─── Step 5: CLI command ───────────────────────────────────────
echo -e "\n${BOLD}[5/6] Installing CLI command: tallyextractor${NC}"
mkdir -p "${BIN_DIR}"

cat > "${CLI_SCRIPT}" <<CLIEOF
#!/usr/bin/env bash
# tallyextractor — Start the Tally Extractor web interface
set -euo pipefail

VENV="${VENV_DIR}"
WEB="${WEB_DIR}"

source "\${VENV}/bin/activate"

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║       Tally Extractor is starting...     ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""
echo "  Open your browser at:  http://127.0.0.1:8888"
echo "  Press Ctrl+C to stop."
echo ""

cd "\${WEB}"
exec python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8888 "\$@"
CLIEOF

chmod +x "${CLI_SCRIPT}"
ok "CLI command written: ${CLI_SCRIPT}"

# Ensure ~/.local/bin is on PATH
if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
    SHELL_RC=""
    if   [ -f "${HOME}/.zshrc" ];   then SHELL_RC="${HOME}/.zshrc"
    elif [ -f "${HOME}/.bashrc" ];  then SHELL_RC="${HOME}/.bashrc"
    elif [ -f "${HOME}/.profile" ]; then SHELL_RC="${HOME}/.profile"
    fi
    if [ -n "${SHELL_RC}" ]; then
        echo "" >> "${SHELL_RC}"
        echo "# Added by Tally Extractor installer" >> "${SHELL_RC}"
        echo 'export PATH="${HOME}/.local/bin:${PATH}"' >> "${SHELL_RC}"
        warn "Added ~/.local/bin to PATH in ${SHELL_RC}."
        warn "Run: source ${SHELL_RC}   to use 'tallyextractor' in the current shell."
    fi
fi

# ─── Step 6: .desktop entry + systemd user service ────────────
echo -e "\n${BOLD}[6/6] Creating .desktop entry and systemd user service...${NC}"

# .desktop file
mkdir -p "${DESKTOP_DIR}"
cat > "${DESKTOP_FILE}" <<DESKEOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Tally Extractor
GenericName=Tally Data Extractor
Comment=Extract and process Tally Prime transactional data via a web interface
Exec=${CLI_SCRIPT}
Icon=accessories-calculator
Terminal=true
Categories=Office;Finance;
Keywords=tally;extractor;accounting;finance;erp;
StartupNotify=false
DESKEOF

ok ".desktop entry written: ${DESKTOP_FILE}"

# Refresh desktop database
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "${DESKTOP_DIR}" &>/dev/null || true
fi

# Systemd user service
mkdir -p "${SYSTEMD_DIR}"
cat > "${SERVICE_FILE}" <<SVCEOF
[Unit]
Description=Tally Extractor Web Interface
After=network.target

[Service]
Type=simple
WorkingDirectory=${WEB_DIR}
ExecStart=${VENV_DIR}/bin/python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8888
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
SVCEOF

ok "Systemd user service written: ${SERVICE_FILE}"
systemctl --user daemon-reload 2>/dev/null || true

# ─── Done ──────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║         Installation Complete!                   ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Installed directory: ${INSTALL_DIR}"
echo ""
echo -e "  ${GREEN}How to start:${NC}"
echo    "    • Run in terminal:           tallyextractor"
echo    "    • Find it in your app menu:  Tally Extractor"
echo    "    • Enable auto-start:         systemctl --user enable --now ${SERVICE_NAME}.service"
echo ""
echo -e "  ${GREEN}Then open your browser at:${NC}  http://127.0.0.1:8888"
echo ""
