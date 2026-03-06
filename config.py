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
COOLDOWN_AFTER_LOSS = int(os.getenv("COOLDOWN_AFTER_LOSS", "1800"))  # 30 phút sau lệnh thua
COIN_COOLDOWN_AFTER_LOSS = int(os.getenv("COIN_COOLDOWN_AFTER_LOSS", "600"))  # per-coin cooldown sau SL (giây)
COIN_COOLDOWN_LOSS1_SEC = max(0, int(os.getenv("COIN_COOLDOWN_LOSS1_SEC", "3600")))
COIN_COOLDOWN_LOSS2_SEC = max(
    COIN_COOLDOWN_LOSS1_SEC,
    int(os.getenv("COIN_COOLDOWN_LOSS2_SEC", "86400")),
)
COIN_MAX_TRADES_PER_DAY = max(0, int(os.getenv("COIN_MAX_TRADES_PER_DAY", "0")))
COIN_MAX_LOSSES_PER_DAY = max(0, int(os.getenv("COIN_MAX_LOSSES_PER_DAY", "2")))
COIN_MAX_CONSECUTIVE_LOSSES = max(
    0, int(os.getenv("COIN_MAX_CONSECUTIVE_LOSSES", "2"))
)
COIN_LOSS_LOOKBACK_MIN = max(0, int(os.getenv("COIN_LOSS_LOOKBACK_MIN", "180")))
DAILY_STOP_LOSS_USDT = float(os.getenv("DAILY_STOP_LOSS_USDT", "-4.0"))  # <=0 để bật, 0 để tắt
DAILY_STOP_LOSS_HALT_APP = os.getenv("DAILY_STOP_LOSS_HALT_APP", "0") == "1"
AUTO_TRADE = os.getenv("AUTO_TRADE", "1") == "1"  # True = tự động vào kèo
AUTO_TRADE_MIN_WIN = int(os.getenv("AUTO_TRADE_MIN_WIN", "65"))  # % win tá»‘i thiá»ƒu Ä‘á»ƒ auto trade
AUTO_TRADE_MIN_WIN_FLOOR = int(os.getenv("AUTO_TRADE_MIN_WIN_FLOOR", "55"))
AUTO_TRADE_MIN_WIN_DEFICIT_STEP = int(
    os.getenv("AUTO_TRADE_MIN_WIN_DEFICIT_STEP", "4")
)
AUTO_TRADE_MIN_WIN_RELAX_FOR_CADENCE = (
    os.getenv("AUTO_TRADE_MIN_WIN_RELAX_FOR_CADENCE", "0") == "1"
)
AUTO_TRADE_MIN_WIN_LOSS_PENALTY = int(
    os.getenv("AUTO_TRADE_MIN_WIN_LOSS_PENALTY", "3")
)
AUTO_TRADE_MIN_WIN_DROUGHT_MIN = int(
    os.getenv("AUTO_TRADE_MIN_WIN_DROUGHT_MIN", "0")
)
AUTO_TRADE_MIN_WIN_DROUGHT = int(
    os.getenv("AUTO_TRADE_MIN_WIN_DROUGHT", "55")
)
TARGET_TRADES_PER_15M = int(os.getenv("TARGET_TRADES_PER_15M", "2"))
MAX_TRADES_PER_30M = max(0, int(os.getenv("MAX_TRADES_PER_30M", "0")))
MAX_ACTIVE_TRADES = int(os.getenv("MAX_ACTIVE_TRADES", "2"))
SIMPLE_SCALP_MODE = os.getenv("SIMPLE_SCALP_MODE", "0") == "1"
SIMPLE_SCALP_USE_AI = os.getenv("SIMPLE_SCALP_USE_AI", "1") == "1"
SIMPLE_SCALP_STRATEGY = os.getenv("SIMPLE_SCALP_STRATEGY", "btc_sync").strip().lower()
SIMPLE_SCALP_USE_BTC_CONTEXT = (
    os.getenv("SIMPLE_SCALP_USE_BTC_CONTEXT", "1") == "1"
)
SIMPLE_SCALP_STREAK_MIN = max(1, int(os.getenv("SIMPLE_SCALP_STREAK_MIN", "1")))
SIMPLE_SCALP_WICK_MIN_PCT = float(os.getenv("SIMPLE_SCALP_WICK_MIN_PCT", "0.10"))
SIMPLE_SCALP_FORCE_M15_TREND = (
    os.getenv("SIMPLE_SCALP_FORCE_M15_TREND", "1") == "1"
)
SIMPLE_SCALP_REQUIRE_BTC_M1_SYNC = (
    os.getenv("SIMPLE_SCALP_REQUIRE_BTC_M1_SYNC", "1") == "1"
)
SIMPLE_SCALP_BTC_M1_STREAK_MIN = max(
    1, int(os.getenv("SIMPLE_SCALP_BTC_M1_STREAK_MIN", "1"))
)
SIMPLE_SCALP_REQUIRE_ALT_STREAK = (
    os.getenv("SIMPLE_SCALP_REQUIRE_ALT_STREAK", "1") == "1"
)
SIMPLE_SCALP_USE_WICK_TRIGGER = (
    os.getenv("SIMPLE_SCALP_USE_WICK_TRIGGER", "0") == "1"
)
SIMPLE_SCALP_REQUIRE_EMA_ALIGN = (
    os.getenv("SIMPLE_SCALP_REQUIRE_EMA_ALIGN", "1") == "1"
)
SIMPLE_SCALP_REQUIRE_BREAKOUT_CONFIRM = (
    os.getenv("SIMPLE_SCALP_REQUIRE_BREAKOUT_CONFIRM", "0") == "1"
)
SIMPLE_SCALP_REQUIRE_M5_WAVE = (
    os.getenv("SIMPLE_SCALP_REQUIRE_M5_WAVE", "1") == "1"
)
SIMPLE_SCALP_M5_WICK_MIN_PCT = float(
    os.getenv("SIMPLE_SCALP_M5_WICK_MIN_PCT", "0.10")
)
SIMPLE_SCALP_REQUIRE_M15_ZONE = (
    os.getenv("SIMPLE_SCALP_REQUIRE_M15_ZONE", "1") == "1"
)
SIMPLE_SCALP_REQUIRE_M5_PINBAR = (
    os.getenv("SIMPLE_SCALP_REQUIRE_M5_PINBAR", "1") == "1"
)
SIMPLE_SCALP_M15_SR_LOOKBACK = max(
    6, int(os.getenv("SIMPLE_SCALP_M15_SR_LOOKBACK", "24"))
)
SIMPLE_SCALP_M15_NEAR_SR_PCT = float(
    os.getenv("SIMPLE_SCALP_M15_NEAR_SR_PCT", "0.35")
)
SIMPLE_SCALP_M15_BREAK_PCT = float(
    os.getenv("SIMPLE_SCALP_M15_BREAK_PCT", "0.08")
)
SIMPLE_SCALP_M5_PINBAR_WICK_BODY_MIN = float(
    os.getenv("SIMPLE_SCALP_M5_PINBAR_WICK_BODY_MIN", "1.8")
)
SIMPLE_SCALP_M5_PINBAR_BODY_MAX_RATIO = float(
    os.getenv("SIMPLE_SCALP_M5_PINBAR_BODY_MAX_RATIO", "0.35")
)
SIMPLE_SCALP_M1_CONFIRM_STREAK = max(
    1, int(os.getenv("SIMPLE_SCALP_M1_CONFIRM_STREAK", "2"))
)
SIMPLE_SCALP_M1_CONFIRM_REQUIRE_EMA = (
    os.getenv("SIMPLE_SCALP_M1_CONFIRM_REQUIRE_EMA", "1") == "1"
)
SIMPLE_SCALP_FORCE_TRADE_ON_AI_TIMEOUT = (
    os.getenv("SIMPLE_SCALP_FORCE_TRADE_ON_AI_TIMEOUT", "0") == "1"
)
SIMPLE_SCALP_DYNAMIC_TP_ENABLED = (
    os.getenv("SIMPLE_SCALP_DYNAMIC_TP_ENABLED", "1") == "1"
)
SIMPLE_SCALP_DYNAMIC_TP_MIN_PCT = float(
    os.getenv("SIMPLE_SCALP_DYNAMIC_TP_MIN_PCT", "0.26")
)
SIMPLE_SCALP_DYNAMIC_TP_MAX_PCT = float(
    os.getenv("SIMPLE_SCALP_DYNAMIC_TP_MAX_PCT", "0.55")
)
SIMPLE_SCALP_DYNAMIC_SL_MIN_PCT = float(
    os.getenv("SIMPLE_SCALP_DYNAMIC_SL_MIN_PCT", "0.10")
)
SIMPLE_SCALP_DYNAMIC_SL_MAX_PCT = float(
    os.getenv("SIMPLE_SCALP_DYNAMIC_SL_MAX_PCT", "0.24")
)
SIMPLE_SCALP_DYNAMIC_SL_TP_RATIO = float(
    os.getenv("SIMPLE_SCALP_DYNAMIC_SL_TP_RATIO", "0.45")
)
SIMPLE_SCALP_AI_BUDGET_SEC = float(
    os.getenv("SIMPLE_SCALP_AI_BUDGET_SEC", "6")
)
SIMPLE_SCALP_USE_NET_USDT_TP_SL = (
    os.getenv("SIMPLE_SCALP_USE_NET_USDT_TP_SL", "0") == "1"
)
SIMPLE_SCALP_NET_TP_USDT = float(os.getenv("SIMPLE_SCALP_NET_TP_USDT", "2.0"))
SIMPLE_SCALP_NET_SL_USDT = float(os.getenv("SIMPLE_SCALP_NET_SL_USDT", "1.2"))
SIMPLE_SCALP_SCORE = float(os.getenv("SIMPLE_SCALP_SCORE", "5.5"))
SIMPLE_SCALP_WIN_PROB = int(os.getenv("SIMPLE_SCALP_WIN_PROB", "100"))
SIMPLE_SCALP_MIN_NET_TP_PCT = float(os.getenv("SIMPLE_SCALP_MIN_NET_TP_PCT", "0.05"))
SIMPLE_SCALP_MIN_RR = float(os.getenv("SIMPLE_SCALP_MIN_RR", "0.45"))
SIMPLE_SCALP_SLIPPAGE_BUFFER_PCT = float(
    os.getenv("SIMPLE_SCALP_SLIPPAGE_BUFFER_PCT", "0.03")
)
SIMPLE_SCALP_MAX_SPREAD_TO_TP_RATIO = float(
    os.getenv("SIMPLE_SCALP_MAX_SPREAD_TO_TP_RATIO", "0.25")
)
ENTRY_MAX_SPREAD_PCT = float(os.getenv("ENTRY_MAX_SPREAD_PCT", "0.05"))
ENTRY_MAX_SIGNAL_DRIFT_PCT = float(os.getenv("ENTRY_MAX_SIGNAL_DRIFT_PCT", "0.12"))
ENTRY_MAX_FILL_SLIPPAGE_PCT = float(os.getenv("ENTRY_MAX_FILL_SLIPPAGE_PCT", "0.20"))
ATR_BRAKE_ENABLED = os.getenv("ATR_BRAKE_ENABLED", "1") == "1"
ATR_BRAKE_PERIOD = max(5, int(os.getenv("ATR_BRAKE_PERIOD", "14")))
ATR_BRAKE_LOOKBACK_BARS = max(
    20, int(os.getenv("ATR_BRAKE_LOOKBACK_BARS", "96"))
)  # 96 nến M15 ~ 24h
ATR_BRAKE_TRIGGER_RATIO = float(os.getenv("ATR_BRAKE_TRIGGER_RATIO", "1.35"))
ATR_BRAKE_MAX_MULTIPLIER = float(os.getenv("ATR_BRAKE_MAX_MULTIPLIER", "2.00"))
ATR_BRAKE_TARGET_NET_SL_USDT = float(os.getenv("ATR_BRAKE_TARGET_NET_SL_USDT", "1.2"))
ATR_BRAKE_MIN_MARGIN_USDT = float(os.getenv("ATR_BRAKE_MIN_MARGIN_USDT", "2.0"))
ATR_BRAKE_MAX_MARGIN_USDT = float(os.getenv("ATR_BRAKE_MAX_MARGIN_USDT", "10.0"))
HARD_GATE_EMA200_ENABLED = os.getenv("HARD_GATE_EMA200_ENABLED", "1") == "1"
HARD_GATE_FAIL_OPEN = os.getenv("HARD_GATE_FAIL_OPEN", "0") == "1"
HARD_GATE_BLOCK_SHORT_ABOVE_EMA200 = (
    os.getenv("HARD_GATE_BLOCK_SHORT_ABOVE_EMA200", "0") == "1"
)
NEWS_CALENDAR_ENABLED = os.getenv("NEWS_CALENDAR_ENABLED", "1") == "1"
NEWS_BLOCK_BEFORE_MIN = max(0, int(os.getenv("NEWS_BLOCK_BEFORE_MIN", "15")))
NEWS_BLOCK_AFTER_MIN = max(0, int(os.getenv("NEWS_BLOCK_AFTER_MIN", "15")))
NEWS_EVENTS_UTC = os.getenv("NEWS_EVENTS_UTC", "").strip()
BONUS_SLOT_ENABLED = os.getenv("BONUS_SLOT_ENABLED", "1") == "1"
BONUS_SLOT_PARTIALS_REQUIRED = max(1, int(os.getenv("BONUS_SLOT_PARTIALS_REQUIRED", "2")))
BONUS_SLOT_TARGET_MARGIN_USDT = float(os.getenv("BONUS_SLOT_TARGET_MARGIN_USDT", "2.5"))
BONUS_SLOT_MARGIN_TOLERANCE_USDT = float(
    os.getenv("BONUS_SLOT_MARGIN_TOLERANCE_USDT", "0.35")
)
LONG_ONLY = os.getenv("LONG_ONLY", "0") == "1"
PARTIAL_TP_ENABLED = os.getenv("PARTIAL_TP_ENABLED", "1") == "1"
PARTIAL_TP_BE_RATIO = float(os.getenv("PARTIAL_TP_BE_RATIO", "0.3333"))
PARTIAL_TP_CLOSE_RATIO = float(os.getenv("PARTIAL_TP_CLOSE_RATIO", "0.6667"))
PARTIAL_TP_CLOSE_FRACTION = float(os.getenv("PARTIAL_TP_CLOSE_FRACTION", "0.5"))
PARTIAL_TP_MIN_HOLD_SEC = float(os.getenv("PARTIAL_TP_MIN_HOLD_SEC", "120"))
QUICK_TAKE_ENABLED = os.getenv("QUICK_TAKE_ENABLED", "1") == "1"
QUICK_TAKE_MIN_HOLD_SEC = float(os.getenv("QUICK_TAKE_MIN_HOLD_SEC", "20"))
QUICK_TAKE_MARGIN_PCT = float(os.getenv("QUICK_TAKE_MARGIN_PCT", "1.15"))
QUICK_TAKE_REQUIRE_FEE_COVER = os.getenv("QUICK_TAKE_REQUIRE_FEE_COVER", "1") == "1"
QUICK_TAKE_FEE_BUFFER_PCT = float(os.getenv("QUICK_TAKE_FEE_BUFFER_PCT", "0.08"))
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
AI_MIN_BUDGET_FLOOR_SEC = float(os.getenv("AI_MIN_BUDGET_FLOOR_SEC", "8"))
AI_TIMEOUT_SAFETY_SEC = float(os.getenv("AI_TIMEOUT_SAFETY_SEC", "1.5"))
AI_MIN_REMAIN_STEP3_SEC = int(os.getenv("AI_MIN_REMAIN_STEP3_SEC", "14"))
AI_MIN_REMAIN_STEP2_SEC = int(os.getenv("AI_MIN_REMAIN_STEP2_SEC", "9"))
AI_MIN_BUDGET_TO_START_SEC = int(os.getenv("AI_MIN_BUDGET_TO_START_SEC", "12"))
AI_L1_FALLBACK_BYPASS_SCORE = float(os.getenv("AI_L1_FALLBACK_BYPASS_SCORE", "7.5"))
AI_SCORE_GATE = float(os.getenv("AI_SCORE_GATE", "7.5"))
_AI_PRE_SCORE_GATE_RAW = os.getenv("AI_PRE_SCORE_GATE", "").strip()
if _AI_PRE_SCORE_GATE_RAW:
    try:
        AI_PRE_SCORE_GATE = float(_AI_PRE_SCORE_GATE_RAW)
    except ValueError:
        AI_PRE_SCORE_GATE = AI_SCORE_GATE
