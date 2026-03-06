# -*- coding: utf-8 -*-
"""
telegram_handler.py â€” Telegram Bot Handler

Chá»©c nÄƒng:
- Gá»­i signal vá»›i nĂºt báº¥m EXECUTE / SKIP
- Nháº­n callback khi user chá»‘t kĂ¨o
- Gá»­i káº¿t quáº£ trade
- Gá»­i thá»‘ng kĂª ngĂ y
"""

import logging
import asyncio
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import config

logger = logging.getLogger("fubot.telegram")

# LÆ°u pending signals (signal_id â†’ signal_data)
pending_signals: dict[str, dict] = {}

# Callback function khi user chá»‘t kĂ¨o (Ä‘Æ°á»£c set tá»« main.py)
_execute_callback = None
_scan_callback = None
_runtime_state_provider = None


def set_execute_callback(callback):
    """Set callback function khi user cháº¥p nháº­n trade."""
    global _execute_callback
    _execute_callback = callback


def set_scan_callback(callback):
    """Set callback function cho manual /scan."""
    global _scan_callback
    _scan_callback = callback


def set_runtime_state_provider(provider):
    """
    Set provider trả về dict runtime state từ main:
    {
      "active_trades": dict,
      "okx": OKXClient,
      "prev_oi": dict
    }
    """
    global _runtime_state_provider
    _runtime_state_provider = provider


def _log_chat_id() -> int:
    return int(config.TELEGRAM_LOG_CHAT_ID or config.TELEGRAM_CHAT_ID)


def _control_chat_id() -> int:
    return int(config.TELEGRAM_CONTROL_CHAT_ID or config.TELEGRAM_CHAT_ID)


def _allowed_control_chat_ids() -> set[int]:
    ids = {int(config.TELEGRAM_CONTROL_CHAT_ID or config.TELEGRAM_CHAT_ID)}
    if int(config.TELEGRAM_LOG_CHAT_ID or config.TELEGRAM_CHAT_ID):
        ids.add(int(config.TELEGRAM_LOG_CHAT_ID or config.TELEGRAM_CHAT_ID))
    return ids


def _is_control_chat(update: Update) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    return int(chat.id) in _allowed_control_chat_ids()


async def _guard_control_chat(update: Update) -> bool:
    """Return True náº¿u command khĂ´ng á»Ÿ control chat (Ä‘Ă£ cháº·n)."""
    if _is_control_chat(update):
        return False
    chat = update.effective_chat
    if chat:
        logger.info(f"Ignored control command from non-control chat: {chat.id}")
        try:
            await chat.send_message(
                text=(
                    "This command is not enabled in this chat.\n"
                    f"Use control chat id: {_control_chat_id()}."
                )
            )
        except Exception:
            pass
    return True


def _active_trade_items() -> list[tuple[str, dict]]:
    if not callable(_runtime_state_provider):
        return []
    state = _runtime_state_provider() or {}
    active_trades = state.get("active_trades", {})
    if not isinstance(active_trades, dict):
        return []
    items = list(active_trades.items())
    items.sort(key=lambda kv: kv[1].get("timestamp", ""))
    return items


def _effective_text(update: Update) -> str:
    msg = update.effective_message
    if not msg:
        return ""
    return (msg.text or msg.caption or "").strip()


async def _reply(update: Update, text: str):
    msg = update.effective_message
    if msg:
        try:
            await msg.reply_text(text)
            return
        except Exception as e:
            logger.warning(f"reply_text failed, fallback to send_message: {e}")

    chat = update.effective_chat
    if chat:
        await chat.send_message(text=text)
        return

    logger.warning("Cannot reply: no effective_message/effective_chat")


