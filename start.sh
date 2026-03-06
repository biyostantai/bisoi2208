#!/bin/bash
# FuBot v6.0 — One-command VPS deployment
# Usage: bash start.sh

set -e

echo "========================================="
echo "  FuBot v6.0 — VPS Setup & Start"
echo "========================================="

# 1. Check Python 3.10+
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] Python3 not found. Install: sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[OK] Python $PY_VERSION"

# 2. Create venv if not exists
if [ ! -d ".venv" ]; then
    echo "[*] Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate
echo "[OK] venv activated"

# 3. Install dependencies
echo "[*] Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# 4. Check .env
if [ ! -f ".env" ]; then
    echo ""
    echo "[!] File .env not found!"
    echo "    Copy .env.example and fill in your keys:"
    echo ""
    echo "    cp .env.example .env"
    echo "    nano .env"
    echo ""
    exit 1
fi
echo "[OK] .env found"

# 5. Quick validation
python3 -c "import config; print(f'[OK] Config loaded — {len(config.COINS)} coins')" || {
    echo "[ERROR] Config validation failed"
    exit 1
}

# 6. Run bot
echo ""
echo "========================================="
echo "  Starting FuBot..."
echo "========================================="
echo ""

# Use nohup so bot survives SSH disconnect
# Logs go to fubot.log + stdout
exec python3 main.py
