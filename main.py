# -*- coding: utf-8 -*-
"""
main.py — FuBot Trading System v6.0

Bot trading futures OKX FULL AUTO:
1. Scan coins mỗi SCAN_INTERVAL giây
2. Tính indicators (CVD, OI, Orderbook Imbalance)
3. Rule Engine lọc signal
4. 6-Layer AI (GPT + DeepSeek V3) phân tích — anti-echo chamber
5. Auto trade khi playbook_score ≥ AI_SCORE_GATE (mặc định 7.5/10)
6. TP/SL tự động, monitor positions
"""

import asyncio
import logging
import sys
import io
import uuid
import time
import os
import atexit
from collections import Counter, deque
from datetime import datetime, timedelta, timezone

from telegram.ext import Application, ContextTypes
from telegram.error import Conflict

import config
from okx_client import OKXClient
from capital_manager import CapitalManager
from indicators import (
    calc_cvd,
    calc_oi_change,
    calc_orderbook_imbalance,
    analyze_funding_rate,
    analyze_candles,
    analyze_micro_setup,
    analyze_sr_levels,
    analyze_btc_trend,
    generate_signal,
)
from ai_filter import analyze_trade, format_ai_result, get_pair_cluster, save_trade_to_memory
from macro_fetcher import get_macro_data
import telegram_handler

# Fix encoding Windows CMD
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── logging ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("fubot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("fubot")

# ── global state ────────────────────────────────────────────
okx = OKXClient()
capital = CapitalManager()

# Lưu OI trước đó để tính OI change
prev_oi: dict[str, float] = {}

# Lưu vị thế đang theo dõi
active_trades: dict[str, dict] = {}

# Đếm số lần partial TP thành công trong session → tính bonus slot
# Cứ 2 lần tp1_done → +1 slot (vòng 2, vòng 3, ...)
partial_tp_count: int = 0

# Per-coin cooldown: coin hit SL → skip coin đó N giây, tránh vào lại liên tục
coin_cooldowns: dict[str, float] = {}
_last_news_block_notice_ts: float = 0.0
_daily_stop_halt_triggered: bool = False



# Lock scan to avoid overlapping scheduler/manual/auto-rescan
scan_lock = asyncio.Lock()

# BTC trend cache (refreshed each scan cycle)
btc_trend: dict = {"trend": "neutral", "strength": "neutral",
                    "allow_long": True, "allow_short": True, "detail": "chưa scan"}
btc_m1_pulse: dict = {
    "trend": "neutral",
    "m1_bull_streak": 0,
    "m1_bear_streak": 0,
    "m1_price_vs_ema21": "unknown",
    "m1_breakout_up": False,
    "m1_breakout_down": False,
    "detail": "chưa scan",
}

# Runtime performance metrics for hourly report
perf_scan_events = deque(maxlen=720)  # {"ts","duration","delay","budget_hit","scanned","total"}
perf_ai_events = deque(maxlen=1200)   # {"ts","latency"}
perf_skip_events = deque(maxlen=1500)  # {"ts","reason"}

_instance_lock_fh = None
_instance_lock_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".fubot.instance.lock",
)