# â”€â”€ commands â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _guard_control_chat(update):
        return

    """Handler cho /start."""
    await _reply(
        update,
        "FuBot control is ready.\n\n"
        "Commands:\n"
        "/status - daily stats\n"
        "/balance - account balance\n"
        "/money - account snapshot\n"
        "/positions - open positions\n"
        "/dang - active trades list\n"
        "/scan - run scan now\n"
        "/stop - stop command\n"
        "/end - shutdown bot\n"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _guard_control_chat(update):
        return

    """Handler cho /status â€” hiá»‡n thá»‘ng kĂª ngĂ y."""
    from capital_manager import CapitalManager
    cm = CapitalManager()
    await _reply(update, cm.stats_text())


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _guard_control_chat(update):
        return

    """Handler for /balance."""
    from okx_client import OKXClient
    try:
        client = OKXClient()
        bal = client.get_balance()
        details = bal.get("data", [{}])[0].get("details", [])

        text = "OKX BALANCE\n" + ("-" * 28) + "\n"
        for d in details:
            ccy = d.get("ccy", "")
            avail = d.get("availBal", "0")
            eq = d.get("eq", "0")
            if float(eq) > 0:
                text += f"{ccy}: {float(eq):.4f} (available: {float(avail):.4f})\n"

        if not details:
            text += "No balance data\n"

        demo_tag = " [DEMO]" if config.OKX_DEMO else " [LIVE]"
        text += f"\nMode: {demo_tag}"

        await _reply(update, text)
    except Exception as e:
        await _reply(update, f"Balance error: {e}")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _guard_control_chat(update):
        return

    """Handler for /positions."""
    from okx_client import OKXClient
    try:
        client = OKXClient()
        positions = client.get_positions()

        if not positions:
            await _reply(update, "No open positions.")
            return

        text = "OPEN POSITIONS\n" + ("-" * 28) + "\n"
        for p in positions:
            inst = p.get("instId", "")
            pos_side = p.get("posSide", "")
            sz = p.get("pos", "0")
            upl = float(p.get("upl", "0"))
            lever = p.get("lever", "1")
            emoji = "+" if upl >= 0 else "-"
            text += (
                f"{emoji} {inst}\n"
                f"   Side: {pos_side} | Size: {sz} | Lever: {lever}x\n"
                f"   PnL: {upl:+.4f} USDT\n\n"
            )

        await _reply(update, text)
    except Exception as e:
        await _reply(update, f"Positions error: {e}")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _guard_control_chat(update):
        return

    """Handler for /scan."""
    await _reply(update, "Scanning market now...")

    # Trigger scan job
    if context.job_queue:
        context.job_queue.run_once(
            _trigger_scan_job, when=0, name="manual_scan"
        )


async def _trigger_scan_job(context: ContextTypes.DEFAULT_TYPE):
    """Job wrapper cho scan."""
    if callable(_scan_callback):
        await _scan_callback(context)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _guard_control_chat(update):
        return

    """Handler cho /stop."""
    await _reply(update, "Stop command received.")


async def cmd_money(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _guard_control_chat(update):
        return

    """Handler for /money - account snapshot."""
    from okx_client import OKXClient
    try:
        client = OKXClient()
        bal = client.get_balance()
        details = bal.get("data", [{}])[0].get("details", [])
        total_eq = bal.get("data", [{}])[0].get("totalEq", "0")

        mode = "LIVE" if not config.OKX_DEMO else "DEMO"

        text = (
            f"OKX ACCOUNT - {mode}\n"
            f"{'-' * 28}\n\n"
            f"Total equity: {float(total_eq):.4f} USDT\n\n"
        )

        for d in details:
            ccy = d.get("ccy", "")
            avail = float(d.get("availBal", "0"))
            eq = float(d.get("eq", "0"))
            frozen = float(d.get("frozenBal", "0"))
            upl = float(d.get("upl", "0"))
            if eq > 0:
                text += (
                    f"{ccy}:\n"
                    f"   Total: {eq:.4f}\n"
                    f"   Available: {avail:.4f}\n"
                    f"   Frozen: {frozen:.4f}\n"
                )
                if upl != 0:
                    text += f"   Unrealized PnL: {upl:+.4f}\n"
                text += "\n"

        if not details:
            text += "No balance data\n"

        # ThĂªm thá»‘ng kĂª ngĂ y
        from capital_manager import CapitalManager
        cm = CapitalManager()
        text += f"{'-' * 28}\n{cm.stats_text()}"

        await _reply(update, text)
    except Exception as e:
        await _reply(update, f"Money check error: {e}")


async def cmd_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler cho /end â€” táº¯t bot hoĂ n toĂ n tá»« xa."""
    if await _guard_control_chat(update):
        return

    await _reply(
        update,
        "BOT IS SHUTTING DOWN...\n\n"
        "All scan/monitor jobs are stopped.\n"
        "Process will exit in 3 seconds."
    )
    logger.info("/end command received - shutting down bot...")

    # Dá»«ng táº¥t cáº£ jobs
    for job in context.job_queue.jobs():
        job.schedule_removal()

    # Táº¯t bot sau 2 giĂ¢y (Ä‘á»ƒ message ká»‹p gá»­i)
    import os
    import threading
    threading.Timer(2.0, lambda: os._exit(0)).start()


async def _build_live_indicators(symbol: str) -> tuple[float, dict]:
    """Lấy giá + indicators realtime để đánh giá vị thế đang chạy."""
    if not callable(_runtime_state_provider):
        raise RuntimeError("Runtime state provider chưa được setup")
    state = _runtime_state_provider() or {}
    okx = state.get("okx")
    prev_oi = state.get("prev_oi")
    if okx is None or prev_oi is None:
        raise RuntimeError("Runtime state thiếu okx/prev_oi")
    from indicators import (
        calc_cvd,
        calc_oi_change,
        calc_orderbook_imbalance,
        analyze_funding_rate,
        analyze_candles,
    )

    market_data = await asyncio.to_thread(okx.get_market_data, symbol)
    current_price = float(market_data.get("price", 0) or 0)
    cvd_data = calc_cvd(market_data["trades"])
    ob_data = calc_orderbook_imbalance(market_data["bids"], market_data["asks"])
    funding_data = analyze_funding_rate(market_data["funding_rate"])
    candle_data = analyze_candles(
        market_data["candles_5m"], market_data["candles_15m"]
    )
    current_oi = market_data["open_interest"]
    prev_value = prev_oi.get(symbol, current_oi)
    oi_data = calc_oi_change(current_oi, prev_value)
    prev_oi[symbol] = current_oi

    indicators = {
        "cvd_bias": cvd_data["bias"],
        "cvd_value": cvd_data["cvd"],
        "buy_vol": cvd_data["buy_vol"],
        "sell_vol": cvd_data["sell_vol"],
        "imbalance": ob_data["imbalance"],
        "ob_bias": ob_data["bias"],
        "funding_rate": market_data["funding_rate"],
        "funding_pct": funding_data["funding_pct"],
        "oi_change_pct": oi_data["oi_change_pct"],
        "trend_5m": candle_data["tf_5m"]["trend"],
        "trend_15m": candle_data["tf_15m"]["trend"],
        "ema_bias_5m": candle_data["tf_5m"]["ema_bias"],
        "ema_bias_15m": candle_data["tf_15m"]["ema_bias"],
        "candle_overall": candle_data["overall"],
        "candle_aligned": candle_data["aligned"],
    }
    return current_price, indicators


async def cmd_dang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List active trades in runtime."""
    if await _guard_control_chat(update):
        return

    items = _active_trade_items()
    if not items:
        await _reply(update, "No active trades right now.")
        return

    if not callable(_runtime_state_provider):
        await _reply(update, "Runtime state not ready.")
        return
    state = _runtime_state_provider() or {}
    okx = state.get("okx")
    if okx is None:
        await _reply(update, "Runtime state missing OKX client.")
        return
    lines = ["Active trades:", "-" * 24]
    for idx, (symbol, trade) in enumerate(items, start=1):
        direction = trade.get("direction", "N/A")
        entry = float(trade.get("entry_price", 0) or 0)
        now_price = entry
        try:
            ticker = await asyncio.to_thread(okx.get_ticker, symbol)
            now_price = float(ticker.get("last", entry) or entry)
        except Exception:
            pass

        pnl_pct = 0.0
        if entry > 0:
            if direction == "LONG":
                pnl_pct = (now_price - entry) / entry * 100
            else:
                pnl_pct = (entry - now_price) / entry * 100
        pnl_margin = pnl_pct * float(config.get_leverage(symbol))

        lines.append(
            f"{idx}. {symbol} {direction} | PnL {pnl_pct:+.3f}% ({pnl_margin:+.2f}% margin)"
        )
        lines.append(f"   /dang_{idx} = AI check | /dang_{idx}c = close now")

    await _reply(update, "\n".join(lines))


async def cmd_dang_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /dang_N  -> AI check trade N
    /dang_Nc -> close trade N immediately
    """
    if await _guard_control_chat(update):
        return

    text = _effective_text(update).lower()
    m = re.match(r"^/dang_(\d+)(c?)(?:@\w+)?$", text)
    if not m:
        return

    idx = int(m.group(1))
    do_close = bool(m.group(2))

    items = _active_trade_items()
    if idx <= 0 or idx > len(items):
        await _reply(update, "Invalid trade index. Use /dang first.")
        return

    symbol, trade = items[idx - 1]
    direction = trade.get("direction", "LONG")
    if not callable(_runtime_state_provider):
        await _reply(update, "Runtime state not ready.")
        return
    state = _runtime_state_provider() or {}
    okx = state.get("okx")
    if okx is None:
        await _reply(update, "Runtime state missing OKX client.")
        return

    if do_close:
        close_result = await asyncio.to_thread(okx.close_position, symbol)
        code = close_result.get("code", "N/A")
        msg = close_result.get("msg", "")
        await _reply(
            update,
            f"Manual close sent for {symbol} ({direction}).\n"
            f"Code: {code} {msg}"
        )
        logger.info(f"Manual close by /dang_{idx}c: {symbol} -> {close_result}")
        return

    await _reply(update, f"Checking trade #{idx}: {symbol}...")

    try:
        entry = float(trade.get("entry_price", 0) or 0)
        tp = float(trade.get("tp", 0) or 0)
        sl = float(trade.get("sl", 0) or 0)
        timestamp = trade.get("timestamp")
        elapsed_min = 0.0
        if timestamp:
            from datetime import datetime

            elapsed_min = max(
                0.0, (datetime.now() - datetime.fromisoformat(timestamp)).total_seconds() / 60.0
            )

        current_price, live_indicators = await _build_live_indicators(symbol)

        if direction == "LONG":
            cur_pnl = (current_price - entry) / entry * 100 if entry > 0 else 0.0
        else:
            cur_pnl = (entry - current_price) / entry * 100 if entry > 0 else 0.0
        cur_pnl_margin = cur_pnl * float(config.get_leverage(symbol))

        dist_tp = abs(tp - current_price) / current_price * 100 if current_price > 0 else 0
        dist_sl = abs(current_price - sl) / current_price * 100 if current_price > 0 else 0

        await _reply(
            update,
            f"CHECK #{idx} - {symbol} {direction}\n"
            f"{'-' * 24}\n"
            f"Entry: {entry:,.6f}\n"
            f"Current: {current_price:,.6f}\n"
            f"PnL: {cur_pnl:+.3f}% ({cur_pnl_margin:+.2f}% margin)\n"
            f"TP: {tp:,.6f} (dist: {dist_tp:.3f}%)\n"
            f"SL: {sl:,.6f} (dist: {dist_sl:.3f}%)\n"
            f"Time open: {elapsed_min:.1f} min\n"
            f"CVD: {live_indicators.get('cvd_bias', 'N/A')}\n"
            f"OB: {live_indicators.get('ob_bias', 'N/A')}\n"
            f"Trend 5m: {live_indicators.get('trend_5m', 'N/A')}"
        )
    except Exception as e:
        logger.error(f"/dang_{idx} check error {symbol}: {e}")
        await _reply(update, f"Check error for trade #{idx}: {e}")


async def channel_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback router so channel posts can trigger control commands reliably."""
    text = _effective_text(update).lower()
    if not text.startswith("/"):
        return
    cmd = text.split()[0]
    cmd = cmd.split("@", 1)[0]
    chat = update.effective_chat
    logger.info(f"Channel command received: chat={getattr(chat, 'id', 'N/A')} cmd={cmd}")

    if cmd in ("/start", "/help"):
        await cmd_start(update, context)
    elif cmd == "/status":
        await cmd_status(update, context)
    elif cmd == "/balance":
        await cmd_balance(update, context)
    elif cmd == "/money":
        await cmd_money(update, context)
    elif cmd == "/positions":
        await cmd_positions(update, context)
    elif cmd == "/scan":
        await cmd_scan(update, context)
    elif cmd == "/stop":
        await cmd_stop(update, context)
    elif cmd == "/end":
        await cmd_end(update, context)
    elif cmd == "/dang":
        await cmd_dang(update, context)
    elif re.match(r"^/dang_\d+c?$", cmd):
        await cmd_dang_select(update, context)


# â”€â”€ gá»­i signal â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async def send_signal(app_or_bot, signal: dict):
    """
    Gá»­i signal tá»›i Telegram channel vá»›i nĂºt báº¥m.

    signal = {
        "id": str,
        "symbol": str,
        "direction": str,
        "price": float,
        "tp": float,
        "sl": float,
        "indicators": dict,
        "ai_result": dict,
        "position_info": dict,
    }
    """
    signal_id = signal["id"]
    pending_signals[signal_id] = signal

    direction_emoji = "LONG" if signal["direction"] == "LONG" else "SHORT"
    ai = signal.get("ai_result", {})
    playbook_score = float(ai.get("playbook_score", 0) or 0)
    confidence = ai.get("confidence", 0)
    risk = ai.get("risk_level", "N/A")
    reasoning = ai.get("reasoning", "")

    risk_map = {"LOW": "LOW", "MEDIUM": "MEDIUM", "HIGH": "HIGH", "EXTREME": "EXTREME"}
    risk_text = risk_map.get(risk, risk)

    grade_map = {"A": "A", "B": "B", "C": "C", "D": "D"}
    grade = grade_map.get(ai.get("entry_quality", "D"), "D")

    # TĂ­nh TP/SL %
    tp_pct = config.get_tp(signal['symbol'])
    sl_pct = config.get_sl(signal['symbol'])

    # Indicators summary
    ind = signal.get("indicators", {})
    ind_text = (
        f"- CVD: {ind.get('cvd_bias', 'N/A')} ({ind.get('cvd_value', 0):.0f})\n"
        f"- OB Imbalance: {ind.get('imbalance', 0):.2f} ({ind.get('ob_bias', 'N/A')})\n"
        f"- Funding: {ind.get('funding_pct', 0):.4f}% ({ind.get('funding_signal', 'N/A')})\n"
        f"- OI Change: {ind.get('oi_change_pct', 0):.2f}% ({ind.get('oi_signal', 'N/A')})"
    )

    # AI 3-Layer summary
    risk_factors = ai.get("risk_factors", [])
    ai_text = (
        f"L1 Analyst: {ai.get('analyst_bias', 'N/A')} "
        f"({ai.get('analyst_strength', '?')})\n"
        f"  Long {ai.get('analyst_prob_long', 0)}% | Short {ai.get('analyst_prob_short', 0)}%\n"
        f"L2 Risk: Score {ai.get('risk_score', '?')}/100 | "
        f"Squeeze {ai.get('squeeze_risk', '?')}%\n"
        f"  {'; '.join(risk_factors[:2]) if risk_factors else 'No warning'}\n"
        f"L3 Verdict: {ai.get('decision', 'N/A')} | "
        f"Grade: {grade}"
    )

    pos_info = signal.get("position_info", {})

    text = (
        f"SIGNAL - AI 3-LAYER\n"
        f"{'-' * 28}\n\n"
        f"{signal['symbol']}\n"
        f"Direction: {direction_emoji}\n"
        f"Price: {signal['price']:,.6f}\n\n"
        f"Indicators:\n{ind_text}\n\n"
        f"AI Analysis:\n{ai_text}\n\n"
        f"Score: {playbook_score:.1f}/10 | Gate: {config.AI_SCORE_GATE:.1f}/10 | Conf: {confidence}%\n"
        f"Risk: {risk_text}\n"
        f"Note: {reasoning}\n\n"
        f"Trade Plan:\n"
        f"- Entry: {signal['price']:,.6f}\n"
        f"- TP: {signal['tp']:,.6f} (+{tp_pct}%)\n"
        f"- SL: {signal['sl']:,.6f} (-{sl_pct}%)\n"
        f"- Size: {pos_info.get('contracts', 'N/A')} contracts\n"
        f"- Leverage: {config.get_leverage(signal['symbol'])}x\n"
        f"{'-' * 28}\n"
        f"Choose action:"
    )

    # Inline buttons
    keyboard = [
        [
            InlineKeyboardButton(
                f"OK {signal['direction']}",
                callback_data=f"trade_{signal_id}",
            ),
            InlineKeyboardButton(
                "SKIP",
                callback_data=f"skip_{signal_id}",
            ),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Gá»­i message
    try:
        # Há»— trá»£ cáº£ Application bot vĂ  Bot object
        bot = getattr(app_or_bot, "bot", app_or_bot)
        await bot.send_message(
            chat_id=_log_chat_id(),
            text=text,
            reply_markup=reply_markup,
        )
        logger.info(f"Sent signal {signal_id} to Telegram")
    except Exception as e:
        logger.error(f"Send signal Telegram error: {e}")


# â”€â”€ auto trade notification â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async def send_auto_trade_signal(bot, signal: dict):
    """Gá»­i thĂ´ng bĂ¡o AUTO TRADE (bot tá»± vĂ o kĂ¨o, khĂ´ng cáº§n báº¥m nĂºt)."""
    ai = signal.get("ai_result", {})
    direction_emoji = "LONG" if signal["direction"] == "LONG" else "SHORT"
    playbook_score = float(ai.get("playbook_score", 0) or 0)
    confidence = ai.get("confidence", 0)
    risk = ai.get("risk_level", "N/A")

    risk_map = {"LOW": "LOW", "MEDIUM": "MEDIUM", "HIGH": "HIGH", "EXTREME": "EXTREME"}
    risk_text = risk_map.get(risk, risk)

    grade_map = {"A": "A", "B": "B", "C": "C", "D": "D"}
    grade = grade_map.get(ai.get("entry_quality", "D"), "D")

    pos_info = signal.get("position_info", {})

    text = (
        f"AUTO TRADE - ORDER SENT\n"
        f"{'-' * 28}\n\n"
        f"{signal['symbol']}\n"
        f"Direction: {direction_emoji}\n"
        f"Price: {signal['price']:,.6f}\n\n"
        f"AI:\n"
        f"  L1: {ai.get('analyst_bias', 'N/A')} ({ai.get('analyst_strength', '?')})\n"
        f"  L2: Risk {ai.get('risk_score', '?')}/100 | Squeeze {ai.get('squeeze_risk', '?')}%\n"
        f"  L3: Grade {grade}\n\n"
        f"Score: {playbook_score:.1f}/10 | Gate: {config.AI_SCORE_GATE:.1f}/10 | Conf: {confidence}%\n"
        f"Risk: {risk_text}\n"
        f"Note: {ai.get('reasoning', 'N/A')}\n\n"
        f"Trade:\n"
        f"- Entry: {signal['price']:,.6f}\n"
        f"- TP: {signal['tp']:,.6f} (+{config.get_tp(signal['symbol'])}%)\n"
        f"- SL: {signal['sl']:,.6f} (-{config.get_sl(signal['symbol'])}%)\n"
        f"- Size: {pos_info.get('contracts', 'N/A')} contracts\n"
        f"- Leverage: {config.get_leverage(signal['symbol'])}x\n"
        f"{'-' * 28}\n"
        f"Waiting TP/SL..."
    )

    try:
        await bot.send_message(chat_id=_log_chat_id(), text=text)
        logger.info(f"Sent auto trade notification {signal['symbol']}")
    except Exception as e:
        logger.error(f"Send auto trade notification error: {e}")


# â”€â”€ callback handler â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xá»­ lĂ½ khi user báº¥m nĂºt EXECUTE hoáº·c SKIP."""
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split("_", 1)
    action = parts[0]
    signal_id = parts[1] if len(parts) > 1 else ""

    signal = pending_signals.pop(signal_id, None)

    if signal is None:
        await query.edit_message_text(
            text=query.message.text + "\n\nSignal expired or already handled."
        )
        return

    if action == "trade":
        await query.edit_message_text(
            text=query.message.text + "\n\nExecuting trade..."
        )

        # Gá»i execute callback
        if _execute_callback:
            try:
                result = await _execute_callback(signal, context)
                if result.get("success"):
                    await send_trade_result(context.bot, signal, result)
                else:
                    await context.bot.send_message(
                        chat_id=_log_chat_id(),
                        text=f"Order error: {result.get('error', 'Unknown')}"
                    )
            except Exception as e:
                await context.bot.send_message(
                    chat_id=_log_chat_id(),
                    text=f"Execution error: {str(e)}"
                )
        else:
            await context.bot.send_message(
                chat_id=_log_chat_id(),
                text="Execute callback not configured"
            )

    elif action == "skip":
        await query.edit_message_text(
            text=query.message.text + "\n\nSkipped."
        )
        logger.info(f"User skip signal {signal_id}")


# â”€â”€ gá»­i káº¿t quáº£ Ä‘áº·t lá»‡nh â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async def send_trade_result(bot, signal: dict, result: dict):
    """Gá»­i xĂ¡c nháº­n ÄĂƒ VĂ€O Lá»†NH (chÆ°a pháº£i káº¿t quáº£ win/loss)."""

    success = result.get("success")
    order_id = result.get("order_id", "N/A")

    if success:
        text = (
            f"ORDER OPENED - WAIT RESULT\n"
            f"{'-' * 28}\n\n"
            f"{signal['symbol']}\n"
            f"Direction: {signal['direction']}\n"
            f"Order ID: {order_id}\n\n"
            f"Trade:\n"
            f"- Entry: ~{signal['price']:,.6f}\n"
            f"- TP: {signal['tp']:,.6f} (+{config.get_tp(signal['symbol'])}%)\n"
            f"- SL: {signal['sl']:,.6f} (-{config.get_sl(signal['symbol'])}%)\n\n"
            f"Waiting TP/SL trigger...\n"
            f"Result WIN/LOSS will be sent after close.\n"
            f"{'-' * 28}\n"
        )
    else:
        text = f"Order failed: {result.get('error', 'Unknown')}"

    await bot.send_message(chat_id=_log_chat_id(), text=text)


async def send_position_closed(bot, symbol: str, direction: str,
                                pnl: float, pnl_pct: float):
    """ThĂ´ng bĂ¡o khi vá»‹ tháº¿ Ä‘Ă£ Ä‘Ă³ng (TP/SL hit)."""
    from capital_manager import CapitalManager

    emoji = "WIN" if pnl >= 0 else "LOSS"
    result = "WIN" if pnl >= 0 else "LOSS"

    text = (
        f"{emoji} TRADE CLOSED - {result}\n"
        f"{'-' * 28}\n\n"
        f"{symbol}\n"
        f"Direction: {direction}\n"
        f"PnL: {pnl:+.4f} USDT ({pnl_pct:+.2f}%)\n"
        f"{'-' * 28}\n"
    )

    await bot.send_message(chat_id=_log_chat_id(), text=text)

    # Thá»‘ng kĂª ngĂ y
    cm = CapitalManager()
    await bot.send_message(
        chat_id=_log_chat_id(),
        text=cm.stats_text(),
    )


async def send_message(bot, text: str):
    """Gá»­i tin nháº¯n text thuáº§n."""
    await bot.send_message(chat_id=_log_chat_id(), text=text)


# â”€â”€ setup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def setup_handlers(app: Application):
    """ÄÄƒng kĂ½ táº¥t cáº£ handlers cho Telegram bot."""
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("money", cmd_money))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("end", cmd_end))
    app.add_handler(CommandHandler("dang", cmd_dang))
    app.add_handler(MessageHandler(filters.Regex(r"^/dang_\d+c?(?:@\w+)?$"), cmd_dang_select))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, channel_command_router))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Telegram handlers set up")




