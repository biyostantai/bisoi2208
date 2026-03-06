#!/bin/bash
# FuBot v6.0 - One-command VPS deployment
# Usage: bash start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================="
echo "  FuBot v6.0 - VPS Setup & Start"
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

# 5. OpenRouter DeepSeek v3.2 only mode (default ON)
# Disable with: OPENROUTER_DEEPSEEK_ONLY=0 bash start.sh
if [ "${OPENROUTER_DEEPSEEK_ONLY:-1}" = "1" ]; then
    export OPENROUTER_DEEPSEEK_ONLY=1
    export AI_FORCE_DEEPSEEK_ONLY=1
    export OPENROUTER_BASE_URL="${OPENROUTER_BASE_URL:-https://openrouter.ai/api/v1}"
    export OPENROUTER_DEEPSEEK_MODEL="${OPENROUTER_DEEPSEEK_MODEL:-deepseek/deepseek-v3.2}"
    case "${OPENROUTER_BASE_URL}" in
        *openrouter.ai/deepseek/*)
            if [ -z "${OPENROUTER_DEEPSEEK_MODEL:-}" ] || [ "${OPENROUTER_DEEPSEEK_MODEL}" = "deepseek/deepseek-v3.2" ]; then
                export OPENROUTER_DEEPSEEK_MODEL="${OPENROUTER_BASE_URL#*openrouter.ai/}"
            fi
            export OPENROUTER_BASE_URL="https://openrouter.ai/api/v1"
            ;;
    esac
    export DEEPSEEK_BASE_URL="$OPENROUTER_BASE_URL"
    export DEEPSEEK_MODEL="$OPENROUTER_DEEPSEEK_MODEL"

    # Key priority for DeepSeek/OpenRouter route:
    # 1) OPENROUTER_API_KEY, 2) GPT_API_KEY if sk-or-..., 3) DEEPSEEK_API_KEY if sk-or-...
    if [ -n "${OPENROUTER_API_KEY:-}" ]; then
        export DEEPSEEK_API_KEY="$OPENROUTER_API_KEY"
    elif [ -n "${GPT_API_KEY:-}" ]; then
        case "${GPT_API_KEY}" in
            sk-or-*) export DEEPSEEK_API_KEY="${GPT_API_KEY}" ;;
        esac
    elif [ -n "${DEEPSEEK_API_KEY:-}" ]; then
        case "${DEEPSEEK_API_KEY}" in
            sk-or-*) export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY}" ;;
        esac
    fi

    # Keep GPT vars aligned for legacy paths.
    if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
        export GPT_API_KEY="${DEEPSEEK_API_KEY}"
    fi
    export GPT_BASE_URL="$DEEPSEEK_BASE_URL"
    export GPT_MODEL="$DEEPSEEK_MODEL"
    export AI_FAST_3L_MODE=1
    export AI_GPT_ONLY_MODE=0
    export GPT_L3_ENSEMBLE=1
    export GPT_L3_QUORUM=1

    # Optional OpenRouter attribution headers.
    export OPENROUTER_X_TITLE="${OPENROUTER_X_TITLE:-fubot}"
    echo "[OK] OpenRouter DeepSeek-only mode ON (${DEEPSEEK_MODEL})"
    echo "[OK] Fast profile ON (FAST3L=1, GPT-only=0, L3 ensemble=1/1)"
else
    echo "[INFO] OpenRouter DeepSeek-only mode OFF (OPENROUTER_DEEPSEEK_ONLY=0)"
fi

# 6. Quick validation
python3 -c "import config; print(f'[OK] Config loaded - {len(config.COINS)} coins')" || {
    echo "[ERROR] Config validation failed"
    exit 1
}

# 7. LLM preflight (skip with SKIP_LLM_PREFLIGHT=1)
if [ "${SKIP_LLM_PREFLIGHT:-0}" != "1" ]; then
    echo "[*] Running LLM preflight..."
    python3 llm_preflight.py || {
        echo "[ERROR] LLM preflight failed. Check API key/base/model in .env"
        echo "[HINT] Set SKIP_LLM_PREFLIGHT=1 only if you intentionally bypass this check."
        exit 1
    }
else
    echo "[WARN] SKIP_LLM_PREFLIGHT=1 - skipping LLM connectivity check"
fi

# 8. Kill any stale instance and clean lock
LOCK_FILE="$SCRIPT_DIR/.fubot.instance.lock"
if [ -f "$LOCK_FILE" ]; then
    OLD_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[*] Stopping old FuBot instance (PID=$OLD_PID)..."
        kill "$OLD_PID" 2>/dev/null
        sleep 2
    fi
    rm -f "$LOCK_FILE"
    echo "[OK] Cleared stale lock"
fi
# Also kill any leftover python3 main.py processes
pkill -f "python3 main.py" 2>/dev/null || true
pkill -f "python main.py" 2>/dev/null || true
sleep 1

# 9. Run bot
echo ""
echo "========================================="
echo "  Starting FuBot..."
echo "========================================="
echo ""

exec python3 main.py
