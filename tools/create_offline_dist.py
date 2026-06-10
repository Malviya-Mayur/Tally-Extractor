#!/usr/bin/env python3
"""
tools/create_offline_dist.py
────────────────────────────
Creates two fully offline, standalone installer bundles for Tally Extractor:

  dist/
  ├── tally-extractor-windows.zip          ← Windows offline installer
  └── tally-extractor-arch-linux.tar.gz    ← Arch Linux offline installer

Each bundle contains:
  install.bat / install.sh   — installer script (no internet needed)
  src/                       — complete application source code
  wheels/                    — pre-downloaded pip wheels for that platform

Run this script ONCE on a machine with internet access, then distribute
either bundle to any target machine — no internet required on the target.

Usage:
  python3 tools/create_offline_dist.py

Options:
  --win-python  Python version for Windows wheels (default: 312)
  --lin-python  Python version for Linux wheels   (default: 312)
  --skip-win    Skip building the Windows bundle
  --skip-linux  Skip building the Linux bundle

Note on Python version targeting:
  Wheels are downloaded for the specified cp version (e.g. cp312 = Python 3.12).
  The installer will warn the user if their Python version does not match,
  and fall back to PyPI as a last resort.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

# ── Repo layout ───────────────────────────────────────────────────────────────
REPO_ROOT    = Path(__file__).resolve().parent.parent
TOOLS_DIR    = REPO_ROOT / "tools"
WEB_DIR      = REPO_ROOT / "tally_web"
REQUIREMENTS = WEB_DIR / "requirements.txt"
DIST_DIR     = REPO_ROOT / "dist"

# Files / dirs to include in src/ (relative to REPO_ROOT)
SRC_INCLUDES = [
    "tally_web",
    "Tally_Pipeline.py",
    "Tally_Pipeline_V2.py",
    "API_Extractor.txt",
    "README.md",
]

# Always exclude these from src/
SRC_EXCLUDES = {
    "venv", "__pycache__", ".git", "tally_out",
    "*.pyc", "*.pyo", "*.log", "web_pipeline.log",
}


# ── Installer script templates ────────────────────────────────────────────────

WINDOWS_INSTALLER = r"""@echo off
setlocal enabledelayedexpansion
title Tally Extractor — Offline Installer

REM ════════════════════════════════════════════════════════════════
REM  Tally Extractor — Offline Windows Installer
REM
REM  This archive contains everything needed.  No internet required.
REM
REM  Usage:
REM    1. Extract this ZIP anywhere (e.g. Downloads folder).
REM    2. Double-click  install.bat  inside the extracted folder.
REM ════════════════════════════════════════════════════════════════

set "BUNDLE_DIR=%~dp0"
if "%BUNDLE_DIR:~-1%"=="\" set "BUNDLE_DIR=%BUNDLE_DIR:~0,-1%"
set "INSTALL_DIR=%USERPROFILE%\Tally-Extractor"
set "WHEELS_DIR=%BUNDLE_DIR%\wheels"
set "SRC_DIR=%BUNDLE_DIR%\src"

echo.
echo  ╔════════════════════════════════════════════════════╗
echo  ║     Tally Extractor — Offline Windows Installer   ║
echo  ╚════════════════════════════════════════════════════╝
echo.
echo  Installing to: %INSTALL_DIR%
echo.

REM ─── Step 1: Python check ─────────────────────────────────────────────────
echo [1/5] Checking for Python 3.10+...
where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: Python is not found in PATH.
    echo  Download Python 3.10 or newer from: https://www.python.org/downloads/
    echo  IMPORTANT: Tick "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

python -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"
    echo  ERROR: !PY_VER! is too old. Requires Python 3.10+.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo  Found: %%v

REM ─── Step 2: Copy source files ────────────────────────────────────────────
echo.
echo [2/5] Copying application files to %INSTALL_DIR%...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
xcopy /e /y /q "%SRC_DIR%\*" "%INSTALL_DIR%\" >nul
if errorlevel 1 (
    echo  ERROR: Failed to copy source files.
    pause
    exit /b 1
)
echo  Application files copied.