else:
    AI_PRE_SCORE_GATE = AI_SCORE_GATE
AI_SCORE_GATE_RELAX_FOR_CADENCE = (
    os.getenv("AI_SCORE_GATE_RELAX_FOR_CADENCE", "0") == "1"
)
AI_SCORE_GATE_DEFICIT_STEP = float(os.getenv("AI_SCORE_GATE_DEFICIT_STEP", "0.2"))
AI_SCORE_GATE_FLOOR = float(os.getenv("AI_SCORE_GATE_FLOOR", "7.8"))
AI_SCORE_GATE_DROUGHT_MIN = int(os.getenv("AI_SCORE_GATE_DROUGHT_MIN", "0"))
AI_SCORE_GATE_DROUGHT = float(os.getenv("AI_SCORE_GATE_DROUGHT", "8.0"))
CADENCE_TURBO_ENABLED = os.getenv("CADENCE_TURBO_ENABLED", "0") == "1"
CADENCE_TURBO_AFTER_MIN = int(os.getenv("CADENCE_TURBO_AFTER_MIN", "0"))
CADENCE_TURBO_SCORE_GATE = float(os.getenv("CADENCE_TURBO_SCORE_GATE", "7.4"))
CADENCE_TURBO_MIN_WIN = int(os.getenv("CADENCE_TURBO_MIN_WIN", "52"))
AI_SCORE_ONLY_MODE = os.getenv("AI_SCORE_ONLY_MODE", "0") == "1"
SCORE_ONLY_MIN_STRUCTURE = float(os.getenv("SCORE_ONLY_MIN_STRUCTURE", "2.8"))
SCORE_ONLY_MIN_SL_SAFETY = float(os.getenv("SCORE_ONLY_MIN_SL_SAFETY", "2.5"))
SCORE_ONLY_MAX_TREND_CONFLICT = float(os.getenv("SCORE_ONLY_MAX_TREND_CONFLICT", "1.5"))
SCORE_ONLY_REQUIRE_FLOW_CONFIRM = os.getenv("SCORE_ONLY_REQUIRE_FLOW_CONFIRM", "1") == "1"
SCORE_ONLY_REQUIRE_EMA_CONFIRM = os.getenv("SCORE_ONLY_REQUIRE_EMA_CONFIRM", "0") == "1"
SCORE_ONLY_FLOW_SOFT_MIN_SCORE = float(
    os.getenv("SCORE_ONLY_FLOW_SOFT_MIN_SCORE", "0")
)
SCORE_ONLY_FLOW_SOFT_MAX_RISK = int(os.getenv("SCORE_ONLY_FLOW_SOFT_MAX_RISK", "55"))
SCORE_ONLY_MAX_PER_CLUSTER = max(1, int(os.getenv("SCORE_ONLY_MAX_PER_CLUSTER", "1")))
SCORE_ONLY_REQUIRE_REGIME_MATCH = (
    os.getenv("SCORE_ONLY_REQUIRE_REGIME_MATCH", "1") == "1"
)
MARKET_REGIME_BB_SQUEEZE_RATIO = float(
    os.getenv("MARKET_REGIME_BB_SQUEEZE_RATIO", "0.85")
)
MARKET_REGIME_BB_EXPAND_RATIO = float(
    os.getenv("MARKET_REGIME_BB_EXPAND_RATIO", "1.20")
)
MARKET_REGIME_BREAKOUT_VOL_MIN = float(
    os.getenv("MARKET_REGIME_BREAKOUT_VOL_MIN", "25")
)
AI_PAIR_MIN_SCORE = float(os.getenv("AI_PAIR_MIN_SCORE", "7.5"))
AI_MAX_SIGNALS_PER_LAYER = int(os.getenv("AI_MAX_SIGNALS_PER_LAYER", "2"))
AI_MIN_SIGNAL_STRENGTH = int(os.getenv("AI_MIN_SIGNAL_STRENGTH", "4"))
SCAN_TIME_BUDGET_SEC = int(os.getenv("SCAN_TIME_BUDGET_SEC", "0"))  # 0 = auto from SCAN_INTERVAL
SCAN_TIME_BUFFER_SEC = int(os.getenv("SCAN_TIME_BUFFER_SEC", "5"))
PERF_REPORT_INTERVAL_SEC = int(os.getenv("PERF_REPORT_INTERVAL_SEC", "3600"))

