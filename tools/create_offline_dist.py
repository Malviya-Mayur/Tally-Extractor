#!/usr/bin/env python3
"""
tools/create_offline_dist.py
────────────────────────────
Builds SINGLE-FILE self-extracting offline installers for Tally Extractor.

Output (dist/):
  tally-extractor-arch-linux-installer.sh   ← single .sh, chmod +x and run
  tally-extractor-windows-installer.bat     ← single .bat, double-click

Each file is fully self-contained: application source + all pip wheels are
encoded as base64 inside the script itself.  No internet, no archive to
extract — just one file.

Also produces the original directory-based bundles for those who prefer them:
  tally-extractor-arch-linux.tar.gz
  tally-extractor-windows.zip

Usage:
  python3 tools/create_offline_dist.py

Options:
  --win-python  cp version for Windows wheels (default: 312 = Python 3.12)
  --lin-python  cp version for Linux wheels   (default: 312)
  --skip-win    Skip Windows output
  --skip-linux  Skip Linux output

How the single-file self-extraction works
  Linux  : The .sh file runs normally up to an `exit 0` line. Everything
           after the __ARCHIVE__ marker is base64-encoded tar.gz. At runtime,
           `sed` strips the header and pipes it to `base64 -d | tar -xz`.
  Windows: The .bat file writes a tiny temp .ps1, which uses
           ReadAllLines() to find the ::__ARCHIVE__ marker and decodes the
           appended base64 zip with [Convert]::FromBase64String.
"""

from __future__ import annotations

import argparse
import base64
import io
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

# ── Repo layout ───────────────────────────────────────────────────────────────
REPO_ROOT    = Path(__file__).resolve().parent.parent
WEB_DIR      = REPO_ROOT / "tally_web"
REQUIREMENTS = WEB_DIR / "requirements.txt"
DIST_DIR     = REPO_ROOT / "dist"

SRC_INCLUDES = [
    "tally_web",
    "Tally_Pipeline.py",
    "Tally_Pipeline_V2.py",
    "API_Extractor.txt",
    "README.md",
]
SRC_EXCLUDES = {"venv", "__pycache__", ".git", "tally_out"}


# ── Self-extracting script headers ────────────────────────────────────────────

# Everything after `exit 0` / `__ARCHIVE__` is base64 tar.gz
LINUX_SFX_HEADER = r"""#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  Tally Extractor — Arch Linux Self-Extracting Offline Installer
#  Single file • no internet required • built for PYVER_PLACEHOLDER
#
#  Usage:
#    chmod +x tally-extractor-arch-linux-installer.sh
#    ./tally-extractor-arch-linux-installer.sh
# ════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
TMPDIR_SFX="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_SFX"' EXIT

echo ""
echo "  ╔════════════════════════════════════════════════════╗"
echo "  ║  Tally Extractor — Self-Extracting Installer (Linux)"
echo "  ╚════════════════════════════════════════════════════╝"
echo ""
echo "  Decoding bundled payload (may take a moment)..."

# Strip everything up to and including __ARCHIVE__, decode, extract
sed '1,/^__ARCHIVE__$/d' "$SCRIPT" | base64 -d | tar -xz -C "$TMPDIR_SFX"

echo "  Launching installer..."
chmod +x "$TMPDIR_SFX/bundle/install.sh"
bash "$TMPDIR_SFX/bundle/install.sh"
exit 0
__ARCHIVE__
"""

# The .bat writes a minimal temp .ps1 (avoids all batch-escaping headaches),
# then immediately runs it. Everything after ::__ARCHIVE__ is base64 zip.
WINDOWS_SFX_HEADER = r"""@echo off
:: ==============================================================
:: Tally Extractor - Windows Self-Extracting Offline Installer
:: Single file, no internet required, built for PYVER_PLACEHOLDER
::
:: Usage: Double-click this file.
:: ==============================================================
echo.
echo   Tally Extractor - Self-Extracting Installer
echo   (Built for PYVER_PLACEHOLDER)
echo.
echo   Preparing extraction...

set "SELF=%~f0"
set "PS1=%TEMP%\te_sfx_%RANDOM%_%RANDOM%.ps1"

:: Write the PowerShell extractor to a temp file (one safe echo per line)
(
echo $ErrorActionPreference='Stop'
echo $self=[IO.Path]::GetFullPath('%SELF%')
echo $lines=[IO.File]::ReadAllLines($self)
echo $s=0
echo for($i=0;$i-lt$lines.Count;$i++){if($lines[$i]-eq'::__ARCHIVE__'){$s=$i+1;break}}
echo $b64=[string]::Join('',$lines[$s..($lines.Count-1)])
echo $bytes=[Convert]::FromBase64String($b64)
echo $tmp=Join-Path $env:TEMP([Guid]::NewGuid())
echo [void][IO.Directory]::CreateDirectory($tmp)
echo $zip=Join-Path $tmp 'b.zip'
echo [IO.File]::WriteAllBytes($zip,$bytes)
echo Write-Host '  Extracting bundle...'
echo Expand-Archive $zip $tmp -Force
echo $bat=Join-Path $tmp 'bundle\install.bat'
echo Write-Host '  Launching installer...'
echo $p=Start-Process cmd -ArgumentList('/c',$bat) -Wait -PassThru
echo exit $p.ExitCode
) > "%PS1%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
del /f /q "%PS1%" 2>nul
exit /b
::__ARCHIVE__
"""


