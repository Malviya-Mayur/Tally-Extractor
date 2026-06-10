# Maintainer: Mayur Malviya <malviya-mayur>
# PKGBUILD for Tally Extractor
#
# To build and install locally:
#   makepkg -si
#
# To build a package tarball for distribution:
#   makepkg --noextract   # if sources are already present
#
# Note: This PKGBUILD installs tally-extractor system-wide under
#   /opt/tally-extractor  and creates a /usr/bin/tallyextractor shim.
#   Python deps are installed into a private venv at install time by the
#   .install helper so they never pollute the system Python.

pkgname=tally-extractor
pkgver=2.0.0
pkgrel=1
pkgdesc="Local web application for extracting and flattening Tally Prime transactional data"
arch=('any')
url="https://github.com/Malviya-Mayur/Tally-Extractor"
license=('MIT')
depends=(
    'python>=3.10'
    'python-pip'
)
optdepends=(
    'python-lxml: 3-5x faster XML parsing'
)
makedepends=('git')

# ── Source ────────────────────────────────────────────────────────────────────
# For a local build from the working tree, comment out the git source
# and uncomment the local line:
#   source=("${pkgname}::file:///path/to/your/Tally-Extractor")

source=("${pkgname}::git+https://github.com/Malviya-Mayur/Tally-Extractor.git#branch=main")
sha256sums=('SKIP')

# ── Installation paths ────────────────────────────────────────────────────────
_install_dir="/opt/tally-extractor"
_venv_dir="${_install_dir}/venv"
_bin_shim="/usr/bin/tallyextractor"
_desktop_dir="/usr/share/applications"
_systemd_dir="/usr/lib/systemd/user"

prepare() {
    cd "${srcdir}/${pkgname}"
    # Nothing to patch — pure Python project
}

build() {
    # No compilation step required
    :
}

package() {
    cd "${srcdir}/${pkgname}"

    # ── Install repo files ───────────────────────────────────────
    install -dm755 "${pkgdir}${_install_dir}"
    cp -r . "${pkgdir}${_install_dir}/"

    # Remove development and git artefacts from the installed copy
    rm -rf \
        "${pkgdir}${_install_dir}/.git" \
        "${pkgdir}${_install_dir}/__pycache__" \
        "${pkgdir}${_install_dir}/tally_web/venv" \
        "${pkgdir}${_install_dir}/tally_web/__pycache__" \
        "${pkgdir}${_install_dir}/tally_web/backend/__pycache__"

    # ── /usr/bin shim ────────────────────────────────────────────
    install -dm755 "${pkgdir}/usr/bin"
    cat > "${pkgdir}${_bin_shim}" <<SHIM
#!/usr/bin/env bash
# tallyextractor — start the Tally Extractor web interface
source "${_venv_dir}/bin/activate"
cd "${_install_dir}/tally_web"
exec python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8888 "\$@"
SHIM
    chmod 755 "${pkgdir}${_bin_shim}"

    # ── .desktop entry ───────────────────────────────────────────
    install -dm755 "${pkgdir}${_desktop_dir}"
    cat > "${pkgdir}${_desktop_dir}/tally-extractor.desktop" <<DESKTOP
[Desktop Entry]
Version=1.0
Type=Application
Name=Tally Extractor
GenericName=Tally Data Extractor
Comment=Extract and process Tally Prime transactional data via a web interface
Exec=${_bin_shim}
Icon=accessories-calculator
Terminal=true
Categories=Office;Finance;
Keywords=tally;extractor;accounting;finance;erp;
StartupNotify=false
DESKTOP

    # ── Systemd user service ─────────────────────────────────────
    install -dm755 "${pkgdir}${_systemd_dir}"
    cat > "${pkgdir}${_systemd_dir}/tally-extractor.service" <<SERVICE
[Unit]
Description=Tally Extractor Web Interface
After=network.target

[Service]
Type=simple
WorkingDirectory=${_install_dir}/tally_web
ExecStart=${_venv_dir}/bin/python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8888
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
SERVICE

    # ── README note about post-install step ──────────────────────
    install -Dm644 README.md "${pkgdir}${_install_dir}/README.md"
}

# ── .install hook: creates the venv after files are in place ─────────────────
post_install() {
    echo "==> Creating Python virtual environment and installing dependencies..."
    python3 -m venv "${_venv_dir}" --clear
    "${_venv_dir}/bin/pip" install --upgrade pip --quiet
    "${_venv_dir}/bin/pip" install \
        -r "${_install_dir}/tally_web/requirements.txt" \
        --quiet

    # Optional fast parser
    "${_venv_dir}/bin/pip" install lxml --quiet 2>/dev/null \
        && echo "==> lxml installed (faster XML parsing enabled)." \
        || echo "==> lxml not available; stdlib XML parser will be used."

    # Fix permissions so any user can run (venv itself is read-only for non-root)
    chmod -R a+rX "${_install_dir}"
    chmod -R u+w  "${_install_dir}/tally_web/venv"

    echo ""
    echo "==> Tally Extractor installed successfully!"
    echo "==> Start with:  tallyextractor"
    echo "==> Then visit:  http://127.0.0.1:8888"
    echo ""
    echo "==> To enable auto-start on login:"
    echo "      systemctl --user enable --now tally-extractor.service"
}

post_upgrade() {
    post_install
}

pre_remove() {
    echo "==> Stopping tally-extractor service (if running)..."
    systemctl --user stop tally-extractor.service 2>/dev/null || true
    systemctl --user disable tally-extractor.service 2>/dev/null || true
}