REM ─── Step 3: Create virtual environment ───────────────────────────────────
echo.
echo [3/5] Creating virtual environment...
set "VENV_DIR=%INSTALL_DIR%\tally_web\venv"
if exist "%VENV_DIR%\Scripts\activate.bat" (
    echo  Virtual environment already exists — skipping.
) else (
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo  ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo  Virtual environment created.
)

REM ─── Step 4: Install packages from bundled wheels ─────────────────────────
echo.
echo [4/5] Installing Python packages from bundled wheels (offline)...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip --quiet --no-index --find-links "%WHEELS_DIR%" 2>nul
"%VENV_DIR%\Scripts\pip.exe" install ^
    --no-index ^
    --find-links "%WHEELS_DIR%" ^
    -r "%INSTALL_DIR%\tally_web\requirements.txt" ^
    --quiet
if errorlevel 1 (
    echo  ERROR: Package installation failed.
    echo  The bundled wheels may not match your Python version.
    echo  Bundled for: PYVER_PLACEHOLDER
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo  Your version: %%v
    pause
    exit /b 1
)
echo  All packages installed successfully.

REM ─── Step 5: Create launcher + shortcuts ──────────────────────────────────
echo.
echo [5/5] Creating launcher and shortcuts...

set "LAUNCHER=%INSTALL_DIR%\Launch Tally Extractor.bat"
(
    echo @echo off
    echo title Tally Extractor
    echo cd /d "%INSTALL_DIR%\tally_web"
    echo echo.
    echo echo   Starting Tally Extractor...
    echo echo   Open your browser at: http://127.0.0.1:8888
    echo echo   Press Ctrl+C to stop.
    echo echo.
    echo "%VENV_DIR%\Scripts\python.exe" -m uvicorn backend.app:app --host 127.0.0.1 --port 8888
    echo pause
) > "%LAUNCHER%"

REM Desktop shortcut via PowerShell
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell; ^
   $s = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Tally Extractor.lnk'); ^
   $s.TargetPath = '%LAUNCHER%'; ^
   $s.WorkingDirectory = '%INSTALL_DIR%\tally_web'; ^
   $s.Description = 'Launch Tally Extractor'; ^
   $s.IconLocation = 'C:\Windows\System32\SHELL32.dll,14'; ^
   $s.Save()"

REM Start Menu shortcut
set "SM=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Tally Extractor"
if not exist "%SM%" mkdir "%SM%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell; ^
   $s = $ws.CreateShortcut('%SM%\Tally Extractor.lnk'); ^
   $s.TargetPath = '%LAUNCHER%'; ^
   $s.WorkingDirectory = '%INSTALL_DIR%\tally_web'; ^
   $s.Description = 'Launch Tally Extractor'; ^
   $s.IconLocation = 'C:\Windows\System32\SHELL32.dll,14'; ^
   $s.Save()"

echo.
echo  ╔════════════════════════════════════════════════════╗
echo  ║          Installation Complete!                   ║
echo  ╚════════════════════════════════════════════════════╝
echo.
echo   How to start:
echo     Double-click "Tally Extractor" on your Desktop, or
echo     Search "Tally Extractor" in the Start Menu.
echo.
echo   Then open:  http://127.0.0.1:8888
echo.
echo   IMPORTANT — Before first use:
echo     1. Open Tally Prime.
echo     2. Load TDL:  F1 Help > TDLs ^& Add-Ons > Manage Local TDLs
echo        File: %INSTALL_DIR%\API_Extractor.txt
echo     3. Enable HTTP Server on port 9000.
echo.
pause
endlocal
"""

LINUX_INSTALLER = """#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  Tally Extractor — Offline Arch Linux Installer
#
#  This archive contains everything needed.  No internet required.
#
#  Usage:
#    1. Extract this archive:  tar -xzf tally-extractor-arch-linux.tar.gz
#    2. Enter the folder:      cd tally-extractor-arch-linux
#    3. Run:                   chmod +x install.sh && ./install.sh
# ════════════════════════════════════════════════════════════════