# Guardian / scalp protection
GUARDIAN_INTERVAL = max(5, int(os.getenv("GUARDIAN_INTERVAL", "15")))
GUARDIAN_CLOSE_THRESHOLD = float(os.getenv("GUARDIAN_CLOSE_THRESHOLD", "40"))
GUARDIAN_FIRST_CHECK_SEC = float(os.getenv("GUARDIAN_FIRST_CHECK_SEC", "120"))
GUARDIAN_MIN_HOLD_SEC = float(os.getenv("GUARDIAN_MIN_HOLD_SEC", "120"))
GUARDIAN_MIN_LOSS_RESCUE_SEC = float(os.getenv("GUARDIAN_MIN_LOSS_RESCUE_SEC", "120"))
GUARDIAN_LOSS_CLOSE_SL_FACTOR = float(os.getenv("GUARDIAN_LOSS_CLOSE_SL_FACTOR", "0.80"))
GUARDIAN_LOCK_PROFIT_BUFFER_PCT = float(
    os.getenv("GUARDIAN_LOCK_PROFIT_BUFFER_PCT", "0.05")
)
GUARDIAN_MAX_FEE_LOSS_PCT = float(os.getenv("GUARDIAN_MAX_FEE_LOSS_PCT", "0.12"))
GUARDIAN_STALE_EXIT_ENABLED = os.getenv("GUARDIAN_STALE_EXIT_ENABLED", "1") == "1"
GUARDIAN_STALE_MIN_NEG_MARGIN_PCT = float(
    os.getenv("GUARDIAN_STALE_MIN_NEG_MARGIN_PCT", "0.60")
)

# BTC Trend Filter
BTC_TREND_FILTER = os.getenv("BTC_TREND_FILTER", "0") == "1"
BTC_TREND_STRICT = os.getenv("BTC_TREND_STRICT", "1") == "1"  # strict = block ngược trend mạnh
BTC_TREND_COUNTERTRADE_WHEN_TURBO = (
    os.getenv("BTC_TREND_COUNTERTRADE_WHEN_TURBO", "0") == "1"
)
BTC_TREND_COUNTERTRADE_MIN_STRENGTH = int(
    os.getenv("BTC_TREND_COUNTERTRADE_MIN_STRENGTH", "5")
)
BTC_TREND_COUNTERTRADE_MAX_PER_LAYER = int(
    os.getenv("BTC_TREND_COUNTERTRADE_MAX_PER_LAYER", "1")
)