# ── Inner bundle installer scripts (run from inside extracted temp dir) ───────

LINUX_BUNDLE_INSTALLER = r"""#!/usr/bin/env bash
# Inner installer — runs from inside the extracted temp bundle.
set -euo pipefail
IFS=$'\n\t'

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info() { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()  { echo -e "${RED}[ERR ]${NC}  $*" >&2; exit 1; }

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WHEELS="${BUNDLE_DIR}/wheels"
SRC="${BUNDLE_DIR}/src"
INSTALL="${HOME}/.local/share/tally-extractor"
WEB="${INSTALL}/tally_web"
VENV="${WEB}/venv"
BIN="${HOME}/.local/bin"
APPS="${HOME}/.local/share/applications"
SVCD="${HOME}/.config/systemd/user"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║      Tally Extractor — Arch Linux Installer      ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# 1. Python check
echo -e "${BOLD}[1/5] Checking Python 3.10+...${NC}"
command -v python3 &>/dev/null || die "python3 not found. Run: sudo pacman -Sy python"
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
python3 -c "import sys; exit(0 if sys.version_info>=(3,10) else 1)" \
    || die "Python $PY_VER is too old. Need 3.10+. Run: sudo pacman -Sy python"
ok "Python $PY_VER"

# 2. Copy source
echo -e "\n${BOLD}[2/5] Installing application to $INSTALL ...${NC}"
mkdir -p "$INSTALL"
cp -r "$SRC/." "$INSTALL/"
ok "Files installed."

# 3. Venv
echo -e "\n${BOLD}[3/5] Creating virtual environment...${NC}"
[ -f "$VENV/bin/activate" ] && info "Already exists — skipping." || python3 -m venv "$VENV"
source "$VENV/bin/activate"

# 4. Packages from bundled wheels
echo -e "\n${BOLD}[4/5] Installing packages (offline)...${NC}"
pip install --upgrade pip --quiet --no-index --find-links "$WHEELS" 2>/dev/null || true
if ! pip install --no-index --find-links "$WHEELS" -r "$WEB/requirements.txt" --quiet; then
    warn "Offline install failed (bundled for PYVER_PLACEHOLDER, you have $PY_VER)."
    warn "Falling back to PyPI (internet required)..."
    pip install -r "$WEB/requirements.txt" --quiet
fi
deactivate
ok "Packages installed."

# 5. CLI + desktop + service
echo -e "\n${BOLD}[5/5] Creating launcher and shortcuts...${NC}"
mkdir -p "$BIN"
CLI="$BIN/tallyextractor"
cat > "$CLI" <<CLIEOF
#!/usr/bin/env bash
source "$VENV/bin/activate"
echo "  Tally Extractor — http://127.0.0.1:8888  (Ctrl+C to stop)"
cd "$WEB"
exec python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8888 "\$@"
CLIEOF
chmod +x "$CLI"

# Add to PATH if needed
[[ ":$PATH:" != *":$BIN:"* ]] && for RC in ~/.zshrc ~/.bashrc ~/.profile; do
    [ -f "$RC" ] && { echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC"; \
        warn "Added ~/.local/bin to PATH in $RC (run: source $RC)"; break; }
done

mkdir -p "$APPS"
cat > "$APPS/tally-extractor.desktop" <<DESK
[Desktop Entry]
Version=1.0
Type=Application
Name=Tally Extractor
Comment=Extract Tally Prime transactional data
Exec=$CLI
Icon=accessories-calculator
Terminal=true
Categories=Office;Finance;
DESK
command -v update-desktop-database &>/dev/null && update-desktop-database "$APPS" 2>/dev/null || true

mkdir -p "$SVCD"
cat > "$SVCD/tally-extractor.service" <<SVC
[Unit]
Description=Tally Extractor
After=network.target
[Service]
WorkingDirectory=$WEB
ExecStart=$VENV/bin/python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8888
Restart=on-failure
[Install]
WantedBy=default.target
SVC
systemctl --user daemon-reload 2>/dev/null || true

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║           Installation Complete!                 ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}Run:${NC}    tallyextractor"
echo -e "  ${GREEN}Menu:${NC}   Tally Extractor"
echo -e "  ${GREEN}URL:${NC}    http://127.0.0.1:8888"
echo ""
echo -e "  ${YELLOW}Before first extraction:${NC}"
echo    "    1. Open Tally Prime & load the TDL file:"
echo    "       F1 Help > TDLs & Add-Ons > Manage Local TDLs"
echo    "       File: $INSTALL/API_Extractor.txt"
echo    "    2. Enable HTTP Server on port 9000."
echo ""
"""

