# -*- coding: utf-8 -*-
"""
ai_filter.py — Multi-Model AI Trading System (Anti-Echo Chamber)

6 lớp AI phân tích (2 models khác nhau để chống "hùa nhau"):
  Layer 1: Market Analyst       — GPT (proxy)    — temp 0.5 — nhạy bén
  Layer 2: Risk Manager         — GPT (proxy)    — temp 0.1 — cứng nhắc
  Layer 3: Trade Validator      — GPT (proxy)    — temp 0.25 — cân bằng
  Layer 4: Devil's Advocate     — DeepSeek V3    — temp 0.4 — NÃO KHÁC, phản biện gắt
  Layer 5: Execution Strategist — GPT (proxy)    — temp 0.2 — chính xác
  Layer 6: Market Regime Detector— DeepSeek V3   — temp 0.3 — NÃO KHÁC, góc nhìn khác

Parallel: L1+L2 chạy song song, L5+L6 chạy song song → tiết kiệm ~40% thời gian

Trade params loaded from config (TP/SL/Leverage dynamic).
"""

import json
import urllib.error
import urllib.request
import time
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from statistics import median
import config
from macro_fetcher import get_macro_data

logger = logging.getLogger("fubot.ai")

MEMORY_FILE = "ai_memory.json"

PAIR_PLAYBOOKS = {
    "SOL": {
        "cluster": "SOL/AVAX",
        "style": "Trend Following",
        "sl_pct": 0.50,
        "tp_pct": 1.00,
        "deepseek_focus": "Gia ben tren/duoi EMA21 M1 theo huong lenh",
        "gpt_focus": "Breakout khoi vung tich luy",
        "bonus_focus": "Dong thuan he sinh thai L1",
    },
    "SUI": {
        "cluster": "SUI/TIA",
        "style": "Trend Following",
        "sl_pct": 0.55,
        "tp_pct": 1.10,
        "deepseek_focus": "Gia ben tren/duoi EMA21 M1 theo huong lenh",
        "gpt_focus": "Breakout khoi vung tich luy",
        "bonus_focus": "Dong thuan he sinh thai L1 moi",
    },
    "DOGE": {
        "cluster": "DOGE/WLD",
        "style": "Momentum Burst",
        "sl_pct": 0.55,
        "tp_pct": 1.10,
        "deepseek_focus": "Gia o bien Bollinger Bands M1",
        "gpt_focus": "Momentum burst sau accumulation",
        "bonus_focus": "Volume spike kem breakout",
    },
    "WLD": {
        "cluster": "DOGE/WLD",
        "style": "Momentum Burst",
        "sl_pct": 0.60,
        "tp_pct": 1.20,
        "deepseek_focus": "CVD spike + OB imbalance manh",
        "gpt_focus": "Breakout voi volume cao bat thuong",
        "bonus_focus": "AI narrative momentum",
    },
    "AVAX": {
        "cluster": "SOL/AVAX",
        "style": "Trend Following",
        "sl_pct": 0.50,
        "tp_pct": 1.00,
        "deepseek_focus": "Gia ben tren/duoi EMA21 M1 theo huong lenh",
        "gpt_focus": "Breakout khoi vung tich luy",
        "bonus_focus": "Dong thuan L1 ecosystem",
    },
    "INJ": {
        "cluster": "INJ/PENDLE",
        "style": "Swing Breakout",
        "sl_pct": 0.60,
        "tp_pct": 1.20,
        "deepseek_focus": "Support/resistance M5 va vung khoi luong lon",
        "gpt_focus": "Breakout structure voi volume xac nhan",
        "bonus_focus": "DeFi catalyst momentum",
    },
    "TIA": {
        "cluster": "SUI/TIA",
        "style": "Trend Following",
        "sl_pct": 0.60,
        "tp_pct": 1.20,
        "deepseek_focus": "Gia ben tren/duoi EMA21 M1 theo huong lenh",
        "gpt_focus": "Trend continuation sau pullback",
        "bonus_focus": "Modular blockchain narrative",
    },
    "PENDLE": {
        "cluster": "INJ/PENDLE",
        "style": "Swing Breakout",
        "sl_pct": 0.65,
        "tp_pct": 1.30,
        "deepseek_focus": "Order block M5 va vung khoi luong lon",
        "gpt_focus": "Rejection hoac breakout tai key level",
        "bonus_focus": "DeFi yield narrative, ATR cao",
    },
}


def _pair_profile(symbol: str) -> dict:
    coin = symbol.split("-")[0].upper()
    profile = PAIR_PLAYBOOKS.get(coin)
    if profile:
        return dict(profile)
    return {
        "cluster": coin,
        "style": "General Intraday",
        "sl_pct": float(config.get_sl(symbol)),
        "tp_pct": float(config.get_tp(symbol)),
        "deepseek_focus": "Volatility and trap detection",
        "gpt_focus": "Momentum confirmation",
        "bonus_focus": "Macro + orderflow confluence",
    }


def _pair_min_score(symbol: str) -> float:
    """Uniform 7.0/10 threshold for all coins."""
    return float(getattr(config, "AI_SCORE_GATE", 7.0))