set -euo pipefail
IFS=$'\\n\\t'

RED='\\033[0;31m'; GREEN='\\033[0;32m'; YELLOW='\\033[1;33m'
CYAN='\\033[0;36m'; BOLD='\\033[1m'; NC='\\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[ERR ]${NC}  $*" >&2; exit 1; }

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WHEELS_DIR="${BUNDLE_DIR}/wheels"
SRC_DIR="${BUNDLE_DIR}/src"
INSTALL_DIR="${HOME}/.local/share/tally-extractor"
WEB_DIR="${INSTALL_DIR}/tally_web"
VENV_DIR="${WEB_DIR}/venv"
BIN_DIR="${HOME}/.local/bin"
DESKTOP_DIR="${HOME}/.local/share/applications"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   Tally Extractor — Offline Arch Linux Installer ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
info "Installing to: ${INSTALL_DIR}"
echo ""

# ─── Step 1: Python check ───────────────────────────────────────
echo -e "${BOLD}[1/5] Checking for Python 3.10+...${NC}"
if ! command -v python3 &>/dev/null; then
    die "python3 not found. Install it: sudo pacman -Sy python"
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    die "Python ${PY_VER} is too old. Requires 3.10+.  sudo pacman -Sy python"
fi
ok "Python ${PY_VER}"

# ─── Step 2: Copy source files ─────────────────────────────────
echo -e "\\n${BOLD}[2/5] Copying application files to ${INSTALL_DIR}...${NC}"
mkdir -p "${INSTALL_DIR}"
cp -r "${SRC_DIR}/." "${INSTALL_DIR}/"
ok "Application files copied."

# ─── Step 3: Virtual environment ───────────────────────────────
echo -e "\\n${BOLD}[3/5] Setting up virtual environment...${NC}"
if [ -f "${VENV_DIR}/bin/activate" ]; then
    info "Virtual environment already exists — skipping."
else
    python3 -m venv "${VENV_DIR}"
    ok "Virtual environment created."
fi
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

# ─── Step 4: Install packages from bundled wheels ──────────────
echo -e "\\n${BOLD}[4/5] Installing Python packages from bundled wheels (offline)...${NC}"
pip install --upgrade pip --quiet --no-index --find-links "${WHEELS_DIR}" 2>/dev/null || true
if ! pip install \\
    --no-index \\
    --find-links "${WHEELS_DIR}" \\
    -r "${WEB_DIR}/requirements.txt" \\
    --quiet; then
    echo ""
    warn "Offline install failed. The bundled wheels were built for: PYVER_PLACEHOLDER"
    warn "Your Python version: ${PY_VER}"
    warn "Falling back to PyPI (requires internet)..."
    pip install -r "${WEB_DIR}/requirements.txt" --quiet
fi
deactivate
ok "All packages installed."

# ─── Step 5: CLI + desktop + service ───────────────────────────
echo -e "\\n${BOLD}[5/5] Creating launcher, .desktop entry, and systemd service...${NC}"
mkdir -p "${BIN_DIR}"

CLI_SCRIPT="${BIN_DIR}/tallyextractor"
cat > "${CLI_SCRIPT}" <<CLIEOF
#!/usr/bin/env bash
source "${VENV_DIR}/bin/activate"
echo ""
echo "  Tally Extractor starting... open http://127.0.0.1:8888"
echo "  Press Ctrl+C to stop."
echo ""
cd "${WEB_DIR}"
exec python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8888 "\\$@"
CLIEOF
chmod +x "${CLI_SCRIPT}"

