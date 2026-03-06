# -*- coding: utf-8 -*-
"""
config.py â€” Load cáº¥u hĂ¬nh tá»« file .env
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _parse_coin_list(value: str) -> list[str]:
    """Parse env string list and drop empty items."""
    return [coin.strip() for coin in value.split(",") if coin.strip()]

# ============ OKX ============
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
OKX_DEMO = os.getenv("OKX_DEMO", "1") == "1"  # True = demo mode
OKX_BASE_URL = "https://www.okx.com"

# ============ TELEGRAM ============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
TELEGRAM_LOG_CHAT_ID = int(
    os.getenv("TELEGRAM_LOG_CHAT_ID", str(TELEGRAM_CHAT_ID))
)
TELEGRAM_CONTROL_CHAT_ID = int(
    os.getenv("TELEGRAM_CONTROL_CHAT_ID", str(TELEGRAM_CHAT_ID))
)

# ============ GPT ============
GPT_BASE_URL = os.getenv("GPT_BASE_URL", "http://localhost:8318")
GPT_API_KEY = os.getenv("GPT_API_KEY", "")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")

# ============ DEEPSEEK (anti-echo cho L4+L6) ============
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# ============ TRADING ============
TRADE_AMOUNT_USDT = float(os.getenv("TRADE_AMOUNT_USDT", "20"))
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
POSITION_MARGIN_TARGET_MODE = os.getenv("POSITION_MARGIN_TARGET_MODE", "at_least").strip().lower()

# Per-coin leverage (VD: LEVERAGE_SOL=15, LEVERAGE_ETH=20)
_COIN_LEVERAGE = {}
for _k, _v in os.environ.items():
    if _k.startswith("LEVERAGE_") and _k != "LEVERAGE":
        _coin_name = _k.replace("LEVERAGE_", "")
        _COIN_LEVERAGE[_coin_name] = int(_v)


def get_leverage(symbol: str = "") -> int:
    """Láº¥y leverage cho coin cá»¥ thá»ƒ. VD: SOL-USDT-SWAP â†’ check LEVERAGE_SOL."""
    if symbol:
        coin_short = symbol.split("-")[0].upper()
        if coin_short in _COIN_LEVERAGE:
            return _COIN_LEVERAGE[coin_short]
    return LEVERAGE


TP_PERCENT = float(os.getenv("TP_PERCENT", "5"))
SL_PERCENT = float(os.getenv("SL_PERCENT", "2"))
SL_MEME_PERCENT = float(os.getenv("SL_MEME_PERCENT", "1.5"))
MEME_COINS = os.getenv("MEME_COINS", "PEPE-USDT-SWAP,WIF-USDT-SWAP,BONK-USDT-SWAP,DOGE-USDT-SWAP,FLOKI-USDT-SWAP,SHIB-USDT-SWAP,MEME-USDT-SWAP").split(",")

# Per-coin TP/SL (VD: TP_SOL=3.5, SL_ETH=1.0)
_COIN_TP = {}
_COIN_SL = {}
for _k, _v in os.environ.items():
    if _k.startswith("TP_") and _k != "TP_PERCENT":
        _COIN_TP[_k.replace("TP_", "")] = float(_v)
    elif _k.startswith("SL_") and _k not in ("SL_PERCENT", "SL_MEME_PERCENT"):
        _COIN_SL[_k.replace("SL_", "")] = float(_v)


def get_tp(symbol: str = "") -> float:
    """Láº¥y TP% cho coin cá»¥ thá»ƒ. VD: SOL-USDT-SWAP â†’ check TP_SOL."""
    if symbol:
        coin_short = symbol.split("-")[0].upper()
        if coin_short in _COIN_TP:
            return _COIN_TP[coin_short]
    return TP_PERCENT


def get_sl(symbol: str = "") -> float:
    """Láº¥y SL% cho coin cá»¥ thá»ƒ. VD: ETH-USDT-SWAP â†’ check SL_ETH."""
    if symbol:
        coin_short = symbol.split("-")[0].upper()
        if coin_short in _COIN_SL:
            return _COIN_SL[coin_short]
    if symbol in MEME_COINS:
        return SL_MEME_PERCENT
    return SL_PERCENT


SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))  # giĂ¢y
COOLDOWN_AFTER_TRADE = int(os.getenv("COOLDOWN_AFTER_TRADE", "600"))  # 10 phĂºt sau má»—i lá»‡nh
COOLDOWN_AFTER_LOSS = int(os.getenv("COOLDOWN_AFTER_LOSS", "900"))  # 15 phút sau lệnh thua
COIN_COOLDOWN_AFTER_LOSS = int(os.getenv("COIN_COOLDOWN_AFTER_LOSS", "600"))  # per-coin cooldown sau SL (giây)
AUTO_TRADE = os.getenv("AUTO_TRADE", "1") == "1"  # True = tự động vào kèo
AUTO_TRADE_MIN_WIN = int(os.getenv("AUTO_TRADE_MIN_WIN", "65"))  # % win tá»‘i thiá»ƒu Ä‘á»ƒ auto trade
AUTO_TRADE_MIN_WIN_FLOOR = int(os.getenv("AUTO_TRADE_MIN_WIN_FLOOR", "55"))
AUTO_TRADE_MIN_WIN_DEFICIT_STEP = int(
    os.getenv("AUTO_TRADE_MIN_WIN_DEFICIT_STEP", "4")
)
AUTO_TRADE_MIN_WIN_LOSS_PENALTY = int(
    os.getenv("AUTO_TRADE_MIN_WIN_LOSS_PENALTY", "3")
)
TARGET_TRADES_PER_15M = int(os.getenv("TARGET_TRADES_PER_15M", "2"))
MAX_ACTIVE_TRADES = int(os.getenv("MAX_ACTIVE_TRADES", "2"))
LONG_ONLY = os.getenv("LONG_ONLY", "0") == "1"
PARTIAL_TP_ENABLED = os.getenv("PARTIAL_TP_ENABLED", "1") == "1"
PARTIAL_TP_BE_RATIO = float(os.getenv("PARTIAL_TP_BE_RATIO", "0.3333"))
PARTIAL_TP_CLOSE_RATIO = float(os.getenv("PARTIAL_TP_CLOSE_RATIO", "0.6667"))
PARTIAL_TP_CLOSE_FRACTION = float(os.getenv("PARTIAL_TP_CLOSE_FRACTION", "0.5"))
PARTIAL_TP_MIN_HOLD_SEC = float(os.getenv("PARTIAL_TP_MIN_HOLD_SEC", "120"))
TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.0005"))
MAX_LOSSES_PER_DAY = int(os.getenv("MAX_LOSSES_PER_DAY", "0"))  # 0 = khong gioi han
MAX_CONSECUTIVE_LOSSES = int(
    os.getenv("MAX_CONSECUTIVE_LOSSES", str(MAX_LOSSES_PER_DAY))
)  # 0 = khong gioi han

# Danh sĂ¡ch coin trade
_DEFAULT_COINS = "PEPE-USDT-SWAP,DOGE-USDT-SWAP,SUI-USDT-SWAP,XRP-USDT-SWAP"
COINS_LAYER1 = _parse_coin_list(os.getenv("COINS_LAYER1", os.getenv("COINS", _DEFAULT_COINS)))
COINS_LAYER2 = _parse_coin_list(os.getenv("COINS_LAYER2", ""))
COINS = COINS_LAYER1 + [c for c in COINS_LAYER2 if c not in COINS_LAYER1]


# Runtime performance tuning
GPT_MAX_TOKENS = int(os.getenv("GPT_MAX_TOKENS", "320"))
GPT_REASONING_EFFORT = os.getenv("GPT_REASONING_EFFORT", "minimal").strip().lower()
GPT_VERBOSITY = os.getenv("GPT_VERBOSITY", "low").strip().lower()
GPT_PARALLEL_HARD_LIMIT = int(os.getenv("GPT_PARALLEL_HARD_LIMIT", "3"))
GPT_TIMEOUT_SEC = int(os.getenv("GPT_TIMEOUT_SEC", "25"))
DEEPSEEK_TIMEOUT_SEC = int(os.getenv("DEEPSEEK_TIMEOUT_SEC", "30"))
GPT_TIMEOUT_HARD_CAP_SEC = int(os.getenv("GPT_TIMEOUT_HARD_CAP_SEC", "12"))
DEEPSEEK_TIMEOUT_HARD_CAP_SEC = int(os.getenv("DEEPSEEK_TIMEOUT_HARD_CAP_SEC", "25"))
AI_GPT_ONLY_MODE = os.getenv("AI_GPT_ONLY_MODE", "0") == "1"
AI_FAST_3L_MODE = os.getenv("AI_FAST_3L_MODE", "0") == "1"  # L1+L2 (GPT) + L4 (DeepSeek)
FAST3L_DEEPSEEK_TIMEOUT_SEC = float(os.getenv("FAST3L_DEEPSEEK_TIMEOUT_SEC", "12"))
FAST3L_DEEPSEEK_FALLBACK_PENALTY = int(
    os.getenv("FAST3L_DEEPSEEK_FALLBACK_PENALTY", "12")
)
FAST3L_ADAPTIVE_VETO = os.getenv("FAST3L_ADAPTIVE_VETO", "1") == "1"
FAST3L_ADVISORY_MIN_L3_WIN = int(os.getenv("FAST3L_ADVISORY_MIN_L3_WIN", "74"))
FAST3L_ADVISORY_MAX_RISK_SCORE = int(os.getenv("FAST3L_ADVISORY_MAX_RISK_SCORE", "20"))
FAST3L_ADVISORY_SOFT_PENALTY = int(os.getenv("FAST3L_ADVISORY_SOFT_PENALTY", "8"))
AI_DEEPSEEK_ASSIST_ENABLED = os.getenv("AI_DEEPSEEK_ASSIST_ENABLED", "1") == "1"
AI_DEEPSEEK_ASSIST_MIN_REMAIN_SEC = float(
    os.getenv("AI_DEEPSEEK_ASSIST_MIN_REMAIN_SEC", "8")
)
AI_DEEPSEEK_ASSIST_TIMEOUT_SEC = float(
    os.getenv("AI_DEEPSEEK_ASSIST_TIMEOUT_SEC", "6")
)
GPT_HEDGE_FANOUT = int(os.getenv("GPT_HEDGE_FANOUT", "2"))
GPT_L3_ENSEMBLE = int(os.getenv("GPT_L3_ENSEMBLE", "5"))
GPT_L3_QUORUM = int(os.getenv("GPT_L3_QUORUM", "3"))
GPT_L5_ADVISORY_MODE = os.getenv("GPT_L5_ADVISORY_MODE", "0") == "1"
GPT_L5_ADVISORY_MIN_L3_WIN = int(os.getenv("GPT_L5_ADVISORY_MIN_L3_WIN", "56"))
GPT_L5_ADVISORY_MAX_RISK_SCORE = int(os.getenv("GPT_L5_ADVISORY_MAX_RISK_SCORE", "24"))
GPT_L5_ADVISORY_PENALTY = int(os.getenv("GPT_L5_ADVISORY_PENALTY", "0"))
AI_TIME_BUDGET_SEC = int(os.getenv("AI_TIME_BUDGET_SEC", "50"))
AI_TIMEOUT_SAFETY_SEC = float(os.getenv("AI_TIMEOUT_SAFETY_SEC", "1.5"))
AI_MIN_REMAIN_STEP3_SEC = int(os.getenv("AI_MIN_REMAIN_STEP3_SEC", "14"))
AI_MIN_REMAIN_STEP2_SEC = int(os.getenv("AI_MIN_REMAIN_STEP2_SEC", "9"))
AI_MIN_BUDGET_TO_START_SEC = int(os.getenv("AI_MIN_BUDGET_TO_START_SEC", "12"))
AI_SCORE_GATE = float(os.getenv("AI_SCORE_GATE", "7.5"))
AI_SCORE_ONLY_MODE = os.getenv("AI_SCORE_ONLY_MODE", "0") == "1"
AI_PAIR_MIN_SCORE = float(os.getenv("AI_PAIR_MIN_SCORE", "7.5"))
AI_MAX_SIGNALS_PER_LAYER = int(os.getenv("AI_MAX_SIGNALS_PER_LAYER", "2"))
AI_MIN_SIGNAL_STRENGTH = int(os.getenv("AI_MIN_SIGNAL_STRENGTH", "4"))
SCAN_TIME_BUDGET_SEC = int(os.getenv("SCAN_TIME_BUDGET_SEC", "0"))  # 0 = auto from SCAN_INTERVAL
SCAN_TIME_BUFFER_SEC = int(os.getenv("SCAN_TIME_BUFFER_SEC", "5"))
PERF_REPORT_INTERVAL_SEC = int(os.getenv("PERF_REPORT_INTERVAL_SEC", "3600"))

# BTC Trend Filter
BTC_TREND_FILTER = os.getenv("BTC_TREND_FILTER", "0") == "1"
BTC_TREND_STRICT = os.getenv("BTC_TREND_STRICT", "1") == "1"  # strict = block ngược trend mạnh