def _float_value(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _build_pair_playbook_score(
    symbol: str,
    direction: str,
    indicators: dict,
    macro_data: dict | None,
) -> dict:
    """Hệ thống chấm điểm 10 đơn giản:
    1. Cấu trúc & lực nến M1/M5: 4 điểm
    2. Độ an toàn SL & râu nến: 4 điểm
    3. Sức mạnh thị trường & BTC: 2 điểm
    >= 7.5 = vào lệnh, < 7.5 = bỏ.
    """
    profile = _pair_profile(symbol)
    sl_target = _float_value(profile.get("sl_pct", 0.45), 0.45)

    # --- Đọc indicators ---
    m1_breakout_up = bool(indicators.get("m1_breakout_up"))
    m1_breakout_down = bool(indicators.get("m1_breakout_down"))
    bull_engulf = bool(indicators.get("m1_bull_engulfing"))
    bear_engulf = bool(indicators.get("m1_bear_engulfing"))
    rej = str(indicators.get("m1_rejection_signal", "none"))
    touch_sup = bool(indicators.get("m5_touch_support"))
    touch_res = bool(indicators.get("m5_touch_resistance"))
    max_wick = _float_value(indicators.get("m1_max_wick_pct_10"), 0.0)
    price_vs_ema21 = str(indicators.get("m1_price_vs_ema21", "unknown")).lower()
    vol_surge = _float_value(indicators.get("m1_volume_surge_pct"), 0.0)
    ob_block_bias = str(indicators.get("m5_order_block_bias", "unknown")).lower()
    ob_block_near = bool(indicators.get("m5_order_block_near", False))
    bb_pos = str(indicators.get("m1_bb_position", "mid")).lower()

    # Candle trend data
    trend_5m = str(indicators.get("trend_5m", "unknown")).lower()
    trend_15m = str(indicators.get("trend_15m", "unknown")).lower()
    candle_aligned = bool(indicators.get("candle_aligned", False))
    cvd_bias = str(indicators.get("cvd_bias", "neutral")).lower()
    ob_bias = str(indicators.get("ob_bias", "neutral")).lower()

    # ═══ 0. KIỂM TRA XUNG ĐỘT HƯỚNG ĐI VS TREND ═══
    # Nếu direction đi ngược cả 2 timeframe → phạt nặng cả score
    warnings = []
    trend_conflict_penalty = 0.0
    _bullish_set = ("bullish", "up", "long", "uptrend")
    _bearish_set = ("bearish", "down", "short", "downtrend")
    if direction == "SHORT":
        if trend_5m in _bullish_set and trend_15m in _bullish_set:
            trend_conflict_penalty = 3.0  # Cả 2 TF bullish mà SHORT → phạt 3 điểm
            warnings.append("CONFLICT: SHORT ngược cả 5m+15m bullish — phạt -3.0")
        elif trend_5m in _bullish_set or trend_15m in _bullish_set:
            trend_conflict_penalty = 1.5  # 1 TF bullish → phạt nhẹ hơn
            warnings.append("CONFLICT: SHORT ngược 1 TF bullish — phạt -1.5")
        if price_vs_ema21 == "above":
            trend_conflict_penalty += 1.0  # Giá trên EMA21 mà SHORT
            warnings.append("CONFLICT: SHORT khi giá trên EMA21 — phạt thêm -1.0")
    elif direction == "LONG":
        if trend_5m in _bearish_set and trend_15m in _bearish_set:
            trend_conflict_penalty = 3.0
            warnings.append("CONFLICT: LONG ngược cả 5m+15m bearish — phạt -3.0")
        elif trend_5m in _bearish_set or trend_15m in _bearish_set:
            trend_conflict_penalty = 1.5
            warnings.append("CONFLICT: LONG ngược 1 TF bearish — phạt -1.5")
        if price_vs_ema21 == "below":
            trend_conflict_penalty += 1.0
            warnings.append("CONFLICT: LONG khi giá dưới EMA21 — phạt thêm -1.0")

    # ═══ 1. CẤU TRÚC & LỰC NẾN M1/M5 (4 điểm) ═══
    # Base point: mọi signal đã qua generate_signal (min_score=4) đều có nền tảng
    structure = 0.5
    if direction == "LONG":
        # Breakout / phá đỉnh gần nhất
        if m1_breakout_up:
            structure += 1.5
        # Engulfing / rút chân mạnh
        if bull_engulf:
            structure += 1.0
        elif rej == "bullish_rejection":
            structure += 1.0
        # Chạm hỗ trợ M5
        if touch_sup:
            structure += 0.5
        # EMA21 thuận chiều
        if price_vs_ema21 == "above":
            structure += 0.5
        # Trend confirm (5m + 15m cùng hướng) — tăng weight vì đây là tín hiệu mạnh
        if trend_5m in ("bullish", "up", "long") and trend_15m in ("bullish", "up", "long"):
            structure += 1.25
        elif candle_aligned:
            structure += 0.75
        # Orderflow confirm (CVD + OB cùng hướng) — tăng weight cho orderflow
        if cvd_bias == "bullish":
            structure += 0.5
        if ob_bias == "bullish":
            structure += 0.5
        # Volume surge (giảm ngưỡng từ 50→25 cho tier thấp)
        if vol_surge >= 50:
            structure += 0.5
        elif vol_surge >= 25:
            structure += 0.3
        # Order block M5 thuận chiều & gần giá
        if ob_block_near and ob_block_bias == "bullish":
            structure += 0.5
        # Bollinger: giá ở lower band thuận LONG
        if bb_pos == "lower":
            structure += 0.25
    else:  # SHORT — base 0.5 đã có ở trên
        if m1_breakout_down:
            structure += 1.5
        if bear_engulf:
            structure += 1.0
        elif rej == "bearish_rejection":
            structure += 1.0
        if touch_res:
            structure += 0.5
        if price_vs_ema21 == "below":
            structure += 0.5
        # Trend confirm — tăng weight
        if trend_5m in ("bearish", "down", "short") and trend_15m in ("bearish", "down", "short"):
            structure += 1.25
        elif candle_aligned:
            structure += 0.75
        # Orderflow — tăng weight
        if cvd_bias == "bearish":
            structure += 0.5
        if ob_bias == "bearish":
            structure += 0.5
        # Volume surge — giảm ngưỡng
        if vol_surge >= 50:
            structure += 0.5
        elif vol_surge >= 25:
            structure += 0.3
        if ob_block_near and ob_block_bias == "bearish":
            structure += 0.5
        if bb_pos == "upper":
            structure += 0.25
    structure = min(4.0, structure)

    # ═══ 2. ĐỘ AN TOÀN SL & RÂU NẾN (4 điểm) ═══
    # So sánh râu nến (wick) lớn nhất 10 nến M1 gần nhất vs SL target
    # Dùng ratio liên tục thay vì cliff — smooth hơn
    if max_wick <= 0.001:
        # Không có data wick → cho điểm trung bình
        sl_score = 2.5
    else:
        wick_ratio = max_wick / max(sl_target, 0.01)
        if wick_ratio < 0.5:
            # Râu rất nhỏ so với SL → rất an toàn
            sl_score = 4.0
        elif wick_ratio < 0.8:
            # Râu nhỏ hơn SL → an toàn
            sl_score = 3.5
        elif wick_ratio < 1.0:
            # Râu gần bằng SL → chấp nhận được
            sl_score = 3.0
        elif wick_ratio < 1.2:
            # Râu vượt SL nhẹ → vẫn chấp nhận
            sl_score = 2.5
        elif wick_ratio < 1.5:
            # Râu vượt SL đáng kể → rủi ro
            sl_score = 1.5
        elif wick_ratio < 2.0:
            # Râu gấp đôi SL → nguy hiểm
            sl_score = 0.5
        else:
            # Râu quá lớn → loại
            sl_score = 0.0

    # ═══ 3. SỨC MẠNH THỊ TRƯỜNG & BTC (2 điểm) ═══
    btc_score = 0.0
    macro = macro_data or {}
    btc_trend = str(macro.get("btc_trend", "UNKNOWN")).upper()
    btc_change = _float_value(macro.get("btc_change_24h"), 0.0)

    if direction == "LONG":
        # Long khi BTC xanh hoặc sideway
        if btc_trend in ("UP", "STRONG_UP"):
            btc_score = 2.0
        elif btc_trend in ("SIDEWAYS", "NEUTRAL", "UNKNOWN") or abs(btc_change) <= 1.5:
            btc_score = 1.5
        elif btc_trend in ("DOWN",) and abs(btc_change) <= 2.5:
            btc_score = 0.5
        # BTC STRONG_DOWN → 0
    else:  # SHORT
        # Short khi BTC đỏ hoặc sideway
        if btc_trend in ("DOWN", "STRONG_DOWN"):
            btc_score = 2.0
        elif btc_trend in ("SIDEWAYS", "NEUTRAL", "UNKNOWN") or abs(btc_change) <= 1.5:
            btc_score = 1.5
        elif btc_trend in ("UP",) and abs(btc_change) <= 2.5:
            btc_score = 0.5
    btc_score = min(2.0, btc_score)

    # ═══ TỔNG ĐIỂM (áp dụng penalty xung đột hướng) ═══
    total = structure + sl_score + btc_score - trend_conflict_penalty
    total = max(0.0, min(10.0, total))

    if sl_score == 0:
        warnings.append(f"Rau nen qua lon: {max_wick:.3f}% > SL {sl_target}%")
    if structure < 2:
        warnings.append("Cau truc nen yeu, khong co tin hieu ro")

    return {
        "profile": profile,
        "score_total": round(total, 1),
        "score_breakdown": {
            "candle_structure": round(structure, 1),
            "sl_safety": round(sl_score, 1),
            "btc_strength": round(btc_score, 1),
            "trend_conflict_penalty": round(trend_conflict_penalty, 1),
        },
        "warnings": warnings,
        "bonus_notes": [],
        "max_wick_pct_10": round(max_wick, 3),
        "volume_surge_pct": round(_float_value(indicators.get("m1_volume_surge_pct"), 0.0), 2),
        "bb_position": str(indicators.get("m1_bb_position", "mid")).lower(),
        "price_vs_ema21": price_vs_ema21,
    }


def _pair_playbook_prompt_text(symbol: str, playbook: dict) -> str:
    profile = playbook.get("profile", {})
    breakdown = playbook.get("score_breakdown", {})
    warnings = playbook.get("warnings", [])
    min_score = _pair_min_score(symbol)
    warning_text = "; ".join(warnings) if warnings else "none"

    return (
        f"Pair: {profile.get('cluster')} | Style: {profile.get('style')}\n"
        f"TP={profile.get('tp_pct')}% | SL={profile.get('sl_pct')}%\n"
        f"Score: {playbook.get('score_total', 0)}/10 "
        f"(structure={breakdown.get('candle_structure', 0)}/4, "
        f"sl_safety={breakdown.get('sl_safety', 0)}/4, "
        f"btc={breakdown.get('btc_strength', 0)}/2)\n"
        f"Wick(M1)={playbook.get('max_wick_pct_10', 0)}%\n"
        f"Warnings: {warning_text}\n"
        f"HARD RULE: score < {min_score:.1f} = SKIP."
    )

# ═══════════════════════════════════════════════════════════════
# SYSTEM PROMPTS cho 3 lớp AI
# ═══════════════════════════════════════════════════════════════

PROMPT_LAYER1_ANALYST = """You are an expert crypto futures MARKET ANALYST for INTRADAY TRADING (5m/15m timeframes).
Your job: analyze market microstructure + macro context and determine trend direction.
You are looking for intraday trades on meme/altcoins with 10x leverage.

You MUST respond ONLY with valid JSON:
{
    "bias": "LONG" or "SHORT" or "NEUTRAL",
    "prob_long": 0-100,
    "prob_short": 0-100,
    "trend_strength": "strong" or "medium" or "weak",
    "volume_analysis": "brief note",
    "orderflow_analysis": "brief note",
    "key_observation": "most important finding in Vietnamese"
}

Analysis rules:
- CVD shows real buying/selling pressure from market orders
- Orderbook imbalance shows pending limit orders
- If CVD bullish + OB bullish = strong LONG signal
- If CVD bearish + OB bearish = strong SHORT signal
- Divergence between CVD and OB = weak/uncertain signal
- High volume delta = more conviction
- prob_long + prob_short should = 100
- MACRO CONTEXT: Fear & Greed, BTC trend, and market cap direction influence intraday trades significantly
- Extreme Fear (< 25) = potential buying opportunity (contrarian)
- Extreme Greed (> 75) = caution, potential reversal
- BTC in strong downtrend = most altcoins will follow down"""

PROMPT_LAYER2_RISK = """You are a crypto futures RISK MANAGER for INTRADAY TRADING (5m/15m).
Your job: evaluate risk factors that could cause an intraday trade to fail.
Consider macro context (Fear & Greed, BTC trend) as additional risk factors.

You MUST respond ONLY with valid JSON:
{
    "risk_level": "LOW" or "MEDIUM" or "HIGH" or "EXTREME",
    "risk_score": 0-100,
    "squeeze_risk": 0-100,
    "funding_warning": true or false,
    "volatility_alert": true or false,
    "oi_concern": true or false,
    "safe_to_trade": true or false,
    "risk_factors": ["list of specific risks in Vietnamese"],
    "recommendation": "brief risk summary in Vietnamese"
}

Risk rules:
- Funding rate > 0.03% or < -0.03% = funding warning
- OI change > 3% = squeeze risk elevated
- OI change > 5% = dangerous, likely squeeze incoming
- Extreme funding + high OI = very dangerous
- Multiple risk factors = exponential risk increase
- risk_score > 60 means safe_to_trade should be false
- Be conservative — protecting capital is priority #1"""

PROMPT_LAYER3_VALIDATOR = """You are a crypto futures TRADE VALIDATOR for SCALPING (M1/M5).
You receive analysis from a Market Analyst and a Risk Manager.
Your job: make the final TRADE or SKIP decision.

SCORING SYSTEM (10 points total):
1. Candle structure & force M1/M5 (4 pts): Breakout, engulfing, rejection patterns
2. SL safety & wick (4 pts): Max wick vs SL target
3. BTC/market strength (2 pts): BTC trend alignment

RULE: >= 7.5/10 = TRADE. < 7.5/10 = SKIP. No exceptions.

You MUST respond ONLY with valid JSON:
{
    "decision": "TRADE" or "SKIP",
    "confidence": 0-100,
    "direction": "LONG" or "SHORT",
    "win_probability": 0-100,
    "risk_level": "LOW" or "MEDIUM" or "HIGH",
    "reasoning": "brief explanation in Vietnamese",
    "entry_quality": "A" or "B" or "C" or "D",
    "lessons_applied": "what you learned from trade history, in Vietnamese",
    "pair_profile": "pair cluster style",
    "playbook_score": 0-10,
    "score_breakdown": {
        "candle_structure": 0-4,
        "sl_safety": 0-4,
        "btc_strength": 0-2
    }
}

Decision rules:
- TRADE only if playbook_score >= 7.5/10
- If playbook_score < 7.5 → decision MUST be SKIP
- Quality > Quantity. Only Grade A/B setups should be TRADE
- If analyst and risk manager disagree → SKIP
- Learn from trade history: avoid loss patterns, repeat win patterns"""


PROMPT_LAYER4_DEVIL = """You are a contrarian TRADE CRITIC. Your job: find flaws in the trade thesis.

RULES:
1. Find AT LEAST 2 reasons this trade could fail
2. If win > 70% → reduce by at least 10%
3. Extreme funding in trade direction → crowded trade → REDUCE_SIZE
4. OI spike > 5% = liquidation risk → REJECT
5. ONLY REJECT if there is a CLEAR and SPECIFIC trap or danger. Ranging/sideways alone is NOT enough to REJECT — scalping works in ranges.
6. If orderflow (CVD+OB) confirms the direction → lean APPROVE even in ranging market
7. Keep adjusted_win_probability realistic — reduce by 5-15% from validator, not more

You MUST respond ONLY with valid JSON:
{
    "veto": true or false,
    "adjusted_win_probability": 0-100,
    "kill_reasons": ["reason 1 in Vietnamese", "reason 2", "reason 3"],
    "trap_detected": true or false,
    "trap_type": "bull_trap" or "bear_trap" or "fake_breakout" or "whale_manipulation" or "none",
    "overconfidence_warning": true or false,
    "leverage_risk_note": "specific danger in Vietnamese",
    "final_verdict": "APPROVE" or "REJECT" or "REDUCE_SIZE",
    "reasoning": "counter-argument in Vietnamese (1-2 sentences)"
}"""


# ═══════════════════════════════════════════════════════════════
# AI MEMORY — học từ lịch sử trade
# ═══════════════════════════════════════════════════════════════

def load_ai_memory() -> str:
    """Load lịch sử trade gần đây → format cho AI đọc với pattern analysis sâu."""
    if not os.path.exists(MEMORY_FILE):
        return "No trade history available yet."

    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            memory = json.load(f)
    except Exception:
        return "No trade history available yet."

    if not memory:
        return "No trade history available yet."

    # Lấy 20 trades gần nhất
    recent = memory[-20:]

    lines = ["Recent trade history (LEARN FROM THIS — your past decisions):"]
    wins = sum(1 for t in recent if t.get("result") == "WIN")
    losses = sum(1 for t in recent if t.get("result") == "LOSS")
    total = len(recent)
    wr = wins / total * 100 if total else 0
    lines.append(f"Last {total} trades: {wins}W / {losses}L "
                 f"(WR: {wr:.0f}%)")

    # Tổng PnL
    total_pnl = sum(t.get("pnl_pct", 0) for t in recent)
    lines.append(f"Total PnL: {total_pnl:+.2f}%")
    lines.append("")

    for t in recent[-10:]:  # chỉ show 10 trade gần nhất chi tiết
        result_emoji = "✓" if t.get("result") == "WIN" else "✗"
        lines.append(
            f"{result_emoji} {t.get('symbol','?')} {t.get('direction','?')} | "
            f"PnL: {t.get('pnl_pct', 0):+.2f}% | "
            f"CVD: {t.get('cvd_bias','?')} | OB: {t.get('ob_bias','?')} | "
            f"Imb: {t.get('imbalance', 0):.1f} | "
            f"Funding: {t.get('funding_signal','?')} | "
            f"AI conf: {t.get('ai_confidence', 0)}%"
        )

    # ═══ DEEP PATTERN ANALYSIS ═══
    win_trades = [t for t in recent if t.get("result") == "WIN"]
    loss_trades = [t for t in recent if t.get("result") == "LOSS"]

    lines.append(f"\n{'='*40}")
    lines.append("DEEP PATTERN ANALYSIS (MANDATORY READING):")
    lines.append(f"{'='*40}")

    # --- OB Bias correlation ---
    ob_stats = {}
    for t in recent:
        ob = t.get("ob_bias", "unknown")
        if ob not in ob_stats:
            ob_stats[ob] = {"win": 0, "loss": 0}
        if t.get("result") == "WIN":
            ob_stats[ob]["win"] += 1
        else:
            ob_stats[ob]["loss"] += 1

    lines.append("\n📊 OB BIAS → WIN/LOSS correlation:")
    for ob, stats in ob_stats.items():
        total_ob = stats["win"] + stats["loss"]
        wr_ob = stats["win"] / total_ob * 100 if total_ob else 0
        danger = " ⚠️ DANGER" if wr_ob < 40 and total_ob >= 2 else ""
        lines.append(f"  OB={ob}: {stats['win']}W/{stats['loss']}L "
                     f"(WR: {wr_ob:.0f}%){danger}")

    # --- CVD Bias correlation ---
    cvd_stats = {}
    for t in recent:
        cvd = t.get("cvd_bias", "unknown")
        if cvd not in cvd_stats:
            cvd_stats[cvd] = {"win": 0, "loss": 0}
        if t.get("result") == "WIN":
            cvd_stats[cvd]["win"] += 1
        else:
            cvd_stats[cvd]["loss"] += 1

    lines.append("\n📊 CVD BIAS → WIN/LOSS correlation:")
    for cvd, stats in cvd_stats.items():
        total_cvd = stats["win"] + stats["loss"]
        wr_cvd = stats["win"] / total_cvd * 100 if total_cvd else 0
        lines.append(f"  CVD={cvd}: {stats['win']}W/{stats['loss']}L "
                     f"(WR: {wr_cvd:.0f}%)")

    # --- Imbalance analysis ---
    if loss_trades:
        loss_imb = [t.get("imbalance", 0) for t in loss_trades]
        avg_loss_imb = sum(loss_imb) / len(loss_imb)
        max_loss_imb = max(loss_imb)
        lines.append(f"\n📊 Imbalance on LOSSES: avg={avg_loss_imb:.1f}, "
                     f"max={max_loss_imb:.1f}")
    if win_trades:
        win_imb = [t.get("imbalance", 0) for t in win_trades]
        avg_win_imb = sum(win_imb) / len(win_imb)
        lines.append(f"📊 Imbalance on WINS: avg={avg_win_imb:.1f}")

    # --- AI Confidence vs Result ---
    if loss_trades:
        loss_conf = [t.get("ai_confidence", 0) for t in loss_trades]
        avg_loss_conf = sum(loss_conf) / len(loss_conf)
        lines.append(f"\n📊 AI Confidence on LOSSES: avg={avg_loss_conf:.0f}% "
                     f"← AI was OVERCONFIDENT on these!")

    # ═══ ANTI-PATTERNS (conditions that ALWAYS lose) ═══
    lines.append(f"\n{'='*40}")
    lines.append("🚫 ANTI-PATTERNS — NEVER TRADE THESE:")
    lines.append(f"{'='*40}")

    anti_patterns = []
    # Check OB neutral → loss rate
    ob_neutral = ob_stats.get("neutral", {"win": 0, "loss": 0})
    if ob_neutral["loss"] > 0 and ob_neutral["loss"] >= ob_neutral["win"]:
        anti_patterns.append(
            f"OB=neutral → {ob_neutral['win']}W/{ob_neutral['loss']}L "
            f"— AVOID trading when orderbook is balanced/neutral")

    # Check high imbalance → loss
    high_imb_losses = [t for t in loss_trades if t.get("imbalance", 0) > 40]
    if high_imb_losses:
        anti_patterns.append(
            f"Imbalance > 40 → {len(high_imb_losses)} losses "
            f"— extreme imbalance likely = spoofing/manipulation")

    # Check consecutive losses (same session rapid trading)
    rapid_losses = []
    for i in range(1, len(recent)):
        if (recent[i].get("result") == "LOSS" and
            recent[i-1].get("result") == "LOSS"):
            t1 = recent[i-1].get("time", "")
            t2 = recent[i].get("time", "")
            if t1[:10] == t2[:10]:  # same day
                rapid_losses.append(recent[i].get("symbol", "?"))
    if rapid_losses:
        anti_patterns.append(
            f"Consecutive losses in same session — STOP trading after 2 losses")

    if not anti_patterns:
        anti_patterns.append("No clear anti-patterns yet — keep collecting data")

    for ap in anti_patterns:
        lines.append(f"  🚫 {ap}")

    # ═══ WIN PATTERNS (conditions that consistently win) ═══
    lines.append(f"\n✅ WIN PATTERNS — REPEAT THESE:")
    win_patterns_list = []
    for ob, stats in ob_stats.items():
        total_ob = stats["win"] + stats["loss"]
        if stats["win"] > stats["loss"] and total_ob >= 2:
            wr_ob = stats["win"] / total_ob * 100
            win_patterns_list.append(
                f"OB={ob} → {stats['win']}W/{stats['loss']}L (WR: {wr_ob:.0f}%) — GOOD condition")

    if not win_patterns_list:
        win_patterns_list.append("Not enough data for reliable win patterns yet")

    for wp in win_patterns_list:
        lines.append(f"  ✅ {wp}")

    lines.append(f"\n⚠️ CRITICAL: If current trade matches ANY anti-pattern → SKIP immediately!")
    lines.append(f"⚠️ Fee impact: ~0.02 USDT per trade (negligible). Focus on WIN patterns.")

    return "\n".join(lines)


def save_trade_to_memory(symbol: str, direction: str, result: str,
                         pnl_pct: float, indicators: dict,
                         ai_confidence: int):
    """Lưu kết quả trade vào AI memory."""
    memory = []
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                memory = json.load(f)
        except Exception:
            memory = []

    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M"),
        "symbol": symbol,
        "direction": direction,
        "result": result,
        "pnl_pct": round(pnl_pct, 2),
        "cvd_bias": indicators.get("cvd_bias", ""),
        "ob_bias": indicators.get("ob_bias", ""),
        "imbalance": indicators.get("imbalance", 0),
        "funding_signal": indicators.get("funding_signal", ""),
        "oi_signal": indicators.get("oi_signal", ""),
        "ai_confidence": ai_confidence,
    }

    memory.append(entry)
    # Giữ tối đa 100 trades
    memory = memory[-100:]

    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════
# API CALLS — 2 Models (Anti-Echo Chamber)
# ═══════════════════════════════════════════════════════════════

_DISABLED_HINT_VALUES = {"", "0", "off", "none", "disable", "disabled", "false", "no"}


def _is_gpt5_model(model: str) -> bool:
    return str(model or "").strip().lower().startswith("gpt-5")


def _build_gpt_speed_hints(model: str) -> dict:
    """Optional GPT-5 speed hints; silently skipped for other models."""
    if not _is_gpt5_model(model):
        return {}

    hints: dict = {}
    effort = str(getattr(config, "GPT_REASONING_EFFORT", "")).strip().lower()
    verbosity = str(getattr(config, "GPT_VERBOSITY", "")).strip().lower()

    if effort not in _DISABLED_HINT_VALUES:
        hints["reasoning_effort"] = effort
    if verbosity not in _DISABLED_HINT_VALUES:
        hints["verbosity"] = verbosity

    return hints


def _is_hint_rejection(error_text: str) -> bool:
    lowered = (error_text or "").lower()
    if not lowered:
        return False
    mentions_hint = any(
        token in lowered for token in ("reasoning_effort", "verbosity")
    )
    is_schema_error = any(
        token in lowered
        for token in (
            "unknown",
            "unexpected",
            "unsupported",
            "additional properties",
            "not allowed",
            "invalid parameter",
        )
    )
    return mentions_hint and is_schema_error


def _call_api(system_prompt: str, user_prompt: str,
              temperature: float, base_url: str, api_key: str,
              model: str, max_tokens: int = 500,
              timeout: int = 30) -> dict:
    """Generic API call — OpenAI-compatible format."""
    url = base_url.rstrip("/")
    # Smart URL construction: avoid double /v1
    if url.endswith("/chat/completions"):
        pass  # already full URL
    elif url.endswith("/v1"):
        url += "/chat/completions"
    else:
        url += "/v1/chat/completions"

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    hinted_body = dict(body)
    hinted_body.update(_build_gpt_speed_hints(model))
    payload_candidates = [hinted_body] if hinted_body != body else []
    payload_candidates.append(body)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    raw = None
    start = time.time()
    for idx, payload in enumerate(payload_candidates):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            err_text = e.read().decode("utf-8", errors="replace")
            should_retry = (
                idx < (len(payload_candidates) - 1)
                and e.code in (400, 404, 422)
                and _is_hint_rejection(err_text)
            )
            if should_retry:
                logger.warning(
                    "GPT speed hints rejected by proxy/API; retrying without hints."
                )
                continue
            raise RuntimeError(f"LLM HTTP {e.code}: {err_text[:300]}") from e
    elapsed = round(time.time() - start, 2)
    if raw is None:
        raise RuntimeError("LLM call failed without response payload")

    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    result = _parse_json(content)
    result["_response_time"] = elapsed
    return result


def _effective_timeout(
    configured_timeout: float,
    hard_cap: float,
    timeout_override: float | None = None,
) -> float:
    """Clamp timeout for each request to keep AI pipeline within scan budget."""
    timeout_value = float(configured_timeout)
    if timeout_override is not None:
        timeout_value = min(timeout_value, float(timeout_override))
    timeout_value = min(timeout_value, float(hard_cap))
    return max(4.0, timeout_value)


