@echo off
REM start.bat — Launch the Tally Pipeline Web Interface on Windows
REM Usage: Double-click or run from command prompt

cd /d "%~dp0"

echo.
echo ==================================================
echo   Tally Pipeline Web Interface
echo ==================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    pause
    exit /b 1
)

python -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo Installing Python dependencies...
    python -m pip install -r requirements.txt --quiet
    echo Dependencies installed.
)

echo Starting server at http://127.0.0.1:8080
echo Open your browser and navigate to: http://127.0.0.1:8080
echo Press Ctrl+C to stop.
echo.

python -m uvicorn backend.app:app --host 127.0.0.1 --port 8080
pause
