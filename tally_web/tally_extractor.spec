# -*- mode: python ; coding: utf-8 -*-
# tally_web/tally_extractor.spec
# ──────────────────────────────
# PyInstaller spec for Tally Extractor single-file Windows .exe
#
# Build command (run from inside tally_web/):
#   pip install pyinstaller
#   pyinstaller tally_extractor.spec
#
# Output: tally_web/dist/TallyExtractor.exe

from PyInstaller.utils.hooks import collect_all, collect_submodules

# ── Collect data/binaries for packages with internal resources ────────────────
datas    = []
binaries = []
hiddenimports = []

for pkg in ("uvicorn", "fastapi", "starlette", "pydantic", "sse_starlette",
            "anyio", "httptools", "websockets", "h11"):
    d, b, h = collect_all(pkg)
    datas    += d
    binaries += b
    hiddenimports += h

# ── Explicit hidden imports that PyInstaller often misses ─────────────────────
hiddenimports += [
    # uvicorn internals
    "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan", "uvicorn.lifespan.off", "uvicorn.lifespan.on",
    # pydantic
    "pydantic_core", "pydantic.deprecated.class_validators",
    # stdlib
    "xml.etree.ElementTree", "email.mime.text", "email.mime.multipart",
    "tkinter", "tkinter.filedialog",
    # our own packages
    "backend", "backend.app", "backend.jobs", "backend.pipeline_runner",
    "backend._compat",
    # pipeline
    "openpyxl", "openpyxl.styles", "openpyxl.utils",
    "requests", "yaml",
]

# ── Bundle our static files and data ─────────────────────────────────────────
# Format: (source_path, dest_folder_inside_bundle)
datas += [
    ("frontend",    "frontend"),    # HTML / CSS / JS served to browser
    ("config.yaml", "."),           # Default configuration
    ("backend",     "backend"),     # Python source (for import)
    # Pipeline scripts (used by pipeline_runner at runtime)
    ("../Tally_Pipeline_V2.py", "."),
    ("../Tally_Pipeline.py",    "."),
    ("../API_Extractor.txt",    "."),
]

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ["app_entry.py"],       # Entry point
    pathex=["."],           # tally_web/  is on sys.path during analysis
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Things we definitely don't need in the bundle
        "matplotlib", "numpy", "pandas", "PIL", "scipy", "IPython",
        "pytest", "setuptools", "distutils",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# ── Single-file EXE ───────────────────────────────────────────────────────────
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TallyExtractor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,    # extract to a fresh temp dir each run
    console=True,           # keep console so the user can see logs & Ctrl+C
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="frontend/favicon.ico",  # uncomment and add an icon file if desired
)