WINDOWS_BUNDLE_INSTALLER = r"""@echo off
setlocal enabledelayedexpansion
title Tally Extractor — Offline Installer
:: Built for PYVER_PLACEHOLDER

set "BUNDLE=%~dp0"
if "%BUNDLE:~-1%"=="\" set "BUNDLE=%BUNDLE:~0,-1%"
set "INSTALL=%USERPROFILE%\Tally-Extractor"
set "WHEELS=%BUNDLE%\wheels"
set "SRC=%BUNDLE%\src"

echo.
echo  ╔══════════════════════════════════════════════════╗
echo  ║     Tally Extractor — Offline Windows Installer ║
echo  ║     Built for: PYVER_PLACEHOLDER                ║
echo  ╚══════════════════════════════════════════════════╝
echo.

echo [1/5] Checking Python 3.10+...
where python >nul 2>&1 || (echo ERROR: Python not found. Get it at https://www.python.org/downloads/ && pause && exit /b 1)
python -c "import sys;exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 (echo ERROR: Python too old. Need 3.10+. && pause && exit /b 1)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo Found: %%v

echo.
echo [2/5] Copying files to %INSTALL%...
if not exist "%INSTALL%" mkdir "%INSTALL%"
xcopy /e /y /q "%SRC%\*" "%INSTALL%\" >nul || (echo ERROR: Copy failed. && pause && exit /b 1)

echo.
echo [3/5] Creating virtual environment...
set "VENV=%INSTALL%\tally_web\venv"
if exist "%VENV%\Scripts\activate.bat" (echo Already exists.) else (
    python -m venv "%VENV%" || (echo ERROR: venv failed. && pause && exit /b 1)
)

echo.
echo [4/5] Installing packages from bundled wheels (offline)...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip --quiet --no-index --find-links "%WHEELS%" 2>nul
"%VENV%\Scripts\pip.exe" install --no-index --find-links "%WHEELS%" -r "%INSTALL%\tally_web\requirements.txt" --quiet
if errorlevel 1 (
    echo  ERROR: Offline package install failed.
    echo  This bundle is built for PYVER_PLACEHOLDER.
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo  Your version: %%v
    echo  Ensure your Python version matches.
    pause & exit /b 1
)

echo.
echo [5/5] Creating launcher and shortcuts...
set "LAUNCHER=%INSTALL%\Launch Tally Extractor.bat"
(
echo @echo off
echo title Tally Extractor
echo cd /d "%INSTALL%\tally_web"
echo echo. & echo   Open http://127.0.0.1:8888 in your browser & echo   Ctrl+C to stop. & echo.
echo "%VENV%\Scripts\python.exe" -m uvicorn backend.app:app --host 127.0.0.1 --port 8888
echo pause
) > "%LAUNCHER%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws=New-Object -ComObject WScript.Shell;$s=$ws.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Tally Extractor.lnk');$s.TargetPath='%LAUNCHER%';$s.IconLocation='C:\Windows\System32\SHELL32.dll,14';$s.Save()"

set "SM=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Tally Extractor"
if not exist "%SM%" mkdir "%SM%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws=New-Object -ComObject WScript.Shell;$s=$ws.CreateShortcut('%SM%\Tally Extractor.lnk');$s.TargetPath='%LAUNCHER%';$s.IconLocation='C:\Windows\System32\SHELL32.dll,14';$s.Save()"

echo.
echo  ╔══════════════════════════════════════════════════╗
echo  ║          Installation Complete!                  ║
echo  ╚══════════════════════════════════════════════════╝
echo.
echo   Start: Desktop shortcut or Start Menu "Tally Extractor"
echo   URL:   http://127.0.0.1:8888
echo.
echo   Before first use:
echo     1. Open Tally Prime.
echo     2. F1 Help ^> TDLs ^& Add-Ons ^> Manage Local TDLs
echo        File: %INSTALL%\API_Extractor.txt
echo     3. Enable HTTP Server on port 9000.
echo.
pause
endlocal
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def should_exclude(p: Path) -> bool:
    return any(part in SRC_EXCLUDES or part.endswith((".pyc", ".pyo", ".log"))
               for part in p.parts)


def copy_src(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for rel in SRC_INCLUDES:
        src = REPO_ROOT / rel
        if not src.exists():
            print(f"  [WARN] Not found, skipping: {src}")
            continue
        if src.is_file():
            shutil.copy2(src, dest / src.name)
        else:
            dst = dest / src.name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst,
                ignore=lambda d, ns: [n for n in ns
                                      if should_exclude(Path(d) / n)])
    print(f"  Source copied → {dest}")


def download_wheels(wheels_dir: Path, platform: str, py_ver: str, abi: str) -> None:
    wheels_dir.mkdir(parents=True, exist_ok=True)
    packages = [l.strip() for l in REQUIREMENTS.read_text().splitlines()
                if l.strip() and not l.startswith("#")]
    flags = ["--platform", platform, "--python-version", py_ver,
             "--implementation", "cp", "--abi", abi, "--only-binary", ":all:"]

    # Bulk attempt
    r = subprocess.run([sys.executable, "-m", "pip", "download",
                        "--dest", str(wheels_dir), *flags,
                        "-r", str(REQUIREMENTS), "--quiet"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("  [WARN] Bulk download failed — trying per-package...")
        failed = []
        for pkg in packages:
            rc = subprocess.run([sys.executable, "-m", "pip", "download",
                                 "--dest", str(wheels_dir), "--no-deps",
                                 *flags, pkg, "--quiet"],
                                capture_output=True).returncode
            if rc != 0:
                failed.append(pkg)
        for pkg in failed:  # pure-Python fallback
            subprocess.run([sys.executable, "-m", "pip", "download",
                            "--dest", str(wheels_dir), "--no-deps",
                            pkg, "--quiet"], capture_output=True)

    # Always grab pip itself
    subprocess.run([sys.executable, "-m", "pip", "download", "pip",
                    "--dest", str(wheels_dir), "--quiet"],
                   capture_output=True, check=False)
    print(f"  {len(list(wheels_dir.glob('*.whl')))} wheels collected")


def make_bundle_bytes_linux(bundle_dir: Path) -> bytes:
    """Pack bundle_dir into an in-memory tar.gz and return the raw bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(bundle_dir, arcname="bundle")
    return buf.getvalue()


