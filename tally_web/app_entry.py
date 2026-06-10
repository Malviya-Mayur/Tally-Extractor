"""
tally_web/app_entry.py
──────────────────────
PyInstaller entry point for Tally Extractor.

When frozen into a .exe, PyInstaller calls this as __main__.
It wires up the resource paths, starts the FastAPI/uvicorn server,
and opens the browser automatically.
"""

from __future__ import annotations

import sys
import os
import threading
import webbrowser
import time
from pathlib import Path


# ── 1. Patch sys.path so `backend` package is importable ─────────────────────
# PyInstaller extracts all bundled files to sys._MEIPASS.
# We need that directory on sys.path so `import backend.app` works.
_BASE = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent  # type: ignore
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

# ── 2. Point config.yaml writes to user data folder ──────────────────────────
# The .exe bundle is read-only at runtime (extracted to a temp dir).
# We redirect config.yaml writes to %APPDATA%\TallyExtractor on Windows.
_USER_DATA = Path(os.environ.get("APPDATA", Path.home())) / "TallyExtractor"
_USER_DATA.mkdir(parents=True, exist_ok=True)

# Copy the default config.yaml to the user data folder if missing
_BUNDLED_CONFIG = _BASE / "config.yaml"
_USER_CONFIG    = _USER_DATA / "config.yaml"
if not _USER_CONFIG.exists() and _BUNDLED_CONFIG.exists():
    import shutil
    shutil.copy2(_BUNDLED_CONFIG, _USER_CONFIG)

# Tell _compat to use the writable user-data config path
os.environ["TALLY_CONFIG_PATH"] = str(_USER_CONFIG)

# ── 3. Start the server + open browser ───────────────────────────────────────
HOST = "127.0.0.1"
PORT = 8888
URL  = f"http://{HOST}:{PORT}"


def _open_browser():
    time.sleep(2.0)          # give uvicorn time to start
    webbrowser.open(URL)


if __name__ == "__main__":
    import uvicorn

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║         Tally Extractor                  ║")
    print("  ╚══════════════════════════════════════════╝")
    print(f"  Server:   {URL}")
    print("  Browser opens automatically in 2 seconds.")
    print("  Close this window to stop.")
    print()

    threading.Thread(target=_open_browser, daemon=True).start()

    uvicorn.run(
        "backend.app:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="warning",   # suppress uvicorn access logs in the console
    )
