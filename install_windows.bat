@echo off
setlocal enabledelayedexpansion
title Tally Extractor — Windows Installer

REM ════════════════════════════════════════════════════════════════
REM  install_windows.bat
REM  Full installer for Tally Extractor on Windows.
REM
REM  Supports Standalone Mode:
REM    If shared as a single file, it will automatically download the
REM    codebase from GitHub and install it to %USERPROFILE%\Tally-Extractor.
REM ════════════════════════════════════════════════════════════════

echo.
echo  ╔══════════════════════════════════════════════════╗
echo  ║         Tally Extractor — Windows Installer      ║
echo  ╚══════════════════════════════════════════════════╝
echo.

REM --- Check Python First ---------------------------------------
echo [1/6] Checking for Python 3.10+...
where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: Python was not found in PATH.
    echo  Please install Python 3.10 or newer from https://www.python.org/downloads/
    echo  Make sure to tick "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

python -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PY_VER=%%v"
    echo  ERROR: Found !PY_VER! but Tally Extractor requires Python 3.10 or newer.
    echo  Download the latest Python from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo  Found: %%v  ^(OK^)

REM --- Determine Installation Mode ------------------------------
set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

if exist "%REPO_ROOT%\tally_web\requirements.txt" (
    echo.
    echo  Detected local codebase. Installing in local directory...
    set "INSTALL_DIR=%REPO_ROOT%"
) else (
    echo.
    echo  Standalone mode: Local codebase files not found.
    echo  Downloading Tally Extractor from GitHub...
    set "INSTALL_DIR=%USERPROFILE%\Tally-Extractor"
    
    if not exist "!INSTALL_DIR!" mkdir "!INSTALL_DIR!"
    
    echo  Downloading ZIP archive...
    powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/Malviya-Mayur/Tally-Extractor/archive/refs/heads/main.zip' -OutFile '%TEMP%\tally.zip'"
    if errorlevel 1 (
        echo  ERROR: Failed to download source files from GitHub. Check your internet connection.
        pause
        exit /b 1
    )
    
    echo  Extracting archive...
    if exist "%TEMP%\tally_extract" rmdir /s /q "%TEMP%\tally_extract"
    powershell -Command "Expand-Archive -Path '%TEMP%\tally.zip' -DestinationPath '%TEMP%\tally_extract' -Force"
    
    echo  Copying files to !INSTALL_DIR!...
    xcopy /e /y /q "%TEMP%\tally_extract\Tally-Extractor-main\*" "!INSTALL_DIR!\"
    
    :: Cleanup temp downloads
    del "%TEMP%\tally.zip"
    rmdir /s /q "%TEMP%\tally_extract"
    echo  Codebase downloaded and ready.
)

set "WEB_DIR=!INSTALL_DIR!\tally_web"
set "VENV_DIR=!WEB_DIR!\venv"
set "LAUNCHER=!INSTALL_DIR!\Launch Tally Extractor.bat"

REM ─── Step 3: Create virtual environment ───────────────────────
echo.
echo [3/6] Creating virtual environment in tally_web\venv\...
if exist "!VENV_DIR!\Scripts\activate.bat" (
    echo  Virtual environment already exists — skipping creation.
) else (
    python -m venv "!VENV_DIR!"
    if errorlevel 1 (
        echo.
        echo  ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo  Virtual environment created.
)

REM ─── Step 4: Install dependencies ─────────────────────────────
echo.
echo [4/6] Installing Python dependencies (this may take a minute)...
"!VENV_DIR!\Scripts\python.exe" -m pip install --upgrade pip --quiet
"!VENV_DIR!\Scripts\pip.exe" install -r "!WEB_DIR!\requirements.txt" --quiet
if errorlevel 1 (
    echo.
    echo  ERROR: pip install failed. Check your internet connection.
    pause
    exit /b 1
)

:: Optional speed upgrade
echo  Installing optional lxml parser (speeds up XML parsing by 3-5x)...
"!VENV_DIR!\Scripts\pip.exe" install lxml --quiet >nul 2>&1
echo  Dependencies setup complete.

REM ─── Step 5: Create launcher ──────────────────────────────────
echo.
echo [5/6] Creating launcher: "Launch Tally Extractor.bat" ...
(
    echo @echo off
    echo title Tally Extractor
    echo cd /d "!WEB_DIR!"
    echo echo.
    echo echo   Starting Tally Extractor...
    echo echo   Open your browser at: http://127.0.0.1:8888
    echo echo   Press Ctrl+C to stop.
    echo echo.
    echo "!VENV_DIR!\Scripts\python.exe" -m uvicorn backend.app:app --host 127.0.0.1 --port 8888
    echo pause
) > "!LAUNCHER!"

REM ─── Step 6: Create Desktop & Start Menu Shortcuts ────────────
echo.
echo [6/6] Creating Desktop and Start Menu shortcuts...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell; ^
   $shortcut = $ws.CreateShortcut([System.Environment]::GetFolderPath('Desktop') + '\Tally Extractor.lnk'); ^
   $shortcut.TargetPath = '!LAUNCHER!'; ^
   $shortcut.WorkingDirectory = '!WEB_DIR!'; ^
   $shortcut.Description = 'Launch Tally Extractor Web Interface'; ^
   $shortcut.IconLocation = 'C:\Windows\System32\SHELL32.dll,14'; ^
   $shortcut.Save()"

set "START_MENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Tally Extractor"
if not exist "%START_MENU%" mkdir "%START_MENU%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell; ^
   $shortcut = $ws.CreateShortcut('%START_MENU%\Tally Extractor.lnk'); ^
   $shortcut.TargetPath = '!LAUNCHER!'; ^
   $shortcut.WorkingDirectory = '!WEB_DIR!'; ^
   $shortcut.Description = 'Launch Tally Extractor Web Interface'; ^
   $shortcut.IconLocation = 'C:\Windows\System32\SHELL32.dll,14'; ^
   $shortcut.Save()"

echo.
echo  ╔══════════════════════════════════════════════════╗
echo  ║          Installation Complete!                  ║
echo  ╚══════════════════════════════════════════════════╝
echo.
echo   Application installed to: !INSTALL_DIR!
echo.
echo   How to start:
echo     • Double-click "Tally Extractor" on your Desktop, OR
echo     • Search for "Tally Extractor" in the Start Menu.
echo.
echo   Then open your browser at:  http://127.0.0.1:8888
echo.
pause
endlocal
