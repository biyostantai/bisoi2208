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
from datetime import datetime, timedelta

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
    analyze_btc_trend,
    generate_signal,
)
from ai_filter import analyze_trade, format_ai_result, save_trade_to_memory
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



# Lock scan to avoid overlapping scheduler/manual/auto-rescan
scan_lock = asyncio.Lock()

# BTC trend cache (refreshed each scan cycle)
btc_trend: dict = {"trend": "neutral", "strength": "neutral",
                    "allow_long": True, "allow_short": True, "detail": "chưa scan"}

# Runtime performance metrics for hourly report
perf_scan_events = deque(maxlen=720)  # {"ts","duration","delay","budget_hit","scanned","total"}
perf_ai_events = deque(maxlen=1200)   # {"ts","latency"}
perf_skip_events = deque(maxlen=1500)  # {"ts","reason"}

_instance_lock_fh = None
_instance_lock_path = ".fubot.instance.lock"


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
    bonus = min(1, partial_tp_count // 2)  # Tối đa +1 slot bonus từ 2 lệnh gốc
    return base + bonus


def _trade_slots_available() -> int:
    return max(0, _max_active_trades() - len(active_trades))


def _recent_trade_count_15m() -> int:
    """Count closed + active trades in the last 15 minutes."""
    now = datetime.now()
    window_start = now - timedelta(minutes=15)
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


def _roundtrip_fee_pct() -> float:
    """Estimated open+close taker fee in % of notional."""
    return max(0.0, float(config.TAKER_FEE_RATE) * 2.0 * 100.0)


def _roundtrip_fee_usdt(symbol: str) -> float:
    """Estimated open+close taker fee in USDT for one trade."""
    notional = float(config.TRADE_AMOUNT_USDT) * float(config.get_leverage(symbol))
    return max(0.0, notional * float(config.TAKER_FEE_RATE) * 2.0)


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
        if config.BTC_TREND_FILTER and candidate_signals:
            btc = btc_trend
            strict = config.BTC_TREND_STRICT
            before_count = len(candidate_signals)
            filtered_btc = []
            kept = []
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
        queued = ranked[:max_ai]
        deferred_items = ranked[max_ai:]
        deferred_by_cap = max(0, len(ranked) - len(queued))
        if deferred_by_cap > 0:
            ai_deferred += deferred_by_cap
            logger.info(
                f"⚡ Ưu tiên AI {layer_name}: xử lý {len(queued)}/{len(ranked)} signal, "
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

        processed_ai = 0
        for idx, (coin, signal) in enumerate(queued, start=1):
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
                            for c, s in left_items
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
                f"🧭 AI ưu tiên {layer_name} {idx}/{len(queued)}: {coin} "
                f"(str={signal.get('strength', 0)})"
            )
            await _process_signal(
                context,
                coin,
                signal,
                scan_deadline=scan_deadline,
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

    # Dừng scan khi đã đầy slot vị thế mở
    if len(active_trades) >= _max_active_trades():
        symbols = ", ".join(active_trades.keys())
        logger.info(
            f"⏸️ Đang giữ lệnh ({symbols}) "
            f"[{len(active_trades)}/{_max_active_trades()}] - chờ đóng bớt rồi scan tiếp"
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

        # Fetch BTC trend filter
        global btc_trend
        if config.BTC_TREND_FILTER:
            try:
                btc_5m = okx.get_candles("BTC-USDT-SWAP", bar="5m", limit=50)
                btc_15m = okx.get_candles("BTC-USDT-SWAP", bar="15m", limit=50)
                btc_trend = analyze_btc_trend(btc_5m, btc_15m)
                logger.info(
                    f"📊 BTC Trend: {btc_trend['trend']} ({btc_trend['strength']}) "
                    f"— {btc_trend['detail']}"
                )
            except Exception as e:
                logger.warning(f"⚠️ Lỗi fetch BTC trend: {e} — cho phép cả 2 hướng")
                btc_trend = {"trend": "neutral", "strength": "neutral",
                             "allow_long": True, "allow_short": True,
                             "detail": f"Lỗi fetch: {e}"}

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
        # 1. Lấy dữ liệu thị trường
        data = okx.get_market_data(coin)
        if data["price"] == 0:
            logger.warning(f"{coin}: Không lấy được giá")
            return None

        # 2. Tính indicators
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

        # 3. Rule engine (đưa candle filter vào)
        signal = generate_signal(cvd_data, oi_data, ob_data, funding_data, candle_data)

        # Thêm indicator data vào signal
        signal["indicators"] = {
            "cvd_bias": cvd_data["bias"],
            "cvd_value": cvd_data["cvd"],
            "buy_vol": cvd_data["buy_vol"],
            "sell_vol": cvd_data["sell_vol"],
            "imbalance": ob_data["imbalance"],
            "ob_bias": ob_data["bias"],
            "funding_rate": data["funding_rate"],
            "funding_pct": funding_data["funding_pct"],
            "funding_signal": funding_data["signal"],
            "oi_change_pct": oi_data["oi_change_pct"],
            "oi_signal": oi_data["signal"],
            "price": data["price"],
            # Candle trend data
            "trend_5m": candle_data["tf_5m"]["trend"],
            "trend_15m": candle_data["tf_15m"]["trend"],
            "ema_bias_5m": candle_data["tf_5m"]["ema_bias"],
            "ema_bias_15m": candle_data["tf_15m"]["ema_bias"],
            "candle_overall": candle_data["overall"],
            "candle_aligned": candle_data["aligned"],
            # Micro setup (M1/M5) for pair-specific playbook scoring
            "m1_ema21": micro_data.get("m1_ema21", 0.0),
            "m1_price_vs_ema21": micro_data.get("m1_price_vs_ema21", "unknown"),
            "m1_breakout_up": micro_data.get("m1_breakout_up", False),
            "m1_breakout_down": micro_data.get("m1_breakout_down", False),
            "m1_max_wick_pct_10": micro_data.get("m1_max_wick_pct_10", 0.0),
            "m1_last_upper_wick_pct": micro_data.get("m1_last_upper_wick_pct", 0.0),
            "m1_last_lower_wick_pct": micro_data.get("m1_last_lower_wick_pct", 0.0),
            "m1_bull_engulfing": micro_data.get("m1_bull_engulfing", False),
            "m1_bear_engulfing": micro_data.get("m1_bear_engulfing", False),
            "m1_rejection_signal": micro_data.get("m1_rejection_signal", "none"),
            "m1_volume_surge_pct": micro_data.get("m1_volume_surge_pct", 0.0),
            "m1_bb_position": micro_data.get("m1_bb_position", "mid"),
            "m1_bb_touch_upper": micro_data.get("m1_bb_touch_upper", False),
            "m1_bb_touch_lower": micro_data.get("m1_bb_touch_lower", False),
            "m5_touch_resistance": micro_data.get("m5_touch_resistance", False),
            "m5_touch_support": micro_data.get("m5_touch_support", False),
            "m5_order_block_bias": micro_data.get("m5_order_block_bias", "unknown"),
            "m5_order_block_near": micro_data.get("m5_order_block_near", False),
        }

        return signal

    except Exception as e:
        logger.error(f"Lỗi analyze {coin}: {e}")
        return None


async def _process_signal(context: ContextTypes.DEFAULT_TYPE,
                          coin: str, signal: dict,
                          scan_deadline: float | None = None):
    """Xử lý signal: gọi AI → auto trade nếu score đạt ngưỡng."""

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

    indicators = signal["indicators"]
    direction = signal["direction"]
    price = indicators["price"]

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

    if config.LONG_ONLY and direction != "LONG":
        logger.info(f"⚖️ LONG_ONLY: bỏ signal {coin} {direction}")
        _record_skip("short_blocked")
        return

    score_gate = float(config.AI_SCORE_GATE)

    # 1. Gọi AI analysis
    ai_profile_label = "FAST3L" if config.AI_FAST_3L_MODE else "6-Layer"
    logger.info(f"🧠 Gọi AI {ai_profile_label} phân tích {coin} {direction}...")
    try:
        ai_budget = float(config.AI_TIME_BUDGET_SEC)
        if scan_deadline is not None:
            remaining = scan_deadline - time.monotonic()
            if remaining < config.AI_MIN_BUDGET_TO_START_SEC:
                logger.info(
                    f"⏱️ Bỏ qua AI {coin}: budget còn {remaining:.1f}s "
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

        ai_result = await asyncio.to_thread(
            analyze_trade,
            coin,
            direction,
            indicators,
            ai_budget,
            score_gate,
        )
        ai_time = float(ai_result.get("total_ai_time", 0) or 0)
        _record_ai_latency(ai_time)
        playbook_score = float(ai_result.get("playbook_score", 0) or 0)
        logger.info(f"AI {ai_profile_label}: {ai_result.get('decision')} "
                     f"(score={playbook_score:.1f}/10, gate={score_gate:.1f}/10, "
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
                f"⏱️ AI overrun {coin}: {ai_time:.2f}s > budget {ai_budget:.2f}s"
            )
            _record_skip("ai_budget_overrun")
    except Exception as e:
        logger.error(f"Lỗi AI: {e}")
        _record_skip("ai_exception")
        ai_result = {
            "decision": "SKIP",
            "confidence": 0,
            "direction": direction,
            "win_probability": 0,
            "playbook_score": 0,
            "risk_level": "HIGH",
            "reasoning": f"AI không hoạt động: {e}",
            "error": True,
        }

    # 2. Kiểm tra score gate (cơ chế chính)
    playbook_score = float(ai_result.get("playbook_score", 0) or 0)
    ai_decision = str(ai_result.get("decision", "SKIP")).upper()
    if ai_result.get("error"):
        logger.info(f"AI bị lỗi — SKIP {coin}")
        _record_skip("ai_error")
        await telegram_handler.send_message(
            context.bot,
            f"❌ AI lỗi — SKIP {coin} {direction}\n"
            f"Lý do: {ai_result.get('reasoning', 'N/A')}"
        )
        return

    if playbook_score < score_gate:
        logger.info(
            f"Score {playbook_score:.1f}/10 < {score_gate:.1f}/10 — SKIP {coin}"
        )
        _record_skip("score_below_threshold")
        await telegram_handler.send_message(
            context.bot,
            f"❌ SKIP {coin} {direction}\n"
            f"Score: {playbook_score:.1f}/10 < {score_gate:.1f}/10\n"
            f"Lý do: {ai_result.get('reasoning', 'N/A')}"
        )
        return

    if ai_decision != "TRADE":
        l4_info = f"L4={ai_result.get('l4_verdict','N/A')}"
        l5_info = f"L5={'GO' if ai_result.get('l5_execute') else 'NO'}"
        l6_info = f"L6={ai_result.get('l6_verdict','N/A')}"
        logger.info(
            f"AI decision={ai_decision} dù score {playbook_score:.1f}/10 đạt ngưỡng — SKIP {coin} "
            f"({l4_info} {l5_info} {l6_info})"
        )
        _record_skip("ai_decision_skip")
        await telegram_handler.send_message(
            context.bot,
            f"❌ SKIP {coin} {direction}\n"
            f"Score: {playbook_score:.1f}/10 (đạt ngưỡng {score_gate:.1f})\n"
            f"AI: {ai_decision} | {l4_info} | {l5_info} | {l6_info}\n"
            f"Trap: {ai_result.get('l4_trap_detected', False)} | "
            f"Regime: {ai_result.get('l6_regime', 'N/A')}\n"
            f"Lý do: {ai_result.get('reasoning', 'N/A')}"
        )
        return

    # 3. Tính TP/SL
    tp, sl = okx.calc_tp_sl_prices(price, direction,
                                    config.get_tp(coin), config.get_sl(coin))

    # 4. Tính position size
    size_str, pos_info = okx.calc_position_size(
        coin, config.TRADE_AMOUNT_USDT, config.get_leverage(coin)
    )
    logger.info(
        f"📐 Size {coin}: target={config.TRADE_AMOUNT_USDT:.2f} USDT | "
        f"actual={pos_info.get('actual_margin_usdt', 'N/A')} USDT | "
        f"mode={pos_info.get('sizing_mode', 'legacy')}"
    )

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

    # 5. Tạo signal object
    signal_id = str(uuid.uuid4())[:8]
    full_signal = {
        "id": signal_id,
        "symbol": coin,
        "direction": direction,
        "price": price,
        "tp": tp,
        "sl": sl,
        "size": size_str,
        "indicators": indicators,
        "ai_result": ai_result,
        "position_info": pos_info,
        "timestamp": datetime.now().isoformat(),
    }

    # 6. AUTO TRADE: score đã đạt ngưỡng ở bước 2
    if config.AUTO_TRADE:
        logger.info(
            f"🚀 AUTO TRADE: {coin} {direction} "
            f"(score={playbook_score:.1f}/10 >= {score_gate:.1f}/10)"
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
            f"Score: {playbook_score:.1f}/10 (≥{score_gate:.1f} ✅)\n"
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
    entry_price = signal["price"]

    # Guard slot/symbol trước khi gọi API đặt lệnh
    if symbol in active_trades:
        return {"success": False, "error": f"{symbol} đang có vị thế mở"}
    if _trade_slots_available() <= 0:
        return {
            "success": False,
            "error": f"Đã đầy slot lệnh ({len(active_trades)}/{_max_active_trades()})",
        }

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
                        "size": str(abs(pos_size)),
                        "be_done": False,
                        "tp1_done": False,
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
            tp = round(entry_price * (1 + config.get_tp(symbol) / 100), 2)
            sl = round(entry_price * (1 - config.get_sl(symbol) / 100), 2)
            signal["tp"] = tp
            signal["sl"] = sl
            logger.info(f"✅ TP/SL đã sửa: TP={tp} (>{entry_price}), SL={sl} (<{entry_price})")
    elif direction == "SHORT":
        if tp >= entry_price or sl <= entry_price:
            logger.warning(f"⚠️ TP/SL bị sai hướng cho SHORT! tp={tp}, sl={sl}, entry={entry_price}")
            logger.warning(f"⚠️ Tính lại TP/SL từ entry price...")
            tp = round(entry_price * (1 - config.get_tp(symbol) / 100), 2)
            sl = round(entry_price * (1 + config.get_sl(symbol) / 100), 2)
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
                    if real_entry > 0 and real_entry != entry_price:
                        logger.info(f"📍 Giá fill thực: {real_entry} (signal: {entry_price})")
                        tp, sl = okx.calc_tp_sl_prices(
                            real_entry, direction,
                            config.get_tp(symbol), config.get_sl(symbol)
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
            "size": size,
            "be_done": False,
            "tp1_done": False,
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
    Chạy mỗi 15 giây.
    """
    global active_trades, partial_tp_count, coin_cooldowns

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
                    "size": str(abs(pos_size)),
                    "be_done": False,
                    "tp1_done": False,
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
                first_check_sec = 15.0

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

                    # Size đang mở thực tế
                    current_pos_size = 0.0
                    try:
                        if open_position is not None:
                            current_pos_size = abs(float(open_position.get("pos", "0")))
                    except Exception:
                        current_pos_size = 0.0

                    # Chốt lời từng phần:
                    # - BE: dời SL về entry khi lãi đạt BE_RATIO × TP
                    # - TP1: chốt 50% khi lãi đạt CLOSE_RATIO × TP, giữ phần còn lại
                    if (
                        config.PARTIAL_TP_ENABLED
                        and current_pos_size > 0
                        and elapsed_sec >= max(first_check_sec, float(config.PARTIAL_TP_MIN_HOLD_SEC))
                    ):
                        target_pnl_margin = float(config.get_tp(symbol)) * leverage
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
                                    if not trade.get("is_bonus", False):
                                        partial_tp_count += 1
                                    logger.info(
                                        f"💰🛡️ BE+TP1 {symbol}: close {part_size} + SL->entry "
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
                                                sl_price=float(entry_price),
                                            )
                                    else:
                                        has_position = False
                                    bonus_msg = ""
                                    if partial_tp_count % 2 == 0 and partial_tp_count > 0:
                                        bonus_msg = f"\n🎰 Vòng {1 + partial_tp_count // 2}: +1 slot mới! (max={_max_active_trades()})"
                                    await telegram_handler.send_message(
                                        context.bot,
                                        f"💰🛡️ {symbol}: chốt 50% + dời SL về entry\n"
                                        f"PnL margin: {cur_pnl_margin:+.2f}%\n"
                                        f"Phần còn lại chạy tới TP cuối.{bonus_msg}"
                                    )
                                else:
                                    logger.warning(f"Partial TP close failed {symbol}: {part_result}")
                            else:
                                trade["tp1_done"] = True
                                trade["be_done"] = True
                                logger.info(f"ℹ️ Partial TP skip {symbol}: size quá nhỏ.")
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
                                        sl_price=float(entry_price),
                                    )
                                    if be_ok:
                                        trade["be_done"] = True
                                        logger.info(
                                            f"🛡️ BE armed {symbol}: margin_pnl={cur_pnl_margin:+.2f}% "
                                            f">= {be_trigger_margin:.2f}% | SL -> entry"
                                        )
                                        await telegram_handler.send_message(
                                            context.bot,
                                            f"🛡️ {symbol}: dời SL về entry (hòa vốn)\n"
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
                                        if not trade.get("is_bonus", False):
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
                                                    float(entry_price)
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
                                    logger.info(
                                        f"ℹ️ Partial TP skip {symbol}: size quá nhỏ để chốt 50%."
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
                capital.record_trade(
                    symbol, direction, entry,
                    exit_price, pnl_usdt, pnl_pct,
                )

                # Per-coin cooldown: coin thua → skip coin đó N giây
                if pnl_usdt < 0 and config.COIN_COOLDOWN_AFTER_LOSS > 0:
                    coin_cooldowns[symbol] = time.time() + config.COIN_COOLDOWN_AFTER_LOSS
                    logger.info(
                        f"⏳ {symbol} cooldown {config.COIN_COOLDOWN_AFTER_LOSS}s "
                        f"sau SL (tránh vào lại liên tục)"
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
                "size": str(abs(pos_size)),
                "be_done": False,
                "tp1_done": False,
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
    if config.AI_FAST_3L_MODE:
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
    if config.AI_FAST_3L_MODE:
        logger.info("AI profile: FAST3L (2 GPT + 1 DeepSeek: L1+L2+L4)")
    if config.LONG_ONLY:
        logger.info("Strategy: LONG_ONLY scalp mode")
    logger.info(f"Scan interval: {config.SCAN_INTERVAL}s")
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
        f"Score gate={config.AI_SCORE_GATE:.1f}/10 | "
        f"AI topN/layer={config.AI_MAX_SIGNALS_PER_LAYER} | "
        f"LONG_ONLY={'on' if config.LONG_ONLY else 'off'} | "
        f"FAST3L={'on' if config.AI_FAST_3L_MODE else 'off'} | "
        f"Max active={_max_active_trades()} | "
        f"Cadence target={config.TARGET_TRADES_PER_15M}/15m | "
        f"Score-only={'on' if config.AI_SCORE_ONLY_MODE else 'off'} | "
        f"Scan budget={_calc_scan_budget_sec():.1f}s"
    )

    # Recover vị thế đang mở (nếu bot vừa restart)
    recovered = await recover_open_positions()
    if config.AI_FAST_3L_MODE:
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
        f"🚀 FuBot AI Trading Bot v8.0 — TỨ HOÀNG GIÁ RẺ\n"
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
        interval=15,
        first=15,
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