def _acquire_instance_lock() -> bool:
    """Prevent multiple bot instances running in parallel."""
    global _instance_lock_fh
    try:
        _instance_lock_fh = open(_instance_lock_path, "a+", encoding="utf-8")
        _instance_lock_fh.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(_instance_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(_instance_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _instance_lock_fh.seek(0)
        _instance_lock_fh.truncate(0)
        _instance_lock_fh.write(str(os.getpid()))
        _instance_lock_fh.flush()
        return True
    except Exception:
        return False


def _release_instance_lock():
    global _instance_lock_fh
    if not _instance_lock_fh:
        return
    try:
        _instance_lock_fh.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(_instance_lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(_instance_lock_fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        _instance_lock_fh.close()
    except Exception:
        pass
    _instance_lock_fh = None


async def telegram_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Telegram errors cleanly; stop duplicate polling instance on 409."""
    err = context.error
    if isinstance(err, Conflict):
        logger.error(
            "Telegram 409 Conflict: another instance is polling this bot token. "
            "Stopping current instance."
        )
        try:
            await context.application.stop()
        except Exception as stop_err:
            logger.error(f"Stop application after conflict failed: {stop_err}")
        return
    logger.exception("Unhandled Telegram error", exc_info=err)


def _record_scan_event(duration_sec: float, delay_sec: float,
                       budget_hit: bool, scanned: int, total: int):
    perf_scan_events.append(
        {
            "ts": time.time(),
            "duration": round(duration_sec, 2),
            "delay": round(delay_sec, 2),
            "budget_hit": bool(budget_hit),
            "scanned": scanned,
            "total": total,
        }
    )


def _record_ai_latency(latency_sec: float):
    if latency_sec and latency_sec > 0:
        perf_ai_events.append({"ts": time.time(), "latency": float(latency_sec)})


def _record_skip(reason: str):
    perf_skip_events.append({"ts": time.time(), "reason": reason})


def _signal_tag(coin: str, signal: dict) -> str:
    direction = signal.get("direction", "?")
    strength = int(signal.get("strength", 0) or 0)
    return f"{coin} {direction} (str={strength})"


async def _send_log_safe(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await telegram_handler.send_message(context.bot, text)
    except Exception as e:
        logger.warning(f"Telegram log send failed: {e}")


def _max_active_trades() -> int:
    base = max(1, int(config.MAX_ACTIVE_TRADES))
    required_partials = max(1, int(getattr(config, "BONUS_SLOT_PARTIALS_REQUIRED", 2)))
    bonus = 0
    if bool(getattr(config, "BONUS_SLOT_ENABLED", True)):
        bonus = min(1, partial_tp_count // required_partials)
    return base + bonus


def _trade_slots_available() -> int:
    return max(0, _max_active_trades() - len(active_trades))


def _effective_auto_trade_min_win() -> int:
    required = int(config.AUTO_TRADE_MIN_WIN)
    floor = int(config.AUTO_TRADE_MIN_WIN_FLOOR)
    effective_floor = floor
    cadence_target = max(0, int(config.TARGET_TRADES_PER_15M))
    if cadence_target > 0 and bool(getattr(config, "AUTO_TRADE_MIN_WIN_RELAX_FOR_CADENCE", False)):
        deficit = max(0, cadence_target - _recent_trade_count_15m())
        required -= deficit * int(config.AUTO_TRADE_MIN_WIN_DEFICIT_STEP)
    required += max(0, int(capital.consecutive_losses)) * int(
        config.AUTO_TRADE_MIN_WIN_LOSS_PENALTY
    )
    drought_min = max(0, int(getattr(config, "AUTO_TRADE_MIN_WIN_DROUGHT_MIN", 0)))
    if drought_min > 0 and _minutes_since_last_closed_trade() >= drought_min:
        drought_gate = int(getattr(config, "AUTO_TRADE_MIN_WIN_DROUGHT", floor))
        required = min(required, drought_gate)
        effective_floor = min(effective_floor, drought_gate)

    if _cadence_turbo_active():
        turbo_win = int(getattr(config, "CADENCE_TURBO_MIN_WIN", floor))
        required = min(required, turbo_win)
        effective_floor = min(effective_floor, turbo_win)

    return max(effective_floor, min(95, required))


def _minutes_since_last_closed_trade() -> float:
    if not capital.trades:
        return 10_000.0
    last = capital.trades[-1]
    trade_time = _trade_time_from_value(last.get("time"))
    if trade_time is None:
        return 10_000.0
    return max(0.0, (datetime.now() - trade_time).total_seconds() / 60.0)


def _cadence_turbo_active() -> bool:
    if not bool(getattr(config, "CADENCE_TURBO_ENABLED", False)):
        return False
    cadence_target = max(0, int(config.TARGET_TRADES_PER_15M))
    if cadence_target <= 0:
        return False
    if _recent_trade_count_15m() >= cadence_target:
        return False
    after_min = max(0, int(getattr(config, "CADENCE_TURBO_AFTER_MIN", 0)))
    if after_min > 0 and _minutes_since_last_closed_trade() < after_min:
        return False
    return True


def _effective_score_gate() -> float:
    gate = float(config.AI_SCORE_GATE)
    floor = min(gate, max(0.0, float(getattr(config, "AI_SCORE_GATE_FLOOR", gate))))

    if bool(getattr(config, "AI_SCORE_GATE_RELAX_FOR_CADENCE", False)):
        cadence_target = max(0, int(config.TARGET_TRADES_PER_15M))
        if cadence_target > 0:
            deficit = max(0, cadence_target - _recent_trade_count_15m())
            step = max(0.0, float(getattr(config, "AI_SCORE_GATE_DEFICIT_STEP", 0.0)))
            gate -= deficit * step

        drought_min = max(0, int(getattr(config, "AI_SCORE_GATE_DROUGHT_MIN", 0)))
        drought_gate = max(0.0, float(getattr(config, "AI_SCORE_GATE_DROUGHT", floor)))
        if drought_min > 0 and _minutes_since_last_closed_trade() >= drought_min:
            gate = min(gate, drought_gate)

    if _cadence_turbo_active():
        turbo_gate = max(0.0, float(getattr(config, "CADENCE_TURBO_SCORE_GATE", floor)))
        gate = min(gate, turbo_gate)
        floor = min(floor, turbo_gate)

    return max(floor, min(float(config.AI_SCORE_GATE), gate))


def _analysis_score_gate(entry_gate: float | None = None) -> float:
    """
    Gate dùng cho pre-check ở analyze_trade.
    Mặc định bám theo entry gate, nhưng có thể nới thấp hơn qua AI_PRE_SCORE_GATE.
    """
    if entry_gate is None:
        entry_gate = _effective_score_gate()
    entry_gate = max(0.0, float(entry_gate))
    pre_gate = max(
        0.0,
        float(getattr(config, "AI_PRE_SCORE_GATE", entry_gate)),
    )
    return min(entry_gate, pre_gate)


def _is_bonus_slot_eligible(position_info: dict | None) -> bool:
    if not bool(getattr(config, "BONUS_SLOT_ENABLED", True)):
        return False
    if not position_info:
        return False
    actual_margin = float(position_info.get("actual_margin_usdt", 0) or 0)
    target = float(getattr(config, "BONUS_SLOT_TARGET_MARGIN_USDT", 2.5))
    tolerance = max(0.0, float(getattr(config, "BONUS_SLOT_MARGIN_TOLERANCE_USDT", 0.35)))
    return abs(actual_margin - target) <= tolerance


def _recent_trade_count_window(minutes: int) -> int:
    """Count closed + active trades in the last N minutes."""
    now = datetime.now()
    window_start = now - timedelta(minutes=max(1, int(minutes)))
    count = 0

    for t in capital.trades[-100:]:
        ts = t.get("time")
        if not ts:
            continue
        try:
            trade_time = datetime.fromisoformat(ts)
        except Exception:
            continue
        if trade_time >= window_start:
            count += 1

    for t in active_trades.values():
        ts = t.get("timestamp")
        if not ts:
            continue
        try:
            trade_time = datetime.fromisoformat(ts)
        except Exception:
            continue
        if trade_time >= window_start:
            count += 1

    return count


def _recent_trade_count_15m() -> int:
    return _recent_trade_count_window(15)


def _recent_trade_count_30m() -> int:
    return _recent_trade_count_window(30)


def _trade_time_from_value(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _symbol_loss_cooldown_remaining(symbol: str) -> tuple[int, int, int] | None:
    """
    Dynamic cooldown theo chuỗi thua của từng coin:
    - thua 1 lệnh gần nhất: cooldown level 1
    - thua liên tiếp >=2 lệnh: cooldown level 2
    """
    relevant = [t for t in capital.trades if t.get("symbol") == symbol]
    if not relevant:
        return None

    consecutive_losses = 0
    last_loss_time: datetime | None = None
    for trade in reversed(relevant):
        try:
            pnl = float(trade.get("pnl", 0) or 0)
        except Exception:
            break
        if pnl < 0:
            consecutive_losses += 1
            if last_loss_time is None:
                last_loss_time = _trade_time_from_value(trade.get("time"))
        else:
            break

    if consecutive_losses <= 0 or last_loss_time is None:
        return None

    cooldown_sec = (
        int(getattr(config, "COIN_COOLDOWN_LOSS2_SEC", 86400))
        if consecutive_losses >= 2
        else int(getattr(config, "COIN_COOLDOWN_LOSS1_SEC", 3600))
    )
    elapsed = max(0.0, (datetime.now() - last_loss_time).total_seconds())
    remaining = int(max(0, cooldown_sec - elapsed))
    if remaining <= 0:
        return None
    return remaining, consecutive_losses, cooldown_sec


def _today_symbol_trade_stats(symbol: str, lookback_minutes: int = 0) -> dict[str, int]:
    now = datetime.now()
    today = now.date()
    lookback_start = None
    if lookback_minutes and lookback_minutes > 0:
        lookback_start = now - timedelta(minutes=int(lookback_minutes))

    relevant: list[dict] = []
    for trade in capital.trades:
        if trade.get("symbol") != symbol:
            continue
        trade_time = _trade_time_from_value(trade.get("time"))
        if trade_time is not None and trade_time.date() != today:
            continue
        if lookback_start is not None and trade_time is not None and trade_time < lookback_start:
            continue
        relevant.append(trade)

    losses = 0
    consecutive_losses = 0
    for trade in relevant:
        try:
            if float(trade.get("pnl", 0) or 0) < 0:
                losses += 1
        except Exception:
            continue

    for trade in reversed(relevant):
        try:
            if float(trade.get("pnl", 0) or 0) < 0:
                consecutive_losses += 1
            else:
                break
        except Exception:
            break

    return {
        "total": len(relevant),
        "losses": losses,
        "consecutive_losses": consecutive_losses,
    }


def _coin_session_block_reason(symbol: str) -> str:
    cooldown_info = _symbol_loss_cooldown_remaining(symbol)
    if cooldown_info is not None:
        remaining, consec_losses, cooldown_sec = cooldown_info
        return (
            f"coin cooldown {remaining}s "
            f"(loss streak={consec_losses}, rule={cooldown_sec}s)"
        )

    day_stats = _today_symbol_trade_stats(symbol)
    loss_lookback_min = max(0, int(getattr(config, "COIN_LOSS_LOOKBACK_MIN", 0)))
    loss_stats = _today_symbol_trade_stats(symbol, lookback_minutes=loss_lookback_min)
    max_trades = max(0, int(getattr(config, "COIN_MAX_TRADES_PER_DAY", 0)))
    if max_trades > 0 and day_stats["total"] >= max_trades:
        return f"coin day cap {day_stats['total']}/{max_trades}"

    max_losses = max(0, int(getattr(config, "COIN_MAX_LOSSES_PER_DAY", 0)))
    if max_losses > 0 and loss_stats["losses"] >= max_losses:
        if loss_lookback_min > 0:
            return f"coin loss cap {loss_stats['losses']}/{max_losses} in {loss_lookback_min}m"
        return f"coin daily loss cap {loss_stats['losses']}/{max_losses}"

    max_consecutive = max(0, int(getattr(config, "COIN_MAX_CONSECUTIVE_LOSSES", 0)))
    if max_consecutive > 0 and loss_stats["consecutive_losses"] >= max_consecutive:
        return (
            f"coin consecutive-loss cap "
            f"{loss_stats['consecutive_losses']}/{max_consecutive}"
        )

    return ""


def _daily_stop_triggered() -> tuple[bool, float, float]:
    """Check if daily net PnL has crossed the configured stop-loss."""
    threshold = float(getattr(config, "DAILY_STOP_LOSS_USDT", 0.0))
    pnl = float(getattr(capital, "total_pnl", 0.0) or 0.0)
    if threshold >= 0:
        return False, threshold, pnl
    return pnl <= threshold, threshold, pnl


def _parse_news_events_utc(raw: str) -> list[tuple[datetime, str]]:
    events: list[tuple[datetime, str]] = []
    text = (raw or "").strip()
    if not text:
        return events

    chunks = text.replace("\n", ";").split(";")
    for chunk in chunks:
        item = chunk.strip()
        if not item:
            continue

        ts_text = item
        label = "high-impact news"
        for sep in ("|", "@", "#"):
            if sep in item:
                left, right = item.split(sep, 1)
                ts_text = left.strip()
                label = right.strip() or label
                break

        if not ts_text:
            continue

        dt_utc: datetime | None = None
        candidates = [ts_text]
        if ts_text.endswith("Z"):
            candidates.append(ts_text[:-1] + "+00:00")
        if "T" not in ts_text:
            candidates.append(ts_text.replace(" ", "T"))

        for candidate in candidates:
            try:
                parsed = datetime.fromisoformat(candidate)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                else:
                    parsed = parsed.astimezone(timezone.utc)
                dt_utc = parsed
                break
            except Exception:
                pass

        if dt_utc is None:
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(ts_text, fmt).replace(tzinfo=timezone.utc)
                    dt_utc = parsed
                    break
                except Exception:
                    continue

        if dt_utc is not None:
            events.append((dt_utc, label))

    events.sort(key=lambda item: item[0])
    return events


def _active_news_blackout() -> dict | None:
    if not bool(getattr(config, "NEWS_CALENDAR_ENABLED", False)):
        return None

    events = _parse_news_events_utc(str(getattr(config, "NEWS_EVENTS_UTC", "")))
    if not events:
        return None

    now_utc = datetime.now(timezone.utc)
    before = timedelta(minutes=max(0, int(getattr(config, "NEWS_BLOCK_BEFORE_MIN", 15))))
    after = timedelta(minutes=max(0, int(getattr(config, "NEWS_BLOCK_AFTER_MIN", 15))))

    for event_time_utc, label in events:
        start = event_time_utc - before
        end = event_time_utc + after
        if start <= now_utc <= end:
            minutes_to_event = (event_time_utc - now_utc).total_seconds() / 60.0
            return {
                "time_utc": event_time_utc,
                "label": label,
                "minutes_to_event": round(minutes_to_event, 1),
            }
    return None


def _entry_spread_guard(indicators: dict) -> tuple[bool, str]:
    spread_pct = max(0.0, float(indicators.get("spread_pct", 0.0) or 0.0))
    max_spread_pct = max(0.0, float(getattr(config, "ENTRY_MAX_SPREAD_PCT", 0.0)))
    if max_spread_pct > 0 and spread_pct > max_spread_pct:
        return False, f"spread={spread_pct:.4f}% > {max_spread_pct:.4f}%"
    return True, f"spread={spread_pct:.4f}%"


def _parse_ohlc(candle: list) -> dict | None:
    if not candle or len(candle) < 5:
        return None
    try:
        o = float(candle[1])
        h = float(candle[2])
        l = float(candle[3])
        c = float(candle[4])
    except Exception:
        return None
    if h <= 0 or l <= 0 or c <= 0:
        return None
    return {"open": o, "high": h, "low": l, "close": c}


def _ema_value(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1.0)
    ema = values[0]
    for value in values[1:]:
        ema = value * k + ema * (1.0 - k)
    return ema


def _ema200_context_from_candles(candles: list, tf_name: str) -> dict:
    parsed = [_parse_ohlc(c) for c in reversed(candles or [])]
    parsed = [c for c in parsed if c is not None]
    closes = [c["close"] for c in parsed]
    if len(closes) < 200:
        return {
            "ok": False,
            "tf": tf_name,
            "close": closes[-1] if closes else 0.0,
            "ema200": 0.0,
            "position": "unknown",
            "reason": f"insufficient_{tf_name}_candles({len(closes)}/200)",
        }

    ema200 = _ema_value(closes, 200)
    close = closes[-1]
    tol = 0.0005
    if close > ema200 * (1.0 + tol):
        position = "above"
    elif close < ema200 * (1.0 - tol):
        position = "below"
    else:
        position = "near"

    return {
        "ok": True,
        "tf": tf_name,
        "close": round(close, 6),
        "ema200": round(ema200, 6),
        "position": position,
        "reason": "ok",
    }


def _atr_percent_stats_from_candles(candles: list, period: int, lookback_bars: int) -> dict:
    parsed = [_parse_ohlc(c) for c in reversed(candles or [])]
    parsed = [c for c in parsed if c is not None]

    if len(parsed) < (period + 2):
        return {
            "ok": False,
            "atr_current_pct": 0.0,
            "atr_avg_pct": 0.0,
            "atr_ratio": 1.0,
            "reason": f"insufficient_candles({len(parsed)})",
        }

    tr_pct: list[float] = []
    prev_close = parsed[0]["close"]
    for c in parsed[1:]:
        base = max(prev_close, 1e-9)
        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - prev_close),
            abs(c["low"] - prev_close),
        )
        tr_pct.append((tr / base) * 100.0)
        prev_close = c["close"]

    if len(tr_pct) < period:
        return {
            "ok": False,
            "atr_current_pct": 0.0,
            "atr_avg_pct": 0.0,
            "atr_ratio": 1.0,
            "reason": f"insufficient_tr({len(tr_pct)})",
        }

    atr_series: list[float] = []
    for idx in range(period - 1, len(tr_pct)):
        window = tr_pct[idx - period + 1 : idx + 1]
        atr_series.append(sum(window) / float(period))

    if not atr_series:
        return {
            "ok": False,
            "atr_current_pct": 0.0,
            "atr_avg_pct": 0.0,
            "atr_ratio": 1.0,
            "reason": "empty_atr_series",
        }

    current_atr_pct = atr_series[-1]
    hist = atr_series[:-1] if len(atr_series) > 1 else atr_series
    n = max(5, int(lookback_bars))
    sample = hist[-n:] if hist else atr_series
    avg_atr_pct = (sum(sample) / len(sample)) if sample else current_atr_pct
    ratio = current_atr_pct / max(avg_atr_pct, 1e-9)

    return {
        "ok": True,
        "atr_current_pct": round(current_atr_pct, 6),
        "atr_avg_pct": round(avg_atr_pct, 6),
        "atr_ratio": round(ratio, 6),
        "reason": "ok",
    }


def _hard_gate_ema200_check(direction: str, indicators: dict) -> tuple[bool, str]:
    if not bool(getattr(config, "HARD_GATE_EMA200_ENABLED", False)):
        return True, "hard_gate disabled"

    pos_m15 = str(indicators.get("price_vs_ema200_m15", "unknown")).lower()
    pos_h1 = str(indicators.get("price_vs_ema200_h1", "unknown")).lower()
    data_ok = pos_m15 in {"above", "below", "near"} and pos_h1 in {"above", "below", "near"}

    if not data_ok:
        if bool(getattr(config, "HARD_GATE_FAIL_OPEN", False)):
            return True, "hard_gate fail-open (missing EMA200 context)"
        return False, "missing EMA200 context M15/H1"

    if direction == "LONG" and (pos_m15 == "below" or pos_h1 == "below"):
        return False, f"LONG blocked by EMA200 (M15={pos_m15}, H1={pos_h1})"

    if (
        direction == "SHORT"
        and bool(getattr(config, "HARD_GATE_BLOCK_SHORT_ABOVE_EMA200", False))
        and pos_m15 == "above"
        and pos_h1 == "above"
    ):
        return False, f"SHORT blocked by EMA200 (M15={pos_m15}, H1={pos_h1})"

    return True, f"hard_gate pass (M15={pos_m15}, H1={pos_h1})"


def _apply_atr_brake(
    symbol: str,
    tp_pct: float,
    sl_pct: float,
    current_size_str: str,
    pos_info: dict,
) -> tuple[float, float, str, dict, bool]:
    """
    In high-volatility regime: widen SL and reduce position size so net SL stays near target.
    Returns (tp_pct, sl_pct, size_str, pos_info, applied).
    """
    if not bool(getattr(config, "ATR_BRAKE_ENABLED", False)):
        return tp_pct, sl_pct, current_size_str, pos_info, False

    atr_ratio = float(pos_info.get("atr_ratio", 1.0) or 1.0)
    trigger = max(1.0, float(getattr(config, "ATR_BRAKE_TRIGGER_RATIO", 1.35)))
    if atr_ratio < trigger:
        return tp_pct, sl_pct, current_size_str, pos_info, False

    max_mult = max(1.0, float(getattr(config, "ATR_BRAKE_MAX_MULTIPLIER", 2.0)))
    multiplier = min(max_mult, max(1.0, atr_ratio / trigger))
    new_sl_pct = max(0.01, float(sl_pct) * multiplier)

    leverage = max(1.0, float(config.get_leverage(symbol)))
    fee_factor = max(0.0, float(config.TAKER_FEE_RATE) * 2.0)
    per_margin_loss = leverage * ((new_sl_pct / 100.0) + fee_factor)
    if per_margin_loss <= 0:
        return tp_pct, sl_pct, current_size_str, pos_info, False

    target_net_sl = max(0.05, float(getattr(config, "ATR_BRAKE_TARGET_NET_SL_USDT", 1.2)))
    target_margin = target_net_sl / per_margin_loss
    min_margin = max(0.1, float(getattr(config, "ATR_BRAKE_MIN_MARGIN_USDT", 2.0)))
    max_margin = max(min_margin, float(getattr(config, "ATR_BRAKE_MAX_MARGIN_USDT", 10.0)))
    target_margin = max(min_margin, min(max_margin, target_margin))

    size_str, resized_info = okx.calc_position_size(symbol, target_margin, int(leverage))
    if size_str == "0" or resized_info.get("error"):
        return tp_pct, sl_pct, current_size_str, pos_info, False

    resized_info["bonus_slot_eligible"] = _is_bonus_slot_eligible(resized_info)
    resized_info["atr_brake_applied"] = True
    resized_info["atr_ratio"] = atr_ratio
    resized_info["atr_multiplier"] = round(multiplier, 4)
    resized_info["target_margin_usdt"] = round(target_margin, 4)
    resized_info["target_net_sl_usdt"] = round(target_net_sl, 4)
    return tp_pct, new_sl_pct, size_str, resized_info, True


def _roundtrip_fee_pct() -> float:
    """Estimated open+close taker fee in % of notional."""
    return max(0.0, float(config.TAKER_FEE_RATE) * 2.0 * 100.0)


def _roundtrip_fee_usdt(symbol: str) -> float:
    """Estimated open+close taker fee in USDT for one trade."""
    notional = float(config.TRADE_AMOUNT_USDT) * float(config.get_leverage(symbol))
    return max(0.0, notional * float(config.TAKER_FEE_RATE) * 2.0)


def _roundtrip_fee_usdt_for_margin(margin_usdt: float, leverage: float) -> float:
    """Estimated open+close taker fee in USDT for a given margin/leverage."""
    return max(0.0, float(margin_usdt) * float(leverage) * float(config.TAKER_FEE_RATE) * 2.0)


def _simple_scalp_net_usdt_tp_sl_pct(
    symbol: str,
    margin_usdt: float,
    net_tp_usdt: float,
    net_sl_usdt: float,
) -> tuple[float, float, str]:
    """
    Convert net PnL targets in USDT into TP/SL % on price move.

    net_tp_usdt: desired take-profit AFTER fees.
    net_sl_usdt: max stop-loss AFTER fees.
    """
    leverage = max(1.0, float(config.get_leverage(symbol)))
    margin = max(0.01, float(margin_usdt))
    fee_usdt = _roundtrip_fee_usdt_for_margin(margin, leverage)

    # TP needs to cover roundtrip fees to realize net target.
    gross_tp_usdt = max(0.01, float(net_tp_usdt) + fee_usdt)

    # SL net includes fees, so gross move can be slightly smaller.
    gross_sl_usdt = max(0.01, float(net_sl_usdt) - fee_usdt)

    denom = max(1e-9, margin * leverage)
    tp_pct = max(0.01, (gross_tp_usdt / denom) * 100.0)
    sl_pct = max(0.01, (gross_sl_usdt / denom) * 100.0)
    note = (
        f"margin={margin:.2f}USDT lev={leverage:.1f}x fee~{fee_usdt:.3f}USDT "
        f"net_tp={net_tp_usdt:.2f}->gross_tp={gross_tp_usdt:.2f} "
        f"net_sl={net_sl_usdt:.2f}->gross_sl={gross_sl_usdt:.2f}"
    )
    return tp_pct, sl_pct, note


def _guardian_fee_floor_margin_pct(symbol: str) -> float:
    leverage = float(config.get_leverage(symbol))
    return max(0.0, _roundtrip_fee_pct() * leverage)


def _guardian_loss_close_margin_pct(symbol: str) -> float:
    leverage = float(config.get_leverage(symbol))
    sl_margin = float(config.get_sl(symbol)) * leverage
    factor = max(0.1, min(1.0, float(config.GUARDIAN_LOSS_CLOSE_SL_FACTOR)))
    return max(0.0, sl_margin * factor)


def _guardian_max_fee_loss_margin_pct(symbol: str) -> float:
    leverage = float(config.get_leverage(symbol))
    configured = max(0.0, float(config.GUARDIAN_MAX_FEE_LOSS_PCT) * leverage)
    return max(_guardian_fee_floor_margin_pct(symbol), configured)


def _quick_take_margin_target(symbol: str) -> float:
    target = max(0.0, float(getattr(config, "QUICK_TAKE_MARGIN_PCT", 0.0)))
    if bool(getattr(config, "QUICK_TAKE_REQUIRE_FEE_COVER", True)):
        fee_floor = _guardian_fee_floor_margin_pct(symbol)
        fee_buffer = max(0.0, float(getattr(config, "QUICK_TAKE_FEE_BUFFER_PCT", 0.0)))
        target = max(target, fee_floor + fee_buffer)
    return target


def _simple_scalp_edge_check(symbol: str, indicators: dict) -> tuple[bool, str]:
    """
    Guard kỳ vọng lãi ròng cho SIMPLE_SCALP_MODE:
    - TP ròng phải dương sau phí + slippage buffer
    - Tỉ lệ Reward/Risk ròng không quá thấp
    - Spread không được quá rộng so với TP
    """
    tp_pct = max(0.0, float(indicators.get("tp_pct", config.get_tp(symbol))))
    sl_pct = max(0.0, float(indicators.get("sl_pct", config.get_sl(symbol))))
    fee_pct = _roundtrip_fee_pct()
    slippage_buffer = max(0.0, float(getattr(config, "SIMPLE_SCALP_SLIPPAGE_BUFFER_PCT", 0.0)))
    trading_cost_pct = fee_pct + slippage_buffer

    net_tp_pct = tp_pct - trading_cost_pct
    net_sl_pct = sl_pct + trading_cost_pct
    min_net_tp_pct = max(0.0, float(getattr(config, "SIMPLE_SCALP_MIN_NET_TP_PCT", 0.0)))
    min_rr = max(0.01, float(getattr(config, "SIMPLE_SCALP_MIN_RR", 0.0)))

    if net_tp_pct < min_net_tp_pct:
        return (
            False,
            "edge_guard: TP ròng quá thấp "
            f"(tp={tp_pct:.3f}%, fee+slip={trading_cost_pct:.3f}%, net={net_tp_pct:.3f}% < {min_net_tp_pct:.3f}%)",
        )

    rr = net_tp_pct / max(1e-9, net_sl_pct)
    if rr < min_rr:
        return (
            False,
            "edge_guard: RR ròng thấp "
            f"(net_tp={net_tp_pct:.3f}% / net_sl={net_sl_pct:.3f}% = {rr:.2f} < {min_rr:.2f})",
        )

    spread_pct = max(0.0, float(indicators.get("spread_pct", 0.0) or 0.0))
    max_spread_ratio = max(0.0, float(getattr(config, "SIMPLE_SCALP_MAX_SPREAD_TO_TP_RATIO", 0.0)))
    if max_spread_ratio > 0 and tp_pct > 0:
        max_spread_pct = tp_pct * max_spread_ratio
        if spread_pct > max_spread_pct:
            return (
                False,
                "edge_guard: spread rộng "
                f"(spread={spread_pct:.4f}% > {max_spread_pct:.4f}% = {max_spread_ratio:.2f}*TP)",
            )

    return True, (
        "edge_guard: pass "
        f"(tp={tp_pct:.3f}%, sl={sl_pct:.3f}%, net_tp={net_tp_pct:.3f}%, net_rr={rr:.2f}, "
        f"spread={spread_pct:.4f}%)"
    )


def _price_precision_hint(price: float) -> int:
    if price >= 1000:
        return 2
    if price >= 100:
        return 3
    if price >= 1:
        return 4
    if price >= 0.1:
        return 5
    if price >= 0.01:
        return 6
    if price >= 0.001:
        return 7
    if price >= 0.0001:
        return 8
    return 10


def _locked_profit_sl_price(symbol: str, direction: str, entry_price: float) -> float:
    """Move SL past fees so BE lock still keeps a net-positive result."""
    lock_pct = _roundtrip_fee_pct() + max(0.0, float(config.GUARDIAN_LOCK_PROFIT_BUFFER_PCT))
    if direction == "LONG":
        target = entry_price * (1 + lock_pct / 100.0)
    else:
        target = entry_price * (1 - lock_pct / 100.0)
    return round(target, _price_precision_hint(entry_price))


def _cluster_name(symbol: str) -> str:
    return get_pair_cluster(symbol)


def _apply_cluster_cap(
    queued: list[tuple[str, dict, dict | None]],
    max_items: int,
) -> tuple[list[tuple[str, dict, dict | None]], list[tuple[str, dict, dict | None, str]]]:
    """Keep queue diversified so one BTC-beta cluster does not consume all slots."""
    if not queued:
        return [], []

    max_per_cluster = max(1, int(getattr(config, "SCORE_ONLY_MAX_PER_CLUSTER", 1)))
    if max_per_cluster <= 0:
        return queued[:max_items], []

    active_counts = Counter(_cluster_name(symbol) for symbol in active_trades)
    kept: list[tuple[str, dict, dict | None]] = []
    deferred: list[tuple[str, dict, dict | None, str]] = []
    queued_counts: Counter[str] = Counter()

    for coin, signal, ai_result in queued:
        cluster = _cluster_name(coin)
        if active_counts[cluster] + queued_counts[cluster] >= max_per_cluster:
            deferred.append((coin, signal, ai_result, cluster))
            continue
        kept.append((coin, signal, ai_result))
        queued_counts[cluster] += 1
        if len(kept) >= max_items:
            break

    return kept, deferred


def _defer_reason_text(reason: str, max_ai: int, total_count: int) -> str:
    if reason == "queue-cap":
        return f"queue cap {max_ai}/{total_count}"
    return f"cluster cap {reason}"


def _simple_scalp_strategy_mode() -> str:
    mode = str(getattr(config, "SIMPLE_SCALP_STRATEGY", "btc_sync")).strip().lower()
    return mode or "btc_sync"


def _simple_scalp_needs_btc_context() -> bool:
    if not bool(getattr(config, "SIMPLE_SCALP_MODE", False)):
        return False
    if not bool(getattr(config, "SIMPLE_SCALP_USE_BTC_CONTEXT", True)):
        return False
    mode = _simple_scalp_strategy_mode()
    if mode == "m15_m5_retest":
        return False
    return True


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _build_btc_m1_pulse(candles_1m: list, candles_5m: list) -> dict:
    """Build fast BTC M1 pulse used by SIMPLE_SCALP_MODE."""
    try:
        micro = analyze_micro_setup(candles_1m, candles_5m)
    except Exception as e:
        return {
            "trend": "neutral",
            "m1_bull_streak": 0,
            "m1_bear_streak": 0,
            "m1_price_vs_ema21": "unknown",
            "m1_breakout_up": False,
            "m1_breakout_down": False,
            "detail": f"error: {e}",
        }

    bull_streak = int(micro.get("m1_bull_streak", 0) or 0)
    bear_streak = int(micro.get("m1_bear_streak", 0) or 0)
    trend = "neutral"
    if bull_streak > 0 and bull_streak >= bear_streak:
        trend = "bullish"
    elif bear_streak > 0 and bear_streak > bull_streak:
        trend = "bearish"

    return {
        "trend": trend,
        "m1_bull_streak": bull_streak,
        "m1_bear_streak": bear_streak,
        "m1_price_vs_ema21": str(micro.get("m1_price_vs_ema21", "unknown")),
        "m1_breakout_up": bool(micro.get("m1_breakout_up", False)),
        "m1_breakout_down": bool(micro.get("m1_breakout_down", False)),
        "detail": (
            f"trend={trend}, streak(bull={bull_streak}, bear={bear_streak}), "
            f"ema21={micro.get('m1_price_vs_ema21', 'unknown')}"
        ),
    }


def _simple_scalp_trigger_text() -> str:
    """Human-readable simple scalp trigger config for logs/reasoning."""
    if _simple_scalp_strategy_mode() == "m15_m5_retest":
        return "M15 S/R zone + M5 pinbar + M1 xác nhận"

    parts = ["BTC M1 đồng pha", "Alt M1 2 nến"]
    if bool(getattr(config, "SIMPLE_SCALP_REQUIRE_EMA_ALIGN", True)):
        parts.append("EMA21 M1 cùng pha")
    if bool(getattr(config, "SIMPLE_SCALP_REQUIRE_BREAKOUT_CONFIRM", False)):
        parts.append("breakout M1 xác nhận")
    if bool(getattr(config, "SIMPLE_SCALP_REQUIRE_M5_WAVE", True)):
        parts.append("M5 wick có sóng")
    return " + ".join(parts)


def _simple_scalp_signal_m15_m5(candle_data: dict, micro_data: dict) -> dict:
    """
    Fast reversal scalp:
    - M15: near support/resistance zone
    - M5: pinbar rejection
    - M1: 2-bar confirmation (and EMA alignment if enabled)
    """
    sr = candle_data.get("sr_15m", {}) if isinstance(candle_data, dict) else {}
    near_support = bool(sr.get("m15_near_support", False))
    near_resistance = bool(sr.get("m15_near_resistance", False))
    breakdown_support = bool(sr.get("m15_breakdown_support", False))
    breakout_resistance = bool(sr.get("m15_breakout_resistance", False))
    dist_to_support = float(sr.get("m15_dist_to_support_pct", 999.0) or 999.0)
    dist_to_resistance = float(sr.get("m15_dist_to_resistance_pct", 999.0) or 999.0)

    bull_streak = int(micro_data.get("m1_bull_streak", 0) or 0)
    bear_streak = int(micro_data.get("m1_bear_streak", 0) or 0)
    m1_confirm = max(1, int(getattr(config, "SIMPLE_SCALP_M1_CONFIRM_STREAK", 2)))
    m1_ema = str(micro_data.get("m1_price_vs_ema21", "unknown"))
    require_m1_ema = bool(getattr(config, "SIMPLE_SCALP_M1_CONFIRM_REQUIRE_EMA", True))

    m5_up_wick = float(micro_data.get("m5_last_upper_wick_pct", 0.0) or 0.0)
    m5_low_wick = float(micro_data.get("m5_last_lower_wick_pct", 0.0) or 0.0)
    m5_body = float(micro_data.get("m5_last_body_pct", 0.0) or 0.0)
    m5_range = float(micro_data.get("m5_last_range_pct", 0.0) or 0.0)
    wick_body_min = max(1.2, float(getattr(config, "SIMPLE_SCALP_M5_PINBAR_WICK_BODY_MIN", 1.8)))
    body_max_ratio = max(0.05, float(getattr(config, "SIMPLE_SCALP_M5_PINBAR_BODY_MAX_RATIO", 0.35)))
    body_ratio = m5_body / max(1e-9, m5_range)

    m5_pinbar_bull = bool(
        micro_data.get("m5_pinbar_bullish", False)
        or (
            m5_low_wick / max(1e-9, m5_body) >= wick_body_min
            and m5_low_wick >= m5_up_wick * 1.15
            and body_ratio <= body_max_ratio
        )
    )
    m5_pinbar_bear = bool(
        micro_data.get("m5_pinbar_bearish", False)
        or (
            m5_up_wick / max(1e-9, m5_body) >= wick_body_min
            and m5_up_wick >= m5_low_wick * 1.15
            and body_ratio <= body_max_ratio
        )
    )

    require_zone = bool(getattr(config, "SIMPLE_SCALP_REQUIRE_M15_ZONE", True))
    require_pinbar = bool(getattr(config, "SIMPLE_SCALP_REQUIRE_M5_PINBAR", True))
    zone_long_ok = near_support and not breakdown_support
    zone_short_ok = near_resistance and not breakout_resistance
    if not require_zone:
        zone_long_ok = not breakdown_support
        zone_short_ok = not breakout_resistance

    m1_long_ok = bull_streak >= m1_confirm
    m1_short_ok = bear_streak >= m1_confirm
    if require_m1_ema:
        m1_long_ok = m1_long_ok and m1_ema == "above"
        m1_short_ok = m1_short_ok and m1_ema == "below"

    long_ok = zone_long_ok and m1_long_ok and (m5_pinbar_bull or not require_pinbar)
    short_ok = zone_short_ok and m1_short_ok and (m5_pinbar_bear or not require_pinbar)
    regime_hint = str(micro_data.get("m1_market_regime_hint", "mixed")).lower()
    volume_surge_pct = float(micro_data.get("m1_volume_surge_pct", 0.0) or 0.0)
    m1_breakout_up = bool(micro_data.get("m1_breakout_up", False))
    m1_breakout_down = bool(micro_data.get("m1_breakout_down", False))

    long_score = (
        (3 if zone_long_ok else 0)
        + (2 if m5_pinbar_bull else 0)
        + min(3, bull_streak)
        + (1 if m1_ema == "above" else 0)
    )
    short_score = (
        (3 if zone_short_ok else 0)
        + (2 if m5_pinbar_bear else 0)
        + min(3, bear_streak)
        + (1 if m1_ema == "below" else 0)
    )

    if regime_hint == "trend":
        trend_long_ok = m1_breakout_up and volume_surge_pct >= 15.0
        trend_short_ok = m1_breakout_down and volume_surge_pct >= 15.0
        long_ok = long_ok and trend_long_ok
        short_ok = short_ok and trend_short_ok
        long_score += 2 if trend_long_ok else -2
        short_score += 2 if trend_short_ok else -2
    elif regime_hint == "sideway":
        # Sideway regime favors rejection entries near S/R.
        long_ok = long_ok and m5_pinbar_bull
        short_ok = short_ok and m5_pinbar_bear

    tp_pct = max(0.0, float(config.SIMPLE_SCALP_DYNAMIC_TP_MIN_PCT))
    sl_pct = max(0.0, float(config.SIMPLE_SCALP_DYNAMIC_SL_MIN_PCT))
    if bool(getattr(config, "SIMPLE_SCALP_DYNAMIC_TP_ENABLED", True)):
        tp_min = max(0.05, float(config.SIMPLE_SCALP_DYNAMIC_TP_MIN_PCT))
        tp_max = max(tp_min, float(config.SIMPLE_SCALP_DYNAMIC_TP_MAX_PCT))
        sl_min = max(0.05, float(config.SIMPLE_SCALP_DYNAMIC_SL_MIN_PCT))
        sl_max = max(sl_min, float(config.SIMPLE_SCALP_DYNAMIC_SL_MAX_PCT))
        sl_tp_ratio = _clamp(float(config.SIMPLE_SCALP_DYNAMIC_SL_TP_RATIO), 0.3, 1.2)
        to_target = dist_to_resistance if long_score >= short_score else dist_to_support
        if to_target > 900:
            to_target = tp_min
        tp_pct = _clamp(max(tp_min, to_target * 0.35), tp_min, tp_max)
        sl_pct = _clamp(max(sl_min, tp_pct * sl_tp_ratio), sl_min, sl_max)

    if long_ok and (not short_ok or long_score >= short_score):
        reasons = [
            f"M15 support zone={zone_long_ok} (dist={dist_to_support:.3f}%)",
            f"M5 pinbar bullish={m5_pinbar_bull} (lw={m5_low_wick:.3f}%, uw={m5_up_wick:.3f}%)",
            f"M1 confirm bullish x{bull_streak} (ema={m1_ema})",
            f"Regime={regime_hint} breakout={m1_breakout_up} vol={volume_surge_pct:.1f}%",
            f"TP/SL={tp_pct:.3f}%/{sl_pct:.3f}%",
        ]
        return {
            "has_signal": True,
            "direction": "LONG",
            "strength": max(4, long_score),
            "reasons": reasons,
            "tp_pct": round(tp_pct, 4),
            "sl_pct": round(sl_pct, 4),
        }

    if short_ok and (not long_ok or short_score > long_score):
        reasons = [
            f"M15 resistance zone={zone_short_ok} (dist={dist_to_resistance:.3f}%)",
            f"M5 pinbar bearish={m5_pinbar_bear} (uw={m5_up_wick:.3f}%, lw={m5_low_wick:.3f}%)",
            f"M1 confirm bearish x{bear_streak} (ema={m1_ema})",
            f"Regime={regime_hint} breakout={m1_breakout_down} vol={volume_surge_pct:.1f}%",
            f"TP/SL={tp_pct:.3f}%/{sl_pct:.3f}%",
        ]
        return {
            "has_signal": True,
            "direction": "SHORT",
            "strength": max(4, short_score),
            "reasons": reasons,
            "tp_pct": round(tp_pct, 4),
            "sl_pct": round(sl_pct, 4),
        }

    fail_reasons = [
        (
            f"No trigger (M15 near_s={near_support}, near_r={near_resistance}, "
            f"break_s={breakdown_support}, break_r={breakout_resistance})"
        ),
        (
            f"M5 pinbar bull={m5_pinbar_bull}, bear={m5_pinbar_bear}, "
            f"body_ratio={body_ratio:.2f}"
        ),
        (
            f"M1 streak bull={bull_streak}, bear={bear_streak}, confirm>={m1_confirm}, "
            f"ema={m1_ema}, regime={regime_hint}, vol={volume_surge_pct:.1f}%"
        ),
    ]
    return {
        "has_signal": False,
        "direction": "NONE",
        "strength": max(1, int(long_score or short_score or 1)),
        "reasons": fail_reasons,
        "tp_pct": round(tp_pct, 4),
        "sl_pct": round(sl_pct, 4),
    }


def _simple_scalp_signal(candle_data: dict, micro_data: dict) -> dict:
    """
    Simple mode:
    - Trigger nhanh theo BTC M1 + Alt M1 cùng hướng
    - Optionally keep BTC M15 trend filter as wind direction
    """
    if _simple_scalp_strategy_mode() == "m15_m5_retest":
        return _simple_scalp_signal_m15_m5(candle_data, micro_data)

    m15_bias = str(btc_trend.get("trend", "neutral")).lower()
    force_m15 = bool(getattr(config, "SIMPLE_SCALP_FORCE_M15_TREND", True))
    use_btc_context = _simple_scalp_needs_btc_context()
    require_btc_sync = bool(getattr(config, "SIMPLE_SCALP_REQUIRE_BTC_M1_SYNC", True))
    if not use_btc_context:
        require_btc_sync = False
        force_m15 = False
    streak_min = max(1, int(getattr(config, "SIMPLE_SCALP_STREAK_MIN", 1)))
    btc_streak_min = max(1, int(getattr(config, "SIMPLE_SCALP_BTC_M1_STREAK_MIN", 1)))
    require_alt_streak = bool(getattr(config, "SIMPLE_SCALP_REQUIRE_ALT_STREAK", True))
    use_wick_trigger = bool(getattr(config, "SIMPLE_SCALP_USE_WICK_TRIGGER", False))
    require_ema_align = bool(getattr(config, "SIMPLE_SCALP_REQUIRE_EMA_ALIGN", True))
    require_breakout = bool(getattr(config, "SIMPLE_SCALP_REQUIRE_BREAKOUT_CONFIRM", False))
    require_m5_wave = bool(getattr(config, "SIMPLE_SCALP_REQUIRE_M5_WAVE", True))
    m5_wick_min = max(0.01, float(getattr(config, "SIMPLE_SCALP_M5_WICK_MIN_PCT", 0.10)))
    wick_min = max(0.01, float(getattr(config, "SIMPLE_SCALP_WICK_MIN_PCT", 0.10)))

    bull_streak = int(micro_data.get("m1_bull_streak", 0) or 0)
    bear_streak = int(micro_data.get("m1_bear_streak", 0) or 0)
    btc_bull_streak = int(btc_m1_pulse.get("m1_bull_streak", 0) or 0)
    btc_bear_streak = int(btc_m1_pulse.get("m1_bear_streak", 0) or 0)
    alt_ema_pos = str(micro_data.get("m1_price_vs_ema21", "unknown"))
    btc_ema_pos = str(btc_m1_pulse.get("m1_price_vs_ema21", "unknown"))
    alt_breakout_up = bool(micro_data.get("m1_breakout_up", False))
    alt_breakout_down = bool(micro_data.get("m1_breakout_down", False))
    btc_breakout_up = bool(btc_m1_pulse.get("m1_breakout_up", False))
    btc_breakout_down = bool(btc_m1_pulse.get("m1_breakout_down", False))
    up_wick = float(micro_data.get("m1_last_upper_wick_pct", 0.0) or 0.0)
    low_wick = float(micro_data.get("m1_last_lower_wick_pct", 0.0) or 0.0)
    m5_up_wick = float(micro_data.get("m5_last_upper_wick_pct", 0.0) or 0.0)
    m5_low_wick = float(micro_data.get("m5_last_lower_wick_pct", 0.0) or 0.0)
    m5_wave = bool(
        micro_data.get("m5_wave_ready")
        or max(m5_up_wick, m5_low_wick) >= m5_wick_min
    )

    long_wick = bool(
        micro_data.get("m1_long_wick_bullish")
        or low_wick >= max(wick_min, up_wick * 1.3)
    )
    short_wick = bool(
        micro_data.get("m1_long_wick_bearish")
        or up_wick >= max(wick_min, low_wick * 1.3)
    )
    long_streak = bull_streak >= streak_min
    short_streak = bear_streak >= streak_min
    btc_up = btc_bull_streak >= btc_streak_min
    btc_down = btc_bear_streak >= btc_streak_min

    reasons_long: list[str] = []
    reasons_short: list[str] = []
    if btc_up:
        reasons_long.append(f"BTC M1 xanh x{btc_bull_streak}")
    if btc_down:
        reasons_short.append(f"BTC M1 đỏ x{btc_bear_streak}")
    if long_wick:
        reasons_long.append(f"M1 lower wick dài {low_wick:.3f}%")
    if long_streak:
        reasons_long.append(f"M1 {bull_streak} nến xanh liên tiếp")
    if short_wick:
        reasons_short.append(f"M1 upper wick dài {up_wick:.3f}%")
    if short_streak:
        reasons_short.append(f"M1 {bear_streak} nến đỏ liên tiếp")
    if m5_wave:
        reasons_long.append(
            f"M5 wick sóng {max(m5_up_wick, m5_low_wick):.3f}%"
        )
        reasons_short.append(
            f"M5 wick sóng {max(m5_up_wick, m5_low_wick):.3f}%"
        )

    if require_alt_streak:
        long_ok = long_streak
        short_ok = short_streak
    else:
        long_ok = long_streak or (use_wick_trigger and long_wick)
        short_ok = short_streak or (use_wick_trigger and short_wick)

    if require_btc_sync:
        long_ok = long_ok and btc_up
        short_ok = short_ok and btc_down
    else:
        long_ok = long_ok or btc_up
        short_ok = short_ok or btc_down

    long_ema_ok = alt_ema_pos == "above" and btc_ema_pos == "above"
    short_ema_ok = alt_ema_pos == "below" and btc_ema_pos == "below"
    if require_ema_align:
        long_ok = long_ok and long_ema_ok
        short_ok = short_ok and short_ema_ok
    if long_ema_ok:
        reasons_long.append("EMA21 M1 cùng pha (BTC+ALT)")
    if short_ema_ok:
        reasons_short.append("EMA21 M1 cùng pha (BTC+ALT)")

    long_breakout_ok = alt_breakout_up or btc_breakout_up
    short_breakout_ok = alt_breakout_down or btc_breakout_down
    if require_breakout:
        long_ok = long_ok and long_breakout_ok
        short_ok = short_ok and short_breakout_ok
    if long_breakout_ok:
        reasons_long.append("Breakout M1 xác nhận")
    if short_breakout_ok:
        reasons_short.append("Breakout M1 xác nhận")

    if require_m5_wave:
        long_ok = long_ok and m5_wave
        short_ok = short_ok and m5_wave

    if force_m15:
        if m15_bias == "bullish":
            short_ok = False
        elif m15_bias == "bearish":
            long_ok = False
        else:
            long_ok = False
            short_ok = False

    if long_ok and not short_ok:
        strength = 4 + int(long_wick) + int(long_streak) + int(btc_up)
        head = [f"BTC M1={btc_m1_pulse.get('trend', 'neutral')}"]
        if force_m15:
            head.append(f"BTC M15={m15_bias}")
        return {
            "has_signal": True,
            "direction": "LONG",
            "strength": strength,
            "reasons": head + reasons_long,
        }

    if short_ok and not long_ok:
        strength = 4 + int(short_wick) + int(short_streak) + int(btc_down)
        head = [f"BTC M1={btc_m1_pulse.get('trend', 'neutral')}"]
        if force_m15:
            head.append(f"BTC M15={m15_bias}")
        return {
            "has_signal": True,
            "direction": "SHORT",
            "strength": strength,
            "reasons": head + reasons_short,
        }

    if require_alt_streak and bull_streak < streak_min:
        long_trigger_reason = f"ALT M1 xanh < {streak_min} nến"
    else:
        long_trigger_reason = "M1 trigger long chưa đủ"
    if require_alt_streak and bear_streak < streak_min:
        short_trigger_reason = f"ALT M1 đỏ < {streak_min} nến"
    else:
        short_trigger_reason = "M1 trigger short chưa đủ"
    if require_m5_wave and not m5_wave:
        wave_reason = f"M5 wick < {m5_wick_min:.2f}%"
        long_trigger_reason = f"{long_trigger_reason}, {wave_reason}"
        short_trigger_reason = f"{short_trigger_reason}, {wave_reason}"
    if require_ema_align and not long_ema_ok:
        long_trigger_reason = f"{long_trigger_reason}, EMA21 long chưa đồng pha"
    if require_ema_align and not short_ema_ok:
        short_trigger_reason = f"{short_trigger_reason}, EMA21 short chưa đồng pha"
    if require_breakout and not long_breakout_ok:
        long_trigger_reason = f"{long_trigger_reason}, breakout long chưa xác nhận"
    if require_breakout and not short_breakout_ok:
        short_trigger_reason = f"{short_trigger_reason}, breakout short chưa xác nhận"
    long_block_reason = "BTC M1 chưa đồng pha" if (require_btc_sync and not btc_up) else long_trigger_reason
    short_block_reason = "BTC M1 chưa đồng pha" if (require_btc_sync and not btc_down) else short_trigger_reason
    return {
        "has_signal": False,
        "direction": "NONE",
        "strength": max(1, int(long_wick or short_wick or long_streak or short_streak)),
        "reasons": [
            (
                f"No trigger (BTC M1={btc_m1_pulse.get('trend', 'neutral')}, "
                f"BTC M15={m15_bias}, {long_block_reason}, {short_block_reason})"
            )
        ],
    }


def _guardian_close_reason(symbol: str, trade: dict, elapsed_sec: float, cur_pnl_margin: float) -> str:
    """Close stale losers early in scalp mode before they bleed into full SL."""
    if not bool(getattr(config, "GUARDIAN_STALE_EXIT_ENABLED", True)):
        return ""

    fee_floor = _guardian_fee_floor_margin_pct(symbol)
    max_fee_loss = _guardian_max_fee_loss_margin_pct(symbol)
    loss_rescue = _guardian_loss_close_margin_pct(symbol)
    max_pnl_margin = float(trade.get("max_pnl_margin", cur_pnl_margin) or cur_pnl_margin)
    first_check_sec = max(0.0, float(config.GUARDIAN_FIRST_CHECK_SEC))
    min_hold_sec = max(first_check_sec, float(config.GUARDIAN_MIN_HOLD_SEC))
    min_loss_rescue_sec = max(first_check_sec, float(config.GUARDIAN_MIN_LOSS_RESCUE_SEC))
    stale_min_neg = max(0.0, float(getattr(config, "GUARDIAN_STALE_MIN_NEG_MARGIN_PCT", 0.0)))

    if bool(trade.get("tp1_done", False)):
        return ""

    if (
        elapsed_sec >= first_check_sec
        and max_pnl_margin < fee_floor
        and cur_pnl_margin <= -max_fee_loss
    ):
        return (
            "guardian_stall: lenh khong co lai som "
            f"(max={max_pnl_margin:+.2f}%, now={cur_pnl_margin:+.2f}%)"
        )

    if elapsed_sec >= min_loss_rescue_sec and cur_pnl_margin <= -loss_rescue:
        return (
            "guardian_loss_rescue: cat lo truoc SL day du "
            f"(now={cur_pnl_margin:+.2f}% <= -{loss_rescue:.2f}%)"
        )

    if (
        elapsed_sec >= min_hold_sec
        and max_pnl_margin < fee_floor
        and cur_pnl_margin <= -stale_min_neg
    ):
        return (
            "guardian_stale_loser: om lenh am qua lau ma chua vuot phi "
            f"(max={max_pnl_margin:+.2f}%, now={cur_pnl_margin:+.2f}%)"
        )

    return ""


def _close_side(direction: str) -> str:
    return "sell" if direction == "LONG" else "buy"


def _first_open_position(positions: list[dict]) -> dict | None:
    for p in positions:
        try:
            if float(p.get("pos", "0")) != 0:
                return p
        except Exception:
            continue
    return None


async def _rearm_tp_sl_for_remaining(symbol: str, trade: dict,
                                     size_str: str,
                                     sl_price: float | None = None) -> bool:
    """
    Huy algo cu va dat lai TP/SL cho size con lai.
    Dung khi doi SL ve entry hoac sau khi da chot 1 phan vi the.
    """
    if not size_str:
        return False
    try:
        if sl_price is not None:
            trade["sl"] = float(sl_price)
            trade["locked_sl_price"] = float(sl_price)
        tp = trade["tp"]
        sl = trade["sl"]
        close_side = _close_side(trade["direction"])
        await asyncio.to_thread(okx.cancel_algo_orders, symbol)
        tp_sl_result = await asyncio.to_thread(
            okx.place_tp_sl,
            symbol,
            close_side,
            size_str,
            str(tp),
            str(sl),
        )
        if tp_sl_result.get("code") == "0":
            trade["size"] = size_str
            return True
        logger.warning(f"Re-arm TP/SL failed {symbol}: {tp_sl_result}")
        return False
    except Exception as e:
        logger.error(f"Re-arm TP/SL error {symbol}: {e}")
        return False


def _calc_scan_budget_sec() -> float:
    """Scan budget to avoid running longer than schedule interval."""
    if config.SCAN_TIME_BUDGET_SEC > 0:
        return float(config.SCAN_TIME_BUDGET_SEC)
    return float(max(10, config.SCAN_INTERVAL - config.SCAN_TIME_BUFFER_SEC))

# ── scan market ─────────────────────────────────────────────
def _get_scan_layers() -> list[tuple[str, list[str]]]:
    """Build scan layers: L1 first, L2 after."""
    l1 = list(dict.fromkeys(config.COINS_LAYER1))
    l2 = [coin for coin in config.COINS_LAYER2 if coin not in l1]
    layers: list[tuple[str, list[str]]] = []

    if l1:
        layers.append(("L1", l1))
    if l2:
        layers.append(("L2", l2))

    # Backward compatible fallback when COINS_LAYER* is empty
    if not layers and config.COINS:
        layers.append(("L1", list(dict.fromkeys(config.COINS))))
    return layers


def _signal_priority_key(signal: dict) -> tuple:
    """Priority score for deciding which signals get AI budget first."""
    indicators = signal.get("indicators", {})
    direction = signal.get("direction", "")
    strength = int(signal.get("strength", 0) or 0)
    cvd_bias = indicators.get("cvd_bias", "neutral")
    ob_bias = indicators.get("ob_bias", "neutral")

    flow_align = int(
        (direction == "LONG" and cvd_bias == "bullish" and ob_bias == "bullish")
        or (direction == "SHORT" and cvd_bias == "bearish" and ob_bias == "bearish")
    )
    candle_aligned = int(bool(indicators.get("candle_aligned")))
    oi_signal = indicators.get("oi_signal", "normal")
    oi_quality = 2 if oi_signal == "normal" else 1 if oi_signal == "warning" else 0
    imbalance = abs(float(indicators.get("imbalance", 1.0)) - 1.0)

    return (strength, flow_align, candle_aligned, oi_quality, imbalance)


def _score_only_result_priority_key(signal: dict, ai_result: dict) -> tuple:
    """Final ranking for score-only candidates after local analysis."""
    decision = str(ai_result.get("decision", "SKIP")).upper() == "TRADE"
    playbook_score = float(ai_result.get("playbook_score", 0) or 0)
    win_probability = int(ai_result.get("win_probability", 0) or 0)
    confidence = int(ai_result.get("confidence", 0) or 0)
    risk_score = int(ai_result.get("risk_score", 100) or 100)
    grade = str(ai_result.get("entry_quality", "D")).upper()
    grade_rank = {"A": 3, "B": 2, "C": 1}.get(grade, 0)
    strength, flow_align, candle_aligned, oi_quality, imbalance = _signal_priority_key(signal)
    return (
        int(decision),
        playbook_score,
        win_probability,
        -risk_score,
        confidence,
        grade_rank,
        strength,
        flow_align,
        candle_aligned,
        oi_quality,
        imbalance,
    )


async def _prefetch_score_only_candidates(
    candidates: list[tuple[str, dict]],
    scan_deadline: float | None = None,
    score_gate: float | None = None,
) -> tuple[list[tuple[str, dict, dict]], bool]:
    """Analyze local score-only candidates in parallel, then rank them."""
    if not candidates:
        return [], False

    # Warm macro cache once so parallel score-only analysis threads
    # don't race to fetch the same macro snapshot.
    try:
        get_macro_data()
    except Exception as e:
        logger.warning(f"Macro prewarm failed before score-only prefetch: {e}")

    if score_gate is None:
        score_gate = _effective_score_gate()
    scheduled: list[tuple[str, dict, float]] = []
    coroutines = []
    budget_blocked = False

    for coin, signal in candidates:
        ai_budget = float(config.AI_TIME_BUDGET_SEC)
        if scan_deadline is not None:
            remaining = scan_deadline - time.monotonic()
            if remaining < config.AI_MIN_BUDGET_TO_START_SEC:
                budget_blocked = True
                break
            ai_budget = min(
                ai_budget,
                max(
                    float(config.AI_MIN_BUDGET_TO_START_SEC),
                    remaining - float(config.AI_TIMEOUT_SAFETY_SEC),
                ),
            )

        scheduled.append((coin, signal, ai_budget))
        coroutines.append(
            asyncio.to_thread(
                analyze_trade,
                coin,
                signal.get("direction", ""),
                signal.get("indicators", {}),
                ai_budget,
                score_gate,
            )
        )

    if not coroutines:
        return [], budget_blocked

    results = await asyncio.gather(*coroutines, return_exceptions=True)
    analyzed: list[tuple[str, dict, dict]] = []

    for (coin, signal, _budget), result in zip(scheduled, results):
        if isinstance(result, Exception):
            logger.error(f"Lỗi score-only prefetch {coin}: {result}")
            _record_skip("score_only_prefetch_error")
            continue
        ai_time = float(result.get("total_ai_time", 0) or 0)
        _record_ai_latency(ai_time)
        analyzed.append((coin, signal, result))

    analyzed.sort(
        key=lambda item: _score_only_result_priority_key(item[1], item[2]),
        reverse=True,
    )
    return analyzed, budget_blocked


async def _scan_layer_once(
    context: ContextTypes.DEFAULT_TYPE,
    layer_name: str,
    coins: list[str],
    scan_deadline: float | None = None,
) -> tuple[int, int, int, int, int, bool, int]:
    """
    Scan one layer.
    Return (signals, no_signal, errors, scanned, budget_skipped, budget_hit, ai_deferred).
    """
    signal_found = 0
    no_signal = 0
    errors = 0
    scanned = 0
    budget_skipped = 0
    budget_hit = False
    ai_deferred = 0
    candidate_signals: list[tuple[str, dict]] = []

    logger.info(f"🔎 Scan {layer_name}: {', '.join(coins)}")

    # Phase 1: scan all coins IN PARALLEL for speed
    now = time.time()
    scannable = []
    for c in coins:
        if c in active_trades:
            scanned += 1
            no_signal += 1
            logger.info(f"⏭️ Bỏ qua {c} — đang có vị thế mở")
        elif (blocked_reason := _coin_session_block_reason(c)):
            scanned += 1
            no_signal += 1
            logger.info(f"⏭️ Bỏ qua {c} — {blocked_reason}")
            _record_skip("coin_session_block")
        elif c in coin_cooldowns and (remaining := coin_cooldowns[c] - now) > 0:
            scanned += 1
            no_signal += 1
            logger.info(f"⏭️ Bỏ qua {c} — coin cooldown còn {int(remaining)}s sau SL")
        else:
            # Dọn cooldown hết hạn
            coin_cooldowns.pop(c, None)
            scannable.append(c)

    if _trade_slots_available() <= 0:
        logger.info(
            f"🛑 Đã đầy slot lệnh ({len(active_trades)}/{_max_active_trades()}) "
            f"- dừng scan {layer_name}"
        )
    elif scannable:
        # Parallel scan all coins at once
        tasks = [asyncio.to_thread(_analyze_coin, coin) for coin in scannable]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for coin, result in zip(scannable, results):
            scanned += 1
            if isinstance(result, Exception):
                errors += 1
                logger.error(f"Lỗi scan {layer_name} {coin}: {result}")
            elif result and result.get("has_signal"):
                signal_found += 1
                candidate_signals.append((coin, result))
                logger.info(
                    f"✅ Signal {layer_name}: {coin} {result['direction']} "
                    f"(str={result.get('strength', 0)})"
                )
            else:
                no_signal += 1

    # Phase 2: spend AI budget on top-ranked signals only.
    if candidate_signals and _trade_slots_available() > 0:
        if config.LONG_ONLY:
            filtered_short_items = [
                item for item in candidate_signals if item[1].get("direction") != "LONG"
            ]
            long_only = [item for item in candidate_signals if item[1].get("direction") == "LONG"]
            skipped_short = len(candidate_signals) - len(long_only)
            if skipped_short > 0:
                ai_deferred += skipped_short
                logger.info(
                    f"⚖️ LONG_ONLY bật: bỏ {skipped_short} signal SHORT ở {layer_name}"
                )
                _record_skip("short_filtered")
                await _send_log_safe(
                    context,
                    "SKIP FILTER (LONG_ONLY)\n"
                    + "\n".join(
                        f"- {_signal_tag(coin, sig)} | reason: SHORT blocked"
                        for coin, sig in filtered_short_items
                    ),
                )
            candidate_signals = long_only

        # BTC Trend Filter: block signals that go against BTC trend
        if (
            config.BTC_TREND_FILTER
            and candidate_signals
            and not bool(getattr(config, "SIMPLE_SCALP_MODE", False))
        ):
            btc = btc_trend
            strict = config.BTC_TREND_STRICT
            filtered_btc = []
            kept = []
            turbo_countertrend_enabled = (
                bool(getattr(config, "BTC_TREND_COUNTERTRADE_WHEN_TURBO", False))
                and _cadence_turbo_active()
            )
            countertrend_min_strength = max(
                1, int(getattr(config, "BTC_TREND_COUNTERTRADE_MIN_STRENGTH", 5))
            )
            countertrend_cap = max(
                0, int(getattr(config, "BTC_TREND_COUNTERTRADE_MAX_PER_LAYER", 1))
            )
            countertrend_used = 0
            countertrend_kept: list[tuple[str, dict]] = []
            for coin, sig in candidate_signals:
                direction = sig.get("direction", "")
                blocked = False
                if direction == "LONG" and not btc["allow_long"]:
                    blocked = True
                elif direction == "SHORT" and not btc["allow_short"]:
                    blocked = True
                elif strict and btc["strength"] == "weak":
                    # Strict mode: weak trend cũng block ngược
                    if direction == "LONG" and btc["trend"] == "bearish":
                        blocked = True
                    elif direction == "SHORT" and btc["trend"] == "bullish":
                        blocked = True
                if blocked:
                    if (
                        turbo_countertrend_enabled
                        and countertrend_used < countertrend_cap
                        and int(sig.get("strength", 0) or 0) >= countertrend_min_strength
                    ):
                        kept.append((coin, sig))
                        countertrend_kept.append((coin, sig))
                        countertrend_used += 1
                        continue
                    filtered_btc.append((coin, sig))
                else:
                    kept.append((coin, sig))
            if filtered_btc:
                ai_deferred += len(filtered_btc)
                logger.info(
                    f"📊 BTC Filter ({btc['trend']}/{btc['strength']}): "
                    f"block {len(filtered_btc)} signal ngược trend ở {layer_name}"
                )
                _record_skip("btc_trend_filtered")
                await _send_log_safe(
                    context,
                    f"SKIP BTC FILTER ({layer_name}) — BTC {btc['trend']} {btc['strength']}\n"
                    + "\n".join(
                        f"- {_signal_tag(c, s)} | reason: ngược BTC trend"
                        for c, s in filtered_btc
                    ),
                )
            if countertrend_kept:
                logger.info(
                    f"⚖️ BTC countertrend override ({layer_name}): giữ {len(countertrend_kept)} "
                    f"signal ngược trend do turbo cadence"
                )
                _record_skip("btc_countertrend_override")
                await _send_log_safe(
                    context,
                    f"ALLOW COUNTERTREND ({layer_name}) — turbo cadence\n"
                    + "\n".join(
                        f"- {_signal_tag(c, s)} | reason: strength >= {countertrend_min_strength}"
                        for c, s in countertrend_kept
                    ),
                )
            candidate_signals = kept

        if not candidate_signals:
            logger.info(f"ℹ️ {layer_name}: không còn signal phù hợp sau lọc")
            return signal_found, no_signal, errors, scanned, budget_skipped, budget_hit, ai_deferred

        min_strength = max(1, int(config.AI_MIN_SIGNAL_STRENGTH))
        weak_items = [
            item for item in candidate_signals
            if int(item[1].get("strength", 0) or 0) < min_strength
        ]
        ranked = sorted(
            [
                item for item in candidate_signals
                if int(item[1].get("strength", 0) or 0) >= min_strength
            ],
            key=lambda item: _signal_priority_key(item[1]),
            reverse=True,
        )
        filtered_out = len(candidate_signals) - len(ranked)
        if filtered_out > 0:
            ai_deferred += filtered_out
            logger.info(
                f"⚡ Lọc signal yếu ở {layer_name}: defer {filtered_out} "
                f"(str < {min_strength})"
            )
            _record_skip("ai_filtered_strength")
            await _send_log_safe(
                context,
                f"SKIP FILTER ({layer_name})\n"
                + "\n".join(
                    f"- {_signal_tag(coin, sig)} | reason: strength < {min_strength}"
                    for coin, sig in weak_items
                ),
            )

        max_ai = min(
            max(1, int(config.AI_MAX_SIGNALS_PER_LAYER)),
            _trade_slots_available(),
        )

        score_only_mode = bool(config.AI_SCORE_ONLY_MODE)
        current_score_gate = _effective_score_gate()
        queued: list[tuple[str, dict, dict | None]] = []

        if score_only_mode:
            analyzed_ranked, prefetch_budget_blocked = await _prefetch_score_only_candidates(
                ranked,
                scan_deadline=scan_deadline,
                score_gate=current_score_gate,
            )
            tradable_ranked = [
                (coin, signal, ai_result)
                for coin, signal, ai_result in analyzed_ranked
                if not ai_result.get("error")
                and str(ai_result.get("decision", "SKIP")).upper() == "TRADE"
                and float(ai_result.get("playbook_score", 0) or 0) >= float(current_score_gate)
            ]
            rejected_local = len(ranked) - len(tradable_ranked)
            if rejected_local > 0:
                ai_deferred += rejected_local
                reject_reasons = Counter()
                reject_scores: list[float] = []
                for _, _, ai_result in analyzed_ranked:
                    if ai_result.get("error"):
                        reject_reasons["analysis_error"] += 1
                        continue
                    decision = str(ai_result.get("decision", "SKIP")).upper()
                    score = float(ai_result.get("playbook_score", 0) or 0)
                    if decision == "TRADE" and score >= float(current_score_gate):
                        continue
                    reject_scores.append(score)
                    if score < float(current_score_gate):
                        reject_reasons[f"score<{current_score_gate:.1f}"] += 1
                    elif decision != "TRADE":
                        reasoning = str(ai_result.get("reasoning", "")).strip()
                        reason_key = reasoning.split("|")[0].strip() if reasoning else "decision_skip"
                        reject_reasons[reason_key] += 1
                    else:
                        reject_reasons["filtered_other"] += 1

                logger.info(
                    f"🧮 SCORE-ONLY lọc sâu {layer_name}: loại {rejected_local}/{len(ranked)} "
                    f"candidate sau local analysis"
                )
                if reject_reasons:
                    top_reasons = ", ".join(
                        f"{name}:{count}"
                        for name, count in reject_reasons.most_common(3)
                    )
                    if reject_scores:
                        logger.info(
                            f"🧮 SCORE-ONLY reject stats {layer_name}: "
                            f"avg_score={sum(reject_scores)/len(reject_scores):.2f} "
                            f"| top={top_reasons}"
                        )
                    else:
                        logger.info(
                            f"🧮 SCORE-ONLY reject stats {layer_name}: top={top_reasons}"
                        )
                _record_skip("score_only_local_reject")

            if prefetch_budget_blocked:
                logger.warning(
                    f"⏱️ Score-only prefetch dừng sớm ở {layer_name} do scan budget thấp"
                )

            queued, cluster_filtered = _apply_cluster_cap(tradable_ranked, max_ai)
            deferred_items = list(cluster_filtered)
            selected_symbols = {coin for coin, _, _ in queued}
            deferred_items.extend(
                (coin, signal, ai_result, "queue-cap")
                for coin, signal, ai_result in tradable_ranked
                if coin not in selected_symbols
                and all(coin != filtered_coin for filtered_coin, _, _, _ in cluster_filtered)
            )
            deferred_by_cap = max(0, len(tradable_ranked) - len(queued))
            if deferred_by_cap > 0:
                ai_deferred += deferred_by_cap
                logger.info(
                    f"⚡ Ưu tiên SCORE-ONLY {layer_name}: xử lý {len(queued)}/{len(tradable_ranked)} kèo đạt chuẩn, "
                    f"defer {deferred_by_cap}"
                )
                _record_skip("ai_deferred_capacity")
                await _send_log_safe(
                    context,
                    f"SKIP DEFER ({layer_name})\n"
                    + "\n".join(
                        f"- {_signal_tag(coin, sig)} | reason: {_defer_reason_text(reason, max_ai, len(tradable_ranked))}"
                        for coin, sig, _, reason in deferred_items
                    ),
                )
        else:
            rough_queued = ranked[:max_ai]
            deferred_items = ranked[max_ai:]
            deferred_by_cap = max(0, len(ranked) - len(rough_queued))
            if deferred_by_cap > 0:
                ai_deferred += deferred_by_cap
                logger.info(
                    f"⚡ Ưu tiên AI {layer_name}: xử lý {len(rough_queued)}/{len(ranked)} signal, "
                    f"defer {deferred_by_cap}"
                )
                _record_skip("ai_deferred_capacity")
                await _send_log_safe(
                    context,
                    f"SKIP DEFER ({layer_name})\n"
                    + "\n".join(
                        f"- {_signal_tag(coin, sig)} | reason: queue cap {max_ai}/{len(ranked)}"
                        for coin, sig in deferred_items
                    ),
                )
            queued = [(coin, signal, None) for coin, signal in rough_queued]

        processed_ai = 0
        for idx, (coin, signal, prefetched_ai_result) in enumerate(queued, start=1):
            if scan_deadline is not None and time.monotonic() >= scan_deadline:
                budget_hit = True
                left_in_queue = len(queued) - processed_ai
                ai_deferred += left_in_queue
                left_items = queued[processed_ai:]
                logger.warning(
                    f"⏱️ Hết scan budget trước AI queue {layer_name} "
                    f"- defer {left_in_queue} signal"
                )
                _record_skip("scan_budget_ai_queue")
                if left_items:
                    await _send_log_safe(
                        context,
                        f"SKIP DEFER ({layer_name})\n"
                        + "\n".join(
                            f"- {_signal_tag(c, s)} | reason: scan budget timeout"
                            for c, s, _ in left_items
                        ),
                    )
                break

            if _trade_slots_available() <= 0:
                remain_queue = len(queued) - processed_ai
                ai_deferred += remain_queue
                logger.info(
                    f"🛑 Hết slot lệnh ({len(active_trades)}/{_max_active_trades()}) "
                    f"- dừng AI queue {layer_name}, defer {remain_queue} signal"
                )
                break

            logger.info(
                f"🧭 {'SCORE-ONLY' if score_only_mode else 'AI'} ưu tiên {layer_name} {idx}/{len(queued)}: {coin} "
                f"(str={signal.get('strength', 0)})"
            )
            await _process_signal(
                context,
                coin,
                signal,
                scan_deadline=scan_deadline,
                precomputed_ai_result=prefetched_ai_result,
            )
            processed_ai += 1

    logger.info(
        f"📝 Kết quả {layer_name}: {scanned}/{len(coins)} | "
        f"Signal: {signal_found} | Skip: {no_signal} | Error: {errors} "
        f"| BudgetSkip: {budget_skipped} | AIDefer: {ai_deferred}"
    )
    return signal_found, no_signal, errors, scanned, budget_skipped, budget_hit, ai_deferred


async def scan_market_once(context: ContextTypes.DEFAULT_TYPE):
    """Scan thị trường theo layer: L1 trước, L2 sau."""
    global _last_news_block_notice_ts, _daily_stop_halt_triggered

    # Dừng scan khi đã đầy slot vị thế mở
    if len(active_trades) >= _max_active_trades():
        symbols = ", ".join(active_trades.keys())
        logger.info(
            f"⏸️ Đang giữ lệnh ({symbols}) "
            f"[{len(active_trades)}/{_max_active_trades()}] - chờ đóng bớt rồi scan tiếp"
        )
        return

    daily_hit, stop_threshold, pnl_today = _daily_stop_triggered()
    if daily_hit:
        logger.warning(
            f"🛑 Daily stop-loss active: pnl={pnl_today:+.4f} <= {stop_threshold:+.4f} USDT"
        )
        _record_skip("daily_stop_loss")
        if (
            bool(getattr(config, "DAILY_STOP_LOSS_HALT_APP", False))
            and not _daily_stop_halt_triggered
            and len(active_trades) == 0
        ):
            _daily_stop_halt_triggered = True
            await _send_log_safe(
                context,
                "DAILY STOP-LOSS HIT\n"
                f"PnL today: {pnl_today:+.4f} USDT <= {stop_threshold:+.4f}\n"
                "Bot will stop by config.",
            )
            try:
                await context.application.stop()
            except Exception as e:
                logger.error(f"Stop application after daily stop-loss failed: {e}")
        return

    news_block = _active_news_blackout()
    if news_block is not None:
        _record_skip("news_blackout")
        now_ts = time.time()
        if now_ts - _last_news_block_notice_ts >= 60.0:
            _last_news_block_notice_ts = now_ts
            event_time = news_block["time_utc"].strftime("%Y-%m-%d %H:%M UTC")
            logger.warning(
                f"📰 News blackout active: {news_block['label']} @ {event_time} "
                f"(T{news_block['minutes_to_event']:+.1f}m)"
            )
            await _send_log_safe(
                context,
                "SKIP SCAN - NEWS BLACKOUT\n"
                f"Event: {news_block['label']}\n"
                f"Time: {event_time}\n"
                f"Now delta: {news_block['minutes_to_event']:+.1f}m",
            )
        return

    # Do not queue more scans while one is running
    if scan_lock.locked():
        logger.info("⏳ Scan đang chạy - bỏ qua lần gọi mới để tránh delay")
        _record_skip("scan_overlap")
        return

    scan_started = time.monotonic()
    scan_budget_sec = _calc_scan_budget_sec()
    scan_deadline = scan_started + scan_budget_sec
    await scan_lock.acquire()
    try:
        logger.info("=" * 50)
        logger.info(
            f"🔍 Bắt đầu scan thị trường theo layer... "
            f"(budget={scan_budget_sec:.1f}s, interval={config.SCAN_INTERVAL}s)"
        )

        # Fetch BTC trend + BTC M1 pulse (for SIMPLE_SCALP_MODE)
        global btc_trend, btc_m1_pulse
        if config.BTC_TREND_FILTER or _simple_scalp_needs_btc_context():
            try:
                btc_1m = okx.get_candles("BTC-USDT-SWAP", bar="1m", limit=120)
                btc_5m = okx.get_candles("BTC-USDT-SWAP", bar="5m", limit=50)
                btc_15m = okx.get_candles("BTC-USDT-SWAP", bar="15m", limit=50)
                btc_trend = analyze_btc_trend(btc_5m, btc_15m)
                btc_m1_pulse = _build_btc_m1_pulse(btc_1m, btc_5m)
                logger.info(
                    f"📊 BTC Trend: {btc_trend['trend']} ({btc_trend['strength']}) "
                    f"— {btc_trend['detail']}"
                )
                logger.info(f"⚡ BTC M1 Pulse: {btc_m1_pulse.get('detail', 'n/a')}")
            except Exception as e:
                logger.warning(f"⚠️ Lỗi fetch BTC context: {e} — cho phép cả 2 hướng")
                btc_trend = {"trend": "neutral", "strength": "neutral",
                             "allow_long": True, "allow_short": True,
                             "detail": f"Lỗi fetch: {e}"}
                btc_m1_pulse = {
                    "trend": "neutral",
                    "m1_bull_streak": 0,
                    "m1_bear_streak": 0,
                    "m1_price_vs_ema21": "unknown",
                    "m1_breakout_up": False,
                    "m1_breakout_down": False,
                    "detail": f"Lỗi fetch: {e}",
                }
        else:
            btc_trend = {"trend": "neutral", "strength": "neutral",
                         "allow_long": True, "allow_short": True, "detail": "disabled"}
            btc_m1_pulse = {
                "trend": "neutral",
                "m1_bull_streak": 0,
                "m1_bear_streak": 0,
                "m1_price_vs_ema21": "unknown",
                "m1_breakout_up": False,
                "m1_breakout_down": False,
                "detail": "disabled",
            }

        signal_found = 0
        no_signal = 0
        errors = 0
        scanned_total = 0
        budget_skipped_total = 0
        budget_hit_any = False
        ai_deferred_total = 0

        layers = _get_scan_layers()
        total_coins = sum(len(coins) for _, coins in layers)

        if not layers:
            logger.warning("Không có coin nào để scan")
            return

        for layer_name, layer_coins in layers:
            if _trade_slots_available() <= 0:
                logger.info(
                    f"🛑 Đã đầy slot lệnh ({len(active_trades)}/{_max_active_trades()}) "
                    "— dừng các layer còn lại"
                )
                break

            s, n, e, scanned, budget_skipped, budget_hit, ai_deferred = await _scan_layer_once(
                context,
                layer_name,
                layer_coins,
                scan_deadline=scan_deadline,
            )
            signal_found += s
            no_signal += n
            errors += e
            scanned_total += scanned
            budget_skipped_total += budget_skipped
            budget_hit_any = budget_hit_any or budget_hit
            ai_deferred_total += ai_deferred

            if budget_hit:
                _record_skip("scan_budget")
                break

        duration_sec = time.monotonic() - scan_started
        delay_sec = max(0.0, duration_sec - config.SCAN_INTERVAL)
        _record_scan_event(
            duration_sec=duration_sec,
            delay_sec=delay_sec,
            budget_hit=budget_hit_any,
            scanned=scanned_total,
            total=total_coins,
        )
        logger.info(
            f"🔍 Scan xong: {scanned_total}/{total_coins} coins | "
            f"Signal: {signal_found} | Skip: {no_signal} | Error: {errors} | "
            f"BudgetSkip: {budget_skipped_total} | AIDefer: {ai_deferred_total} | "
            f"Duration: {duration_sec:.2f}s"
        )
        if delay_sec > 0:
            logger.warning(
                f"⏱️ Scan duration vượt interval {delay_sec:.2f}s "
                f"({duration_sec:.2f}s > {config.SCAN_INTERVAL}s)"
            )
        logger.info("=" * 50)
    finally:
        scan_lock.release()

def _analyze_coin(coin: str) -> dict | None:
    """Phân tích 1 coin (chạy trong thread)."""
    global prev_oi

    try:
        simple_mode = bool(getattr(config, "SIMPLE_SCALP_MODE", False))

        # 1. Lấy dữ liệu thị trường
        if simple_mode:
            # Fast path: simple scalp chỉ cần price + candles M1/M5
            data = okx.get_simple_scalp_data(coin)
        else:
            data = okx.get_market_data(coin)

        if float(data.get("price", 0) or 0) == 0:
            logger.warning(f"{coin}: Không lấy được giá")
            return None

        # 2. Tính indicators
        if simple_mode:
            candles_1m = data.get("candles_1m", [])
            candles_5m = data.get("candles_5m", [])
            candles_15m = data.get("candles_15m", [])
            micro_data = analyze_micro_setup(candles_1m, candles_5m)
            if candles_5m and candles_15m:
                candle_data = analyze_candles(candles_5m, candles_15m)
            else:
                candle_data = {
                    "tf_5m": {"trend": "neutral", "ema_bias": "neutral"},
                    "tf_15m": {"trend": "neutral", "ema_bias": "neutral"},
                    "overall": "mixed",
                    "aligned": False,
                }
            sr_15m = analyze_sr_levels(
                candles_15m,
                lookback=int(getattr(config, "SIMPLE_SCALP_M15_SR_LOOKBACK", 24)),
                near_threshold_pct=float(getattr(config, "SIMPLE_SCALP_M15_NEAR_SR_PCT", 0.35)),
                break_threshold_pct=float(getattr(config, "SIMPLE_SCALP_M15_BREAK_PCT", 0.08)),
            )
            candle_data["sr_15m"] = sr_15m
            cvd_data = {"bias": "neutral", "cvd": 0.0, "buy_vol": 0.0, "sell_vol": 0.0}
            ob_data = {"imbalance": 0.0, "bias": "neutral"}
            funding_data = {"funding_pct": 0.0, "signal": "neutral"}
            oi_data = {"oi_change_pct": 0.0, "signal": "neutral"}
            signal = _simple_scalp_signal(candle_data, micro_data)
            signal.setdefault("tp_pct", float(config.get_tp(coin)))
            signal.setdefault("sl_pct", float(config.get_sl(coin)))
        else:
            cvd_data = calc_cvd(data["trades"])
            ob_data = calc_orderbook_imbalance(data["bids"], data["asks"])
            funding_data = analyze_funding_rate(data["funding_rate"])
            candle_data = analyze_candles(data["candles_5m"], data["candles_15m"])
            micro_data = analyze_micro_setup(data.get("candles_1m", []), data["candles_5m"])

            # OI change
            current_oi = data["open_interest"]
            p_oi = prev_oi.get(coin, current_oi)
            oi_data = calc_oi_change(current_oi, p_oi)
            prev_oi[coin] = current_oi

            rule_context = dict(candle_data)
            rule_context.update({
                "m1_price_vs_ema21": micro_data.get("m1_price_vs_ema21", "unknown"),
            })
            signal = generate_signal(cvd_data, oi_data, ob_data, funding_data, rule_context)

        candles_15m_extended = data.get("candles_15m", []) if isinstance(data, dict) else []
        min_15m_bars = max(
            50,
            int(getattr(config, "ATR_BRAKE_PERIOD", 14))
            + int(getattr(config, "ATR_BRAKE_LOOKBACK_BARS", 96))
            + 5,
        )
        if bool(getattr(config, "HARD_GATE_EMA200_ENABLED", False)):
            min_15m_bars = max(min_15m_bars, 220)
        if len(candles_15m_extended) < min_15m_bars:
            candles_15m_extended = okx.get_candles(
                coin,
                bar="15m",
                limit=min(300, min_15m_bars),
            )

        candles_1h_extended: list = data.get("candles_1h", []) if isinstance(data, dict) else []
        if (
            bool(getattr(config, "HARD_GATE_EMA200_ENABLED", False))
            and len(candles_1h_extended) < 200
        ):
            candles_1h_extended = okx.get_candles(coin, bar="1H", limit=220)

        atr_stats = _atr_percent_stats_from_candles(
            candles_15m_extended,
            int(getattr(config, "ATR_BRAKE_PERIOD", 14)),
            int(getattr(config, "ATR_BRAKE_LOOKBACK_BARS", 96)),
        )
        ema200_m15_ctx = _ema200_context_from_candles(candles_15m_extended, "m15")
        ema200_h1_ctx = _ema200_context_from_candles(candles_1h_extended, "h1")

        # Thêm indicator data vào signal
        ticker_data = data.get("ticker", {}) if isinstance(data, dict) else {}
        bid_px = float(ticker_data.get("bidPx", 0.0) or 0.0)
        ask_px = float(ticker_data.get("askPx", 0.0) or 0.0)
        spread_pct = 0.0
        if bid_px > 0 and ask_px > 0 and float(data["price"]) > 0:
            spread_pct = max(0.0, (ask_px - bid_px) / float(data["price"]) * 100.0)

        signal["indicators"] = {
            "cvd_bias": cvd_data["bias"],
            "cvd_value": cvd_data["cvd"],
            "buy_vol": cvd_data["buy_vol"],
            "sell_vol": cvd_data["sell_vol"],
            "imbalance": ob_data["imbalance"],
            "ob_bias": ob_data["bias"],
            "funding_rate": float(data.get("funding_rate", 0.0) or 0.0),
            "funding_pct": funding_data["funding_pct"],
            "funding_signal": funding_data["signal"],
            "oi_change_pct": oi_data["oi_change_pct"],
            "oi_signal": oi_data["signal"],
            "price": data["price"],
            "bid_px": bid_px,
            "ask_px": ask_px,
            "spread_pct": spread_pct,
            # Candle trend data
            "trend_5m": candle_data.get("tf_5m", {}).get("trend", "neutral"),
            "trend_15m": candle_data.get("tf_15m", {}).get("trend", "neutral"),
            "ema_bias_5m": candle_data.get("tf_5m", {}).get("ema_bias", "neutral"),
            "ema_bias_15m": candle_data.get("tf_15m", {}).get("ema_bias", "neutral"),
            "candle_overall": candle_data.get("overall", "mixed"),
            "candle_aligned": candle_data.get("aligned", False),
            # Micro setup (M1/M5) for pair-specific playbook scoring
            "m1_ema21": micro_data.get("m1_ema21", 0.0),
            "m1_price_vs_ema21": micro_data.get("m1_price_vs_ema21", "unknown"),
            "m1_breakout_up": micro_data.get("m1_breakout_up", False),
            "m1_breakout_down": micro_data.get("m1_breakout_down", False),
            "m1_max_wick_pct_10": micro_data.get("m1_max_wick_pct_10", 0.0),
            "m1_last_upper_wick_pct": micro_data.get("m1_last_upper_wick_pct", 0.0),
            "m1_last_lower_wick_pct": micro_data.get("m1_last_lower_wick_pct", 0.0),
            "m1_bull_streak": micro_data.get("m1_bull_streak", 0),
            "m1_bear_streak": micro_data.get("m1_bear_streak", 0),
            "m1_long_wick_bullish": micro_data.get("m1_long_wick_bullish", False),
            "m1_long_wick_bearish": micro_data.get("m1_long_wick_bearish", False),
            "m1_bull_engulfing": micro_data.get("m1_bull_engulfing", False),
            "m1_bear_engulfing": micro_data.get("m1_bear_engulfing", False),
            "m1_rejection_signal": micro_data.get("m1_rejection_signal", "none"),
            "m1_volume_surge_pct": micro_data.get("m1_volume_surge_pct", 0.0),
            "m1_bb_position": micro_data.get("m1_bb_position", "mid"),
            "m1_bb_touch_upper": micro_data.get("m1_bb_touch_upper", False),
            "m1_bb_touch_lower": micro_data.get("m1_bb_touch_lower", False),
            "m1_bb_width_pct": micro_data.get("m1_bb_width_pct", 0.0),
            "m1_bb_width_ratio": micro_data.get("m1_bb_width_ratio", 1.0),
            "m1_bb_squeeze": micro_data.get("m1_bb_squeeze", False),
            "m1_bb_expansion": micro_data.get("m1_bb_expansion", False),
            "m1_market_regime_hint": micro_data.get("m1_market_regime_hint", "unknown"),
            "m5_touch_resistance": micro_data.get("m5_touch_resistance", False),
            "m5_touch_support": micro_data.get("m5_touch_support", False),
            "m5_last_upper_wick_pct": micro_data.get("m5_last_upper_wick_pct", 0.0),
            "m5_last_lower_wick_pct": micro_data.get("m5_last_lower_wick_pct", 0.0),
            "m5_last_body_pct": micro_data.get("m5_last_body_pct", 0.0),
            "m5_last_range_pct": micro_data.get("m5_last_range_pct", 0.0),
            "m5_pinbar_bullish": micro_data.get("m5_pinbar_bullish", False),
            "m5_pinbar_bearish": micro_data.get("m5_pinbar_bearish", False),
            "m5_wave_ready": micro_data.get("m5_wave_ready", False),
            "m5_order_block_bias": micro_data.get("m5_order_block_bias", "unknown"),
            "m5_order_block_near": micro_data.get("m5_order_block_near", False),
            # M15 support/resistance context for reversal scalp
            "m15_support": candle_data.get("sr_15m", {}).get("m15_support", 0.0),
            "m15_resistance": candle_data.get("sr_15m", {}).get("m15_resistance", 0.0),
            "m15_dist_to_support_pct": candle_data.get("sr_15m", {}).get("m15_dist_to_support_pct", 999.0),
            "m15_dist_to_resistance_pct": candle_data.get("sr_15m", {}).get("m15_dist_to_resistance_pct", 999.0),
            "m15_near_support": candle_data.get("sr_15m", {}).get("m15_near_support", False),
            "m15_near_resistance": candle_data.get("sr_15m", {}).get("m15_near_resistance", False),
            "m15_breakdown_support": candle_data.get("sr_15m", {}).get("m15_breakdown_support", False),
            "m15_breakout_resistance": candle_data.get("sr_15m", {}).get("m15_breakout_resistance", False),
            "m15_sr_bias": candle_data.get("sr_15m", {}).get("m15_sr_bias", "unknown"),
            # Volatility + hard gate context
            "atr_m15_current_pct": atr_stats.get("atr_current_pct", 0.0),
            "atr_m15_avg_pct": atr_stats.get("atr_avg_pct", 0.0),
            "atr_m15_ratio": atr_stats.get("atr_ratio", 1.0),
            "atr_m15_ok": bool(atr_stats.get("ok", False)),
            "ema200_m15": ema200_m15_ctx.get("ema200", 0.0),
            "close_m15": ema200_m15_ctx.get("close", 0.0),
            "price_vs_ema200_m15": ema200_m15_ctx.get("position", "unknown"),
            "ema200_m15_ok": bool(ema200_m15_ctx.get("ok", False)),
            "ema200_h1": ema200_h1_ctx.get("ema200", 0.0),
            "close_h1": ema200_h1_ctx.get("close", 0.0),
            "price_vs_ema200_h1": ema200_h1_ctx.get("position", "unknown"),
            "ema200_h1_ok": bool(ema200_h1_ctx.get("ok", False)),
            # Resolved TP/SL percent for this signal (can be dynamic)
            "tp_pct": signal.get("tp_pct", config.get_tp(coin)),
            "sl_pct": signal.get("sl_pct", config.get_sl(coin)),
        }

        return signal

    except Exception as e:
        logger.error(f"Lỗi analyze {coin}: {e}")
        return None


async def _process_signal(context: ContextTypes.DEFAULT_TYPE,
                          coin: str, signal: dict,
                          scan_deadline: float | None = None,
                          precomputed_ai_result: dict | None = None):
    """Xử lý signal: chạy analysis → auto trade nếu score đạt ngưỡng."""

    # Double-check: tránh trùng symbol / vượt slot
    if coin in active_trades:
        logger.info(f"⏸️ Bỏ qua signal {coin} — vị thế đang mở")
        return
    if _trade_slots_available() <= 0:
        logger.info(
            f"⏸️ Bỏ qua signal {coin} — đã đầy slot "
            f"({len(active_trades)}/{_max_active_trades()})"
        )
        return
    blocked_reason = _coin_session_block_reason(coin)
    if blocked_reason:
        logger.info(f"⏸️ Bỏ qua signal {coin} — {blocked_reason}")
        _record_skip("coin_session_block")
        return

    indicators = signal["indicators"]
    direction = signal["direction"]
    price = indicators["price"]

    spread_ok, spread_reason = _entry_spread_guard(indicators)
    if not spread_ok:
        logger.info(f"⏭️ Bỏ qua signal {coin} — entry spread guard: {spread_reason}")
        _record_skip("entry_spread_guard")
        await _send_log_safe(
            context,
            f"SKIP {coin} {direction}\n"
            f"reason: spread guard -> {spread_reason}",
        )
        return

    news_block = _active_news_blackout()
    if news_block is not None:
        event_time = news_block["time_utc"].strftime("%Y-%m-%d %H:%M UTC")
        logger.info(
            f"⏭️ Bỏ qua signal {coin} — news blackout {news_block['label']} "
            f"(T{news_block['minutes_to_event']:+.1f}m)"
        )
        _record_skip("news_blackout_signal")
        await _send_log_safe(
            context,
            f"SKIP {coin} {direction}\n"
            f"reason: news blackout ({news_block['label']} @ {event_time}, "
            f"T{news_block['minutes_to_event']:+.1f}m)",
        )
        return

    hard_gate_ok, hard_gate_reason = _hard_gate_ema200_check(direction, indicators)
    if not hard_gate_ok:
        logger.info(f"⏭️ Bỏ qua signal {coin} — hard gate EMA200: {hard_gate_reason}")
        _record_skip("hard_gate_ema200")
        await _send_log_safe(
            context,
            f"SKIP {coin} {direction}\n"
            f"reason: hard gate EMA200 -> {hard_gate_reason}",
        )
        return

    # Kiểm tra được phép trade không (3 thua liên tiếp = dừng)
    can, reason = capital.can_trade()
    if not can:
        logger.info(f"🛑 Không được trade: {reason}")
        _record_skip("capital_block")
        await _send_log_safe(
            context,
            f"SKIP {coin} {direction}\n"
            f"reason: capital/risk gate -> {reason}"
        )
        return

    max_trades_30m = max(0, int(getattr(config, "MAX_TRADES_PER_30M", 0)))
    if max_trades_30m > 0:
        recent_30m = _recent_trade_count_30m()
        if recent_30m >= max_trades_30m:
            logger.info(
                f"⏱️ Trade-rate cap 30m: {recent_30m}/{max_trades_30m} "
                f"- skip {coin} {direction}"
            )
            _record_skip("trade_rate_cap_30m")
            await _send_log_safe(
                context,
                f"SKIP {coin} {direction}\n"
                f"reason: trade-rate cap 30m {recent_30m}/{max_trades_30m}"
            )
            return

    if config.LONG_ONLY and direction != "LONG":
        logger.info(f"⚖️ LONG_ONLY: bỏ signal {coin} {direction}")
        _record_skip("short_blocked")
        return

    simple_mode = bool(getattr(config, "SIMPLE_SCALP_MODE", False))
    simple_use_ai = bool(getattr(config, "SIMPLE_SCALP_USE_AI", True))
    simple_local_only = simple_mode and not simple_use_ai
    final_score_gate = 0.0 if simple_local_only else _effective_score_gate()
    analysis_score_gate = (
        0.0 if simple_local_only else _analysis_score_gate(final_score_gate)
    )
    if simple_local_only and precomputed_ai_result is None:
        precomputed_ai_result = {
            "decision": "TRADE",
            "confidence": int(max(55, min(95, (signal.get("strength", 4) or 4) * 15))),
            "direction": direction,
            "win_probability": int(
                max(40, min(100, int(getattr(config, "SIMPLE_SCALP_WIN_PROB", 100))))
            ),
            "playbook_score": float(getattr(config, "SIMPLE_SCALP_SCORE", 5.5)),
            "risk_level": "MEDIUM",
            "risk_score": 35,
            "entry_quality": "B",
            "reasoning": f"SIMPLE_SCALP_MODE: {_simple_scalp_trigger_text()}",
            "total_ai_time": 0.0,
            "ai_budget_sec": 0.0,
        }

    # 1. Chạy analysis
    score_only_mode = bool(config.AI_SCORE_ONLY_MODE) or simple_local_only
    ai_profile_label = "FAST3L" if config.AI_FAST_3L_MODE else "6-Layer"
    if simple_mode:
        analysis_label = "SIMPLE-LOCAL" if score_only_mode else f"SIMPLE+AI {ai_profile_label}"
    else:
        analysis_label = "SCORE-ONLY" if score_only_mode else f"AI {ai_profile_label}"
    logger.info(
        f"{'🧮' if score_only_mode else '🧠'} {analysis_label} phân tích {coin} {direction}..."
    )
    try:
        ai_budget = float(config.AI_TIME_BUDGET_SEC)
        if precomputed_ai_result is None:
            if scan_deadline is not None:
                remaining = scan_deadline - time.monotonic()
                if remaining < config.AI_MIN_BUDGET_TO_START_SEC:
                    logger.info(
                        f"⏱️ Bỏ qua phân tích {coin}: budget còn {remaining:.1f}s "
                        f"< {config.AI_MIN_BUDGET_TO_START_SEC}s"
                    )
                    _record_skip("scan_budget_low")
                    return
                ai_budget = min(
                    ai_budget,
                    max(
                        float(config.AI_MIN_BUDGET_TO_START_SEC),
                        remaining - float(config.AI_TIMEOUT_SAFETY_SEC),
                    ),
                )
            if simple_mode and simple_use_ai:
                simple_ai_budget = max(
                    2.0, float(getattr(config, "SIMPLE_SCALP_AI_BUDGET_SEC", ai_budget))
                )
                ai_budget = min(ai_budget, simple_ai_budget)

            ai_result = await asyncio.to_thread(
                analyze_trade,
                coin,
                direction,
                indicators,
                ai_budget,
                analysis_score_gate,
            )
            ai_time = float(ai_result.get("total_ai_time", 0) or 0)
            _record_ai_latency(ai_time)
        else:
            ai_result = dict(precomputed_ai_result)
            ai_time = float(ai_result.get("total_ai_time", 0) or 0)
            ai_budget = float(ai_result.get("ai_budget_sec", ai_budget) or ai_budget)
        playbook_score = float(ai_result.get("playbook_score", 0) or 0)
        if score_only_mode:
            logger.info(
                f"{analysis_label}: {ai_result.get('decision')} "
                f"(score={playbook_score:.1f}/10, gate={analysis_score_gate:.1f}->{final_score_gate:.1f}/10, "
                f"win_local={ai_result.get('win_probability')}%, "
                f"risk={ai_result.get('risk_score', '?')}, "
                f"time={ai_result.get('total_ai_time')}s, "
                f"budget={ai_result.get('ai_budget_sec', ai_budget)}s)"
            )
        else:
            logger.info(f"AI {ai_profile_label}: {ai_result.get('decision')} "
                         f"(score={playbook_score:.1f}/10, gate={analysis_score_gate:.1f}->{final_score_gate:.1f}/10, "
                         f"win_L3={ai_result.get('win_probability_l3', '?')}%, "
                         f"win_final={ai_result.get('win_probability')}%, "
                         f"L4={ai_result.get('l4_verdict', '?')}, "
                         f"L5_exec={ai_result.get('l5_execute', '?')}, "
                         f"L6={ai_result.get('l6_verdict', '?')}, "
                         f"trap={ai_result.get('l4_trap_detected', False)}, "
                         f"timing={ai_result.get('l5_timing', '?')}, "
                         f"regime={ai_result.get('l6_regime', '?')}, "
                         f"time={ai_result.get('total_ai_time')}s, "
                         f"budget={ai_result.get('ai_budget_sec', ai_budget)}s)")
        if ai_time > (ai_budget + 0.2):
            logger.warning(
                f"⏱️ Analysis overrun {coin}: {ai_time:.2f}s > budget {ai_budget:.2f}s"
            )
            _record_skip("ai_budget_overrun")
    except Exception as e:
        logger.error(f"Lỗi phân tích: {e}")
        _record_skip("ai_exception")
        ai_result = {
            "decision": "SKIP",
            "confidence": 0,
            "direction": direction,
            "win_probability": 0,
            "playbook_score": 0,
            "risk_level": "HIGH",
            "reasoning": f"Phân tích không hoạt động: {e}",
            "error": True,
        }

    # 2. Kiểm tra score gate (cơ chế chính)
    playbook_score = float(ai_result.get("playbook_score", 0) or 0)
    ai_decision = str(ai_result.get("decision", "SKIP")).upper()
    if ai_result.get("error"):
        logger.info(f"Phân tích bị lỗi — SKIP {coin}")
        _record_skip("ai_error")
        await telegram_handler.send_message(
            context.bot,
            f"❌ Phân tích lỗi — SKIP {coin} {direction}\n"
            f"Lý do: {ai_result.get('reasoning', 'N/A')}"
        )
        return

    if playbook_score < final_score_gate:
        logger.info(
            f"Score {playbook_score:.1f}/10 < entry gate {final_score_gate:.1f}/10 "
            f"(ai pre-gate {analysis_score_gate:.1f}) — SKIP {coin}"
        )
        _record_skip("score_below_threshold")
        await telegram_handler.send_message(
            context.bot,
            f"❌ SKIP {coin} {direction}\n"
            f"Score: {playbook_score:.1f}/10 < {final_score_gate:.1f}/10 "
            f"(AI pre-gate {analysis_score_gate:.1f})\n"
            f"Lý do: {ai_result.get('reasoning', 'N/A')}"
        )
        return

    timeout_override_applied = False
    if (
        simple_mode
        and simple_use_ai
        and bool(getattr(config, "SIMPLE_SCALP_FORCE_TRADE_ON_AI_TIMEOUT", True))
        and ai_decision != "TRADE"
        and playbook_score >= final_score_gate
    ):
        timeout_reason = str(ai_result.get("reasoning", "")).lower()
        risk_score = int(ai_result.get("risk_score", 100) or 100)
        if (
            risk_score <= 30
            and (
                "budget low before l3" in timeout_reason
                or "timeout" in timeout_reason
                or "timed out" in timeout_reason
                or "l1 bias=neutral" in timeout_reason
            )
        ):
            logger.warning(
                f"⚙️ SIMPLE timeout override {coin}: score={playbook_score:.1f} "
                f">= gate {final_score_gate:.1f}, risk={risk_score} -> FORCE TRADE"
            )
            ai_decision = "TRADE"
            timeout_override_applied = True
            ai_result["decision"] = "TRADE"
            ai_result["reasoning"] = (
                f"{ai_result.get('reasoning', '')} | simple_timeout_override=on"
            ).strip()

    if ai_decision != "TRADE":
        if score_only_mode:
            logger.info(
                f"SCORE-ONLY decision={ai_decision} dù score {playbook_score:.1f}/10 đạt ngưỡng "
                f"— SKIP {coin} (risk={ai_result.get('risk_score', 'N/A')})"
            )
        else:
            l4_info = f"L4={ai_result.get('l4_verdict','N/A')}"
            l5_info = f"L5={'GO' if ai_result.get('l5_execute') else 'NO'}"
            l6_info = f"L6={ai_result.get('l6_verdict','N/A')}"
            logger.info(
                f"AI decision={ai_decision} dù score {playbook_score:.1f}/10 đạt ngưỡng — SKIP {coin} "
                f"({l4_info} {l5_info} {l6_info})"
            )
        _record_skip("ai_decision_skip")
        if score_only_mode:
            await telegram_handler.send_message(
                context.bot,
                f"❌ SKIP {coin} {direction}\n"
                f"Score: {playbook_score:.1f}/10 (đạt ngưỡng {final_score_gate:.1f})\n"
                f"Mode: SCORE-ONLY | Risk: {ai_result.get('risk_level', 'N/A')}"
                f" ({ai_result.get('risk_score', 'N/A')}/100)\n"
                f"Lý do: {ai_result.get('reasoning', 'N/A')}"
            )
        else:
            l4_info = f"L4={ai_result.get('l4_verdict','N/A')}"
            l5_info = f"L5={'GO' if ai_result.get('l5_execute') else 'NO'}"
            l6_info = f"L6={ai_result.get('l6_verdict','N/A')}"
            await telegram_handler.send_message(
                context.bot,
                f"❌ SKIP {coin} {direction}\n"
                f"Score: {playbook_score:.1f}/10 (đạt ngưỡng {final_score_gate:.1f})\n"
                f"AI: {ai_decision} | {l4_info} | {l5_info} | {l6_info}\n"
                f"Trap: {ai_result.get('l4_trap_detected', False)} | "
                f"Regime: {ai_result.get('l6_regime', 'N/A')}\n"
                f"Lý do: {ai_result.get('reasoning', 'N/A')}"
            )
        return

    win_probability = int(ai_result.get("win_probability", 0) or 0)
    required_win = _effective_auto_trade_min_win()
    if timeout_override_applied and win_probability < required_win:
        logger.warning(
            f"⚙️ SIMPLE timeout override bypass win-gate {coin}: "
            f"win={win_probability}% -> {required_win}%"
        )
        win_probability = required_win
        ai_result["win_probability"] = required_win
    if win_probability < required_win:
        logger.info(
            f"Win probability {win_probability}% < auto gate {required_win}% — SKIP {coin}"
        )
        _record_skip("win_probability_below_threshold")
        await telegram_handler.send_message(
            context.bot,
            f"❌ SKIP {coin} {direction}\n"
            f"Score: {playbook_score:.1f}/10 | Win: {win_probability}% < {required_win}%\n"
            f"Lý do: entry quality chưa đủ chắc cho auto trade"
        )
        return

    # 3. Resolve TP/SL % ban đầu từ signal/config
    tp_pct = max(
        0.01,
        float(signal.get("tp_pct", indicators.get("tp_pct", config.get_tp(coin)))),
    )
    sl_pct = max(
        0.01,
        float(signal.get("sl_pct", indicators.get("sl_pct", config.get_sl(coin)))),
    )

    # 4. Tính position size
    size_str, pos_info = okx.calc_position_size(
        coin, config.TRADE_AMOUNT_USDT, config.get_leverage(coin)
    )
    logger.info(
        f"📐 Size {coin}: target={config.TRADE_AMOUNT_USDT:.2f} USDT | "
        f"actual={pos_info.get('actual_margin_usdt', 'N/A')} USDT | "
        f"mode={pos_info.get('sizing_mode', 'legacy')}"
    )
    pos_info["bonus_slot_eligible"] = _is_bonus_slot_eligible(pos_info)

    # 4b. Kiểm tra có đủ margin không
    if size_str == "0" or pos_info.get("error"):
        error_msg = pos_info.get("error", "Không tính được position size")
        logger.warning(f"⚠️ {coin}: {error_msg}")
        _record_skip("position_size_error")
        await telegram_handler.send_message(
            context.bot,
            f"⚠️ Bỏ qua {coin} {direction}\n"
            f"Lý do: {error_msg}"
        )
        return

    # 5. SIMPLE mode: map TP/SL theo mục tiêu USD ròng (sau phí)
    if simple_mode and bool(getattr(config, "SIMPLE_SCALP_USE_NET_USDT_TP_SL", False)):
        margin_usdt = float(pos_info.get("actual_margin_usdt", 0.0) or 0.0)
        if margin_usdt <= 0:
            margin_usdt = float(config.TRADE_AMOUNT_USDT)
        net_tp_usdt = max(0.01, float(getattr(config, "SIMPLE_SCALP_NET_TP_USDT", 2.0)))
        net_sl_usdt = max(0.01, float(getattr(config, "SIMPLE_SCALP_NET_SL_USDT", 1.2)))
        tp_pct, sl_pct, usdt_note = _simple_scalp_net_usdt_tp_sl_pct(
            coin,
            margin_usdt,
            net_tp_usdt,
            net_sl_usdt,
        )
        logger.info(
            f"🎯 SIMPLE USD TP/SL {coin}: {usdt_note} -> TP/SL={tp_pct:.3f}%/{sl_pct:.3f}%"
        )

    pos_info["atr_ratio"] = float(indicators.get("atr_m15_ratio", 1.0) or 1.0)
    pos_info["atr_current_pct"] = float(indicators.get("atr_m15_current_pct", 0.0) or 0.0)
    pos_info["atr_avg_pct"] = float(indicators.get("atr_m15_avg_pct", 0.0) or 0.0)
    (
        atr_tp_pct,
        atr_sl_pct,
        atr_size_str,
        atr_pos_info,
        atr_applied,
    ) = _apply_atr_brake(coin, tp_pct, sl_pct, size_str, pos_info)
    if atr_applied:
        tp_pct = atr_tp_pct
        sl_pct = atr_sl_pct
        size_str = atr_size_str
        pos_info = atr_pos_info
        logger.info(
            f"🧯 ATR brake {coin}: ratio={pos_info.get('atr_ratio', 1.0):.2f} "
            f"-> SL={sl_pct:.3f}% | target_margin={pos_info.get('target_margin_usdt', 'n/a')} "
            f"| size={size_str}"
        )

    indicators["tp_pct"] = tp_pct
    indicators["sl_pct"] = sl_pct
    tp, sl = okx.calc_tp_sl_prices(price, direction, tp_pct, sl_pct)

    if simple_mode:
        edge_ok, edge_reason = _simple_scalp_edge_check(coin, indicators)
        if not edge_ok:
            logger.info(f"⚠️ SIMPLE edge guard — SKIP {coin}: {edge_reason}")
            _record_skip("simple_scalp_edge_guard")
            await telegram_handler.send_message(
                context.bot,
                f"❌ SKIP {coin} {direction}\n"
                f"Lý do: {edge_reason}"
            )
            return
        logger.info(f"✅ SIMPLE edge guard {coin}: {edge_reason}")

    # 6. Tạo signal object
    signal_id = str(uuid.uuid4())[:8]
    full_signal = {
        "id": signal_id,
        "symbol": coin,
        "direction": direction,
        "price": price,
        "tp": tp,
        "sl": sl,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "size": size_str,
        "indicators": indicators,
        "ai_result": ai_result,
        "position_info": pos_info,
        "timestamp": datetime.now().isoformat(),
    }

    # 7. AUTO TRADE: score đã đạt ngưỡng ở bước 2
    if config.AUTO_TRADE:
        logger.info(
            f"🚀 AUTO TRADE: {coin} {direction} "
            f"(score={playbook_score:.1f}/10 >= {final_score_gate:.1f}/10)"
        )

        # Gửi thông báo đang vào kèo
        await telegram_handler.send_auto_trade_signal(context.bot, full_signal)

        # Thực hiện trade
        result = await execute_trade(full_signal, context)

        if result.get("success"):
            await telegram_handler.send_trade_result(context.bot, full_signal, result)
        else:
            await telegram_handler.send_message(
                context.bot,
                f"❌ Lỗi đặt lệnh {coin}: {result.get('error', 'Unknown')}"
            )
    else:
        # AUTO_TRADE tắt — báo signal nhưng không tự vào
        logger.info(
            f"AUTO_TRADE tắt — chỉ báo signal {coin} {direction} score={playbook_score:.1f}/10"
        )
        await telegram_handler.send_message(
            context.bot,
            f"📊 Signal {coin} {direction}\n"
            f"Score: {playbook_score:.1f}/10 (≥{final_score_gate:.1f} ✅)\n"
            f"Risk: {ai_result.get('risk_level', 'N/A')}\n"
            f"Auto trade TẮT — vào lệnh thủ công nếu muốn"
        )


# ── execute trade ───────────────────────────────────────────
async def execute_trade(signal: dict, context: ContextTypes.DEFAULT_TYPE) -> dict:
    """
    Thực hiện trade khi user chốt kèo.
    Được gọi từ telegram_handler callback.
    """
    global active_trades

    symbol = signal["symbol"]
    direction = signal["direction"]
    size = signal["size"]
    tp = signal["tp"]
    sl = signal["sl"]
    tp_pct = max(0.01, float(signal.get("tp_pct", config.get_tp(symbol))))
    sl_pct = max(0.01, float(signal.get("sl_pct", config.get_sl(symbol))))
    entry_price = signal["price"]
    signal_price = float(entry_price)

    news_block = _active_news_blackout()
    if news_block is not None:
        event_time = news_block["time_utc"].strftime("%Y-%m-%d %H:%M UTC")
        return {
            "success": False,
            "error": (
                f"News blackout active ({news_block['label']} @ {event_time}, "
                f"T{news_block['minutes_to_event']:+.1f}m)"
            ),
        }

    # Guard slot/symbol trước khi gọi API đặt lệnh
    if symbol in active_trades:
        return {"success": False, "error": f"{symbol} đang có vị thế mở"}
    if _trade_slots_available() <= 0:
        return {
            "success": False,
            "error": f"Đã đầy slot lệnh ({len(active_trades)}/{_max_active_trades()})",
        }

    # Guard spread + signal drift trước khi bắn market order.
    try:
        ticker = await asyncio.to_thread(okx.get_ticker, symbol)
        bid_px = float(ticker.get("bidPx", "0") or 0.0)
        ask_px = float(ticker.get("askPx", "0") or 0.0)
        if bid_px > 0 and ask_px > 0 and signal_price > 0:
            mid_px = (bid_px + ask_px) / 2.0
            spread_pct = ((ask_px - bid_px) / max(mid_px, 1e-9)) * 100.0
            max_spread_pct = max(0.0, float(getattr(config, "ENTRY_MAX_SPREAD_PCT", 0.0)))
            if max_spread_pct > 0 and spread_pct > max_spread_pct:
                return {
                    "success": False,
                    "error": (
                        f"Spread guard: {spread_pct:.4f}% > {max_spread_pct:.4f}%"
                    ),
                }

            ref_px = ask_px if direction == "LONG" else bid_px
            drift_pct = abs(ref_px - signal_price) / max(signal_price, 1e-9) * 100.0
            max_drift_pct = max(
                0.0, float(getattr(config, "ENTRY_MAX_SIGNAL_DRIFT_PCT", 0.0))
            )
            if max_drift_pct > 0 and drift_pct > max_drift_pct:
                return {
                    "success": False,
                    "error": (
                        f"Signal drift guard: {drift_pct:.4f}% > {max_drift_pct:.4f}%"
                    ),
                }
            logger.info(
                f"✅ Entry guard {symbol}: spread={spread_pct:.4f}% drift={drift_pct:.4f}%"
            )
    except Exception as e:
        logger.warning(f"⚠️ Không check được spread/drift cho {symbol}: {e}")

    # ── Kiểm tra vị thế thực tế trên OKX trước khi mở lệnh ──
    try:
        existing_positions = await asyncio.to_thread(okx.get_positions, symbol)
        for p in existing_positions:
            pos_size = float(p.get("pos", "0"))
            if pos_size != 0:
                existing_dir = "LONG" if pos_size > 0 else "SHORT"
                logger.warning(
                    f"⚠️ {symbol} đã có vị thế {existing_dir} trên OKX (size={pos_size}), "
                    f"không mở thêm {direction}"
                )
                # Thêm vào tracking nếu chưa có
                if symbol not in active_trades:
                    entry_px = float(p.get("avgPx", "0"))
                    tp_ex, sl_ex = okx.calc_tp_sl_prices(
                        entry_px, existing_dir,
                        config.get_tp(symbol), config.get_sl(symbol)
                    )
                    active_trades[symbol] = {
                        "signal": {"indicators": {}, "ai_result": {}},
                        "order_id": p.get("posId", "detected"),
                        "direction": existing_dir,
                        "entry_price": entry_px,
                        "tp": tp_ex,
                        "sl": sl_ex,
                        "tp_pct": float(config.get_tp(symbol)),
                        "sl_pct": float(config.get_sl(symbol)),
                        "size": str(abs(pos_size)),
                        "be_done": False,
                        "tp1_done": False,
                        "locked_sl_price": None,
                        "max_pnl_margin": 0.0,
                        "min_pnl_margin": 0.0,
                        "actual_margin_usdt": 0.0,
                        "bonus_slot_eligible": False,
                        "timestamp": datetime.now().isoformat(),
                        "recovered": True,
                    }
                return {
                    "success": False,
                    "error": f"{symbol} đã có vị thế {existing_dir} trên OKX",
                }
    except Exception as e:
        logger.warning(f"⚠️ Không kiểm tra được vị thế OKX cho {symbol}: {e}")

    # ── Hủy algo orders orphan trước khi mở lệnh mới ──
    try:
        await asyncio.to_thread(okx.cancel_algo_orders, symbol)
    except Exception:
        pass

    # ── Validate TP/SL logic trước khi đặt lệnh ──
    if direction == "LONG":
        if tp <= entry_price or sl >= entry_price:
            logger.warning(f"⚠️ TP/SL bị sai hướng cho LONG! tp={tp}, sl={sl}, entry={entry_price}")
            logger.warning(f"⚠️ Tính lại TP/SL từ entry price...")
            tp = round(entry_price * (1 + tp_pct / 100), 2)
            sl = round(entry_price * (1 - sl_pct / 100), 2)
            signal["tp"] = tp
            signal["sl"] = sl
            logger.info(f"✅ TP/SL đã sửa: TP={tp} (>{entry_price}), SL={sl} (<{entry_price})")
    elif direction == "SHORT":
        if tp >= entry_price or sl <= entry_price:
            logger.warning(f"⚠️ TP/SL bị sai hướng cho SHORT! tp={tp}, sl={sl}, entry={entry_price}")
            logger.warning(f"⚠️ Tính lại TP/SL từ entry price...")
            tp = round(entry_price * (1 - tp_pct / 100), 2)
            sl = round(entry_price * (1 + sl_pct / 100), 2)
            signal["tp"] = tp
            signal["sl"] = sl
            logger.info(f"✅ TP/SL đã sửa: TP={tp} (<{entry_price}), SL={sl} (>{entry_price})")

    logger.info(f"📊 {symbol} {direction}: Entry={entry_price}, TP={tp}, SL={sl}")

    try:
        # 1. Set leverage
        logger.info(f"Setting leverage {config.get_leverage(symbol)}x cho {symbol}")
        await asyncio.to_thread(okx.set_leverage, symbol, config.get_leverage(symbol))

        # 2. Place market order
        side = "buy" if direction == "LONG" else "sell"
        logger.info(f"Đặt lệnh {side} {size} contracts {symbol}")
        order_result = await asyncio.to_thread(okx.place_market_order, symbol, side, size)

        if order_result.get("code") != "0":
            error_msg = order_result.get("msg", "Unknown error")
            data = order_result.get("data", [{}])
            if data:
                error_msg = data[0].get("sMsg", error_msg)
            logger.error(f"Lỗi đặt lệnh: {error_msg}")
            return {"success": False, "error": error_msg}

        order_id = order_result.get("data", [{}])[0].get("ordId", "N/A")
        logger.info(f"✅ Lệnh đã đặt: {order_id}")

        # 2b. Lấy giá fill thực tế, tính lại TP/SL chính xác
        try:
            await asyncio.sleep(0.5)  # chờ order fill
            positions = await asyncio.to_thread(okx.get_positions, symbol)
            for p in positions:
                if float(p.get("pos", "0")) != 0:
                    real_entry = float(p.get("avgPx", "0"))
                    if real_entry > 0:
                        fill_slippage_pct = (
                            abs(real_entry - signal_price) / max(signal_price, 1e-9) * 100.0
                        )
                        max_fill_slippage = max(
                            0.0, float(getattr(config, "ENTRY_MAX_FILL_SLIPPAGE_PCT", 0.0))
                        )
                        if max_fill_slippage > 0 and fill_slippage_pct > max_fill_slippage:
                            logger.warning(
                                f"🛑 Fill slippage guard {symbol}: {fill_slippage_pct:.4f}% "
                                f"> {max_fill_slippage:.4f}% -> close ngay"
                            )
                            try:
                                await asyncio.to_thread(okx.close_position, symbol)
                            except Exception as close_err:
                                logger.error(f"Lỗi đóng vị thế sau slippage guard: {close_err}")
                            return {
                                "success": False,
                                "error": (
                                    f"Fill slippage guard: {fill_slippage_pct:.4f}% "
                                    f"> {max_fill_slippage:.4f}%"
                                ),
                            }

                    if real_entry > 0 and real_entry != entry_price:
                        logger.info(f"📍 Giá fill thực: {real_entry} (signal: {entry_price})")
                        tp, sl = okx.calc_tp_sl_prices(
                            real_entry,
                            direction,
                            tp_pct,
                            sl_pct,
                        )
                        entry_price = real_entry
                        signal["price"] = real_entry
                        signal["tp"] = tp
                        signal["sl"] = sl
                        logger.info(f"✅ TP/SL cập nhật theo giá fill: TP={tp}, SL={sl}")
                    break
        except Exception as e:
            logger.warning(f"Không lấy được giá fill: {e}")

        # 3. Place TP/SL (retry lên đến 3 lần nếu thất bại)
        close_side = "sell" if direction == "LONG" else "buy"
        logger.info(f"Đặt TP={tp} SL={sl} (direction={direction}, close_side={close_side})")
        tp_sl_ok = False
        for attempt in range(1, 4):
            tp_sl_result = await asyncio.to_thread(
                okx.place_tp_sl,
                symbol,
                close_side,
                size,
                str(tp),
                str(sl),
            )
            if tp_sl_result.get("code") == "0":
                tp_sl_ok = True
                if attempt > 1:
                    logger.info(f"✅ TP/SL đặt thành công (lần {attempt})")
                break
            logger.warning(f"TP/SL attempt {attempt}/3 failed: {tp_sl_result}")
            if attempt < 3:
                await asyncio.sleep(0.5)

        if not tp_sl_ok:
            # Last resort: đóng vị thế nếu không đặt được TP/SL
            logger.error(f"❌ TP/SL THẤT BẠI SAU 3 LẦN — đóng vị thế {symbol} để bảo vệ vốn")
            try:
                await asyncio.to_thread(okx.close_position, symbol)
            except Exception as ce:
                logger.error(f"Lỗi đóng vị thế: {ce}")
            return {"success": False, "error": "TP/SL failed after 3 retries, position closed"}

        # 4. Lưu trade info
        base_slots = max(1, int(config.MAX_ACTIVE_TRADES))
        is_bonus = len(active_trades) >= base_slots
        active_trades[symbol] = {
            "signal": signal,
            "order_id": order_id,
            "direction": direction,
            "entry_price": entry_price,
            "tp": tp,
            "sl": sl,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "size": size,
            "be_done": False,
            "tp1_done": False,
            "locked_sl_price": None,
            "max_pnl_margin": 0.0,
            "min_pnl_margin": 0.0,
            "actual_margin_usdt": float(signal.get("position_info", {}).get("actual_margin_usdt", 0) or 0),
            "bonus_slot_eligible": bool(signal.get("position_info", {}).get("bonus_slot_eligible", False)),
            "tp_sl_verified": True,
            "is_bonus": is_bonus,
            "timestamp": datetime.now().isoformat(),
        }

        return {
            "success": True,
            "order_id": order_id,
            "tp_sl_set": tp_sl_ok,
        }

    except Exception as e:
        logger.error(f"Lỗi execute trade: {e}")
        return {"success": False, "error": str(e)}


# ── position monitor ────────────────────────────────────────
async def monitor_positions(context: ContextTypes.DEFAULT_TYPE):
    """
    Kiểm tra vị thế đang mở — xem đã hit TP/SL chưa.
    + Auto-detect vị thế mồ côi trên OKX (mở tay hoặc từ trước).
    Chạy theo GUARDIAN_INTERVAL.
    """
    global active_trades, partial_tp_count, coin_cooldowns, _daily_stop_halt_triggered

    # ═══ AUTO-DETECT: tìm vị thế trên OKX mà bot chưa track ═══
    if not active_trades:
        try:
            all_pos = await asyncio.to_thread(okx.get_positions)
            for p in all_pos:
                pos_size = float(p.get("pos", "0"))
                if pos_size == 0:
                    continue
                sym = p.get("instId", "")
                if sym not in config.COINS:
                    continue
                # Có vị thế mồ côi → thêm vào tracking
                direction = "LONG" if pos_size > 0 else "SHORT"
                entry_price = float(p.get("avgPx", "0"))
                tp, sl = okx.calc_tp_sl_prices(
                    entry_price, direction, config.get_tp(sym), config.get_sl(sym)
                )
                active_trades[sym] = {
                    "signal": {"indicators": {}, "ai_result": {}},
                    "order_id": p.get("posId", "detected"),
                    "direction": direction,
                    "entry_price": entry_price,
                    "tp": tp,
                    "sl": sl,
                    "tp_pct": float(config.get_tp(sym)),
                    "sl_pct": float(config.get_sl(sym)),
                    "size": str(abs(pos_size)),
                    "be_done": False,
                    "tp1_done": False,
                    "locked_sl_price": None,
                    "max_pnl_margin": 0.0,
                    "min_pnl_margin": 0.0,
                    "actual_margin_usdt": 0.0,
                    "bonus_slot_eligible": False,
                    "timestamp": datetime.now().isoformat(),
                    "recovered": True,
                    "tp_sl_verified": False,
                }
                logger.info(f"🔍 Phát hiện vị thế: {sym} {direction} @ {entry_price}")
                # Đặt TP/SL lên OKX cho vị thế mồ côi
                tp_sl_ok = False
                try:
                    close_side = "sell" if direction == "LONG" else "buy"
                    tp_sl_result = await asyncio.to_thread(
                        okx.place_tp_sl, sym, close_side,
                        str(abs(pos_size)), str(tp), str(sl),
                    )
                    tp_sl_ok = tp_sl_result.get("code") == "0"
                    if tp_sl_ok:
                        active_trades[sym]["tp_sl_verified"] = True
                    else:
                        logger.warning(f"⚠️ TP/SL orphan {sym}: {tp_sl_result}")
                except Exception as e2:
                    logger.error(f"❌ Lỗi đặt TP/SL orphan {sym}: {e2}")
                await telegram_handler.send_message(
                    context.bot,
                    f"🔍 Phát hiện vị thế trên OKX:\n"
                    f"  {sym} {direction} @ {entry_price:,.2f}\n"
                    f"  TP: {tp:,.2f} | SL: {sl:,.2f}\n"
                    f"  TP/SL đặt: {'✅' if tp_sl_ok else '❌'}"
                )
        except Exception as e:
            logger.error(f"Lỗi detect orphan positions: {e}")
        if not active_trades:
            return

    for symbol in list(active_trades.keys()):
        try:
            positions = await asyncio.to_thread(okx.get_positions, symbol)

            # Nếu không còn vị thế → đã đóng (TP/SL hit)
            open_position = _first_open_position(positions)
            has_position = open_position is not None

            trade = active_trades.get(symbol, {})
            trade.setdefault("be_done", False)
            trade.setdefault("tp1_done", False)
            trade.setdefault("locked_sl_price", None)
            trade.setdefault("max_pnl_margin", 0.0)
            trade.setdefault("min_pnl_margin", 0.0)
            trade.setdefault("guardian_close_pending_until", 0.0)
            open_time_str = trade.get("timestamp")

            # ═══ KIỂM TRA TP/SL CÒN SỐNG — re-arm nếu mất ═══
            if has_position and not trade.get("tp_sl_verified", False):
                try:
                    has_algo = await asyncio.to_thread(okx.has_algo_orders, symbol)
                    if not has_algo:
                        logger.warning(f"⚠️ {symbol} CÓ VỊ THẾ NHƯNG KHÔNG CÓ TP/SL — re-arm!")
                        close_side = "sell" if trade["direction"] == "LONG" else "buy"
                        pos_size = str(abs(float(open_position.get("pos", trade.get("size", "0")))))
                        rearm_result = await asyncio.to_thread(
                            okx.place_tp_sl, symbol, close_side, pos_size,
                            str(trade.get("tp", 0)), str(trade.get("sl", 0)),
                        )
                        if rearm_result.get("code") == "0":
                            logger.info(f"✅ Re-arm TP/SL thành công cho {symbol}")
                            trade["tp_sl_verified"] = True
                        else:
                            logger.error(f"❌ Re-arm TP/SL thất bại {symbol}: {rearm_result}")
                    else:
                        trade["tp_sl_verified"] = True
                except Exception as e_algo:
                    logger.warning(f"Lỗi check algo orders {symbol}: {e_algo}")

            # ═══ POSITION MONITOR — Partial TP / BE ═══
            if has_position and open_time_str:
                import time as _time
                now_ts = _time.time()
                open_dt = datetime.fromisoformat(open_time_str)
                elapsed_sec = (datetime.now() - open_dt).total_seconds()
                first_check_sec = float(config.GUARDIAN_FIRST_CHECK_SEC)

                try:
                    # Lấy giá hiện tại
                    market_data = await asyncio.to_thread(okx.get_market_data, symbol)
                    current_price = market_data["price"]

                    if current_price <= 0:
                        continue

                    direction = trade["direction"]
                    entry_price = trade["entry_price"]

                    # Tính PnL hiện tại
                    if direction == "LONG":
                        cur_pnl = (current_price - entry_price) / entry_price * 100
                    else:
                        cur_pnl = (entry_price - current_price) / entry_price * 100
                    leverage = float(config.get_leverage(symbol))
                    cur_pnl_margin = cur_pnl * leverage
                    trade["max_pnl_margin"] = max(
                        float(trade.get("max_pnl_margin", cur_pnl_margin) or cur_pnl_margin),
                        cur_pnl_margin,
                    )
                    trade["min_pnl_margin"] = min(
                        float(trade.get("min_pnl_margin", cur_pnl_margin) or cur_pnl_margin),
                        cur_pnl_margin,
                    )
                    locked_sl_price = _locked_profit_sl_price(symbol, direction, entry_price)

                    # Size đang mở thực tế
                    current_pos_size = 0.0
                    try:
                        if open_position is not None:
                            current_pos_size = abs(float(open_position.get("pos", "0")))
                    except Exception:
                        current_pos_size = 0.0

                    close_pending_until = float(
                        trade.get("guardian_close_pending_until", 0.0) or 0.0
                    )
                    if close_pending_until > now_ts:
                        continue

                    guardian_reason = ""
                    if current_pos_size > 0:
                        guardian_reason = _guardian_close_reason(
                            symbol,
                            trade,
                            elapsed_sec,
                            cur_pnl_margin,
                        )
                    if guardian_reason and current_pos_size > 0:
                        close_size = await asyncio.to_thread(
                            okx.normalize_size, symbol, current_pos_size
                        )
                        if close_size:
                            close_result = await asyncio.to_thread(
                                okx.place_reduce_market_order,
                                symbol,
                                _close_side(direction),
                                close_size,
                            )
                            if close_result.get("code") == "0":
                                trade["close_reason"] = guardian_reason
                                trade["guardian_close_pending_until"] = (
                                    now_ts + float(config.GUARDIAN_INTERVAL)
                                )
                                logger.info(
                                    f"🛑 Guardian close {symbol}: {guardian_reason} | "
                                    f"margin_pnl={cur_pnl_margin:+.2f}%"
                                )
                                await telegram_handler.send_message(
                                    context.bot,
                                    f"🛑 Guardian close {symbol}\n"
                                    f"Lý do: {guardian_reason}\n"
                                    f"PnL margin: {cur_pnl_margin:+.2f}%"
                                )
                                continue
                            logger.warning(
                                f"Guardian close failed {symbol}: {close_result}"
                            )

                    # Quick scalp take-profit: chốt full nhanh khi đã lời đủ qua phí.
                    if (
                        bool(getattr(config, "QUICK_TAKE_ENABLED", False))
                        and current_pos_size > 0
                        and not bool(trade.get("tp1_done", False))
                    ):
                        quick_min_hold = max(
                            first_check_sec,
                            float(getattr(config, "QUICK_TAKE_MIN_HOLD_SEC", 20)),
                        )
                        quick_target = _quick_take_margin_target(symbol)
                        if elapsed_sec >= quick_min_hold and cur_pnl_margin >= quick_target:
                            close_size = await asyncio.to_thread(
                                okx.normalize_size, symbol, current_pos_size
                            )
                            if close_size:
                                close_result = await asyncio.to_thread(
                                    okx.place_reduce_market_order,
                                    symbol,
                                    _close_side(direction),
                                    close_size,
                                )
                                if close_result.get("code") == "0":
                                    trade["close_reason"] = (
                                        "quick_take_scalp: "
                                        f"{cur_pnl_margin:+.2f}% >= {quick_target:.2f}%"
                                    )
                                    trade["guardian_close_pending_until"] = (
                                        now_ts + float(config.GUARDIAN_INTERVAL)
                                    )
                                    logger.info(
                                        f"⚡ Quick take {symbol}: close full at "
                                        f"margin_pnl={cur_pnl_margin:+.2f}% "
                                        f"(target={quick_target:.2f}%)"
                                    )
                                    await telegram_handler.send_message(
                                        context.bot,
                                        f"⚡ Quick take {symbol}\n"
                                        f"Chốt full nhanh: {cur_pnl_margin:+.2f}% "
                                        f"(target {quick_target:.2f}%)"
                                    )
                                    continue
                                logger.warning(f"Quick take failed {symbol}: {close_result}")

                    # Chốt lời từng phần:
                    # - BE: dời SL về entry khi lãi đạt BE_RATIO × TP
                    # - TP1: chốt 50% khi lãi đạt CLOSE_RATIO × TP, giữ phần còn lại
                    if (
                        config.PARTIAL_TP_ENABLED
                        and current_pos_size > 0
                        and elapsed_sec >= max(first_check_sec, float(config.PARTIAL_TP_MIN_HOLD_SEC))
                    ):
                        target_tp_pct = float(trade.get("tp_pct", config.get_tp(symbol)))
                        target_pnl_margin = target_tp_pct * leverage
                        be_trigger_margin = target_pnl_margin * float(config.PARTIAL_TP_BE_RATIO)
                        tp1_trigger_margin = target_pnl_margin * float(config.PARTIAL_TP_CLOSE_RATIO)
                        be_done = bool(trade.get("be_done", False))
                        tp1_done = bool(trade.get("tp1_done", False))

                        # Nếu BE và TP1 cùng mốc → gộp: chốt 50% + dời SL entry 1 lần
                        same_trigger = abs(be_trigger_margin - tp1_trigger_margin) < 0.01

                        if same_trigger and (not tp1_done) and cur_pnl_margin >= tp1_trigger_margin:
                            # Gộp: chốt 50% + BE cùng lúc
                            part_size = await asyncio.to_thread(
                                okx.calc_partial_close_size,
                                symbol,
                                current_pos_size,
                                float(config.PARTIAL_TP_CLOSE_FRACTION),
                            )
                            if part_size:
                                part_side = _close_side(direction)
                                part_result = await asyncio.to_thread(
                                    okx.place_reduce_market_order,
                                    symbol, part_side, part_size,
                                )
                                if part_result.get("code") == "0":
                                    trade["tp1_done"] = True
                                    trade["be_done"] = True
                                    if (
                                        not trade.get("is_bonus", False)
                                        and bool(trade.get("bonus_slot_eligible", False))
                                    ):
                                        partial_tp_count += 1
                                    logger.info(
                                        f"💰🛡️ BE+TP1 {symbol}: close {part_size} + SL->fee-lock "
                                        f"at margin_pnl={cur_pnl_margin:+.2f}% "
                                        f"(bonus={trade.get('is_bonus')}, partial_tp #{partial_tp_count})"
                                    )
                                    positions_after = await asyncio.to_thread(okx.get_positions, symbol)
                                    open_after = _first_open_position(positions_after)
                                    if open_after is not None:
                                        remain_size = abs(float(open_after.get("pos", "0")))
                                        remain_size_str = await asyncio.to_thread(
                                            okx.normalize_size, symbol, remain_size
                                        )
                                        if remain_size_str:
                                            await _rearm_tp_sl_for_remaining(
                                                symbol, trade, remain_size_str,
                                                sl_price=locked_sl_price,
                                            )
                                    else:
                                        has_position = False
                                    bonus_msg = ""
                                    if partial_tp_count % 2 == 0 and partial_tp_count > 0:
                                        bonus_msg = f"\n🎰 Vòng {1 + partial_tp_count // 2}: +1 slot mới! (max={_max_active_trades()})"
                                    await telegram_handler.send_message(
                                        context.bot,
                                        f"💰🛡️ {symbol}: chốt 50% + dời SL qua phí\n"
                                        f"PnL margin: {cur_pnl_margin:+.2f}%\n"
                                        f"Phần còn lại chạy tới TP cuối.{bonus_msg}"
                                    )
                                else:
                                    logger.warning(f"Partial TP close failed {symbol}: {part_result}")
                            else:
                                trade["tp1_done"] = True
                                full_size_str = await asyncio.to_thread(
                                    okx.normalize_size, symbol, current_pos_size
                                )
                                if full_size_str:
                                    be_ok = await _rearm_tp_sl_for_remaining(
                                        symbol,
                                        trade,
                                        full_size_str,
                                        sl_price=locked_sl_price,
                                    )
                                    trade["be_done"] = bool(be_ok)
                                logger.info(
                                    f"ℹ️ Partial TP skip {symbol}: size quá nhỏ, chỉ fee-lock phần còn lại."
                                )
                        else:
                            # BE và TP1 khác mốc → xử lý riêng
                            if (not be_done) and cur_pnl_margin >= be_trigger_margin:
                                full_size_str = await asyncio.to_thread(
                                    okx.normalize_size, symbol, current_pos_size
                                )
                                if full_size_str:
                                    be_ok = await _rearm_tp_sl_for_remaining(
                                        symbol,
                                        trade,
                                        full_size_str,
                                        sl_price=locked_sl_price,
                                    )
                                    if be_ok:
                                        trade["be_done"] = True
                                        logger.info(
                                            f"🛡️ BE armed {symbol}: margin_pnl={cur_pnl_margin:+.2f}% "
                                            f">= {be_trigger_margin:.2f}% | SL -> fee-lock"
                                        )
                                        await telegram_handler.send_message(
                                            context.bot,
                                            f"🛡️ {symbol}: dời SL qua phí để khóa lãi\n"
                                            f"PnL margin: {cur_pnl_margin:+.2f}%"
                                        )

                            if (not tp1_done) and cur_pnl_margin >= tp1_trigger_margin:
                                part_size = await asyncio.to_thread(
                                    okx.calc_partial_close_size,
                                    symbol,
                                    current_pos_size,
                                    float(config.PARTIAL_TP_CLOSE_FRACTION),
                                )
                                if part_size:
                                    part_side = _close_side(direction)
                                    part_result = await asyncio.to_thread(
                                        okx.place_reduce_market_order,
                                        symbol,
                                        part_side,
                                        part_size,
                                    )
                                    if part_result.get("code") == "0":
                                        trade["tp1_done"] = True
                                        if (
                                            not trade.get("is_bonus", False)
                                            and bool(trade.get("bonus_slot_eligible", False))
                                        ):
                                            partial_tp_count += 1
                                        logger.info(
                                            f"💰 Partial TP {symbol}: close {part_size} "
                                            f"at margin_pnl={cur_pnl_margin:+.2f}% "
                                            f"(bonus={trade.get('is_bonus')}, partial_tp #{partial_tp_count})"
                                        )
                                        positions_after = await asyncio.to_thread(okx.get_positions, symbol)
                                        open_after = _first_open_position(positions_after)
                                        if open_after is not None:
                                            remain_size = abs(float(open_after.get("pos", "0")))
                                            remain_size_str = await asyncio.to_thread(
                                                okx.normalize_size, symbol, remain_size
                                            )
                                            if remain_size_str:
                                                be_sl = (
                                                    float(trade.get("locked_sl_price", locked_sl_price))
                                                    if bool(trade.get("be_done", False))
                                                    else None
                                                )
                                                await _rearm_tp_sl_for_remaining(
                                                    symbol,
                                                    trade,
                                                    remain_size_str,
                                                    sl_price=be_sl,
                                                )
                                        else:
                                            has_position = False
                                        bonus_msg = ""
                                        if partial_tp_count % 2 == 0 and partial_tp_count > 0:
                                            bonus_msg = f"\n🎰 Vòng {1 + partial_tp_count // 2}: +1 slot mới! (max={_max_active_trades()})"
                                        await telegram_handler.send_message(
                                            context.bot,
                                            f"💰 {symbol}: chốt 50% vị thế\n"
                                            f"PnL margin: {cur_pnl_margin:+.2f}%\n"
                                            f"Giữ phần còn lại tới TP cuối.{bonus_msg}"
                                        )
                                    else:
                                        logger.warning(
                                            f"Partial TP close failed {symbol}: {part_result}"
                                        )
                                else:
                                    trade["tp1_done"] = True
                                    if not bool(trade.get("be_done", False)):
                                        full_size_str = await asyncio.to_thread(
                                            okx.normalize_size, symbol, current_pos_size
                                        )
                                        if full_size_str:
                                            be_ok = await _rearm_tp_sl_for_remaining(
                                                symbol,
                                                trade,
                                                full_size_str,
                                                sl_price=locked_sl_price,
                                            )
                                            trade["be_done"] = bool(be_ok)
                                    logger.info(
                                        f"ℹ️ Partial TP skip {symbol}: size quá nhỏ để chốt 50%, giữ fee-lock."
                                    )

                except Exception as e:
                    logger.error(f"Position monitor error {symbol}: {e}")

            if not has_position and symbol in active_trades:
                trade = active_trades.pop(symbol)
                logger.info(f"Vị thế {symbol} đã đóng")

                # Hủy mọi algo orders orphan (TP/SL) còn sót lại
                # Tránh trường hợp orphan trigger mở lệnh đảo chiều
                try:
                    await asyncio.to_thread(okx.cancel_algo_orders, symbol)
                except Exception as e:
                    logger.warning(f"Lỗi cancel orphan algos {symbol}: {e}")

                entry = trade["entry_price"]
                direction = trade["direction"]
                pnl_usdt = 0.0
                pnl_pct = 0.0
                exit_price = 0.0

                # Lấy PnL thực tế từ OKX (positions history)
                try:
                    history = await asyncio.to_thread(
                        okx.get_positions_history, symbol, 5
                    )
                    if history:
                        # Lấy vị thế đóng gần nhất
                        latest = history[0]
                        real_pnl = float(latest.get("realizedPnl", "0"))
                        fee = float(latest.get("fee", "0"))  # fee âm
                        funding_fee = float(latest.get("fundingFee", "0"))
                        close_avg = float(latest.get("closeAvgPx", "0"))
                        open_avg = float(latest.get("openAvgPx", str(entry)))

                        pnl_usdt = real_pnl + fee + funding_fee  # PnL ròng
                        exit_price = close_avg if close_avg > 0 else 0
                        if open_avg > 0:
                            entry = open_avg

                        if entry > 0:
                            if direction == "LONG":
                                pnl_pct = (exit_price - entry) / entry * 100
                            else:
                                pnl_pct = (entry - exit_price) / entry * 100

                        logger.info(
                            f"💰 PnL thực tế {symbol}: {pnl_usdt:+.4f} USDT "
                            f"({pnl_pct:+.2f}%) | Fee: {fee:.4f} | "
                            f"Entry: {entry} → Exit: {exit_price}"
                        )
                    else:
                        logger.warning(f"Không lấy được history {symbol}, ước lượng PnL")
                        # Fallback: ước lượng từ giá hiện tại
                        ticker = await asyncio.to_thread(okx.get_ticker, symbol)
                        current_price = float(ticker.get("last", "0"))
                        exit_price = current_price
                        if direction == "LONG":
                            pnl_pct = (current_price - entry) / entry * 100
                        else:
                            pnl_pct = (entry - current_price) / entry * 100
                        gross_pnl_usdt = (
                            config.TRADE_AMOUNT_USDT
                            * config.get_leverage(symbol)
                            * pnl_pct
                            / 100
                        )
                        pnl_usdt = gross_pnl_usdt - _roundtrip_fee_usdt(symbol)
                        logger.info(
                            f"Fallback net PnL {symbol}: gross={gross_pnl_usdt:+.4f} "
                            f"- fee_est={_roundtrip_fee_usdt(symbol):.4f} = {pnl_usdt:+.4f}"
                        )

                except Exception as e:
                    logger.error(f"Lỗi lấy PnL history {symbol}: {e}")
                    # Fallback
                    ticker = await asyncio.to_thread(okx.get_ticker, symbol)
                    current_price = float(ticker.get("last", "0"))
                    exit_price = current_price
                    if direction == "LONG":
                        pnl_pct = (current_price - entry) / entry * 100
                    else:
                        pnl_pct = (entry - current_price) / entry * 100
                    gross_pnl_usdt = (
                        config.TRADE_AMOUNT_USDT
                        * config.get_leverage(symbol)
                        * pnl_pct
                        / 100
                    )
                    pnl_usdt = gross_pnl_usdt - _roundtrip_fee_usdt(symbol)
                    logger.info(
                        f"Fallback net PnL {symbol}: gross={gross_pnl_usdt:+.4f} "
                        f"- fee_est={_roundtrip_fee_usdt(symbol):.4f} = {pnl_usdt:+.4f}"
                    )

                # Đồng bộ dấu pnl_pct theo pnl_usdt để AI memory không bị lệch nhãn
                if pnl_usdt > 0 and pnl_pct < 0:
                    pnl_pct = abs(pnl_pct)
                elif pnl_usdt < 0 and pnl_pct > 0:
                    pnl_pct = -abs(pnl_pct)

                # Ghi nhận kết quả
                trade_ts = _trade_time_from_value(trade.get("timestamp"))
                hold_seconds = None
                if trade_ts is not None:
                    hold_seconds = round(
                        max(0.0, (datetime.now() - trade_ts).total_seconds()),
                        1,
                    )
                ai_result = trade.get("signal", {}).get("ai_result", {}) or {}
                close_reason = trade.get("close_reason", "")
                if not close_reason:
                    if pnl_usdt > 0 and bool(trade.get("tp1_done", False)):
                        close_reason = "tp_partial_or_fee_lock"
                    elif pnl_usdt > 0:
                        close_reason = "profit_exit"
                    elif pnl_usdt < 0:
                        close_reason = "sl_or_adverse_close"
                    else:
                        close_reason = "flat_close"

                capital.record_trade(
                    symbol, direction, entry,
                    exit_price, pnl_usdt, pnl_pct,
                    {
                        "close_reason": close_reason,
                        "hold_seconds": hold_seconds,
                        "tp1_done": bool(trade.get("tp1_done", False)),
                        "be_done": bool(trade.get("be_done", False)),
                        "max_pnl_margin": round(float(trade.get("max_pnl_margin", 0) or 0), 4),
                        "min_pnl_margin": round(float(trade.get("min_pnl_margin", 0) or 0), 4),
                        "win_probability": int(ai_result.get("win_probability", 0) or 0),
                        "playbook_score": round(float(ai_result.get("playbook_score", 0) or 0), 2),
                        "risk_score": int(ai_result.get("risk_score", 0) or 0),
                        "entry_quality": str(ai_result.get("entry_quality", "") or ""),
                        "decision": str(ai_result.get("decision", "") or ""),
                        "actual_margin_usdt": round(
                            float(trade.get("actual_margin_usdt", 0) or 0), 4
                        ),
                        "bonus_slot_eligible": bool(trade.get("bonus_slot_eligible", False)),
                        "is_bonus": bool(trade.get("is_bonus", False)),
                    },
                )

                daily_hit, stop_threshold, pnl_today = _daily_stop_triggered()
                if daily_hit:
                    logger.warning(
                        f"🛑 Daily stop-loss hit after close {symbol}: "
                        f"pnl={pnl_today:+.4f} <= {stop_threshold:+.4f} USDT"
                    )
                    await telegram_handler.send_message(
                        context.bot,
                        "DAILY STOP-LOSS HIT\n"
                        f"PnL today: {pnl_today:+.4f} USDT <= {stop_threshold:+.4f}\n"
                        "Auto trade disabled for the rest of day.",
                    )
                    if (
                        bool(getattr(config, "DAILY_STOP_LOSS_HALT_APP", False))
                        and not _daily_stop_halt_triggered
                        and len(active_trades) == 0
                    ):
                        _daily_stop_halt_triggered = True
                        try:
                            await context.application.stop()
                        except Exception as e:
                            logger.error(f"Stop application after daily stop-loss failed: {e}")
                        return

                # Per-coin cooldown escalation:
                # thua 1 lệnh -> nghỉ 1h, thua tiếp -> nghỉ 24h
                if pnl_usdt < 0:
                    cooldown_info = _symbol_loss_cooldown_remaining(symbol)
                    if cooldown_info is not None:
                        remaining, consec_losses, cooldown_sec = cooldown_info
                        coin_cooldowns[symbol] = max(
                            float(coin_cooldowns.get(symbol, 0.0)),
                            time.time() + float(remaining),
                        )
                        logger.info(
                            f"⏳ {symbol} cooldown {remaining}s "
                            f"(loss streak={consec_losses}, rule={cooldown_sec}s)"
                        )

                # Lưu vào AI memory
                trade_result = "WIN" if pnl_usdt >= 0 else "LOSS"
                trade_indicators = trade.get("signal", {}).get("indicators", {})
                trade_confidence = trade.get("signal", {}).get("ai_result", {}).get("confidence", 0)
                save_trade_to_memory(
                    symbol, direction, trade_result,
                    pnl_pct, trade_indicators, trade_confidence,
                )

                # Thông báo Telegram
                await telegram_handler.send_position_closed(
                    context.bot, symbol, direction, pnl_usdt, pnl_pct,
                )

                # ═══ AUTO RE-SCAN: đóng xong → scan lại ngay lập tức ═══
                # Reset bonus slots khi tất cả vị thế đã đóng
                if not active_trades and partial_tp_count > 0:
                    logger.info(
                        f"🔄 Tất cả vị thế đã đóng — reset partial_tp_count "
                        f"({partial_tp_count} → 0), bonus slots reset"
                    )
                    partial_tp_count = 0
                logger.info("🔄 Vị thế đóng xong → tự động scan lại L1 rồi L2...")
                await asyncio.sleep(3)  # chờ 3s cho OKX cập nhật
                await scan_market_once(context)

        except Exception as e:
            logger.error(f"Lỗi monitor {symbol}: {e}")


# ── job scheduler ───────────────────────────────────────────
async def scan_job(context: ContextTypes.DEFAULT_TYPE):
    """Job: scan thị trường (chạy theo interval)."""
    await scan_market_once(context)


async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Job: monitor positions (chạy mỗi 30s)."""
    await monitor_positions(context)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int((len(ordered) - 1) * pct)
    return float(ordered[idx])


async def performance_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Hourly performance report for latency, scan delay, and skip reasons."""
    now = time.time()
    window_sec = max(300, config.PERF_REPORT_INTERVAL_SEC)
    since = now - window_sec

    recent_scans = [e for e in perf_scan_events if e["ts"] >= since]
    recent_ai = [e["latency"] for e in perf_ai_events if e["ts"] >= since]
    recent_skips = [e["reason"] for e in perf_skip_events if e["ts"] >= since]

    scan_durations = [e["duration"] for e in recent_scans]
    scan_delays = [e["delay"] for e in recent_scans]
    budget_hits = sum(1 for e in recent_scans if e.get("budget_hit"))

    skip_counter = Counter(recent_skips)
    top_skips = ", ".join(
        f"{k}:{v}" for k, v in skip_counter.most_common(5)
    ) if skip_counter else "none"

    scan_count = len(recent_scans)
    ai_count = len(recent_ai)

    scan_avg = sum(scan_durations) / scan_count if scan_count else 0.0
    scan_p90 = _percentile(scan_durations, 0.90) if scan_count else 0.0
    delay_avg = sum(scan_delays) / scan_count if scan_count else 0.0
    delay_max = max(scan_delays) if scan_count else 0.0

    ai_avg = sum(recent_ai) / ai_count if ai_count else 0.0
    ai_p90 = _percentile(recent_ai, 0.90) if ai_count else 0.0
    ai_max = max(recent_ai) if ai_count else 0.0

    stats = capital.daily_stats()
    report_text = (
        f"📈 PERF REPORT ({int(window_sec/60)}m)\n"
        f"{'━' * 28}\n"
        f"Trades: {stats['total_trades']} | Winrate: {stats['winrate']}% | "
        f"PnL: {stats['total_pnl']:+.4f} USDT\n\n"
        f"🧠 AI latency: n={ai_count}\n"
        f"  avg={ai_avg:.2f}s | p90={ai_p90:.2f}s | max={ai_max:.2f}s\n\n"
        f"🔍 Scan: n={scan_count}\n"
        f"  avg={scan_avg:.2f}s | p90={scan_p90:.2f}s\n"
        f"  delay_avg={delay_avg:.2f}s | delay_max={delay_max:.2f}s\n"
        f"  budget_hit={budget_hits}\n\n"
        f"🚫 Skip reasons: {top_skips}\n"
        f"{'━' * 28}"
    )
    await telegram_handler.send_message(context.bot, report_text)


async def recover_open_positions():
    """
    Khi bot khởi động lại, detect vị thế đang mở trên OKX
    và thêm vào active_trades để monitor tiếp.
    """
    global active_trades
    try:
        all_positions = await asyncio.to_thread(okx.get_positions)
        recovered = []
        for p in all_positions:
            pos_size = float(p.get("pos", "0"))
            if pos_size == 0:
                continue
            symbol = p.get("instId", "")
            if symbol not in config.COINS:
                continue
            direction = "LONG" if pos_size > 0 else "SHORT"
            entry_price = float(p.get("avgPx", "0"))
            # Tính lại TP/SL từ entry
            tp, sl = okx.calc_tp_sl_prices(
                entry_price, direction,
                config.get_tp(symbol), config.get_sl(symbol)
            )
            active_trades[symbol] = {
                "signal": {"indicators": {}, "ai_result": {}},
                "order_id": p.get("posId", "recovered"),
                "direction": direction,
                "entry_price": entry_price,
                "tp": tp,
                "sl": sl,
                "tp_pct": float(config.get_tp(symbol)),
                "sl_pct": float(config.get_sl(symbol)),
                "size": str(abs(pos_size)),
                "be_done": False,
                "tp1_done": False,
                "locked_sl_price": None,
                "max_pnl_margin": 0.0,
                "min_pnl_margin": 0.0,
                "actual_margin_usdt": 0.0,
                "bonus_slot_eligible": False,
                "timestamp": datetime.now().isoformat(),
                "recovered": True,
                "tp_sl_verified": False,
            }
            recovered.append(f"{symbol} {direction} @ {entry_price}")
            logger.info(f"♻️ Recovered position: {symbol} {direction} @ {entry_price}")
            # Đặt TP/SL lên OKX cho vị thế recovered
            try:
                close_side = "sell" if direction == "LONG" else "buy"
                tp_sl_result = await asyncio.to_thread(
                    okx.place_tp_sl, symbol, close_side,
                    str(abs(pos_size)), str(tp), str(sl),
                )
                tp_sl_ok = tp_sl_result.get("code") == "0"
                if tp_sl_ok:
                    logger.info(f"✅ TP/SL đặt cho {symbol}: TP={tp} SL={sl}")
                    active_trades[symbol]["tp_sl_verified"] = True
                else:
                    logger.warning(f"⚠️ TP/SL recovered {symbol}: {tp_sl_result}")
            except Exception as e2:
                logger.error(f"❌ Lỗi đặt TP/SL recovered {symbol}: {e2}")
        return recovered
    except Exception as e:
        logger.error(f"Lỗi recover positions: {e}")
        return []


async def post_init(app: Application):
    """Chạy sau khi bot khởi động."""
    mode = "DEMO" if config.OKX_DEMO else "LIVE"
    coins_l1 = list(dict.fromkeys(config.COINS_LAYER1))
    coins_l2 = [c for c in config.COINS_LAYER2 if c not in coins_l1]
    coins_all = config.COINS

    logger.info(f"🚀 FuBot Trading Bot đã khởi động — Mode: {mode}")
    logger.info(f"Coins L1: {', '.join(coins_l1) if coins_l1 else 'None'}")
    logger.info(f"Coins L2: {', '.join(coins_l2) if coins_l2 else 'None'}")
    logger.info(f"Coins all: {', '.join(coins_all) if coins_all else 'None'}")
    logger.info(
        f"Config: {', '.join(f'{c.split(chr(45))[0]}: TP={config.get_tp(c)}% SL={config.get_sl(c)}% {config.get_leverage(c)}x' for c in coins_all)}"
    )
    deepseek_assist_active = bool(
        config.AI_GPT_ONLY_MODE
        and config.AI_DEEPSEEK_ASSIST_ENABLED
        and config.DEEPSEEK_API_KEY
    )
    if bool(getattr(config, "SIMPLE_SCALP_MODE", False)):
        logger.info(f"Analysis: SIMPLE-SCALP ({_simple_scalp_trigger_text()}, market order)")
    elif config.AI_SCORE_ONLY_MODE:
        logger.info("Analysis: SCORE-ONLY (pure Python playbook + flow + BTC filter, 0 API calls)")
    elif config.AI_FAST_3L_MODE:
        logger.info("AI: FAST3L (GPT L1+L2 + DeepSeek L4 adaptive referee)")
    elif config.AI_GPT_ONLY_MODE:
        if deepseek_assist_active:
            logger.info(
                "AI: GPT aggressive mode + DeepSeek assist lite (L4 advisory, L6 skipped)"
            )
        else:
            logger.info("AI: GPT aggressive mode (L1+L2+L3+L5), skip DeepSeek layers")
    else:
        logger.info("AI: GPT (L1+L2+L3+L5) + DeepSeek V3 (L4+L6) | Anti-Echo")
    if (
        not config.AI_SCORE_ONLY_MODE
        and not bool(getattr(config, "SIMPLE_SCALP_MODE", False))
        and config.AI_FAST_3L_MODE
    ):
        logger.info("AI profile: FAST3L (2 GPT + 1 DeepSeek: L1+L2+L4)")
    if config.LONG_ONLY:
        logger.info("Strategy: LONG_ONLY scalp mode")
    logger.info(f"Scan interval: {config.SCAN_INTERVAL}s")
    entry_gate_now = _effective_score_gate()
    pre_gate_now = _analysis_score_gate(entry_gate_now)
    logger.info(
        f"Perf tuning: GPT timeout={config.GPT_TIMEOUT_SEC}s | "
        f"DeepSeek timeout={config.DEEPSEEK_TIMEOUT_SEC}s | "
        f"GPT cap={config.GPT_TIMEOUT_HARD_CAP_SEC}s | "
        f"DeepSeek cap={config.DEEPSEEK_TIMEOUT_HARD_CAP_SEC}s | "
        f"GPT hedge={config.GPT_HEDGE_FANOUT}x | "
        f"L3 ensemble={config.GPT_L3_ENSEMBLE}/{config.GPT_L3_QUORUM} | "
        f"FAST3L veto={'adaptive' if config.FAST3L_ADAPTIVE_VETO else 'strict'} "
        f"(l3>={config.FAST3L_ADVISORY_MIN_L3_WIN}, risk<={config.FAST3L_ADVISORY_MAX_RISK_SCORE}) | "
        f"DS assist={'on' if deepseek_assist_active else 'off'} "
        f"(min_remain={config.AI_DEEPSEEK_ASSIST_MIN_REMAIN_SEC}s, "
        f"to={config.AI_DEEPSEEK_ASSIST_TIMEOUT_SEC}s) | "
        f"AI budget={config.AI_TIME_BUDGET_SEC}s | "
        f"Score gate(entry_base={config.AI_SCORE_GATE:.1f}, "
        f"entry_now={entry_gate_now:.1f}, ai_pre={pre_gate_now:.1f})/10 | "
        f"AI topN/layer={config.AI_MAX_SIGNALS_PER_LAYER} | "
        f"LONG_ONLY={'on' if config.LONG_ONLY else 'off'} | "
        f"FAST3L={'on' if config.AI_FAST_3L_MODE else 'off'} | "
        f"Max active={_max_active_trades()} | "
        f"Max 30m={config.MAX_TRADES_PER_30M if config.MAX_TRADES_PER_30M > 0 else 'off'} | "
        f"Cadence target={config.TARGET_TRADES_PER_15M}/15m | "
        f"Cadence relax={'on' if config.AUTO_TRADE_MIN_WIN_RELAX_FOR_CADENCE else 'off'} | "
        f"Cadence turbo={'on' if _cadence_turbo_active() else 'off'} | "
        f"QuickTake={'on' if config.QUICK_TAKE_ENABLED else 'off'} "
        f"@{_quick_take_margin_target(coins_all[0]) if coins_all else 0:.2f}% | "
        f"Score-only={'on' if config.AI_SCORE_ONLY_MODE else 'off'} | "
        f"Auto win gate={_effective_auto_trade_min_win()}% | "
        f"Daily stop={config.DAILY_STOP_LOSS_USDT:+.2f} | "
        f"Entry guard(spread<={config.ENTRY_MAX_SPREAD_PCT:.3f}%, "
        f"drift<={config.ENTRY_MAX_SIGNAL_DRIFT_PCT:.3f}%, "
        f"fill<={config.ENTRY_MAX_FILL_SLIPPAGE_PCT:.3f}%) | "
        f"ATR brake={'on' if config.ATR_BRAKE_ENABLED else 'off'} "
        f"(trig={config.ATR_BRAKE_TRIGGER_RATIO:.2f}x) | "
        f"HardGateEMA200={'on' if config.HARD_GATE_EMA200_ENABLED else 'off'} | "
        f"NewsBlock={'on' if config.NEWS_CALENDAR_ENABLED else 'off'} | "
        f"Scan budget={_calc_scan_budget_sec():.1f}s"
    )

    # Recover vị thế đang mở (nếu bot vừa restart)
    recovered = await recover_open_positions()
    if bool(getattr(config, "SIMPLE_SCALP_MODE", False)):
        ai_stack_line = (
            f"  🎯 Trigger: {_simple_scalp_trigger_text()}\n"
            "  ⚡ Entry: Market, 1 lệnh mỗi lần\n"
            "  🤖 AI filter: BYPASS (simple mode)\n\n"
        )
    elif config.AI_SCORE_ONLY_MODE:
        ai_stack_line = (
            "  🧮 Local: Pair playbook + orderflow + microstructure\n"
            "  📊 BTC Filter: EMA 5m + 15m\n"
            "  ⚡ GPT/DeepSeek: DISABLED (0 API calls)\n\n"
        )
    elif config.AI_FAST_3L_MODE:
        ai_stack_line = (
            "  📶 GPT: L1 Analyst | L2 Risk\n"
            "  🔷 DeepSeek: L4 Referee (adaptive gate)\n"
            "  ⚡ L3/L5/L6: SKIPPED để giảm timeout\n\n"
        )
    elif config.AI_GPT_ONLY_MODE:
        if deepseek_assist_active:
            ai_stack_line = (
                "  📶 GPT: L1 | L2 | L3 | L5 | HEDGE/ENSEMBLE\n"
                "  🔷 DeepSeek: L4 assist (advisory, timeout ngắn)\n\n"
            )
        else:
            ai_stack_line = (
                "  📶 GPT: L1 | L2 | L3 | L5 | HEDGE/ENSEMBLE\n"
                "  ⚡ DeepSeek: SKIPPED (GPT-only fast mode)\n\n"
            )
    else:
        ai_stack_line = (
            "  📶 GPT: L1 | L2 | L3(5/3) | L5\n"
            "  🔷 DeepSeek: L4x2 Devil | L6 Regime (full thorough)\n\n"
        )
    if bool(getattr(config, "SIMPLE_SCALP_MODE", False)):
        ai_mode_title = "⚡ SIMPLE SCALP MODE\n"
    elif config.AI_SCORE_ONLY_MODE:
        ai_mode_title = "🧮 SCORE-ONLY + MACRO ANALYSIS\n"
    else:
        ai_mode_title = (
            "🧠 AI: FAST3L + MACRO ANALYSIS\n"
            if config.AI_FAST_3L_MODE
            else "🧠 AI: 6-Layer + MACRO ANALYSIS\n"
        )
    partial_tp_line = (
        f"  📈 Partial TP: BE tại {int(config.PARTIAL_TP_BE_RATIO*100)}% TP | "
        f"chốt {int(config.PARTIAL_TP_CLOSE_FRACTION*100)}% tại {int(config.PARTIAL_TP_CLOSE_RATIO*100)}% TP\n"
        if config.PARTIAL_TP_ENABLED
        else "  📈 Partial TP: OFF\n"
    )

    await telegram_handler.send_message(
        app.bot,
        f"🚀 FuBot {'Score-Only' if config.AI_SCORE_ONLY_MODE else 'AI'} Trading Bot v8.0 — TỨ HOÀNG GIÁ RẺ\n"
        f"{'━' * 28}\n\n"
        f"⚙️ Mode: {mode}\n"
        f"🤖 Chế độ: INTRADAY — 5m/15m\n"
        f"🪙 Coin L1: {', '.join(coins_l1) if coins_l1 else 'None'}\n"
        f"🪙 Coin L2: {', '.join(coins_l2) if coins_l2 else 'None'}\n"
        f"🪙 Coin all: {', '.join(coins_all) if coins_all else 'None'}\n"
        f"💰 Vốn/lệnh: {config.TRADE_AMOUNT_USDT} USDT (backup {config.TRADE_AMOUNT_USDT} USDT)\n"
        f"📊 {', '.join(f'{c.split(chr(45))[0]}: TP {config.get_tp(c)}% | SL {config.get_sl(c)}%' for c in coins_all)}\n"
        f"⚡ Leverage: {', '.join(f'{c.split(chr(45))[0]} {config.get_leverage(c)}x' for c in coins_all)}\n"
        f"♾️ Kèo: Không giới hạn | Tối đa {_max_active_trades()} vị thế mở\n"
        f"🎰 Bonus slot: {'ON' if config.BONUS_SLOT_ENABLED else 'OFF'} | "
        f"{config.BONUS_SLOT_PARTIALS_REQUIRED} TP1 đủ chuẩn => +1 slot "
        f"(chỉ cho ~{config.BONUS_SLOT_TARGET_MARGIN_USDT} USDT)\n"
        f"⏳ Cooldown: {config.COOLDOWN_AFTER_TRADE}s | Sau thua: {config.COOLDOWN_AFTER_LOSS}s\n"
        f"🎯 Mục tiêu nhịp: {config.TARGET_TRADES_PER_15M} kèo / 15 phút\n"
        f"🎚️ Score gate: >= {config.AI_SCORE_GATE:.1f}/10\n"
        f"🔍 Scan: mỗi {config.SCAN_INTERVAL}s\n\n"
        f"⚖️ Chiến lược: {'LONG only' if config.LONG_ONLY else 'LONG+SHORT'}\n"
        f"⚙️ AI profile: {'FAST3L (2 GPT + 1 DeepSeek)' if config.AI_FAST_3L_MODE else 'Standard'}\n"
        f"{ai_mode_title}"
        f"  📊 Nến: 5m (entry) + 15m (trend)\n"
        f"  🌍 Macro: Fear/Greed + BTC Dom + MCap\n"
        f"{ai_stack_line}"
        f"{partial_tp_line}"
        f"  🔄 Auto re-scan sau khi đóng vị thế\n\n"
        f"📈 Perf report: mỗi {int(max(300, config.PERF_REPORT_INTERVAL_SEC)/60)} phút\n"
        f"✅ Auto trade khi Score ≥ {config.AI_SCORE_GATE:.1f}/10\n"
        f"/money — check tiền | /end — tắt bot",
    )
    if int(config.TELEGRAM_CONTROL_CHAT_ID) != int(config.TELEGRAM_LOG_CHAT_ID):
        try:
            await app.bot.send_message(
                chat_id=int(config.TELEGRAM_CONTROL_CHAT_ID),
                text=(
                    "CONTROL CHAT READY\n"
                    f"{'-' * 28}\n"
                    "This chat is for control commands only.\n"
                    "Use: /money /dang /dang_1 /dang_1c /end\n"
                    "Trade logs stay in the log chat."
                ),
            )
        except Exception as e:
            logger.warning(f"Cannot send control chat startup message: {e}")

    if recovered:
        await telegram_handler.send_message(
            app.bot,
            f"♻️ Phát hiện {len(recovered)} vị thế từ trước:\n"
            + "\n".join(f"  • {r}" for r in recovered)
            + "\n⏰ Đang monitor..."
        )


# ── main ────────────────────────────────────────────────────
def main():
    """Khởi chạy bot."""
    if not _acquire_instance_lock():
        print("❌ Another FuBot instance is already running (lock active).")
        print("   Stop old process first, then run this bot again.")
        return
    atexit.register(_release_instance_lock)

    print("=" * 50)
    print("  🤖 FuBot Trading System")
    print(f"  Mode: {'DEMO' if config.OKX_DEMO else 'LIVE'}")
    print("=" * 50)

    # Validate config
    if not config.TELEGRAM_TOKEN:
        print("❌ Thiếu TELEGRAM_TOKEN trong .env")
        return
    if not config.OKX_API_KEY:
        print("❌ Thiếu OKX_API_KEY trong .env")
        return

    # Set execute callback
    telegram_handler.set_execute_callback(execute_trade)
    telegram_handler.set_scan_callback(scan_market_once)
    telegram_handler.set_runtime_state_provider(
        lambda: {
            "active_trades": active_trades,
            "okx": okx,
            "prev_oi": prev_oi,
        }
    )

    # Build Telegram Application
    app = Application.builder().token(config.TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_error_handler(telegram_error_handler)

    # Setup handlers
    telegram_handler.setup_handlers(app)

    # Schedule jobs
    job_queue = app.job_queue

    # Scan thị trường mỗi SCAN_INTERVAL giây
    job_queue.run_repeating(
        scan_job,
        interval=config.SCAN_INTERVAL,
        first=15,  # bắt đầu sau 15s
        name="market_scan",
    )

    # Monitor positions mỗi 15s
    job_queue.run_repeating(
        monitor_job,
        interval=config.GUARDIAN_INTERVAL,
        first=config.GUARDIAN_INTERVAL,
        name="position_monitor",
    )

    # Performance report gửi Telegram theo chu kỳ
    job_queue.run_repeating(
        performance_report_job,
        interval=max(300, config.PERF_REPORT_INTERVAL_SEC),
        first=max(300, config.PERF_REPORT_INTERVAL_SEC),
        name="performance_report",
    )

    # Start bot
    logger.info("Starting Telegram polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
