"""
tally_web/backend/_compat.py
────────────────────────────
Path resolution helper for PyInstaller bundles.

When the app is frozen into a single .exe by PyInstaller, all bundled files
live under sys._MEIPASS (a temp directory unpacked at runtime). This module
provides a single get_resource_path() function that returns the correct
absolute path whether running normally or as a frozen bundle.

Import this instead of using Path(__file__) directly anywhere you need to
reference a bundled resource (frontend/, config.yaml, etc.).
"""

from __future__ import annotations

import sys
from pathlib import Path


def _base_dir() -> Path:
    """Return the root directory for bundled resources."""
    if getattr(sys, "frozen", False):
        # PyInstaller bundle: files are extracted to sys._MEIPASS
        return Path(sys._MEIPASS)          # type: ignore[attr-defined]
    # Normal execution: tally_web/ (one level above backend/)
    return Path(__file__).resolve().parent.parent


BASE_DIR: Path = _base_dir()

# Convenience paths
FRONTEND_DIR: Path = BASE_DIR / "frontend"

# When running as a frozen .exe, app_entry.py redirects config writes to
# %APPDATA%/TallyExtractor/config.yaml (the bundle itself is read-only).
# Fall back to the bundled path when running normally.
_env_config = sys.modules["os"].environ.get("TALLY_CONFIG_PATH")  # type: ignore[attr-defined]
CONFIG_PATH: Path = Path(_env_config) if _env_config else BASE_DIR / "config.yaml"