def _call_gpt(system_prompt: str, user_prompt: str,
              temperature: float = 0.3,
              timeout_sec: float | None = None,
              max_tokens: int | None = None) -> dict:
    """Gọi GPT qua proxy — cho L1, L2, L3, L5."""
    timeout = _effective_timeout(
        config.GPT_TIMEOUT_SEC,
        config.GPT_TIMEOUT_HARD_CAP_SEC,
        timeout_override=timeout_sec,
    )
    fast_prefix = (
        "CRITICAL: Return valid JSON ONLY. "
        "No chain-of-thought, no markdown, no extra prose. "
        "Keep every text field concise and direct.\n\n"
    )
    token_cap = int(max_tokens if max_tokens is not None else config.GPT_MAX_TOKENS)
    token_cap = max(120, token_cap)
    return _call_api(
        fast_prefix + system_prompt, user_prompt, temperature,
        config.GPT_BASE_URL, config.GPT_API_KEY, config.GPT_MODEL,
        max_tokens=token_cap, timeout=timeout,
    )


def _call_deepseek(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.3,
    timeout_sec: float | None = None,
    max_tokens: int = 400,
) -> dict:
    """Gọi DeepSeek V3 — cho L4 + L6 (não khác, chống echo chamber).
    Ép trả JSON nhanh bằng prompt engineering + max_tokens thấp."""
    fast_prefix = (
        "CRITICAL: Respond IMMEDIATELY with JSON only. "
        "NO reasoning, NO explanation, NO thinking process. "
        "Output ONLY the JSON object, nothing else.\n\n"
    )
    timeout = _effective_timeout(
        config.DEEPSEEK_TIMEOUT_SEC,
        config.DEEPSEEK_TIMEOUT_HARD_CAP_SEC,
        timeout_override=timeout_sec,
    )
    return _call_api(
        fast_prefix + system_prompt, user_prompt, temperature,
        config.DEEPSEEK_BASE_URL, config.DEEPSEEK_API_KEY, config.DEEPSEEK_MODEL,
        max_tokens=max_tokens, timeout=timeout,
    )


def _hedged_gpt_call(
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    timeout_sec: float | None,
    fanout: int,
    max_tokens: int | None = None,
) -> dict:
    """Run duplicated GPT calls in parallel and return the first usable response."""
    fanout = max(
        1,
        min(int(fanout), max(1, int(getattr(config, "GPT_PARALLEL_HARD_LIMIT", 3)))),
    )
    if fanout == 1:
        return _call_gpt(
            system_prompt,
            user_prompt,
            temperature=temperature,
            timeout_sec=timeout_sec,
            max_tokens=max_tokens,
        )

    temps = []
    for i in range(fanout):
        # Small temperature jitter to avoid identical degenerate outputs.
        jitter = ((i % 3) - 1) * 0.03
        temps.append(max(0.0, min(1.0, temperature + jitter)))

    futures = []
    errors = []
    fallback_result = None
    pool = ThreadPoolExecutor(max_workers=fanout)
    try:
        for t in temps:
            futures.append(
                pool.submit(
                    _call_gpt,
                    system_prompt,
                    user_prompt,
                    t,
                    timeout_sec,
                    max_tokens,
                )
            )

        for fut in as_completed(futures):
            try:
                result = fut.result()
            except Exception as e:
                errors.append(str(e))
                continue

            if fallback_result is None:
                fallback_result = result

            if not result.get("_parse_error"):
                for p in futures:
                    if p is not fut:
                        p.cancel()
                return result
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    if fallback_result is not None:
        return fallback_result
    raise RuntimeError("All hedged GPT calls failed: " + " | ".join(errors[:3]))


def _aggregate_l3_votes(results: list[dict], direction: str) -> dict:
    """Aggregate multiple L3 outputs into one robust verdict."""
    if not results:
        raise RuntimeError("No L3 vote results")

    def _int_field(item: dict, key: str, default: int = 0) -> int:
        try:
            return int(item.get(key, default))
        except Exception:
            return int(default)

    def _majority(values: list[str], fallback: str) -> str:
        counts: dict[str, int] = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        if not counts:
            return fallback
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    decisions = [str(r.get("decision", "SKIP")) for r in results]
    vote_decision = _majority(decisions, "SKIP")
    win_vals = [_int_field(r, "win_probability", 0) for r in results]
    conf_vals = [_int_field(r, "confidence", 0) for r in results]
    grade_vals = [str(r.get("entry_quality", "D")) for r in results]
    risk_vals = [str(r.get("risk_level", "HIGH")) for r in results]
    direction_vals = [str(r.get("direction", direction)) for r in results]

    merged = dict(results[0])
    merged["decision"] = vote_decision
    merged["direction"] = _majority(direction_vals, direction)
    merged["win_probability"] = int(round(median(win_vals)))
    merged["confidence"] = int(round(median(conf_vals)))
    merged["entry_quality"] = _majority(grade_vals, "D")
    merged["risk_level"] = _majority(risk_vals, "HIGH")
    merged["l3_votes"] = len(results)

    notes = [str(r.get("reasoning", "")).strip() for r in results if r.get("reasoning")]
    if notes:
        merged["reasoning"] = notes[0]
    lesson_notes = [str(r.get("lessons_applied", "")).strip() for r in results if r.get("lessons_applied")]
    if lesson_notes:
        merged["lessons_applied"] = lesson_notes[0]

    return merged


def _parse_json(content: str) -> dict:
    """Parse JSON từ GPT response."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    start = content.find("{")
    end = content.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(content[start:end])
        except json.JSONDecodeError:
            pass

    return {"_parse_error": True, "_raw": content[:300]}


def _macro_snapshot_bundle() -> tuple[str, dict]:
    """Get one macro snapshot text + raw data and reuse across one analysis."""
    fallback = {
        "fear_greed_value": 50,
        "fear_greed_label": "Neutral",
        "fear_greed_signal": "NEUTRAL",
        "btc_price": 0,
        "btc_change_24h": 0.0,
        "btc_trend": "UNKNOWN",
        "btc_dominance": 0.0,
        "eth_dominance": 0.0,
        "total_market_cap_B": 0.0,
        "market_cap_change_24h": 0.0,
        "total_volume_24h_B": 0.0,
        "market_trend": "UNKNOWN",
    }
    try:
        data = get_macro_data()
        if not isinstance(data, dict) or not data:
            data = dict(fallback)
    except Exception as e:
        logger.warning(f"Macro snapshot fallback: {e}")
        data = dict(fallback)

    text = (
        f"Fear & Greed Index: {data.get('fear_greed_value', 50)}/100 "
        f"({data.get('fear_greed_label', 'Neutral')}) - {data.get('fear_greed_signal', 'NEUTRAL')}\n"
        f"BTC Price: ${data.get('btc_price', 0):,.0f} "
        f"({data.get('btc_change_24h', 0.0):+.1f}% 24h) - Trend: {data.get('btc_trend', 'UNKNOWN')}\n"
        f"BTC Dominance: {data.get('btc_dominance', 0)}% | ETH Dominance: {data.get('eth_dominance', 0)}%\n"
        f"Total Market Cap: ${data.get('total_market_cap_B', 0)}B "
        f"({data.get('market_cap_change_24h', 0):+.1f}% 24h)\n"
        f"24h Volume: ${data.get('total_volume_24h_B', 0)}B\n"
        f"Market Sentiment: {data.get('market_trend', 'UNKNOWN')}"
    )
    return text, data


def _macro_snapshot_text() -> str:
    text, _ = _macro_snapshot_bundle()
    return text


def _fallback_l1_from_indicators(symbol: str, indicators: dict, error: str) -> dict:
    """Deterministic L1 fallback when GPT timeout/error occurs."""
    trend_5m = str(indicators.get("trend_5m", "")).lower()
    trend_15m = str(indicators.get("trend_15m", "")).lower()
    candle_overall = str(indicators.get("candle_overall", "")).lower()
    ob_bias = str(indicators.get("ob_bias", "neutral")).lower()
    cvd_bias = str(indicators.get("cvd_bias", "neutral")).lower()

    bullish_5m = trend_5m in ("bullish", "up", "long")
    bullish_15m = trend_15m in ("bullish", "up", "long")
    bearish_5m = trend_5m in ("bearish", "down", "short")
    bearish_15m = trend_15m in ("bearish", "down", "short")
    candle_bull = candle_overall in ("bullish", "up")
    candle_bear = candle_overall in ("bearish", "down")
    ob_bull = ob_bias == "bullish"
    ob_bear = ob_bias == "bearish"
    cvd_bull = cvd_bias == "bullish"
    cvd_bear = cvd_bias == "bearish"

    align_long = (bullish_5m and bullish_15m) or candle_bull
    align_short = (bearish_5m and bearish_15m) or candle_bear
    flow_long = ob_bull and cvd_bull
    flow_short = ob_bear and cvd_bear

    if align_long and flow_long:
        bias, prob_long, prob_short, strength = "LONG", 82, 18, "strong"
        key_obs = f"{symbol}: fallback L1 -> trend + orderflow bullish"
    elif align_short and flow_short:
        bias, prob_long, prob_short, strength = "SHORT", 18, 82, "strong"
        key_obs = f"{symbol}: fallback L1 -> trend + orderflow bearish"
    elif align_long:
        bias, prob_long, prob_short, strength = "LONG", 72, 28, "medium"
        key_obs = f"{symbol}: fallback L1 -> trend bullish"
    elif align_short:
        bias, prob_long, prob_short, strength = "SHORT", 28, 72, "medium"
        key_obs = f"{symbol}: fallback L1 -> trend bearish"
    elif flow_long:
        bias, prob_long, prob_short, strength = "LONG", 66, 34, "medium"
        key_obs = f"{symbol}: fallback L1 -> orderflow bullish"
    elif flow_short:
        bias, prob_long, prob_short, strength = "SHORT", 34, 66, "medium"
        key_obs = f"{symbol}: fallback L1 -> orderflow bearish"
    elif cvd_bull or ob_bull:
        bias, prob_long, prob_short, strength = "LONG", 58, 42, "medium"
        key_obs = f"{symbol}: fallback L1 -> partial bullish flow"
    elif cvd_bear or ob_bear:
        bias, prob_long, prob_short, strength = "SHORT", 42, 58, "medium"
        key_obs = f"{symbol}: fallback L1 -> partial bearish flow"
    else:
        bias, prob_long, prob_short, strength = "NEUTRAL", 50, 50, "weak"
        key_obs = f"{symbol}: fallback L1 -> mixed signals"

    return {
        "bias": bias,
        "prob_long": prob_long,
        "prob_short": prob_short,
        "trend_strength": strength,
        "volume_analysis": "Fallback mode from indicators",
        "orderflow_analysis": "Fallback mode from indicators",
        "key_observation": key_obs,
        "fallback_used": True,
        "error": error,
    }


def _fallback_l2_from_indicators(symbol: str, indicators: dict, error: str) -> dict:
    """Deterministic L2 fallback when GPT timeout/error occurs."""
    def _safe_float(value: object, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    funding_pct = abs(_safe_float(indicators.get("funding_pct", 0.0)))
    oi_change_pct = abs(_safe_float(indicators.get("oi_change_pct", 0.0)))
    imbalance = abs(_safe_float(indicators.get("imbalance", 0.0)))
    ob_bias = str(indicators.get("ob_bias", "neutral")).lower()
    cvd_bias = str(indicators.get("cvd_bias", "neutral")).lower()

    score = 15
    if funding_pct >= 0.08:
        score += 22
    elif funding_pct >= 0.05:
        score += 12
    elif funding_pct >= 0.03:
        score += 6

    if oi_change_pct >= 8:
        score += 22
    elif oi_change_pct >= 5:
        score += 14
    elif oi_change_pct >= 3:
        score += 8

    if imbalance >= 0.40:
        score += 18
    elif imbalance >= 0.30:
        score += 10
    elif imbalance >= 0.22:
        score += 5

    if ob_bias == "neutral" or cvd_bias == "neutral":
        score += 5
    if (ob_bias == "bullish" and cvd_bias == "bearish") or (
        ob_bias == "bearish" and cvd_bias == "bullish"
    ):
        score += 8

    if "PEPE" in symbol or "WIF" in symbol:
        score += 5

    score = max(5, min(95, int(round(score))))
    if score <= 25:
        risk_level = "LOW"
    elif score <= 45:
        risk_level = "MEDIUM"
    elif score <= 65:
        risk_level = "HIGH"
    else:
        risk_level = "EXTREME"

    squeeze_risk = max(0, min(100, int(round(oi_change_pct * 8))))
    safe_to_trade = score < 60

    return {
        "risk_level": risk_level,
        "risk_score": score,
        "squeeze_risk": squeeze_risk,
        "funding_warning": funding_pct >= 0.05,
        "volatility_alert": score >= 50,
        "oi_concern": oi_change_pct >= 5.0,
        "safe_to_trade": safe_to_trade,
        "risk_factors": ["Fallback risk model from indicators"],
        "recommendation": f"Fallback L2 for {symbol}: score={score}",
        "fallback_used": True,
        "error": error,
    }


# ═══════════════════════════════════════════════════════════════
# 3-LAYER AI ANALYSIS
# ═══════════════════════════════════════════════════════════════

def layer1_market_analyst(
    symbol: str,
    indicators: dict,
    macro_text: str,
    timeout_sec: float | None = None,
) -> dict:
    """Layer 1: Phân tích thị trường — xu hướng, xác suất."""
    prompt = f"""Analyze market data for {symbol} (INTRADAY — 5m/15m timeframe):

Price: {indicators.get('price', 0):,.2f}
CVD Bias: {indicators.get('cvd_bias', 'unknown')}
CVD Value: {indicators.get('cvd_value', 0):,.0f}
Buy Volume: {indicators.get('buy_vol', 0):,.0f}
Sell Volume: {indicators.get('sell_vol', 0):,.0f}
Orderbook Imbalance: {indicators.get('imbalance', 0):.3f}
Orderbook Bias: {indicators.get('ob_bias', 'unknown')}
Funding Rate: {indicators.get('funding_rate', 0):.6f} ({indicators.get('funding_pct', 0):.4f}%)
OI Change: {indicators.get('oi_change_pct', 0):.2f}%

[CANDLE TREND — 5m/15m]
5m Entry: {indicators.get('trend_5m', 'unknown')} (EMA bias: {indicators.get('ema_bias_5m', 'unknown')})
15m Trend: {indicators.get('trend_15m', 'unknown')} (EMA bias: {indicators.get('ema_bias_15m', 'unknown')})
Overall:  {indicators.get('candle_overall', 'unknown')} | Aligned: {indicators.get('candle_aligned', False)}

[🌍 MACRO CONTEXT]
{macro_text}

IMPORTANT: This is INTRADAY TRADING on meme/altcoins (minutes to hours, not days).
Only LONG when candle trend is bullish on BOTH 5m and 15m.
Only SHORT when candle trend is bearish on BOTH 5m and 15m.
If trends conflict or overall=mixed → bias=NEUTRAL.
Consider macro sentiment: Extreme Fear can mean buying opportunity, Extreme Greed means caution.
BTC trend strongly influences altcoins — if BTC is STRONG_DOWN, altcoins likely follow.

Determine market bias and trend strength. Return JSON only."""

    try:
        result = _hedged_gpt_call(
            PROMPT_LAYER1_ANALYST,
            prompt,
            temperature=0.5,
            timeout_sec=timeout_sec,
            fanout=config.GPT_HEDGE_FANOUT,
            max_tokens=260,
        )
        logger.info(f"  [L1 Analyst] {symbol}: bias={result.get('bias')} "
                    f"prob_long={result.get('prob_long')}% "
                    f"strength={result.get('trend_strength')} "
                    f"({result.get('_response_time', '?')}s GPT)")
        return result
    except Exception as e:
        logger.error(f"  [L1] Error: {e}")
        fallback = _fallback_l1_from_indicators(symbol, indicators, str(e))
        logger.warning(
            f"  [L1] Using indicator fallback: bias={fallback.get('bias')} "
            f"strength={fallback.get('trend_strength')}"
        )
        return fallback


def layer2_risk_manager(
    symbol: str,
    indicators: dict,
    timeout_sec: float | None = None,
) -> dict:
    """Layer 2: Đánh giá rủi ro — squeeze, funding, volatility."""
    prompt = f"""Evaluate risk for {symbol} futures trade:

Funding Rate: {indicators.get('funding_rate', 0):.6f} ({indicators.get('funding_pct', 0):.4f}%)
Funding Signal: {indicators.get('funding_signal', 'unknown')}
OI Change: {indicators.get('oi_change_pct', 0):.2f}%
OI Signal: {indicators.get('oi_signal', 'unknown')}
Orderbook Imbalance: {indicators.get('imbalance', 0):.3f}
CVD Value: {indicators.get('cvd_value', 0):,.0f}
Price: {indicators.get('price', 0):,.2f}