# Add ~/.local/bin to PATH if not already there
if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
    for RC in "${HOME}/.zshrc" "${HOME}/.bashrc" "${HOME}/.profile"; do
        if [ -f "$RC" ]; then
            echo "" >> "$RC"
            echo "# Added by Tally Extractor installer" >> "$RC"
            echo 'export PATH="${HOME}/.local/bin:${PATH}"' >> "$RC"
            warn "Added ~/.local/bin to PATH in $RC"
            warn "Run: source $RC  to activate in current shell."
            break
        fi
    done
fi

mkdir -p "${DESKTOP_DIR}"
cat > "${DESKTOP_DIR}/tally-extractor.desktop" <<DESKEOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Tally Extractor
Comment=Extract and process Tally Prime transactional data
Exec=${CLI_SCRIPT}
Icon=accessories-calculator
Terminal=true
Categories=Office;Finance;
Keywords=tally;accounting;erp;
DESKEOF
command -v update-desktop-database &>/dev/null && update-desktop-database "${DESKTOP_DIR}" 2>/dev/null || true

mkdir -p "${SYSTEMD_DIR}"
cat > "${SYSTEMD_DIR}/tally-extractor.service" <<SVCEOF
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
systemctl --user daemon-reload 2>/dev/null || true

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║         Installation Complete!                   ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}Start:${NC}          tallyextractor"
echo -e "  ${GREEN}App menu:${NC}       Tally Extractor"
echo -e "  ${GREEN}Auto-start:${NC}     systemctl --user enable --now tally-extractor.service"
echo ""
echo -e "  ${GREEN}Browser:${NC}        http://127.0.0.1:8888"
echo ""
echo -e "  ${YELLOW}Before first use:${NC}"
echo    "    1. Open Tally Prime."
echo    "    2. Load TDL: F1 Help > TDLs & Add-Ons > Manage Local TDLs"
echo    "       File: ${INSTALL_DIR}/API_Extractor.txt"
echo    "    3. Enable HTTP Server on port 9000."
echo ""
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd: list[str], **kwargs) -> None:
    """Run a subprocess, raising on failure."""
    print(f"    $ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")


def should_exclude(path: Path) -> bool:
    """Return True if a path should be excluded from the src bundle."""
    for part in path.parts:
        if part in SRC_EXCLUDES:
            return True
        if part.endswith(".pyc") or part.endswith(".pyo") or part.endswith(".log"):
            return True
    return False


def copy_src(dest_src: Path) -> None:
    """Copy application source files into dest_src/."""
    dest_src.mkdir(parents=True, exist_ok=True)
    for rel in SRC_INCLUDES:
        src_path = REPO_ROOT / rel
        if not src_path.exists():
            print(f"  [WARN] Source item not found, skipping: {src_path}")
            continue
        if src_path.is_file():
            shutil.copy2(src_path, dest_src / src_path.name)
        else:
            dst = dest_src / src_path.name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(
                src_path, dst,
                ignore=lambda _dir, names: [
                    n for n in names
                    if should_exclude(Path(_dir) / n)
                ],
            )
    print(f"  Source files copied to {dest_src}")


