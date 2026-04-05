#!/bin/bash
# start.sh — Launch the Tally Pipeline Web Interface
# Usage: bash start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "═══════════════════════════════════════════"
echo "  Tally Pipeline Web Interface"
echo "═══════════════════════════════════════════"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "❌ Python 3 is required but not found."
  exit 1
fi

# Install dependencies if needed
if ! python3 -c "import fastapi" &>/dev/null 2>&1; then
  echo "Installing Python dependencies…"
  python3 -m pip install -r requirements.txt --quiet
  echo "✅ Dependencies installed."
fi

echo "Starting server at http://127.0.0.1:8080"
echo "Open your browser and navigate to: http://127.0.0.1:8080"
echo "(Press Ctrl+C to stop)"
echo ""

python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8080 --reload