Assess all risk factors. Return JSON only."""

    try:
        result = _hedged_gpt_call(
            PROMPT_LAYER2_RISK,
            prompt,
            temperature=0.1,
            timeout_sec=timeout_sec,
            fanout=config.GPT_HEDGE_FANOUT,
            max_tokens=220,
        )
        logger.info(f"  [L2 Risk] {symbol}: risk={result.get('risk_level')} "
                    f"score={result.get('risk_score')} "
                    f"squeeze={result.get('squeeze_risk')}% "
                    f"safe={result.get('safe_to_trade')} "
                    f"({result.get('_response_time', '?')}s GPT)")
        return result
    except Exception as e:
        logger.error(f"  [L2] Error: {e}")
        fallback = _fallback_l2_from_indicators(symbol, indicators, str(e))
        logger.warning(
            f"  [L2] Using indicator fallback: risk={fallback.get('risk_level')} "
            f"score={fallback.get('risk_score')} safe={fallback.get('safe_to_trade')}"
        )
        return fallback


def layer3_trade_validator(symbol: str, direction: str,
                           analyst: dict, risk: dict,
                           indicators: dict, macro_text: str,
                           macro_data: dict | None = None,
                           timeout_sec: float | None = None) -> dict:
    """Layer 3: Quyết định cuối cùng — TRADE hoặc SKIP."""

    # Load AI memory
    memory_text = load_ai_memory()
    playbook = _build_pair_playbook_score(symbol, direction, indicators, macro_data)
    playbook_text = _pair_playbook_prompt_text(symbol, playbook)

    prompt = f"""Final trade decision for {symbol} ({direction}):

═══ MARKET ANALYST REPORT ═══
Bias: {analyst.get('bias')}
Long Probability: {analyst.get('prob_long')}%
Short Probability: {analyst.get('prob_short')}%
Trend Strength: {analyst.get('trend_strength')}
Volume: {analyst.get('volume_analysis', 'N/A')}
Orderflow: {analyst.get('orderflow_analysis', 'N/A')}
Key Finding: {analyst.get('key_observation', 'N/A')}

═══ RISK MANAGER REPORT ═══
Risk Level: {risk.get('risk_level')}
Risk Score: {risk.get('risk_score')}/100
Squeeze Risk: {risk.get('squeeze_risk')}%
Funding Warning: {risk.get('funding_warning')}
OI Concern: {risk.get('oi_concern')}
Safe to Trade: {risk.get('safe_to_trade')}
Risks: {', '.join(risk.get('risk_factors', []))}
Summary: {risk.get('recommendation', 'N/A')}

═══ MARKET DATA ═══
Price: {indicators.get('price', 0):,.2f}
Funding: {indicators.get('funding_pct', 0):.4f}%
OI Change: {indicators.get('oi_change_pct', 0):.2f}%
OB Imbalance: {indicators.get('imbalance', 0):.3f}

═══ CANDLE TREND ═══
5m Entry: {indicators.get('trend_5m', 'unknown')} (EMA: {indicators.get('ema_bias_5m', 'unknown')})
15m Trend: {indicators.get('trend_15m', 'unknown')} (EMA: {indicators.get('ema_bias_15m', 'unknown')})
Overall: {indicators.get('candle_overall', 'unknown')} | Aligned: {indicators.get('candle_aligned', False)}
RULE: ONLY TRADE if both 5m AND 15m confirm. If mixed → SKIP.

═══ MACRO CONTEXT ═══
{macro_text}
IMPORTANT: Consider macro sentiment for intraday trades.
Extreme Fear + strong technical setup = high conviction entry.
Extreme Greed + weak setup = SKIP (market likely to correct).
BTC downtrend = most altcoins follow — be cautious with LONG.

═══ PAIR PLAYBOOK HARD SCORE ═══
{playbook_text}

Mandatory output:
- pair_profile
- playbook_score (0-10)
- score_breakdown: candle_structure(0-4), sl_safety(0-4), btc_strength(0-2)

═══ TRADE STRATEGY (INTRADAY) ═══
TP: {config.get_tp(symbol)}% | SL: {config.get_sl(symbol)}% | Leverage: {config.get_leverage(symbol)}x
TP {config.get_tp(symbol)}% price move → {config.get_tp(symbol) * config.get_leverage(symbol)}% margin gain
SL {config.get_sl(symbol)}% price move → {config.get_sl(symbol) * config.get_leverage(symbol)}% margin loss
Only trade Grade A/B setups with ALIGNED candles.

═══ TRADE HISTORY (AI MEMORY) ═══
{memory_text}

Make your final decision. Return JSON only."""

    try:
        fanout = max(
            1,
            min(
                int(config.GPT_L3_ENSEMBLE),
                max(1, int(getattr(config, "GPT_PARALLEL_HARD_LIMIT", 3))),
            ),
        )
        quorum = max(1, min(fanout, int(config.GPT_L3_QUORUM)))

        if fanout == 1:
            result = _call_gpt(
                PROMPT_LAYER3_VALIDATOR,
                prompt,
                temperature=0.25,
                timeout_sec=timeout_sec,
                max_tokens=320,
            )
        else:
            started = time.time()
            votes: list[dict] = []
            errors: list[str] = []
            temps = []
            futures = []
            for i in range(fanout):
                jitter = ((i % 3) - 1) * 0.05
                temps.append(max(0.05, min(0.6, 0.25 + jitter)))

            pool = ThreadPoolExecutor(max_workers=fanout)
            try:
                futures = [
                    pool.submit(
                        _call_gpt,
                        PROMPT_LAYER3_VALIDATOR,
                        prompt,
                        t,
                        timeout_sec,
                        320,
                    )
                    for t in temps
                ]
                for fut in as_completed(futures):
                    try:
                        one = fut.result()
                    except Exception as e:
                        errors.append(str(e))
                        continue

                    if one.get("_parse_error"):
                        continue
                    votes.append(one)
                    if len(votes) >= quorum:
                        break
            finally:
                for fut in futures:
                    if not fut.done():
                        fut.cancel()
                pool.shutdown(wait=False, cancel_futures=True)

            if not votes:
                raise RuntimeError("L3 ensemble failed: " + " | ".join(errors[:3]))

            result = _aggregate_l3_votes(votes, direction)
            result["_response_time"] = round(time.time() - started, 2)
            result["l3_vote_quorum"] = f"{len(votes)}/{fanout}"

        profile = playbook.get("profile", {})
        result["pair_profile"] = (
            f"{profile.get('cluster', 'N/A')} | {profile.get('style', 'N/A')}"
        )
        result["playbook_score"] = round(float(playbook.get("score_total", 0)), 1)
        result["score_breakdown"] = dict(playbook.get("score_breakdown", {}))
        result["playbook_warnings"] = list(playbook.get("warnings", []))
        result["playbook_bonus_notes"] = list(playbook.get("bonus_notes", []))

        logger.info(f"  [L3 Validator] {symbol}: decision={result.get('decision')} "
                    f"conf={result.get('confidence')}% "
                    f"win={result.get('win_probability')}% "
                    f"playbook={result.get('playbook_score')}/10 "
                    f"grade={result.get('entry_quality')} "
                    f"votes={result.get('l3_vote_quorum', '1/1')} "
                    f"({result.get('_response_time', '?')}s GPT)")
        return result
    except Exception as e:
        logger.error(f"  [L3] Error: {e}")
        playbook = _build_pair_playbook_score(symbol, direction, indicators, macro_data)
        profile = playbook.get("profile", {})
        return {
            "decision": "SKIP", "confidence": 0, "direction": direction,
            "win_probability": 0, "risk_level": "HIGH",
            "reasoning": f"AI Layer3 lỗi: {e}", "error": True,
            "pair_profile": f"{profile.get('cluster', 'N/A')} | {profile.get('style', 'N/A')}",
            "playbook_score": round(float(playbook.get("score_total", 0)), 1),
            "score_breakdown": dict(playbook.get("score_breakdown", {})),
            "playbook_warnings": list(playbook.get("warnings", [])),
            "playbook_bonus_notes": list(playbook.get("bonus_notes", [])),
        }


# ═══════════════════════════════════════════════════════════════
# LAYER 4: DEVIL'S ADVOCATE
# ═══════════════════════════════════════════════════════════════

def layer4_devils_advocate(symbol: str, direction: str,
                           analyst: dict, risk: dict,
                           validator: dict, indicators: dict,
                           macro_text: str,
                           timeout_sec: float | None = None,
                           fail_open: bool = False,
                           fast_mode: bool = False) -> dict:
    """
    Layer 4: Phản biện — tìm lý do trade thất bại.
    Có quyền VETO trade ngay cả khi Layer 3 nói TRADE.
    Đặc biệt quan trọng với 20x leverage.
    """
    if fast_mode:
        macro_short = str(macro_text).strip()
        if len(macro_short) > 280:
            macro_short = macro_short[:280] + "..."
        prompt = f"""FAST3L L4 REFEREE for {symbol} ({direction}):

L1: bias={analyst.get('bias')} long={analyst.get('prob_long')} short={analyst.get('prob_short')} strength={analyst.get('trend_strength')}
L2: risk={risk.get('risk_level')} score={risk.get('risk_score')} squeeze={risk.get('squeeze_risk')} safe={risk.get('safe_to_trade')}
L3: decision={validator.get('decision')} win={validator.get('win_probability')} conf={validator.get('confidence')} grade={validator.get('entry_quality')}
MKT: cvd={indicators.get('cvd_bias')} ob={indicators.get('ob_bias')} imb={indicators.get('imbalance', 0):.3f} funding={indicators.get('funding_pct', 0):.4f}% oi={indicators.get('oi_change_pct', 0):.2f}%
LEV: x{config.get_leverage(symbol)} TP={config.get_tp(symbol)}% SL={config.get_sl(symbol)}%
MACRO: {macro_short}

STRICT OUTPUT:
- JSON only, no markdown fences
- kill_reasons: exactly 3 short items (<= 10 words/item)
- reasoning: 1 short sentence (<= 18 words)
- keep numbers concise
Output JSON only with the exact schema."""
    else:
        memory_text = load_ai_memory()
        prompt = f"""CHALLENGE this trade decision for {symbol} ({direction}):

═══ LAYER 1 — MARKET ANALYST ═══
Bias: {analyst.get('bias')}
Long Prob: {analyst.get('prob_long')}% | Short Prob: {analyst.get('prob_short')}%
Trend Strength: {analyst.get('trend_strength')}
Key Finding: {analyst.get('key_observation', 'N/A')}

═══ LAYER 2 — RISK MANAGER ═══
Risk Level: {risk.get('risk_level')} | Score: {risk.get('risk_score')}/100
Squeeze Risk: {risk.get('squeeze_risk')}%
Funding Warning: {risk.get('funding_warning')}
OI Concern: {risk.get('oi_concern')}
Safe: {risk.get('safe_to_trade')}
Risks: {', '.join(risk.get('risk_factors', []))}

═══ LAYER 3 — VALIDATOR DECISION ═══
Decision: {validator.get('decision')}
Win Probability: {validator.get('win_probability')}%
Confidence: {validator.get('confidence')}%
Entry Grade: {validator.get('entry_quality')}
Reasoning: {validator.get('reasoning', 'N/A')}

═══ RAW MARKET DATA ═══
Price: {indicators.get('price', 0):,.2f}
CVD Bias: {indicators.get('cvd_bias', 'N/A')} (Value: {indicators.get('cvd_value', 0):,.0f})
Orderbook Imbalance: {indicators.get('imbalance', 0):.3f} ({indicators.get('ob_bias', 'N/A')})
Funding Rate: {indicators.get('funding_pct', 0):.4f}%
OI Change: {indicators.get('oi_change_pct', 0):.2f}%

═══ LEVERAGE ═══
Leverage: {config.get_leverage(symbol)}x (intraday meme/altcoins)
TP: {config.get_tp(symbol)}% = {config.get_tp(symbol) * config.get_leverage(symbol)}% on margin
SL: {config.get_sl(symbol)}% = {config.get_sl(symbol) * config.get_leverage(symbol)}% on margin

═══ MACRO CONTEXT ═══
{macro_text}
IMPORTANT for intraday trades: Macro sentiment matters. BTC trend impacts all altcoins.
If BTC is in STRONG_DOWN and Fear & Greed < 25 → market crash risk, challenge LONG aggressively.
If Extreme Greed + LONG setup → potential reversal, push back hard.

═══ TRADE HISTORY ═══
{memory_text}

Your job: FIND FLAWS in this trade setup. Challenge everything.
Return JSON only."""

    try:
        result = _call_deepseek(
            PROMPT_LAYER4_DEVIL,
            prompt,
            temperature=0.3 if fast_mode else 0.4,
            timeout_sec=timeout_sec,
            max_tokens=320 if fast_mode else 400,
        )
        logger.info(f"  [L4 Devil] {symbol}: verdict={result.get('final_verdict')} "
                    f"veto={result.get('veto')} "
                    f"adj_win={result.get('adjusted_win_probability')}% "
                    f"trap={result.get('trap_type', 'none')} "
                    f"overconf={result.get('overconfidence_warning')} "
                    f"({result.get('_response_time', '?')}s DeepSeek)")
        return result
    except Exception as e:
        logger.error(f"  [L4] Error: {e}")
        if fail_open:
            # Assist mode: do not hard-block trade if DeepSeek transiently fails.
            return {
                "veto": False,
                "adjusted_win_probability": validator.get("win_probability", 0),
                "kill_reasons": [f"L4 assist error: {e}"],
                "trap_detected": False,
                "trap_type": "none",
                "overconfidence_warning": False,
                "final_verdict": "APPROVE",
                "reasoning": f"Layer 4 assist lỗi, fallback fail-open: {e}",
                "error": True,
            }
        return {
            "veto": True,
            "adjusted_win_probability": 0,
            "kill_reasons": [f"L4 loi: {e}"],
            "trap_detected": True,
            "trap_type": "none",
            "overconfidence_warning": True,
            "final_verdict": "REJECT",
            "reasoning": f"Layer 4 loi -> fail-closed, tu choi trade: {e}",
            "error": True,
        }


# ═══════════════════════════════════════════════════════════════
# LAYER 5: EXECUTION STRATEGIST
# ═══════════════════════════════════════════════════════════════

PROMPT_LAYER5_EXEC = """You are a crypto futures EXECUTION STRATEGIST.
You are the FINAL layer before a real trade is executed with real money.

All 4 previous layers have analyzed. Your job:
1. Verify the TIMING is right (not entering at top/bottom of a move)
2. Check if price has ALREADY moved too much (late entry)
3. Evaluate if TP is REALISTIC given current market conditions (see trade params below)
4. Confirm the risk/reward is actually favorable at THIS EXACT moment

IMPORTANT: This is a SMALL ACCOUNT (2 USDT per trade, 10x leverage).
Fees are negligible (~0.02 USDT). Focus on WIN RATE and trade quality.

You MUST respond ONLY with valid JSON:
{
    "execute": true or false,
    "final_win_probability": 0-100,
    "timing_quality": "excellent" or "good" or "late" or "too_late",
    "tp_reachable": true or false,
    "tp_difficulty": "easy" or "moderate" or "hard" or "very_hard",
    "momentum_check": "strong" or "fading" or "exhausted" or "building",
    "price_position": "near_support" or "near_resistance" or "mid_range" or "breakout" or "extended",
    "entry_timing": "optimal" or "acceptable" or "suboptimal" or "missed",
    "execution_notes": ["list of execution considerations in Vietnamese"],
    "final_recommendation": "detailed execution advice in Vietnamese (2-3 sentences)"
}

Execution rules:
- TP price move needs sustained momentum. If momentum is even slightly fading → tp_reachable = false
- If price already moved significantly in the signal direction → likely LATE ENTRY → execute = false
- CVD momentum fading (volume decreasing) = trend exhaustion → skip
- Orderbook thinning on TP side = price will struggle to reach TP
- If timing_quality is "too_late" or "late" → execute = false
- If tp_reachable = false → execute = false regardless of other factors
- Only execute when timing is optimal or good
- This layer's final_win_probability is the DEFINITIVE number used for trade decision
- Reduce win by 10-15% if timing is suboptimal
- Reduce win by 15-25% if entry is late
- If momentum_check is "fading" or "exhausted" → execute = false
- Be EXTREMELY precise and conservative: this is the last check before real money is risked
- win_probability above 80% is VERY rare. Be realistic.
- If OB bias is neutral → tp_reachable = false (no orderbook support for the move)
- FEE CHECK: fees are ~0.02 USDT per trade (negligible). Focus on signal quality not fees."""


def layer5_execution_strategist(symbol: str, direction: str,
                                analyst: dict, risk: dict,
                                validator: dict, devil: dict,
                                indicators: dict,
                                timeout_sec: float | None = None) -> dict:
    """
    Layer 5: Execution Strategist — tối ưu timing, xác nhận entry quality.
    Kiểm tra xem TP có thực tế không, timing có tốt không.
    """
    memory_text = load_ai_memory()

    prompt = f"""EXECUTION CHECK for {symbol} ({direction}):