def make_bundle_bytes_windows(bundle_dir: Path) -> bytes:
    """Pack bundle_dir into an in-memory ZIP and return the raw bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in bundle_dir.rglob("*"):
            if f.is_file():
                zf.write(f, Path("bundle") / f.relative_to(bundle_dir))
    return buf.getvalue()


def write_sfx_linux(sfx_path: Path, bundle_bytes: bytes, py_ver: str) -> None:
    """Write a single self-extracting .sh with the bundle appended as base64."""
    b64 = base64.b64encode(bundle_bytes).decode("ascii")
    header = LINUX_SFX_HEADER.replace("PYVER_PLACEHOLDER",
                                      f"Python 3.{py_ver[1:]} (cp{py_ver})")
    with open(sfx_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(header)
        for i in range(0, len(b64), 76):
            f.write(b64[i:i+76] + "\n")
    sfx_path.chmod(0o755)
    print(f"  Single-file installer → {sfx_path.name}  "
          f"({sfx_path.stat().st_size/1_048_576:.1f} MB)")


def write_sfx_windows(sfx_path: Path, bundle_bytes: bytes, py_ver: str) -> None:
    """Write a single self-extracting .bat with the bundle appended as base64."""
    b64 = base64.b64encode(bundle_bytes).decode("ascii")
    header = WINDOWS_SFX_HEADER.replace("PYVER_PLACEHOLDER",
                                        f"Python 3.{py_ver[1:]} (cp{py_ver})")
    with open(sfx_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(header)
        for i in range(0, len(b64), 76):
            f.write(b64[i:i+76] + "\r\n")
    print(f"  Single-file installer → {sfx_path.name}  "
          f"({sfx_path.stat().st_size/1_048_576:.1f} MB)")


# ── Build functions ───────────────────────────────────────────────────────────

def build_linux(py_ver: str) -> tuple[Path, Path]:
    sep = "─" * 60
    print(f"\n{sep}\n  Building Arch Linux installers (cp{py_ver})...\n{sep}")
    build = DIST_DIR / "_build_linux"
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)

    print("  [1/3] Copying source...")
    copy_src(build / "src")

    print(f"  [2/3] Downloading wheels (cp{py_ver}-manylinux_2_17_x86_64)...")
    download_wheels(build / "wheels", "manylinux_2_17_x86_64", py_ver, f"cp{py_ver}")

    print("  [3/3] Writing installer scripts...")
    label = f"Python 3.{py_ver[1:]} (cp{py_ver})"
    install_sh = build / "install.sh"
    install_sh.write_text(
        LINUX_BUNDLE_INSTALLER.replace("PYVER_PLACEHOLDER", label),
        encoding="utf-8")
    install_sh.chmod(0o755)

    # — classic .tar.gz bundle
    tar_out = DIST_DIR / "tally-extractor-arch-linux.tar.gz"
    with tarfile.open(tar_out, "w:gz") as tf:
        tf.add(build, arcname="tally-extractor-arch-linux")
    print(f"  Bundle archive  → {tar_out.name}  "
          f"({tar_out.stat().st_size/1_048_576:.1f} MB)")

    # — single-file SFX
    sfx_out = DIST_DIR / "tally-extractor-arch-linux-installer.sh"
    bundle_bytes = make_bundle_bytes_linux(build)
    write_sfx_linux(sfx_out, bundle_bytes, py_ver)

    shutil.rmtree(build)
    return tar_out, sfx_out


def build_windows(py_ver: str) -> tuple[Path, Path]:
    sep = "─" * 60
    print(f"\n{sep}\n  Building Windows installers (cp{py_ver})...\n{sep}")
    build = DIST_DIR / "_build_windows"
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)

    print("  [1/3] Copying source...")
    copy_src(build / "src")

    print(f"  [2/3] Downloading wheels (cp{py_ver}-win_amd64)...")
    download_wheels(build / "wheels", "win_amd64", py_ver, f"cp{py_ver}")

    print("  [3/3] Writing installer scripts...")
    label = f"Python 3.{py_ver[1:]} (cp{py_ver})"
    install_bat = build / "install.bat"
    install_bat.write_text(
        WINDOWS_BUNDLE_INSTALLER.replace("PYVER_PLACEHOLDER", label),
        encoding="utf-8")

    # — classic .zip bundle
    zip_out = DIST_DIR / "tally-extractor-windows.zip"
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in build.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(build))
    print(f"  Bundle archive  → {zip_out.name}  "
          f"({zip_out.stat().st_size/1_048_576:.1f} MB)")

    # — single-file SFX
    sfx_out = DIST_DIR / "tally-extractor-windows-installer.bat"
    bundle_bytes = make_bundle_bytes_windows(build)
    write_sfx_windows(sfx_out, bundle_bytes, py_ver)

    shutil.rmtree(build)
    return zip_out, sfx_out


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Build single-file self-extracting offline installers.")
    p.add_argument("--win-python", default="312",
                   help="cp version for Windows wheels (default: 312)")
    p.add_argument("--lin-python", default="312",
                   help="cp version for Linux wheels (default: 312)")
    p.add_argument("--skip-win",   action="store_true")
    p.add_argument("--skip-linux", action="store_true")
    args = p.parse_args()

    print()
    print("=" * 60)
    print("  Tally Extractor — Offline Installer Builder")
    print("=" * 60)
    print(f"  Windows target : cp{args.win_python}-win_amd64")
    print(f"  Linux target   : cp{args.lin_python}-manylinux_2_17_x86_64")
    print(f"  Output dir     : {DIST_DIR}")
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    all_outputs: list[Path] = []
    sfx_outputs: list[Path] = []

    if not args.skip_linux:
        tar, sfx = build_linux(args.lin_python)
        all_outputs += [tar, sfx]
        sfx_outputs.append(sfx)

    if not args.skip_win:
        zp, sfx = build_windows(args.win_python)
        all_outputs += [zp, sfx]
        sfx_outputs.append(sfx)

    print()
    print("=" * 60)
    print("  Done!")
    print("=" * 60)
    for f in all_outputs:
        print(f"  {f.name:<55} {f.stat().st_size/1_048_576:5.1f} MB")
    print()
    print("  ★ Single-file installers (share just these):")
    for f in sfx_outputs:
        print(f"    {f}")
    print()


if __name__ == "__main__":
    main()