def download_wheels(wheels_dir: Path, platform: str, python_ver: str, abi: str) -> None:
    """
    Download pip wheels for the target platform into wheels_dir.

    Strategy:
      1. Try bulk download with --only-binary :all: (fast, clean).
      2. If any package fails (no binary wheel available), fall back to
         downloading each package individually with --no-deps so we can
         collect whatever is available and report only the failures.
      3. Also grab the pure-Python packages without any platform restrictions
         as a safety net (they are platform-neutral .whl files).
    """
    wheels_dir.mkdir(parents=True, exist_ok=True)

    # Read requirements
    packages = [
        line.strip()
        for line in REQUIREMENTS.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]

    platform_flags = [
        "--platform", platform,
        "--python-version", python_ver,
        "--implementation", "cp",
        "--abi", abi,
        "--only-binary", ":all:",
    ]

    # ── Attempt 1: bulk binary download ──────────────────────────
    bulk_cmd = [
        sys.executable, "-m", "pip", "download",
        "--dest", str(wheels_dir),
        *platform_flags,
        "-r", str(REQUIREMENTS),
        "--quiet",
    ]
    result = subprocess.run(bulk_cmd, capture_output=True, text=True)
    if result.returncode == 0:
        # All packages downloaded as binaries — best case
        pass
    else:
        print("  [WARN] Bulk binary download failed; downloading package-by-package...")
        failed: list[str] = []
        for pkg in packages:
            per_cmd = [
                sys.executable, "-m", "pip", "download",
                "--dest", str(wheels_dir),
                "--no-deps",
                *platform_flags,
                pkg,
                "--quiet",
            ]
            r = subprocess.run(per_cmd, capture_output=True, text=True)
            if r.returncode != 0:
                failed.append(pkg)
                print(f"    [SKIP binary] {pkg}")

        # ── Attempt 2: pure-Python fallback (no platform flags) ──
        # pip download without platform flags fetches wheels compatible
        # with the *build machine*, which works fine for pure-Python pkgs.
        if failed:
            print(f"  Downloading {len(failed)} package(s) without platform flag (pure-Python)...")
            for pkg in failed:
                fallback_cmd = [
                    sys.executable, "-m", "pip", "download",
                    "--dest", str(wheels_dir),
                    "--no-deps",
                    pkg,
                    "--quiet",
                ]
                r = subprocess.run(fallback_cmd, capture_output=True, text=True)
                if r.returncode != 0:
                    print(f"    [FAIL] Could not download {pkg} at all — target machine will need internet.")

    # Also grab pip itself so 'pip install --upgrade pip' can work offline
    subprocess.run([
        sys.executable, "-m", "pip", "download", "pip",
        "--dest", str(wheels_dir), "--quiet",
    ], check=False)

    count = len(list(wheels_dir.glob("*.whl")))
    print(f"  {count} wheel file(s) collected in {wheels_dir.name}/")


def build_windows_bundle(win_py_ver: str) -> Path:
    """Create dist/tally-extractor-windows.zip"""
    print("\n" + "─" * 60)
    print("  Building Windows bundle...")
    print("─" * 60)

    bundle_dir = DIST_DIR / "_build_windows"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)

    # 1. Source files
    print("  [1/3] Copying source files...")
    copy_src(bundle_dir / "src")

    # 2. Windows wheels
    print(f"  [2/3] Downloading Windows wheels (cp{win_py_ver}-win_amd64)...")
    download_wheels(
        wheels_dir=bundle_dir / "wheels",
        platform="win_amd64",
        python_ver=win_py_ver,
        abi=f"cp{win_py_ver}",
    )

    # 3. Installer script
    print("  [3/3] Writing install.bat...")
    installer_text = WINDOWS_INSTALLER.replace(
        "PYVER_PLACEHOLDER",
        f"Python 3.{win_py_ver[1:]} (cp{win_py_ver})"
    )
    (bundle_dir / "install.bat").write_text(installer_text, encoding="utf-8")
    (bundle_dir / "README.txt").write_text(
        "Tally Extractor — Offline Windows Installer\n"
        "=============================================\n\n"
        "1. Extract this ZIP to any folder.\n"
        "2. Double-click install.bat\n\n"
        f"Requires: Python 3.{win_py_ver[1:]} (https://www.python.org/downloads/)\n"
        "Tick 'Add Python to PATH' when installing Python.\n",
        encoding="utf-8",
    )

    # 4. Zip it up
    out_zip = DIST_DIR / "tally-extractor-windows.zip"
    print(f"  Compressing into {out_zip.name}...")
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in bundle_dir.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(bundle_dir))

    shutil.rmtree(bundle_dir)
    size_mb = out_zip.stat().st_size / 1_048_576
    print(f"  ✅ Windows bundle ready: {out_zip}  ({size_mb:.1f} MB)")
    return out_zip