═══ ALL PREVIOUS LAYERS SUMMARY ═══
L1 Analyst: Bias={analyst.get('bias')}, Strength={analyst.get('trend_strength')}, Long={analyst.get('prob_long')}%
L2 Risk: Level={risk.get('risk_level')}, Score={risk.get('risk_score')}/100, Squeeze={risk.get('squeeze_risk')}%
L3 Validator: Decision={validator.get('decision')}, Win={validator.get('win_probability')}%, Grade={validator.get('entry_quality')}
L4 Devil: Verdict={devil.get('final_verdict')}, Adj Win={devil.get('adjusted_win_probability')}%, Trap={devil.get('trap_type')}

═══ RAW MARKET DATA ═══
Price: {indicators.get('price', 0):,.2f}
CVD Bias: {indicators.get('cvd_bias', 'N/A')} (Value: {indicators.get('cvd_value', 0):,.0f})
Buy Volume: {indicators.get('buy_vol', 0):,.0f}
Sell Volume: {indicators.get('sell_vol', 0):,.0f}
Orderbook Imbalance: {indicators.get('imbalance', 0):.3f} ({indicators.get('ob_bias', 'N/A')})
Funding Rate: {indicators.get('funding_pct', 0):.4f}%
OI Change: {indicators.get('oi_change_pct', 0):.2f}%

═══ CANDLE TREND ═══
5m Entry: {indicators.get('trend_5m', 'unknown')} (EMA: {indicators.get('ema_bias_5m', 'unknown')})
15m Trend: {indicators.get('trend_15m', 'unknown')} (EMA: {indicators.get('ema_bias_15m', 'unknown')})
Overall: {indicators.get('candle_overall', 'unknown')} | Aligned: {indicators.get('candle_aligned', False)}
If candles not aligned → tp_reachable=false, execute=false.
Leverage: {config.get_leverage(symbol)}x
TP: {config.get_tp(symbol)}% ({config.get_tp(symbol) * config.get_leverage(symbol)}% on margin)
SL: {config.get_sl(symbol)}% ({config.get_sl(symbol) * config.get_leverage(symbol)}% on margin)
Risk/Reward: {config.get_tp(symbol)/config.get_sl(symbol):.1f}:1

═══ KEY QUESTION ═══
Can price realistically move {config.get_tp(symbol)}% in {direction} direction from {indicators.get('price', 0):,.2f}?
Is this the RIGHT MOMENT to enter, or is the move already happening/done?

═══ TRADE HISTORY ═══
{memory_text}

Evaluate execution quality. Return JSON only."""

    try:
        result = _hedged_gpt_call(
            PROMPT_LAYER5_EXEC,
            prompt,
            temperature=0.2,
            timeout_sec=timeout_sec,
            fanout=config.GPT_HEDGE_FANOUT,
            max_tokens=300,
        )
        logger.info(f"  [L5 Exec] {symbol}: execute={result.get('execute')} "
                    f"win={result.get('final_win_probability')}% "
                    f"timing={result.get('timing_quality')} "
                    f"tp_reach={result.get('tp_reachable')} "
                    f"momentum={result.get('momentum_check')} "
                    f"({result.get('_response_time', '?')}s GPT)")
        return result
    except Exception as e:
        logger.error(f"  [L5] Error: {e}")
        return {
            "execute": True,
            "final_win_probability": devil.get("adjusted_win_probability", 50),
            "timing_quality": "acceptable",
            "tp_reachable": True,
            "tp_difficulty": "moderate",
            "momentum_check": "building",
            "price_position": "mid_range",
            "entry_timing": "acceptable",
            "execution_notes": [f"L5 lỗi: {e}"],
            "final_recommendation": f"Layer 5 lỗi, dùng kết quả Layer 4: {e}",
            "error": True,
        }


# ═══════════════════════════════════════════════════════════════
# LAYER 6: MARKET REGIME DETECTOR
# ═══════════════════════════════════════════════════════════════

PROMPT_LAYER6_REGIME = """You are a MARKET REGIME DETECTOR for crypto futures trading.
You are Layer 6 — the ULTIMATE and FINAL checkpoint before real money is placed.

All 5 previous layers have analyzed the trade. Your UNIQUE job:
1. Classify the current MARKET REGIME (trending, ranging, volatile, choppy)
2. Determine if this regime SUPPORTS the TP target specified in trade params
3. Check for regime TRANSITION signals (market about to shift)
4. Evaluate overall market HEALTH and whether conditions favor this specific trade

IMPORTANT: This is a SMALL ACCOUNT (2 USDT per trade, 10x leverage).
Fees are negligible (~0.02 USDT). Focus on WIN RATE and trade quality.
Only trending regimes with strong momentum justify a trade.

You MUST respond ONLY with valid JSON:
{
    "regime": "trending" or "ranging" or "volatile" or "choppy",
    "regime_strength": "strong" or "moderate" or "weak",
    "regime_supports_trade": true or false,
    "regime_score": 0-100,
    "transition_detected": true or false,
    "transition_to": "trending" or "ranging" or "volatile" or "choppy" or "none",
    "market_health": "healthy" or "uncertain" or "stressed" or "dangerous",
    "tp_compatible": true or false,
    "regime_confluence": true or false,
    "ultimate_win_probability": 0-100,
    "ultimate_verdict": "EXECUTE" or "ABORT",
    "regime_notes": ["list of regime observations in Vietnamese"],
    "final_judgment": "definitive reason in Vietnamese (2-3 sentences)"
}

Regime Classification Rules:
- TRENDING: CVD consistently one direction, OB bias aligns, OI growing = good for TP
- RANGING: CVD alternating, OB near zero, OI flat/declining = BAD for TP (will hit SL first)
- VOLATILE: OI spiking, funding extreme, CVD erratic = DANGEROUS with leverage
- CHOPPY: Mixed signals, CVD/OB disagree, weak trend = most trades FAIL in choppy markets

Critical Regime Rules:
- RANGING market: ABORT — TP almost NEVER reached in range, price oscillates
- CHOPPY market: ABORT — no clear direction, stop-hunt central
- VOLATILE market: ABORT unless ALL 5 previous layers strongly agree (win > 80%)
- TRENDING market: Only regime where TP target is realistic
- If regime_strength is "weak" → reduce win by 10-15%
- If transition_detected = true (market changing regime) → ABORT, too risky during transition
- regime_confluence = true only when regime MATCHES the trade direction AND supports TP
- ultimate_win_probability should be the ABSOLUTE FINAL number — be the harshest critic
- You decide the FATE of this trade. Protect capital above all else.
- If in doubt, ABORT. Missing a trade costs nothing. Losing money costs everything.
- If OB bias is neutral → regime likely RANGING or CHOPPY → ABORT
- FEE REALITY: fees are negligible (~0.02 USDT). Focus on WIN RATE not fee optimization."""


def layer6_regime_detector(symbol: str, direction: str,
                           analyst: dict, risk: dict,
                           validator: dict, devil: dict,
                           executor: dict, indicators: dict,
                           macro_text: str,
                           timeout_sec: float | None = None) -> dict:
    """
    Layer 6: Market Regime Detector — xác định chế độ thị trường.
    Kiểm tra xem regime hiện tại có hỗ trợ trade với TP target không.
    Đây là lớp cuối cùng, quyết định EXECUTE hay ABORT.
    """
    memory_text = load_ai_memory()

    prompt = f"""REGIME ANALYSIS for {symbol} ({direction}):

═══ ALL 5 LAYERS SUMMARY ═══
L1 Analyst: Bias={analyst.get('bias')}, Strength={analyst.get('trend_strength')}, Long={analyst.get('prob_long')}%
L2 Risk: Level={risk.get('risk_level')}, Score={risk.get('risk_score')}/100, Squeeze={risk.get('squeeze_risk')}%
L3 Validator: Decision={validator.get('decision')}, Win={validator.get('win_probability')}%, Grade={validator.get('entry_quality')}
L4 Devil: Verdict={devil.get('final_verdict')}, Adj Win={devil.get('adjusted_win_probability')}%, Trap={devil.get('trap_type')}
L5 Executor: Execute={executor.get('execute')}, Win={executor.get('final_win_probability')}%, Timing={executor.get('timing_quality')}, TP Reach={executor.get('tp_reachable')}, Momentum={executor.get('momentum_check')}

═══ RAW MARKET DATA ═══
Price: {indicators.get('price', 0):,.2f}
CVD Bias: {indicators.get('cvd_bias', 'N/A')} (Value: {indicators.get('cvd_value', 0):,.0f})
Buy Volume: {indicators.get('buy_vol', 0):,.0f}
Sell Volume: {indicators.get('sell_vol', 0):,.0f}
Orderbook Imbalance: {indicators.get('imbalance', 0):.3f} ({indicators.get('ob_bias', 'N/A')})
Funding Rate: {indicators.get('funding_pct', 0):.4f}%
OI Change: {indicators.get('oi_change_pct', 0):.2f}%

═══ TRADE PARAMS ═══
Leverage: {config.get_leverage(symbol)}x
TP: {config.get_tp(symbol)}% ({config.get_tp(symbol) * config.get_leverage(symbol)}% on margin)
SL: {config.get_sl(symbol)}% ({config.get_sl(symbol) * config.get_leverage(symbol)}% on margin)
Risk/Reward: {config.get_tp(symbol)/config.get_sl(symbol):.1f}:1

═══ KEY REGIME QUESTION ═══
Is {symbol} currently in a TRENDING regime that can sustain a {config.get_tp(symbol)}% price movement in {direction} direction?
Or is the market RANGING/CHOPPY/VOLATILE where this trade will likely fail?

═══ MACRO CONTEXT ═══
{macro_text}
IMPORTANT: For intraday trades targeting {config.get_tp(symbol)}% TP, macro environment is critical.
BTC trend + Fear/Greed + total market cap direction all influence whether an intraday trade can succeed.
Only EXECUTE in trending regimes with supportive macro sentiment.

REMEMBER: Fees cost ~0.5% of account per trade. Only trade in strong trending regimes.

═══ TRADE HISTORY ═══
{memory_text}

Classify the market regime and make your ULTIMATE verdict. Return JSON only."""

    try:
        result = _call_deepseek(
            PROMPT_LAYER6_REGIME,
            prompt,
            temperature=0.3,
            timeout_sec=timeout_sec,
        )
        logger.info(f"  [L6 Regime] {symbol}: regime={result.get('regime')} "
                    f"strength={result.get('regime_strength')} "
                    f"supports={result.get('regime_supports_trade')} "
                    f"verdict={result.get('ultimate_verdict')} "
                    f"win={result.get('ultimate_win_probability')}% "
                    f"health={result.get('market_health')} "
                    f"({result.get('_response_time', '?')}s DeepSeek)")
        return result
    except Exception as e:
        logger.error(f"  [L6] Error: {e}")
        return {
            "regime": "unknown",
            "regime_strength": "weak",
            "regime_supports_trade": False,
            "regime_score": 0,
            "transition_detected": True,
            "transition_to": "none",
            "market_health": "dangerous",
            "tp_compatible": False,
            "regime_confluence": False,
            "ultimate_win_probability": 0,
            "ultimate_verdict": "ABORT",
            "regime_notes": [f"L6 loi: {e}"],
            "final_judgment": f"Layer 6 loi -> fail-closed, ABORT: {e}",
            "error": True,
        }


# ═══════════════════════════════════════════════════════════════
# MAIN ANALYSIS FUNCTION
# ═══════════════════════════════════════════════════════════════

def _build_skip_result(direction: str, analyst: dict, risk: dict,
                       total_time: float, reason: str,
                       validator: dict = None) -> dict:
    """Build SKIP result cho early exit — tránh lặp code."""
    l3_win = validator.get("win_probability", 0) if validator else 0
    return {
        "decision": "SKIP",
        "confidence": 0,
        "direction": direction,
        "win_probability": 0,
        "win_probability_l3": l3_win,
        "win_probability_l4": 0,
        "win_probability_l5": 0,
        "win_probability_l6": 0,
        "risk_level": risk.get("risk_level", "HIGH"),
        "reasoning": reason,
        "skip_reason": reason,
        "entry_quality": validator.get("entry_quality", "D") if validator else "D",
        "lessons_applied": "",
        "pair_profile": validator.get("pair_profile", "") if validator else "",
        "playbook_score": validator.get("playbook_score", 0) if validator else 0,
        "score_breakdown": validator.get("score_breakdown", {}) if validator else {},
        "playbook_warnings": validator.get("playbook_warnings", []) if validator else [],
        "playbook_bonus_notes": validator.get("playbook_bonus_notes", []) if validator else [],
        "analyst_bias": analyst.get("bias", "NEUTRAL"),
        "analyst_prob_long": analyst.get("prob_long", 50),
        "analyst_prob_short": analyst.get("prob_short", 50),
        "analyst_strength": analyst.get("trend_strength", "weak"),
        "analyst_key": analyst.get("key_observation", ""),
        "risk_score": risk.get("risk_score", 50),
        "squeeze_risk": risk.get("squeeze_risk", 50),
        "risk_factors": risk.get("risk_factors", []),
        "risk_recommendation": risk.get("recommendation", ""),
        "safe_to_trade": risk.get("safe_to_trade", False),
        "l4_verdict": "N/A", "l4_veto": False,
        "l4_trap_detected": False, "l4_trap_type": "none",
        "l4_overconfidence": False, "l4_kill_reasons": [],
        "l4_leverage_note": "", "l4_reasoning": "Skipped (early exit)",
        "l5_execute": False, "l5_timing": "N/A",
        "l5_tp_reachable": False, "l5_tp_difficulty": "N/A",
        "l5_momentum": "N/A", "l5_price_position": "N/A",
        "l5_entry_timing": "N/A", "l5_notes": [],
        "l5_recommendation": "Skipped (early exit)",
        "l6_regime": "N/A", "l6_regime_strength": "N/A",
        "l6_supports_trade": False, "l6_regime_score": 0,
        "l6_transition": False, "l6_transition_to": "none",
        "l6_market_health": "N/A", "l6_tp_compatible": False,
        "l6_confluence": False, "l6_verdict": "N/A",
        "l6_notes": [], "l6_judgment": "Skipped (early exit)",
        "total_ai_time": total_time,
        "layers_called": 2 if not validator else 3,
        "early_exit": True,
        "score_gate": float(getattr(config, "AI_SCORE_GATE", 7.5)),
    }


def _build_score_trade_result(
    direction: str,
    analyst: dict,
    risk: dict,
    validator: dict,
    total_time: float,
    score_gate: float,
    ai_budget_sec: float,
    ai_budget_remaining_sec: float,
) -> dict:
    """Build TRADE result when score-only mode passes."""
    pair_score = float(validator.get("playbook_score", 0) or 0)
    score_conf = int(max(0, min(100, round(pair_score * 10))))
    l3_win = int(validator.get("win_probability", score_conf))
    final_conf = int(max(score_conf, int(validator.get("confidence", 0) or 0)))
    final_win = int(max(score_conf, l3_win))

    base_reason = str(validator.get("reasoning", "") or "").strip()
    score_reason = f"Score gate pass: {pair_score:.1f}/10 >= {score_gate:.1f}/10"
    reasoning = f"{base_reason} | {score_reason}" if base_reason else score_reason

    return {
        "decision": "TRADE",
        "confidence": final_conf,
        "direction": validator.get("direction", direction),
        "win_probability": final_win,
        "win_probability_l3": l3_win,
        "win_probability_l4": l3_win,
        "win_probability_l5": l3_win,
        "win_probability_l6": l3_win,
        "risk_level": validator.get("risk_level", risk.get("risk_level", "MEDIUM")),
        "reasoning": reasoning,
        "skip_reason": "",
        "entry_quality": validator.get("entry_quality", "B"),
        "lessons_applied": validator.get("lessons_applied", ""),
        "pair_profile": validator.get("pair_profile", ""),
        "playbook_score": pair_score,
        "score_breakdown": validator.get("score_breakdown", {}),
        "playbook_warnings": validator.get("playbook_warnings", []),
        "playbook_bonus_notes": validator.get("playbook_bonus_notes", []),
        "analyst_bias": analyst.get("bias", "NEUTRAL"),
        "analyst_prob_long": analyst.get("prob_long", 50),
        "analyst_prob_short": analyst.get("prob_short", 50),
        "analyst_strength": analyst.get("trend_strength", "weak"),
        "analyst_key": analyst.get("key_observation", ""),
        "risk_score": risk.get("risk_score", 50),
        "squeeze_risk": risk.get("squeeze_risk", 50),
        "risk_factors": risk.get("risk_factors", []),
        "risk_recommendation": risk.get("recommendation", ""),
        "safe_to_trade": risk.get("safe_to_trade", False),
        "l4_verdict": "SKIPPED(score-only)",
        "l4_veto": False,
        "l4_trap_detected": False,
        "l4_trap_type": "none",
        "l4_overconfidence": False,
        "l4_kill_reasons": [],
        "l4_leverage_note": "",
        "l4_reasoning": "Skipped in score-only mode",
        "l4_mode": "disabled",
        "l5_execute": True,
        "l5_timing": "good",
        "l5_tp_reachable": True,
        "l5_tp_difficulty": "moderate",
        "l5_momentum": "building",
        "l5_price_position": "mid_range",
        "l5_entry_timing": "acceptable",
        "l5_notes": ["Skipped in score-only mode"],
        "l5_recommendation": "Execute by score gate",
        "l6_regime": "SKIPPED(score-only)",
        "l6_regime_strength": "N/A",
        "l6_supports_trade": True,
        "l6_regime_score": 0,
        "l6_transition": False,
        "l6_transition_to": "none",
        "l6_market_health": "N/A",
        "l6_tp_compatible": True,
        "l6_confluence": True,
        "l6_verdict": "EXECUTE",
        "l6_notes": ["Skipped in score-only mode"],
        "l6_judgment": "Score-only mode",
        "total_ai_time": total_time,
        "ai_budget_sec": round(ai_budget_sec, 2),
        "ai_budget_remaining_sec": round(max(0.0, ai_budget_remaining_sec), 2),
        "layers_called": 3,
        "step3_mode": "score_only",
        "score_gate": float(score_gate),
    }


def analyze_trade(
    symbol: str,
    direction: str,
    indicators: dict,
    time_budget_sec: float | None = None,
    score_gate: float | None = None,
) -> dict:
    """
    Phân tích trade qua 6 lớp AI — SPEED OPTIMIZED.

    PARALLEL Step 1: L1 (GPT) + L2 (GPT)
    FAST EXIT: nếu L1 NEUTRAL/weak hoặc L2 unsafe → skip ngay (~7-14s)
    SEQUENTIAL Step 2: L3 (GPT) — cần L1+L2
    FAST EXIT: nếu L3 win < threshold → skip ngay (~20-25s thay vì 50s)
    PARALLEL Step 3: L4 + L5 + L6 CÙNG LÚC (3 workers)
    """
    ai_profile_label = "FAST3L" if config.AI_FAST_3L_MODE else "6-Layer"
    logger.info(f"🧠 AI {ai_profile_label} Analysis: {symbol} {direction}")
    deepseek_available = bool(config.DEEPSEEK_API_KEY and config.DEEPSEEK_BASE_URL)
    deepseek_assist_mode = bool(
        config.AI_GPT_ONLY_MODE
        and config.AI_DEEPSEEK_ASSIST_ENABLED
        and deepseek_available
    )
    if config.AI_FAST_3L_MODE:
        logger.info("  📡 FAST3L stack: GPT L1+L2 + DeepSeek L4")
    elif config.AI_GPT_ONLY_MODE:
        if deepseek_assist_mode:
            logger.info("  📡 GPT aggressive mode + 🔷 DeepSeek assist lite (L4 advisory)")
        else:
            logger.info("  📡 GPT aggressive mode: L1+L2+L3+L5 (DeepSeek skipped)")
    else:
        logger.info("  📡 GPT: L1+L2+L3+L5 | 🔷 DeepSeek: L4+L6 (anti-echo)")
    if config.AI_FAST_3L_MODE:
        logger.info("  ⚙️ FAST3L: 2 GPT (L1+L2) + 1 DeepSeek (L4 referee)")
    total_start = time.time()
    budget_sec = float(time_budget_sec or config.AI_TIME_BUDGET_SEC)
    budget_deadline = total_start + max(8.0, budget_sec)
    analysis_min_win = int(
        min(
            int(getattr(config, "AUTO_TRADE_MIN_WIN", 65)),
            int(getattr(config, "AUTO_TRADE_MIN_WIN_FLOOR", 55)),
        )
    )
    required_score = float(score_gate) if score_gate is not None else float(_pair_min_score(symbol))
    macro_text, macro_data = _macro_snapshot_bundle()
    logger.info(
        f"  ⏳ AI budget: {budget_sec:.1f}s | score gate={required_score:.1f}/10"
    )

    def _remaining_budget() -> float:
        return max(0.0, budget_deadline - time.time())

    def _gpt_timeout_from_remaining(remaining_sec: float) -> float:
        cap = min(float(config.GPT_TIMEOUT_SEC), float(config.GPT_TIMEOUT_HARD_CAP_SEC))
        return max(4.0, min(cap, max(4.0, remaining_sec - config.AI_TIMEOUT_SAFETY_SEC)))

    def _deepseek_timeout_from_remaining(remaining_sec: float) -> float:
        cap = min(
            float(config.DEEPSEEK_TIMEOUT_SEC),
            float(config.DEEPSEEK_TIMEOUT_HARD_CAP_SEC),
        )
        return max(4.0, min(cap, max(4.0, remaining_sec - config.AI_TIMEOUT_SAFETY_SEC)))

    # ══ PRE-SCORE: tính playbook score trước (pure Python, không gọi API) ══
    pre_playbook = _build_pair_playbook_score(symbol, direction, indicators, macro_data)
    pre_score = float(pre_playbook.get("score_total", 0))
    pre_breakdown = pre_playbook.get("score_breakdown", {})
    pre_profile = pre_playbook.get("profile", {})
    logger.info(
        f"  📊 Pre-score: {pre_score:.1f}/10 "
        f"(structure={pre_breakdown.get('candle_structure', 0)}/4, "
        f"sl_safety={pre_breakdown.get('sl_safety', 0)}/4, "
        f"btc={pre_breakdown.get('btc_strength', 0)}/2, "
        f"conflict=-{pre_breakdown.get('trend_conflict_penalty', 0)}) "
        f"wick={pre_playbook.get('max_wick_pct_10', 0)}%"
    )

    # ══ FAST EXIT #0: Score gate (tức thì, không tốn API) ══
    if pre_score < required_score:
        total_time = round(time.time() - total_start, 2)
        logger.info(
            f"  ⚡ FAST EXIT: score={pre_score:.1f}/10 < {required_score:.1f}/10 → SKIP ({total_time}s)"
        )
        # Build skip result với score thật
        skip = _build_skip_result(
            direction,
            {"bias": "NEUTRAL", "prob_long": 50, "prob_short": 50,
             "trend_strength": "weak", "key_observation": ""},
            {"risk_level": "MEDIUM", "risk_score": 50, "squeeze_risk": 50,
             "safe_to_trade": False, "risk_factors": [], "recommendation": ""},
            total_time,
            reason=f"Score {pre_score:.1f}/10 < {required_score:.1f}/10",
        )
        # Gán score thật vào result
        skip["playbook_score"] = pre_score
        skip["score_breakdown"] = dict(pre_breakdown)
        skip["pair_profile"] = (
            f"{pre_profile.get('cluster', 'N/A')} | {pre_profile.get('style', 'N/A')}"
        )
        skip["playbook_warnings"] = list(pre_playbook.get("warnings", []))
        skip["playbook_bonus_notes"] = list(pre_playbook.get("bonus_notes", []))
        skip["layers_called"] = 0
        return skip

    # ══ SCORE-ONLY MODE: bypass ALL AI calls ══
    if bool(getattr(config, "AI_SCORE_ONLY_MODE", False)):
        total_time = round(time.time() - total_start, 2)
        logger.info(
            f"  ✅ SCORE-ONLY PASS: playbook_score={pre_score:.1f}/10 >= {required_score:.1f}/10 "
            f"→ TRADE ({total_time}s, score-only, 0 API calls)"
        )
        # Build validator with pre-score data
        validator = {}
        validator["playbook_score"] = round(float(pre_score), 1)
        validator["score_breakdown"] = dict(pre_breakdown)
        validator["pair_profile"] = (
            f"{pre_profile.get('cluster', symbol.split('-')[0])} | {pre_profile.get('style', 'General')}"
        )
        validator["playbook_warnings"] = pre_playbook.get("warnings", [])
        validator["playbook_bonus_notes"] = pre_playbook.get("bonus_notes", [])
        # Fake analyst/risk for _build_score_trade_result
        analyst = {"bias": direction.upper(), "prob_long": 60, "prob_short": 40,
                    "trend_strength": "medium", "key_observation": "score-only mode"}
        risk = {"risk_level": "LOW", "risk_score": 30, "squeeze_risk": 20,
                "safe_to_trade": True, "risk_factors": [], "recommendation": "score-only"}
        return _build_score_trade_result(
            direction=direction,
            analyst=analyst,
            risk=risk,
            validator=validator,
            total_time=total_time,
            score_gate=required_score,
            ai_budget_sec=budget_sec,
            ai_budget_remaining_sec=budget_sec,
        )

    # ══ STEP 1: L1 + L2 SONG SONG ══
    logger.info(f"  ▶ Step 1: L1 + L2 PARALLEL...")
    step1_start = time.time()
    remaining_before_step1 = _remaining_budget()
    step1_timeout = _gpt_timeout_from_remaining(remaining_before_step1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_l1 = pool.submit(
            layer1_market_analyst,
            symbol,
            indicators,
            macro_text,
            step1_timeout,
        )
        fut_l2 = pool.submit(
            layer2_risk_manager,
            symbol,
            indicators,
            step1_timeout,
        )
        analyst = fut_l1.result()
        risk = fut_l2.result()
    step1_time = round(time.time() - step1_start, 2)
    logger.info(
        f"  ⏱️ Step 1 done: {step1_time}s (parallel, timeout={step1_timeout:.1f}s)"
    )

    # ══ FAST EXIT #1: L1+L2 quá yếu → skip ngay, không tốn thêm API call ══
    l1_bias = analyst.get("bias", "NEUTRAL")
    l1_strength = analyst.get("trend_strength", "weak")
    l2_safe = risk.get("safe_to_trade", False)
    l2_risk_score = risk.get("risk_score", 80)

    # L1 NEUTRAL/weak với direction không match → chắc chắn fail
    # NHƯNG nếu là fallback (GPT timeout) VÀ pre-score cao → cho qua để L3 đánh giá
    bias_matches = (
        (direction == "LONG" and l1_bias == "LONG") or
        (direction == "SHORT" and l1_bias == "SHORT")
    )
    l1_is_fallback = analyst.get("fallback_used", False)
    if not bias_matches and l1_strength == "weak":
        # Nếu L1 timeout (fallback) + pre-score >= 7.5 → bỏ qua L1 check, tiếp tục L3
        if l1_is_fallback and pre_score >= 7.5:
            logger.info(f"  ⚠️ L1 fallback (timeout) nhưng pre-score={pre_score:.1f}≥7.5 → bypass L1 check, tiếp tục L3")
        else:
            total_time = round(time.time() - total_start, 2)
            logger.info(f"  ⚡ FAST EXIT: L1 bias={l1_bias} ({l1_strength}) "
                        f"≠ {direction} → SKIP ({total_time}s)")
            skip = _build_skip_result(
                direction, analyst, risk, total_time,
                reason=f"L1 bias={l1_bias} không match {direction}, strength=weak"
            )
            skip["playbook_score"] = pre_score
            skip["score_breakdown"] = dict(pre_breakdown)
            skip["pair_profile"] = f"{pre_profile.get('cluster', 'N/A')} | {pre_profile.get('style', 'N/A')}"
            return skip

    # L2 nói unsafe + risk cao → không đáng trade
    if not l2_safe and l2_risk_score >= 60:
        total_time = round(time.time() - total_start, 2)
        logger.info(f"  ⚡ FAST EXIT: L2 unsafe (risk={l2_risk_score}) → SKIP ({total_time}s)")
        skip = _build_skip_result(
            direction, analyst, risk, total_time,
            reason=f"L2 risk_score={l2_risk_score}, safe=False"
        )
        skip["playbook_score"] = pre_score
        skip["score_breakdown"] = dict(pre_breakdown)
        skip["pair_profile"] = f"{pre_profile.get('cluster', 'N/A')} | {pre_profile.get('style', 'N/A')}"
        return skip

    remain_before_step2 = _remaining_budget()
    # FAST3L step2 is local synthesis (no extra API), so require far less remaining budget.
    min_before_step2 = (
        1.0 if config.AI_FAST_3L_MODE else float(config.AI_MIN_REMAIN_STEP2_SEC)
    )
    if remain_before_step2 < min_before_step2:
        total_time = round(time.time() - total_start, 2)
        logger.info(
            f"  ⚡ FAST EXIT: remaining budget {remain_before_step2:.1f}s "
            f"< {min_before_step2:.1f}s trước L3 → SKIP ({total_time}s)"
        )
        skip = _build_skip_result(
            direction,
            analyst,
            risk,
            total_time,
            reason=f"AI budget low before L3 ({remain_before_step2:.1f}s)",
        )
        skip["playbook_score"] = pre_score
        skip["score_breakdown"] = dict(pre_breakdown)
        skip["pair_profile"] = f"{pre_profile.get('cluster', 'N/A')} | {pre_profile.get('style', 'N/A')}"
        return skip

    if config.AI_FAST_3L_MODE:
        logger.info("  ▶ Step 2: FAST 3-LAYER QC (L1+L2 synthesis)...")
        dir_prob = int(
            analyst.get("prob_long", 50) if direction == "LONG"
            else analyst.get("prob_short", 50)
        )
        risk_penalty = max(0, int(l2_risk_score) - 15)
        synth_win = max(0, min(95, dir_prob - risk_penalty))
        ob_bias = str(indicators.get("ob_bias", "neutral")).lower()
        cvd_bias = str(indicators.get("cvd_bias", "neutral")).lower()
        flow_ok = (
            (direction == "LONG" and ob_bias == "bullish" and cvd_bias == "bullish")
            or (direction == "SHORT" and ob_bias == "bearish" and cvd_bias == "bearish")
        )
        if not flow_ok:
            synth_win = max(0, synth_win - 10)
        strong_ok = l1_strength == "strong"
        risk_ok = bool(l2_safe and l2_risk_score <= 20)
        grade = "A" if strong_ok and risk_ok and flow_ok else "D"
        decision = (
            "TRADE" if (bias_matches and strong_ok and risk_ok and flow_ok and synth_win >= analysis_min_win) else "SKIP"
        )
        validator = {
            "decision": decision,
            "confidence": dir_prob,
            "direction": direction,
            "win_probability": synth_win,
            "risk_level": risk.get("risk_level", "MEDIUM"),
            "reasoning": (
                f"FAST3L: bias={l1_bias}/{l1_strength}, risk={l2_risk_score}, "
                f"safe={l2_safe}, base_win={synth_win}%"
            ),
            "entry_quality": grade,
            "lessons_applied": "FAST3L mode",
            "l3_vote_quorum": "synth",
        }
    else:
        # ══ STEP 2: L3 Validator ══
        logger.info("  ▶ Step 2: L3 Validator...")
        step2_timeout = _gpt_timeout_from_remaining(remain_before_step2)
        validator = layer3_trade_validator(
            symbol,
            direction,
            analyst,
            risk,
            indicators,
            macro_text,
            macro_data=macro_data,
            timeout_sec=step2_timeout,
        )

    if "playbook_score" not in validator:
        validator["playbook_score"] = round(float(pre_score), 1)
        validator["score_breakdown"] = dict(pre_breakdown)
        validator["pair_profile"] = (
            f"{pre_profile.get('cluster', symbol.split('-')[0])} | {pre_profile.get('style', 'General')}"
        )
        validator["playbook_warnings"] = pre_playbook.get("warnings", [])
        validator["playbook_bonus_notes"] = pre_playbook.get("bonus_notes", [])

    # ══ FAST EXIT #2: score gate (cơ chế chính) ══
    l3_win = validator.get("win_probability", 0)
    l3_grade = validator.get("entry_quality", "D")
    pair_score = float(validator.get("playbook_score", 0) or 0)
    if pair_score < required_score:
        total_time = round(time.time() - total_start, 2)
        logger.info(
            f"  ⚡ FAST EXIT: playbook_score={pair_score:.1f}/10 < {required_score:.1f}/10 "
            f"→ SKIP ({total_time}s)"
        )
        return _build_skip_result(
            direction,
            analyst,
            risk,
            total_time,
            reason=f"Pair playbook score {pair_score:.1f}/10 < {required_score:.1f}/10",
            validator=validator,
        )

    if bool(getattr(config, "AI_SCORE_ONLY_MODE", False)):
        # Fallback — should not reach here if early exit above works
        total_time = round(time.time() - total_start, 2)
        logger.info(
            f"  ✅ SCORE PASS (fallback): playbook_score={pair_score:.1f}/10 >= {required_score:.1f}/10 "
            f"→ TRADE ({total_time}s, score-only)"
        )
        return _build_score_trade_result(
            direction=direction,
            analyst=analyst,
            risk=risk,
            validator=validator,
            total_time=total_time,
            score_gate=required_score,
            ai_budget_sec=budget_sec,
            ai_budget_remaining_sec=max(0.0, budget_deadline - time.time()),
        )

    if l3_win < analysis_min_win:
        # Nếu pre-score cao (≥ 7.5), vẫn cho qua L4/L5/L6 để verify sâu
        # thay vì block ở L3 — L4 Devil's Advocate sẽ bắt trap
        if pair_score >= 7.5:
            logger.info(
                f"  ⚠️ L3 win={l3_win}% < {analysis_min_win}% nhưng pre-score={pair_score:.1f}≥7.5"
                f" → tiếp tục L4/L5/L6 verify"
            )
        else:
            total_time = round(time.time() - total_start, 2)
            logger.info(
                f"  ⚡ FAST EXIT: L3 win={l3_win}% grade={l3_grade} "
                f"< {analysis_min_win}% → SKIP ({total_time}s)"
            )
            return _build_skip_result(
                direction,
                analyst,
                risk,
                total_time,
                reason=f"L3 win={l3_win}% (grade {l3_grade}) < internal gate {analysis_min_win}%",
                validator=validator,
            )

    # Hard budget: nếu không còn đủ thời gian thì bỏ Step 3 để giữ tốc độ scan
    remain_before_step3 = _remaining_budget()
    if remain_before_step3 < config.AI_MIN_REMAIN_STEP3_SEC:
        total_time = round(time.time() - total_start, 2)
        logger.info(
            f"  ⚡ FAST EXIT: remaining budget {remain_before_step3:.1f}s "
            f"< {config.AI_MIN_REMAIN_STEP3_SEC}s → SKIP ({total_time}s)"
        )
        return _build_skip_result(
            direction,
            analyst,
            risk,
            total_time,
            reason=(
                f"AI time budget low ({remain_before_step3:.1f}s) - skip deep layers"
            ),
            validator=validator,
        )

    # ══ STEP 3: EXECUTION + (optional) DeepSeek critics ══
    step3_start = time.time()
    step3_gpt_timeout = _gpt_timeout_from_remaining(remain_before_step3)
    step3_deepseek_timeout = _deepseek_timeout_from_remaining(remain_before_step3)

    step3_mode = "full_6layer"
    layers_called = 6
    l4_hard_veto = True

    if config.AI_FAST_3L_MODE:
        fast3l_deepseek_timeout_cap = max(
            4.0, float(config.FAST3L_DEEPSEEK_TIMEOUT_SEC)
        )
        fast3l_penalty = max(0, int(config.FAST3L_DEEPSEEK_FALLBACK_PENALTY))
        step3_deepseek_timeout = max(
            4.0, min(step3_deepseek_timeout, fast3l_deepseek_timeout_cap)
        )
        if not deepseek_available:
            total_time = round(time.time() - total_start, 2)
            logger.info("  ⚡ FAST EXIT: FAST3L cần DeepSeek nhưng không khả dụng → SKIP")
            return _build_skip_result(
                direction,
                analyst,
                risk,
                total_time,
                reason="FAST3L requires DeepSeek L4 but DeepSeek is unavailable",
                validator=validator,
            )

        logger.info(
            f"  ▶ Step 3: DeepSeek L4 referee (hard gate, to={step3_deepseek_timeout:.1f}s)..."
        )
        devil = layer4_devils_advocate(
            symbol,
            direction,
            analyst,
            risk,
            validator,
            indicators,
            macro_text,
            timeout_sec=step3_deepseek_timeout,
            fail_open=True,
            fast_mode=True,
        )
        if devil.get("error"):
            fallback_win = max(0, int(l3_win) - fast3l_penalty)
            kill_reasons = devil.get("kill_reasons", [])
            if not isinstance(kill_reasons, list):
                kill_reasons = [str(kill_reasons)]
            kill_reasons.append(
                f"L4 timeout/error fallback: win -{fast3l_penalty}%"
            )
            devil.update(
                {
                    "veto": False,
                    "adjusted_win_probability": fallback_win,
                    "kill_reasons": kill_reasons,
                    "final_verdict": "REDUCE_SIZE",
                    "trap_detected": False,
                    "trap_type": "none",
                    "overconfidence_warning": True,
                }
            )
            step3_mode = "fast3l_2gpt_fallback_on_l4_error"
            l4_hard_veto = False
            logger.warning(
                f"  ⚠️ FAST3L fallback: L4 lỗi/timeout, bỏ veto cứng | "
                f"win {l3_win}% -> {fallback_win}%"
            )
        else:
            step3_mode = "fast3l_2gpt_1deepseek"
            l4_hard_veto = True
            if (
                config.FAST3L_ADAPTIVE_VETO
                and l2_safe
                and l1_strength in ("strong", "medium")
                and bias_matches
                and int(l3_win) >= int(config.FAST3L_ADVISORY_MIN_L3_WIN)
                and int(l2_risk_score) <= int(config.FAST3L_ADVISORY_MAX_RISK_SCORE)
                and (devil.get("veto", False) or devil.get("final_verdict") == "REJECT")
            ):
                soft_penalty = max(3, int(config.FAST3L_ADVISORY_SOFT_PENALTY))
                advisory_floor = max(0, int(l3_win) - soft_penalty)
                try:
                    l4_adj_raw = int(devil.get("adjusted_win_probability", advisory_floor))
                except Exception:
                    l4_adj_raw = advisory_floor
                advisory_win = max(0, min(int(l3_win), max(l4_adj_raw, advisory_floor)))
                kill_reasons = devil.get("kill_reasons", [])
                if not isinstance(kill_reasons, list):
                    kill_reasons = [str(kill_reasons)]
                kill_reasons.append(
                    f"Adaptive veto->advisory (l3={l3_win}, risk={l2_risk_score})"
                )
                devil.update(
                    {
                        "veto": False,
                        "final_verdict": "REDUCE_SIZE",
                        "adjusted_win_probability": advisory_win,
                        "kill_reasons": kill_reasons,
                    }
                )
                step3_mode = "fast3l_2gpt_adaptive_veto"
                l4_hard_veto = False
                logger.warning(
                    f"  ⚖️ FAST3L adaptive veto: REJECT -> advisory | "
                    f"win {l3_win}% -> {advisory_win}% (risk={l2_risk_score})"
                )
        # FAST3L: use L4 output as final referee; skip L5/L6 for latency.
        executor = {
            "execute": True,
            "final_win_probability": devil.get("adjusted_win_probability", l3_win),
            "timing_quality": "good",
            "tp_reachable": True,
            "tp_difficulty": "moderate",
            "momentum_check": "building",
            "price_position": "mid_range",
            "entry_timing": "acceptable",
            "execution_notes": ["FAST3L mode: L5 skipped"],
            "final_recommendation": "FAST3L mode",
        }
        regime = {
            "regime": step3_mode,
            "regime_strength": "moderate",
            "regime_supports_trade": True,
            "regime_score": 60,
            "transition_detected": False,
            "transition_to": "none",
            "market_health": "uncertain",
            "tp_compatible": True,
            "regime_confluence": True,
            "ultimate_win_probability": executor.get("final_win_probability", l3_win),
            "ultimate_verdict": "EXECUTE",
            "regime_notes": ["FAST3L mode: L6 skipped for latency"],
            "final_judgment": "FAST3L mode",
        }
        layers_called = 3
        step3_time = round(time.time() - step3_start, 2)
        logger.info(
            f"  ⏱️ Step 3 done: {step3_time}s ({step3_mode}, deepseek_to={step3_deepseek_timeout:.1f}s)"
        )
    elif config.AI_GPT_ONLY_MODE:
        remain_before_assist = _remaining_budget()
        can_run_deepseek_assist = bool(
            deepseek_assist_mode
            and remain_before_assist >= float(config.AI_DEEPSEEK_ASSIST_MIN_REMAIN_SEC)
        )

        devil = {
            "veto": False,
            "adjusted_win_probability": l3_win,
            "final_verdict": "APPROVE",
            "trap_detected": False,
            "trap_type": "none",
            "overconfidence_warning": False,
            "kill_reasons": [],
            "leverage_risk_note": "",
            "reasoning": "Skipped in GPT-only fast mode",
        }

        if can_run_deepseek_assist:
            assist_timeout_cap = max(4.0, float(config.AI_DEEPSEEK_ASSIST_TIMEOUT_SEC))
            assist_timeout = max(
                4.0,
                min(
                    step3_deepseek_timeout,
                    assist_timeout_cap,
                    max(4.0, remain_before_assist - float(config.AI_TIMEOUT_SAFETY_SEC)),
                ),
            )
            logger.info(
                f"  ▶ Step 3: GPT FAST PATH + DeepSeek assist (L4 advisory, to={assist_timeout:.1f}s)..."
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_l5 = pool.submit(
                    layer5_execution_strategist,
                    symbol,
                    direction,
                    analyst,
                    risk,
                    validator,
                    devil,
                    indicators,
                    step3_gpt_timeout,
                )
                fut_l4 = pool.submit(
                    layer4_devils_advocate,
                    symbol,
                    direction,
                    analyst,
                    risk,
                    validator,
                    indicators,
                    macro_text,
                    assist_timeout,
                    True,
                )
                executor = fut_l5.result()
                devil = fut_l4.result()

            step3_mode = "gpt_plus_deepseek_assist"
            layers_called = 5
            l4_hard_veto = False
            regime_note = "DeepSeek assist (L4 advisory only); L6 skipped for latency"
        else:
            logger.info("  ▶ Step 3: GPT-ONLY FAST PATH (L5 only, skip L4/L6)...")
            executor = layer5_execution_strategist(
                symbol,
                direction,
                analyst,
                risk,
                validator,
                devil,
                indicators,
                step3_gpt_timeout,
            )
            step3_mode = "gpt_only"
            layers_called = 4
            l4_hard_veto = False
            regime_note = "DeepSeek layers skipped for latency"

        try:
            rapid_win = int(round(float(executor.get("final_win_probability", l3_win))))
        except Exception:
            rapid_win = int(l3_win)
        regime = {
            "regime": step3_mode,
            "regime_strength": "moderate",
            "regime_supports_trade": True,
            "regime_score": 60,
            "transition_detected": False,
            "transition_to": "none",
            "market_health": "uncertain",
            "tp_compatible": bool(executor.get("tp_reachable", True)),
            "regime_confluence": True,
            "ultimate_win_probability": rapid_win,
            "ultimate_verdict": "EXECUTE" if executor.get("execute", True) else "ABORT",
            "regime_notes": [regime_note],
            "final_judgment": regime_note,
        }
        step3_time = round(time.time() - step3_start, 2)
        logger.info(
            f"  ⏱️ Step 3 done: {step3_time}s ({step3_mode}, gpt_to={step3_gpt_timeout:.1f}s)"
        )
    else:
        logger.info(f"  ▶ Step 3: L4x2 + L5 + L6 ALL PARALLEL (thorough)...")
        with ThreadPoolExecutor(max_workers=4) as pool:
            fut_l4a = pool.submit(
                layer4_devils_advocate,
                symbol,
                direction,
                analyst,
                risk,
                validator,
                indicators,
                macro_text,
                step3_deepseek_timeout,
                True,  # fail_open — timeout != veto
            )
            fut_l4b = pool.submit(
                layer4_devils_advocate,
                symbol,
                direction,
                analyst,
                risk,
                validator,
                indicators,
                macro_text,
                step3_deepseek_timeout,
                True,  # fail_open — timeout != veto
            )
            fut_l5 = pool.submit(
                layer5_execution_strategist,
                symbol,
                direction,
                analyst,
                risk,
                validator,
                # L5 chưa có L4 result → pass placeholder
                {"adjusted_win_probability": l3_win, "final_verdict": "PENDING"},
                indicators,
                step3_gpt_timeout,
            )
            fut_l6 = pool.submit(
                layer6_regime_detector,
                symbol,
                direction,
                analyst,
                risk,
                validator,
                {"adjusted_win_probability": l3_win, "final_verdict": "PENDING"},
                {"execute": True, "final_win_probability": l3_win},
                indicators,
                macro_text,
                step3_deepseek_timeout,
            )
            devil_a = fut_l4a.result()
            devil_b = fut_l4b.result()
            executor = fut_l5.result()
            regime = fut_l6.result()

        # Merge L4 double-check
        err_a = devil_a.get("error", False)
        err_b = devil_b.get("error", False)
        veto_a = (devil_a.get("veto", False) or devil_a.get("final_verdict") == "REJECT") and not err_a
        veto_b = (devil_b.get("veto", False) or devil_b.get("final_verdict") == "REJECT") and not err_b
        if err_a and err_b:
            # Both L4 errored (timeout) → skip L4 veto, use L3 result
            devil = {"veto": False, "adjusted_win_probability": l3_win,
                     "final_verdict": "APPROVE", "error": True,
                     "trap_detected": False, "reasoning": "L4 both timeout, skip veto"}
            logger.warning(
                f"  ⚠️ L4 double-check: both ERROR (timeout) → SKIP veto, keep L3 win={l3_win}%"
            )
        elif veto_a and veto_b:
            # Cả 2 L4 copy đều REJECT → HARD BLOCK, NHƯNG nếu pre-score rất cao thì chuyển thành advisory
            if pair_score >= 8.5:
                devil = devil_a if int(devil_a.get('adjusted_win_probability', 0)) >= int(devil_b.get('adjusted_win_probability', 0)) else devil_b
                devil["veto"] = False
                devil["final_verdict"] = "REDUCE_SIZE"
                logger.info(
                    f"  ⚠️ L4 double-check: veto_a={veto_a} veto_b={veto_b} NHƯNG pre-score={pair_score:.1f}≥8.5 → ADVISORY only"
                )
            else:
                devil = devil_a if veto_a else devil_b
                devil["veto"] = True
                devil["final_verdict"] = "REJECT"
                logger.info(
                    f"  🛡️ L4 double-check: veto_a={veto_a} veto_b={veto_b} → HARD BLOCK"
                )
        elif veto_a or veto_b:
            # Chỉ 1/2 L4 veto → advisory, không hard block
            devil = devil_a if veto_a else devil_b
            devil["veto"] = False
            devil["final_verdict"] = "REDUCE_SIZE"
            logger.info(
                f"  ⚠️ L4 double-check: veto_a={veto_a} veto_b={veto_b} → ADVISORY (1/2 disagree)"
            )
        else:
            # Both APPROVE (or one errored + one approved) → use more conservative
            win_a = int(devil_a.get("adjusted_win_probability", l3_win))
            win_b = int(devil_b.get("adjusted_win_probability", l3_win))
            devil = devil_a if win_a <= win_b else devil_b
            logger.info(
                f"  ✅ L4 double-check: both APPROVE (win_a={win_a}% win_b={win_b}%)"
            )

        step3_time = round(time.time() - step3_start, 2)
        logger.info(
            f"  ⏱️ Step 3 done: {step3_time}s (4-way parallel L4x2+L5+L6, "
            f"gpt_to={step3_gpt_timeout:.1f}s, deepseek_to={step3_deepseek_timeout:.1f}s)"
        )

    total_time = round(time.time() - total_start, 2)
    logger.info(f"🧠 AI {ai_profile_label} Analysis done in {total_time}s "
                f"(steps: {step1_time}s + L3 + {step3_time}s parallel)")

    # ── Compile final result ──
    l3_decision = validator.get("decision", "SKIP")
    l4_verdict = devil.get("final_verdict", "APPROVE")
    l4_veto = devil.get("veto", False)
    l4_adj_win = devil.get("adjusted_win_probability", l3_win)
    l5_execute = executor.get("execute", True)
    l5_win = executor.get("final_win_probability", l4_adj_win)
    l6_verdict = regime.get("ultimate_verdict", "EXECUTE")
    l6_win = regime.get("ultimate_win_probability", l5_win)
    l6_supports = regime.get("regime_supports_trade", True)

    timing_quality = str(executor.get("timing_quality", "")).lower()
    momentum_check = str(executor.get("momentum_check", "")).lower()
    l5_advisory_enabled = bool(
        config.AI_GPT_ONLY_MODE
        and bool(getattr(config, "GPT_L5_ADVISORY_MODE", False))
    )
    l5_advisory_min_l3 = int(getattr(config, "GPT_L5_ADVISORY_MIN_L3_WIN", analysis_min_win))
    l5_advisory_max_risk = int(getattr(config, "GPT_L5_ADVISORY_MAX_RISK_SCORE", 24))
    l5_advisory_penalty = max(0, int(getattr(config, "GPT_L5_ADVISORY_PENALTY", 0)))
    l5_soft_advisory = bool(
        l5_advisory_enabled
        and (not l5_execute)
        and int(l3_win) >= max(int(analysis_min_win), l5_advisory_min_l3)
        and bool(l2_safe)
        and int(l2_risk_score) <= l5_advisory_max_risk
        and l1_strength in ("strong", "medium")
        and timing_quality in ("late", "suboptimal")
        and momentum_check in ("fading", "building", "strong")
    )

    # Pre-score override: nếu pre-score rất cao → chuyển L4/L5/L6 thành advisory
    _high_prescore = pair_score >= 9.0 and l1_strength == "strong" and l2_safe

    # Layer 4 veto (hard in full mode, advisory in assist mode)
    if l4_hard_veto and (l4_veto or l4_verdict == "REJECT"):
        if _high_prescore:
            # Pre-score cao → L4 chỉ là advisory, penalty thay vì block
            final_decision = l3_decision
            final_win = max(l4_adj_win, l3_win - 8)
            logger.info(f"  ⚠️ Layer 4 VETO nhưng pre-score={pair_score:.1f}≥8.0 → ADVISORY "
                        f"(win {l3_win}% → {final_win}%)")
        else:
            final_decision = "SKIP"
            final_win = min(l4_adj_win, l3_win - 10)
            logger.info(f"  ❌ Layer 4 VETO: {l3_decision} → SKIP "
                        f"(win {l3_win}% → {final_win}%)")
    elif (not l4_hard_veto) and (l4_veto or l4_verdict == "REJECT"):
        final_decision = l3_decision
        final_win = min(l3_win, l4_adj_win, l5_win, l6_win)
        logger.info(
            f"  ⚠️ Layer 4 advisory REJECT: keep GPT path but cap win to {final_win}%"
        )
    # Layer 5 reject
    elif not l5_execute:
        if l5_soft_advisory:
            advisory_win = max(0, int(l3_win) - l5_advisory_penalty)
            final_decision = l3_decision
            final_win = advisory_win
            logger.warning(
                f"  ⚖️ Layer 5 advisory override: "
                f"l3={l3_win}%, risk={l2_risk_score}, timing={timing_quality}, "
                f"momentum={momentum_check}, win->{advisory_win}%"
            )
        elif _high_prescore:
            final_decision = l3_decision
            final_win = max(l5_win, l3_win - 10)
            logger.info(f"  ⚠️ Layer 5 NO-EXECUTE nhưng pre-score={pair_score:.1f}≥8.0 → ADVISORY "
                        f"(win={final_win}%)")
        else:
            final_decision = "SKIP"
            final_win = l5_win
            logger.info(f"  ❌ Layer 5 NO-EXECUTE: win={l5_win}%, "
                        f"timing={executor.get('timing_quality')}")
    # Layer 6 abort
    elif l6_verdict == "ABORT" or not l6_supports:
        if _high_prescore and regime.get("regime", "").lower() in ("ranging", "sideways", "choppy"):
            # Ranging market + high pre-score → advisory, không block
            final_decision = l3_decision
            final_win = max(l6_win, l3_win - 10)
            logger.info(f"  ⚠️ Layer 6 ABORT (regime={regime.get('regime')}) nhưng pre-score={pair_score:.1f}≥8.0 → ADVISORY "
                        f"(win={final_win}%)")
        else:
            final_decision = "SKIP"
            final_win = l6_win
            logger.info(f"  ❌ Layer 6 ABORT: regime={regime.get('regime')}, "
                        f"health={regime.get('market_health')}, win={l6_win}%")
    elif l4_verdict == "REDUCE_SIZE":
        final_decision = l3_decision
        final_win = min(l4_adj_win, l5_win, l6_win)
        logger.info(f"  ⚠️ Layer 4 REDUCE: win {l3_win}% → {final_win}%")
    else:  # ALL layers APPROVE
        final_decision = l3_decision
        final_win = min(l3_win, l4_adj_win, l5_win, l6_win)
        # Nếu pre-score cao (≥ 8.0) và L4/L5/L6 đều approve,
        # không để L3 win thấp kéo final_win xuống
        if pair_score >= 8.0 and l3_win < analysis_min_win:
            final_win = min(l4_adj_win, l5_win, l6_win)
            logger.info(f"  ✅ All 6 layers APPROVE: L3 win={l3_win}% thấp nhưng "
                        f"pre-score={pair_score:.1f}≥8.0, dùng L4/L5/L6 win={final_win}%")
        else:
            logger.info(f"  ✅ All 6 layers APPROVE: win={final_win}%")

    final_win = max(0, final_win)
    if final_win >= analysis_min_win:
        final_decision = "TRADE"
        logger.info(f"  ✅ Win {final_win}% >= {analysis_min_win}% internal gate → TRADE")
    elif pair_score >= 9.0 and l1_strength == "strong" and l2_safe:
        # Pre-score rất cao + L1 strong + L2 safe → override final gate
        final_decision = "TRADE"
        final_win = max(final_win, analysis_min_win)
        logger.info(f"  ✅ Pre-score={pair_score:.1f}≥9.0 + L1={l1_strength} + L2=safe → OVERRIDE gate → TRADE")
    else:
        final_decision = "SKIP"
        logger.info(f"  ❌ Win {final_win}% < {analysis_min_win}% internal gate → SKIP")

    result = {
        # Final decision
        "decision": final_decision,
        "confidence": validator.get("confidence", 0),
        "direction": validator.get("direction", direction),
        "win_probability": final_win,
        "win_probability_l3": l3_win,
        "win_probability_l4": l4_adj_win,
        "win_probability_l5": l5_win,
        "win_probability_l6": l6_win,
        "risk_level": validator.get("risk_level", risk.get("risk_level", "HIGH")),
        "reasoning": validator.get("reasoning", ""),
        "skip_reason": validator.get("reasoning", "") if final_decision == "SKIP" else "",
        "entry_quality": validator.get("entry_quality", "D"),
        "lessons_applied": validator.get("lessons_applied", ""),
        "pair_profile": validator.get("pair_profile", ""),
        "playbook_score": validator.get("playbook_score", 0),
        "score_gate": float(required_score),
        "score_breakdown": validator.get("score_breakdown", {}),
        "playbook_warnings": validator.get("playbook_warnings", []),
        "playbook_bonus_notes": validator.get("playbook_bonus_notes", []),

        # Layer 1
        "analyst_bias": analyst.get("bias", "NEUTRAL"),
        "analyst_prob_long": analyst.get("prob_long", 50),
        "analyst_prob_short": analyst.get("prob_short", 50),
        "analyst_strength": analyst.get("trend_strength", "weak"),
        "analyst_key": analyst.get("key_observation", ""),

        # Layer 2
        "risk_score": risk.get("risk_score", 50),
        "squeeze_risk": risk.get("squeeze_risk", 50),
        "risk_factors": risk.get("risk_factors", []),
        "risk_recommendation": risk.get("recommendation", ""),
        "safe_to_trade": risk.get("safe_to_trade", False),

        # Layer 4
        "l4_verdict": l4_verdict,
        "l4_veto": l4_veto,
        "l4_trap_detected": devil.get("trap_detected", False),
        "l4_trap_type": devil.get("trap_type", "none"),
        "l4_overconfidence": devil.get("overconfidence_warning", False),
        "l4_kill_reasons": devil.get("kill_reasons", []),
        "l4_leverage_note": devil.get("leverage_risk_note", ""),
        "l4_reasoning": devil.get("reasoning", ""),
        "l4_mode": "hard_veto" if l4_hard_veto else "advisory",

        # Layer 5
        "l5_execute": l5_execute,
        "l5_timing": executor.get("timing_quality", "acceptable"),
        "l5_tp_reachable": executor.get("tp_reachable", True),
        "l5_tp_difficulty": executor.get("tp_difficulty", "moderate"),
        "l5_momentum": executor.get("momentum_check", "building"),
        "l5_price_position": executor.get("price_position", "mid_range"),
        "l5_entry_timing": executor.get("entry_timing", "acceptable"),
        "l5_notes": executor.get("execution_notes", []),
        "l5_recommendation": executor.get("final_recommendation", ""),

        # Layer 6
        "l6_regime": regime.get("regime", "unknown"),
        "l6_regime_strength": regime.get("regime_strength", "weak"),
        "l6_supports_trade": l6_supports,
        "l6_regime_score": regime.get("regime_score", 50),
        "l6_transition": regime.get("transition_detected", False),
        "l6_transition_to": regime.get("transition_to", "none"),
        "l6_market_health": regime.get("market_health", "uncertain"),
        "l6_tp_compatible": regime.get("tp_compatible", True),
        "l6_confluence": regime.get("regime_confluence", False),
        "l6_verdict": l6_verdict,
        "l6_notes": regime.get("regime_notes", []),
        "l6_judgment": regime.get("final_judgment", ""),

        # Meta
        "total_ai_time": total_time,
        "ai_budget_sec": round(budget_sec, 2),
        "ai_budget_remaining_sec": round(max(0.0, budget_deadline - time.time()), 2),
        "layers_called": layers_called,
        "step3_mode": step3_mode,
    }

    return result


# ═══════════════════════════════════════════════════════════════
# FORMAT FOR TELEGRAM
# ═══════════════════════════════════════════════════════════════

def format_ai_result(result: dict) -> str:
    """Format kết quả 6-layer AI cho Telegram message."""
    decision_emoji = "✅" if result.get("decision") == "TRADE" else "❌"

    risk_map = {
        "LOW": "🟢 Thấp",
        "MEDIUM": "🟡 TB",
        "HIGH": "🔴 Cao",
        "EXTREME": "⛔ Cực cao",
    }
    risk_text = risk_map.get(result.get("risk_level", "HIGH"), "⚪ N/A")

    grade_map = {"A": "⭐ A", "B": "✨ B", "C": "🔸 C", "D": "🔻 D"}
    grade = grade_map.get(result.get("entry_quality", "D"), "D")

    # Layer 1 summary
    analyst_line = (
        f"Xu hướng: {result.get('analyst_bias', 'N/A')} "
        f"({result.get('analyst_strength', 'N/A')})\n"
        f"  Long: {result.get('analyst_prob_long', 0)}% | "
        f"Short: {result.get('analyst_prob_short', 0)}%"
    )

    # Layer 2 summary
    risk_factors = result.get("risk_factors", [])
    risk_line = (
        f"Risk Score: {result.get('risk_score', 'N/A')}/100\n"
        f"  Squeeze: {result.get('squeeze_risk', 'N/A')}%\n"
        f"  Factors: {'; '.join(risk_factors[:3]) if risk_factors else 'Không có'}"
    )

    # Layer 4 summary
    l4_verdict_map = {
        "APPROVE": "✅ Duyệt",
        "REJECT": "🚫 Từ chối",
        "REDUCE_SIZE": "⚠️ Cảnh báo",
    }
    l4_verdict = l4_verdict_map.get(result.get("l4_verdict", "APPROVE"), "❓")
    l4_trap = result.get("l4_trap_type", "none")
    trap_text = {
        "bull_trap": "🪤 Bull Trap",
        "bear_trap": "🪤 Bear Trap",
        "fake_breakout": "🪤 Fake Breakout",
        "whale_manipulation": "🐋 Whale Manipulation",
        "none": "✅ Không phát hiện",
    }.get(l4_trap, "❓")

    l4_kill = result.get("l4_kill_reasons", [])
    l4_kills_text = "\n  ".join(f"• {r}" for r in l4_kill[:3]) if l4_kill else "Không có"

    # Layer 5 summary
    timing_map = {
        "excellent": "🟢 Xuất sắc",
        "good": "🟢 Tốt",
        "late": "🟡 Hơi muộn",
        "too_late": "🔴 Quá muộn",
    }
    momentum_map = {
        "strong": "💪 Mạnh",
        "building": "📈 Đang tăng",
        "fading": "📉 Đang yếu",
        "exhausted": "💀 Kiệt sức",
    }
    tp_diff_map = {
        "easy": "🟢 Dễ",
        "moderate": "🟡 Vừa",
        "hard": "🟠 Khó",
        "very_hard": "🔴 Rất khó",
    }

    l5_timing = timing_map.get(result.get("l5_timing", "acceptable"), "🟡 Chấp nhận")
    l5_momentum = momentum_map.get(result.get("l5_momentum", "building"), "❓")
    l5_tp_diff = tp_diff_map.get(result.get("l5_tp_difficulty", "moderate"), "❓")
    l5_execute = "✅ Thực hiện" if result.get("l5_execute") else "🚫 Không thực hiện"

    # Layer 6 summary
    regime_map = {
        "trending": "📈 Trending",
        "ranging": "↔️ Ranging",
        "volatile": "⚡ Volatile",
        "choppy": "🌊 Choppy",
        "unknown": "❓ Không rõ",
    }
    health_map = {
        "healthy": "🟢 Khỏe mạnh",
        "uncertain": "🟡 Không rõ",
        "stressed": "🟠 Căng thẳng",
        "dangerous": "🔴 Nguy hiểm",
    }
    l6_regime = regime_map.get(result.get("l6_regime", "unknown"), "❓")
    l6_health = health_map.get(result.get("l6_market_health", "uncertain"), "❓")
    l6_verdict = "✅ EXECUTE" if result.get("l6_verdict") == "EXECUTE" else "🚫 ABORT"
    l6_tp = "✅ Có thể" if result.get("l6_tp_compatible") else "❌ Khó đạt"
    l6_transition = "⚠️ Đang chuyển!" if result.get("l6_transition") else "✅ Ổn định"
    l6_confluence = "✅ Có" if result.get("l6_confluence") else "❌ Không"

    win_l3 = result.get("win_probability_l3", result.get("win_probability", 0))
    win_l4 = result.get("win_probability_l4", win_l3)
    win_l5 = result.get("win_probability_l5", win_l4)
    win_l6 = result.get("win_probability_l6", win_l5)
    win_final = result.get("win_probability", 0)

    profile_label = "FAST3L" if config.AI_FAST_3L_MODE else "6-Layer"
    text = (
        f"🧠 AI {profile_label} Analysis\n"
        f"{'━' * 28}\n\n"
        f"📊 L1 — Market Analyst:\n"
        f"  {analyst_line}\n\n"
        f"🛡️ L2 — Risk Manager:\n"
        f"  {risk_line}\n\n"
        f"⚖️ L3 — Validator:\n"
        f"  Win: {win_l3}% | Conf: {result.get('confidence', 0)}%\n"
        f"  Playbook: {result.get('playbook_score', 0)}/10 | "
        f"Pair: {result.get('pair_profile', 'N/A')}\n"
        f"  Rủi ro: {risk_text} | Grade: {grade}\n\n"
        f"😈 L4 — Devil's Advocate:\n"
        f"  Phán quyết: {l4_verdict}\n"
        f"  Trap: {trap_text}\n"
        f"  Phản biện:\n  {l4_kills_text}\n"
        f"  Win sau L4: {win_l4}%\n\n"
        f"🎯 L5 — Execution Strategist:\n"
        f"  Quyết định: {l5_execute}\n"
        f"  Timing: {l5_timing}\n"
        f"  Momentum: {l5_momentum}\n"
        f"  TP đạt được: {l5_tp_diff}\n"
        f"  Win sau L5: {win_l5}%\n\n"
        f"🌐 L6 — Market Regime Detector:\n"
        f"  Chế độ: {l6_regime} ({result.get('l6_regime_strength', 'N/A')})\n"
        f"  Sức khỏe TT: {l6_health}\n"
        f"  Hỗ trợ trade: {l6_confluence}\n"
        f"  TP tương thích: {l6_tp}\n"
        f"  Chuyển đổi: {l6_transition}\n"
        f"  Phán quyết: {l6_verdict}\n"
        f"  Win sau L6: {win_l6}%\n\n"
        f"{'━' * 28}\n"
        f"{decision_emoji} KẾT LUẬN: {result.get('decision', 'N/A')}\n"
        f"Win: {win_l3}% → {win_l4}% → {win_l5}% → {win_l6}% → {win_final}%\n"
        f"💬 {result.get('reasoning', 'N/A')}\n"
        f"{'━' * 28}\n"
        f"⏱️ AI time: {result.get('total_ai_time', 0)}s (6 layers)"
    )

    return text