def build_linux_bundle(lin_py_ver: str) -> Path:
    """Create dist/tally-extractor-arch-linux.tar.gz"""
    print("\n" + "─" * 60)
    print("  Building Arch Linux bundle...")
    print("─" * 60)

    bundle_dir = DIST_DIR / "_build_linux"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)

    # 1. Source files
    print("  [1/3] Copying source files...")
    copy_src(bundle_dir / "src")

    # 2. Linux wheels
    print(f"  [2/3] Downloading Linux wheels (cp{lin_py_ver}-manylinux)...")
    download_wheels(
        wheels_dir=bundle_dir / "wheels",
        platform="manylinux_2_17_x86_64",
        python_ver=lin_py_ver,
        abi=f"cp{lin_py_ver}",
    )

    # 3. Installer script
    print("  [3/3] Writing install.sh...")
    installer_text = LINUX_INSTALLER.replace(
        "PYVER_PLACEHOLDER",
        f"Python 3.{lin_py_ver[1:]} (cp{lin_py_ver})"
    )
    install_sh = bundle_dir / "install.sh"
    install_sh.write_text(installer_text, encoding="utf-8")
    install_sh.chmod(0o755)
    (bundle_dir / "README.txt").write_text(
        "Tally Extractor — Offline Arch Linux Installer\n"
        "================================================\n\n"
        "1. Extract:  tar -xzf tally-extractor-arch-linux.tar.gz\n"
        "2. Enter:    cd tally-extractor-arch-linux\n"
        "3. Install:  chmod +x install.sh && ./install.sh\n\n"
        f"Requires: Python 3.{lin_py_ver[1:]}+  (sudo pacman -Sy python)\n",
        encoding="utf-8",
    )

    # 4. Create .tar.gz
    out_tar = DIST_DIR / "tally-extractor-arch-linux.tar.gz"
    print(f"  Compressing into {out_tar.name}...")
    with tarfile.open(out_tar, "w:gz") as tf:
        tf.add(bundle_dir, arcname="tally-extractor-arch-linux")

    shutil.rmtree(bundle_dir)
    size_mb = out_tar.stat().st_size / 1_048_576
    print(f"  ✅ Linux bundle ready:   {out_tar}  ({size_mb:.1f} MB)")
    return out_tar


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build offline standalone installer bundles for Tally Extractor."
    )
    parser.add_argument(
        "--win-python", default="312",
        help="Python version tag for Windows wheels, e.g. 312 for 3.12 (default: 312)"
    )
    parser.add_argument(
        "--lin-python",
        help="Python version tag for Linux wheels (default: auto-detect from current Python)"
    )
    parser.add_argument("--skip-win",   action="store_true", help="Skip Windows bundle")
    parser.add_argument("--skip-linux", action="store_true", help="Skip Linux bundle")
    args = parser.parse_args()

    # Default Linux to cp312 — widest binary wheel availability.
    # cp314 (Python 3.14) has almost no binary wheels in PyPI yet.
    lin_py_ver = args.lin_python or "312"

    print()
    print("=" * 60)
    print("  Tally Extractor — Offline Bundle Builder")
    print("=" * 60)
    print(f"  Windows target:  Python cp{args.win_python}-win_amd64")
    print(f"  Linux target:    Python cp{lin_py_ver}-manylinux_2_17_x86_64")
    print(f"  Output dir:      {DIST_DIR}")
    print()

    DIST_DIR.mkdir(parents=True, exist_ok=True)

    outputs: list[Path] = []
    if not args.skip_win:
        outputs.append(build_windows_bundle(args.win_python))
    if not args.skip_linux:
        outputs.append(build_linux_bundle(lin_py_ver))

    print()
    print("=" * 60)
    print("  All bundles built successfully!")
    print("=" * 60)
    for p in outputs:
        size_mb = p.stat().st_size / 1_048_576
        print(f"  {p.name:<45} {size_mb:6.1f} MB")
    print()
    print("  Each bundle is self-contained — no internet needed on target.")
    print()


if __name__ == "__main__":
    main()
