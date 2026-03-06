# -*- coding: utf-8 -*-
"""
indicators.py — Tính các chỉ báo alpha cho trading

Chỉ báo:
1. CVD  (Cumulative Volume Delta)  — lực mua vs bán
2. OI   (Open Interest Change)     — mức leverage thị trường
3. Orderbook Imbalance             — áp lực bid/ask
4. Funding Rate                    — chi phí giữ vị thế
"""


def calc_cvd(trades: list[dict]) -> dict:
    """
    Tính CVD từ danh sách trades gần đây.

    Mỗi trade: {"side": "buy"/"sell", "sz": float, "px": float}
    Returns: {"cvd": float, "buy_vol": float, "sell_vol": float, "bias": str}
    """
    buy_vol = 0.0
    sell_vol = 0.0

    for t in trades:
        vol = float(t.get("sz", 0)) * float(t.get("px", 0))
        if t.get("side") == "buy":
            buy_vol += vol
        else:
            sell_vol += vol

    cvd = buy_vol - sell_vol
    total = buy_vol + sell_vol

    if total == 0:
        bias = "neutral"
    elif cvd / total > 0.1:
        bias = "bullish"
    elif cvd / total < -0.1:
        bias = "bearish"
    else:
        bias = "neutral"

    return {
        "cvd": round(cvd, 2),
        "buy_vol": round(buy_vol, 2),
        "sell_vol": round(sell_vol, 2),
        "bias": bias,
    }


def calc_oi_change(current_oi: float, prev_oi: float) -> dict:
    """
    Tính % thay đổi Open Interest.

    Returns: {"oi_current": float, "oi_prev": float, "oi_change_pct": float, "signal": str}
    """
    if prev_oi == 0:
        change_pct = 0.0
    else:
        change_pct = ((current_oi - prev_oi) / prev_oi) * 100

    # OI tăng quá nhanh → nguy hiểm (squeeze risk)
    if abs(change_pct) > 5:
        signal = "danger"
    elif abs(change_pct) > 3:
        signal = "warning"
    else:
        signal = "normal"

    return {
        "oi_current": current_oi,
        "oi_prev": prev_oi,
        "oi_change_pct": round(change_pct, 2),
        "signal": signal,
    }


def calc_orderbook_imbalance(bids: list, asks: list, depth: int = 10) -> dict:
    """
    Tính tỷ lệ bid/ask volume từ orderbook.

    bids/asks: [[price, amount, ...], ...]
    Returns: {"bid_vol": float, "ask_vol": float, "imbalance": float, "bias": str}
    """
    bid_vol = sum(float(b[1]) for b in bids[:depth]) if bids else 0
    ask_vol = sum(float(a[1]) for a in asks[:depth]) if asks else 0

    if ask_vol == 0:
        imbalance = 999.0
    else:
        imbalance = bid_vol / ask_vol

    if imbalance > 1.5:
        bias = "bullish"
    elif imbalance < 0.67:
        bias = "bearish"
    else:
        bias = "neutral"

    return {
        "bid_vol": round(bid_vol, 2),
        "ask_vol": round(ask_vol, 2),
        "imbalance": round(imbalance, 3),
        "bias": bias,
    }


def analyze_funding_rate(funding_rate: float) -> dict:
    """
    Đánh giá funding rate.

    Returns: {"funding_rate": float, "signal": str, "note": str}
    """
    rate = funding_rate

    if rate > 0.05:
        signal = "extreme_long"
        note = "Funding quá cao — nhiều long, risk short squeeze ngược"
    elif rate > 0.01:
        signal = "high_long"
        note = "Funding cao — thị trường nghiêng long"
    elif rate < -0.05:
        signal = "extreme_short"
        note = "Funding âm mạnh — nhiều short, risk long squeeze"
    elif rate < -0.01:
        signal = "high_short"
        note = "Funding âm — thị trường nghiêng short"
    else:
        signal = "neutral"
        note = "Funding bình thường"

    return {
        "funding_rate": rate,
        "funding_pct": round(rate * 100, 4),
        "signal": signal,
        "note": note,
    }


def analyze_candles(candles_5m: list, candles_15m: list) -> dict:
    """
    Phân tích xu hướng từ nến 5m (entry) và 15m (trend).
    OKX trả về newest first: [ts, open, high, low, close, vol, ...]
    EMA9 và EMA21 trên close → xác định uptrend/downtrend/sideways.
    """
    def _ema(prices: list, period: int) -> float:
        if not prices or len(prices) < 2:
            return prices[-1] if prices else 0.0
        k = 2.0 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = p * k + ema * (1 - k)
        return ema

    def _analyze_tf(candles: list) -> dict:
        if not candles or len(candles) < 5:
            return {"trend": "unknown", "ema_bias": "neutral",
                    "ema9": 0.0, "ema21": 0.0, "close": 0.0, "body_bias": "neutral"}
        # OKX newest first → reverse để oldest first cho EMA
        closes = [float(c[4]) for c in reversed(candles)]
        opens = [float(c[1]) for c in reversed(candles)]
        ema9 = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        cur_close = closes[-1]
        cur_open = opens[-1]
        # EMA trend
        if ema9 > ema21 * 1.0002:
            ema_bias, trend = "bullish", "uptrend"
        elif ema9 < ema21 * 0.9998:
            ema_bias, trend = "bearish", "downtrend"
        else:
            ema_bias, trend = "neutral", "sideways"
        # Current candle body
        if cur_close > cur_open * 1.0005:
            body_bias = "bullish"
        elif cur_close < cur_open * 0.9995:
            body_bias = "bearish"
        else:
            body_bias = "neutral"
        return {
            "trend": trend,
            "ema_bias": ema_bias,
            "ema9": round(ema9, 6),
            "ema21": round(ema21, 6),
            "close": round(cur_close, 6),
            "body_bias": body_bias,
        }

    tf_5m = _analyze_tf(candles_5m)
    tf_15m = _analyze_tf(candles_15m)

    biases = [tf_5m["ema_bias"], tf_15m["ema_bias"]]
    bull_cnt = biases.count("bullish")
    bear_cnt = biases.count("bearish")

    if bull_cnt == 2:
        overall = "strong_bullish"
    elif bull_cnt == 1 and bear_cnt == 0:
        overall = "weak_bullish"
    elif bear_cnt == 2:
        overall = "strong_bearish"
    elif bear_cnt == 1 and bull_cnt == 0:
        overall = "weak_bearish"
    else:
        overall = "mixed"  # 5m vs 15m mâu thuẫn → nguy hiểm

    return {
        "tf_5m": tf_5m,
        "tf_15m": tf_15m,
        "overall": overall,
        "aligned": bull_cnt == 2 or bear_cnt == 2,
    }


def analyze_micro_setup(candles_1m: list, candles_5m: list) -> dict:
    """
    Tinh feature nhanh cho playbook theo tung cap:
    - Wick/rut chan M1
    - Breakout M1
    - EMA21 M1
    - Bollinger edge M1
    - Support/Resistance M5
    - Order block gan gia M5 (xap xi)
    """

    def _default() -> dict:
        return {
            "m1_ema21": 0.0,
            "m1_price_vs_ema21": "unknown",
            "m1_breakout_up": False,
            "m1_breakout_down": False,
            "m1_max_wick_pct_10": 0.0,
            "m1_last_upper_wick_pct": 0.0,
            "m1_last_lower_wick_pct": 0.0,
            "m1_bull_streak": 0,
            "m1_bear_streak": 0,
            "m1_long_wick_bullish": False,
            "m1_long_wick_bearish": False,
            "m1_bull_engulfing": False,
            "m1_bear_engulfing": False,
            "m1_rejection_signal": "none",
            "m1_volume_surge_pct": 0.0,
            "m1_bb_position": "mid",
            "m1_bb_touch_upper": False,
            "m1_bb_touch_lower": False,
            "m1_bb_width_pct": 0.0,
            "m1_bb_width_ratio": 1.0,
            "m1_bb_squeeze": False,
            "m1_bb_expansion": False,
            "m1_market_regime_hint": "unknown",
            "m5_touch_resistance": False,
            "m5_touch_support": False,
            "m5_last_upper_wick_pct": 0.0,
            "m5_last_lower_wick_pct": 0.0,
            "m5_last_body_pct": 0.0,
            "m5_last_range_pct": 0.0,
            "m5_pinbar_bullish": False,
            "m5_pinbar_bearish": False,
            "m5_wave_ready": False,
            "m5_order_block_bias": "unknown",
            "m5_order_block_near": False,
        }

    out = _default()

    def _safe_float(value, fallback=0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(fallback)

    def _ema(prices: list[float], period: int) -> float:
        if not prices:
            return 0.0
        k = 2.0 / (period + 1)
        ema_value = prices[0]
        for p in prices[1:]:
            ema_value = p * k + ema_value * (1 - k)
        return ema_value

    def _parse_candle(raw: list) -> dict | None:
        if not raw or len(raw) < 6:
            return None
        o = _safe_float(raw[1])
        h = _safe_float(raw[2])
        l = _safe_float(raw[3])
        c = _safe_float(raw[4])
        v = _safe_float(raw[5])
        if c <= 0 or h <= 0 or l <= 0:
            return None
        return {"open": o, "high": h, "low": l, "close": c, "vol": v}

    m1 = [_parse_candle(c) for c in reversed(candles_1m or [])]
    m1 = [c for c in m1 if c is not None]
    m5 = [_parse_candle(c) for c in reversed(candles_5m or [])]
    m5 = [c for c in m5 if c is not None]

    if len(m1) < 5:
        return out

    closes = [c["close"] for c in m1]
    vols = [c["vol"] for c in m1]
    last = m1[-1]
    prev = m1[-2] if len(m1) >= 2 else m1[-1]
    last_close = last["close"]

    ema21 = _ema(closes, 21)
    out["m1_ema21"] = round(ema21, 6)
    if last_close > ema21 * 1.0005:
        out["m1_price_vs_ema21"] = "above"
    elif last_close < ema21 * 0.9995:
        out["m1_price_vs_ema21"] = "below"
    else:
        out["m1_price_vs_ema21"] = "near"

    def _wick_parts(candle: dict) -> tuple[float, float]:
        body_high = max(candle["open"], candle["close"])
        body_low = min(candle["open"], candle["close"])
        px = max(candle["close"], 1e-9)
        upper = max(0.0, candle["high"] - body_high) / px * 100
        lower = max(0.0, body_low - candle["low"]) / px * 100
        return upper, lower

    recent_10 = m1[-10:] if len(m1) >= 10 else m1
    max_wick = 0.0
    for c in recent_10:
        up, low = _wick_parts(c)
        max_wick = max(max_wick, up, low)
    out["m1_max_wick_pct_10"] = round(max_wick, 3)

    last_up, last_low = _wick_parts(last)
    out["m1_last_upper_wick_pct"] = round(last_up, 3)
    out["m1_last_lower_wick_pct"] = round(last_low, 3)

    bull_streak = 0
    bear_streak = 0
    for c in reversed(m1):
        if c["close"] > c["open"] * 1.0001:
            if bear_streak == 0:
                bull_streak += 1
            else:
                break
        elif c["close"] < c["open"] * 0.9999:
            if bull_streak == 0:
                bear_streak += 1
            else:
                break
        else:
            break
    out["m1_bull_streak"] = int(bull_streak)
    out["m1_bear_streak"] = int(bear_streak)

    out["m1_bull_engulfing"] = bool(
        last["close"] > last["open"]
        and prev["close"] < prev["open"]
        and last["close"] >= prev["open"]
        and last["open"] <= prev["close"]
    )
    out["m1_bear_engulfing"] = bool(
        last["close"] < last["open"]
        and prev["close"] > prev["open"]
        and last["open"] >= prev["close"]
        and last["close"] <= prev["open"]
    )

    if out["m1_last_lower_wick_pct"] >= max(0.10, out["m1_last_upper_wick_pct"] * 1.3):
        out["m1_rejection_signal"] = "bullish_rejection"
    elif out["m1_last_upper_wick_pct"] >= max(0.10, out["m1_last_lower_wick_pct"] * 1.3):
        out["m1_rejection_signal"] = "bearish_rejection"
    else:
        out["m1_rejection_signal"] = "none"
    out["m1_long_wick_bullish"] = out["m1_rejection_signal"] == "bullish_rejection"
    out["m1_long_wick_bearish"] = out["m1_rejection_signal"] == "bearish_rejection"

    if len(vols) >= 2:
        base_vol = vols[-61:-1] if len(vols) > 60 else vols[:-1]
        avg_vol = sum(base_vol) / len(base_vol) if base_vol else 0.0
        if avg_vol > 0:
            out["m1_volume_surge_pct"] = round(((vols[-1] - avg_vol) / avg_vol) * 100, 2)

    if len(m1) >= 21:
        lookback = m1[-21:-1]
        prev_high = max(c["high"] for c in lookback)
        prev_low = min(c["low"] for c in lookback)
        out["m1_breakout_up"] = bool(last_close > prev_high * 1.0005)
        out["m1_breakout_down"] = bool(last_close < prev_low * 0.9995)

    if len(m1) >= 20:
        closes20 = closes[-20:]
        ma20 = sum(closes20) / len(closes20)
        variance = sum((c - ma20) ** 2 for c in closes20) / len(closes20)
        std20 = variance ** 0.5
        upper = ma20 + 2 * std20
        lower = ma20 - 2 * std20
        if std20 <= 0:
            out["m1_bb_position"] = "mid"
        elif last_close >= upper * 0.998:
            out["m1_bb_position"] = "upper"
        elif last_close <= lower * 1.002:
            out["m1_bb_position"] = "lower"
        else:
            out["m1_bb_position"] = "mid"
        out["m1_bb_touch_upper"] = bool(last_close >= upper * 0.998) if std20 > 0 else False
        out["m1_bb_touch_lower"] = bool(last_close <= lower * 1.002) if std20 > 0 else False
        width_pct = ((upper - lower) / ma20 * 100.0) if ma20 > 0 else 0.0
        out["m1_bb_width_pct"] = round(width_pct, 4)

        # BB width regime hint: compare current width with rolling historical width.
        width_series = []
        if len(closes) >= 24:
            for idx in range(19, len(closes)):
                win = closes[idx - 19:idx + 1]
                ma = sum(win) / len(win)
                if ma <= 0:
                    continue
                var = sum((c - ma) ** 2 for c in win) / len(win)
                std = var ** 0.5
                width_series.append((4.0 * std / ma) * 100.0)
        if width_series:
            cur_width = width_series[-1]
            hist = width_series[-16:-1] if len(width_series) > 15 else width_series[:-1]
            if not hist:
                hist = [cur_width]
            hist_avg = sum(hist) / len(hist) if hist else cur_width
            width_ratio = cur_width / hist_avg if hist_avg > 0 else 1.0
        else:
            width_ratio = 1.0

        out["m1_bb_width_ratio"] = round(width_ratio, 3)
        out["m1_bb_squeeze"] = bool(width_ratio <= 0.85)
        out["m1_bb_expansion"] = bool(width_ratio >= 1.20)
        if out["m1_bb_squeeze"]:
            out["m1_market_regime_hint"] = "sideway"
        elif out["m1_bb_expansion"]:
            out["m1_market_regime_hint"] = "trend"
        else:
            out["m1_market_regime_hint"] = "mixed"

    if m5:
        m5_last = m5[-1]
        m5_last_up, m5_last_low = _wick_parts(m5_last)
        out["m5_last_upper_wick_pct"] = round(m5_last_up, 3)
        out["m5_last_lower_wick_pct"] = round(m5_last_low, 3)
        px = max(m5_last["close"], 1e-9)
        body_pct = abs(m5_last["close"] - m5_last["open"]) / px * 100
        range_pct = max(0.0, m5_last["high"] - m5_last["low"]) / px * 100
        out["m5_last_body_pct"] = round(body_pct, 3)
        out["m5_last_range_pct"] = round(range_pct, 3)
        wick_body_ratio_low = m5_last_low / max(1e-9, body_pct)
        wick_body_ratio_up = m5_last_up / max(1e-9, body_pct)
        body_ratio = body_pct / max(1e-9, range_pct)
        out["m5_pinbar_bullish"] = bool(
            wick_body_ratio_low >= 1.8
            and m5_last_low >= m5_last_up * 1.2
            and body_ratio <= 0.35
        )
        out["m5_pinbar_bearish"] = bool(
            wick_body_ratio_up >= 1.8
            and m5_last_up >= m5_last_low * 1.2
            and body_ratio <= 0.35
        )
        out["m5_wave_ready"] = bool(max(m5_last_up, m5_last_low) >= 0.10)

    if len(m5) >= 6:
        m5_last = m5[-1]
        hist = m5[-21:-1] if len(m5) > 20 else m5[:-1]
        if hist:
            hist_high = max(c["high"] for c in hist)
            hist_low = min(c["low"] for c in hist)
            px = max(m5_last["close"], 1e-9)
            out["m5_touch_resistance"] = bool(abs(m5_last["close"] - hist_high) / px <= 0.0015)
            out["m5_touch_support"] = bool(abs(m5_last["close"] - hist_low) / px <= 0.0015)

        sample = m5[-21:-1] if len(m5) > 20 else m5[:-1]
        if sample:
            ob_candle = max(sample, key=lambda c: c["vol"])
            out["m5_order_block_bias"] = (
                "bullish" if ob_candle["close"] >= ob_candle["open"] else "bearish"
            )
            low = min(ob_candle["open"], ob_candle["close"], ob_candle["low"])
            high = max(ob_candle["open"], ob_candle["close"], ob_candle["high"])
            now = m5_last["close"]
            out["m5_order_block_near"] = bool((low * 0.998) <= now <= (high * 1.002))

    return out


def analyze_sr_levels(
    candles_15m: list,
    lookback: int = 24,
    near_threshold_pct: float = 0.35,
    break_threshold_pct: float = 0.08,
) -> dict:
    """
    Build simple support/resistance context from 15m candles.

    Returns:
    {
      "m15_support": float,
      "m15_resistance": float,
      "m15_price": float,
      "m15_dist_to_support_pct": float,
      "m15_dist_to_resistance_pct": float,
      "m15_near_support": bool,
      "m15_near_resistance": bool,
      "m15_breakdown_support": bool,
      "m15_breakout_resistance": bool,
      "m15_sr_bias": str
    }
    """

    def _default() -> dict:
        return {
            "m15_support": 0.0,
            "m15_resistance": 0.0,
            "m15_price": 0.0,
            "m15_dist_to_support_pct": 999.0,
            "m15_dist_to_resistance_pct": 999.0,
            "m15_near_support": False,
            "m15_near_resistance": False,
            "m15_breakdown_support": False,
            "m15_breakout_resistance": False,
            "m15_sr_bias": "unknown",
        }

    out = _default()

    def _safe_float(value, fallback=0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(fallback)

    def _parse_candle(raw: list) -> dict | None:
        if not raw or len(raw) < 6:
            return None
        o = _safe_float(raw[1])
        h = _safe_float(raw[2])
        l = _safe_float(raw[3])
        c = _safe_float(raw[4])
        if c <= 0 or h <= 0 or l <= 0:
            return None
        return {"open": o, "high": h, "low": l, "close": c}

    m15 = [_parse_candle(c) for c in reversed(candles_15m or [])]
    m15 = [c for c in m15 if c is not None]
    if len(m15) < 6:
        return out

    # Prefer closed candle to reduce repaint noise
    ref_idx = -2 if len(m15) >= 2 else -1
    ref = m15[ref_idx]
    if ref["close"] <= 0:
        return out

    history_end = ref_idx if ref_idx != -1 else len(m15) - 1
    start = max(0, history_end - max(6, int(lookback)))
    history = m15[start:history_end]
    if len(history) < 4:
        history = m15[:-1]
    if len(history) < 4:
        return out

    support = min(c["low"] for c in history)
    resistance = max(c["high"] for c in history)
    price = ref["close"]

    dist_support = max(0.0, (price - support) / max(price, 1e-9) * 100.0)
    dist_resistance = max(0.0, (resistance - price) / max(price, 1e-9) * 100.0)
    near_threshold = max(0.05, float(near_threshold_pct))
    break_threshold = max(0.01, float(break_threshold_pct))

    near_support = dist_support <= near_threshold
    near_resistance = dist_resistance <= near_threshold
    breakdown_support = price < support * (1 - break_threshold / 100.0)
    breakout_resistance = price > resistance * (1 + break_threshold / 100.0)

    if breakdown_support:
        sr_bias = "breakdown"
    elif breakout_resistance:
        sr_bias = "breakout"
    elif near_support and not near_resistance:
        sr_bias = "support"
    elif near_resistance and not near_support:
        sr_bias = "resistance"
    else:
        sr_bias = "mid"

    out.update(
        {
            "m15_support": round(support, 8),
            "m15_resistance": round(resistance, 8),
            "m15_price": round(price, 8),
            "m15_dist_to_support_pct": round(dist_support, 4),
            "m15_dist_to_resistance_pct": round(dist_resistance, 4),
            "m15_near_support": bool(near_support),
            "m15_near_resistance": bool(near_resistance),
            "m15_breakdown_support": bool(breakdown_support),
            "m15_breakout_resistance": bool(breakout_resistance),
            "m15_sr_bias": sr_bias,
        }
    )
    return out


def analyze_btc_trend(candles_5m: list, candles_15m: list) -> dict:
    """
    Phân tích xu hướng BTC để lọc hướng trade altcoin.
    Dùng EMA9/EMA21 trên 5m + 15m giống analyze_candles.

    Returns: {
        "trend": "bullish" | "bearish" | "neutral",
        "strength": "strong" | "weak" | "neutral",
        "allow_long": bool,
        "allow_short": bool,
        "detail": str,
    }
    """
    candle_result = analyze_candles(candles_5m, candles_15m)
    overall = candle_result.get("overall", "mixed")

    if overall == "strong_bullish":
        return {
            "trend": "bullish",
            "strength": "strong",
            "allow_long": True,
            "allow_short": False,
            "detail": "BTC 5m+15m đều bullish → chỉ LONG",
        }
    elif overall == "strong_bearish":
        return {
            "trend": "bearish",
            "strength": "strong",
            "allow_long": False,
            "allow_short": True,
            "detail": "BTC 5m+15m đều bearish → chỉ SHORT",
        }
    elif overall == "weak_bullish":
        return {
            "trend": "bullish",
            "strength": "weak",
            "allow_long": True,
            "allow_short": True,
            "detail": "BTC hơi bullish → ưu tiên LONG, SHORT cẩn thận",
        }
    elif overall == "weak_bearish":
        return {
            "trend": "bearish",
            "strength": "weak",
            "allow_long": True,
            "allow_short": True,
            "detail": "BTC hơi bearish → ưu tiên SHORT, LONG cẩn thận",
        }
    else:
        return {
            "trend": "neutral",
            "strength": "neutral",
            "allow_long": True,
            "allow_short": True,
            "detail": "BTC sideways/mixed → không lọc hướng",
        }


def generate_signal(cvd_data: dict, oi_data: dict, ob_data: dict,
                    funding_data: dict, candle_data: dict = None) -> dict:
    """
    Tổng hợp tất cả indicators → quyết định có signal hay không.

    Returns: {"has_signal": bool, "direction": str, "strength": int, "reasons": list}
    """
    reasons = []
    long_score = 0
    short_score = 0

    # 0. Candle timeframe filter (5m + 15m) + micro EMA hint if available
    if candle_data is not None:
        overall = candle_data.get("overall", "unknown")
        if overall == "mixed":
            # 5m vs 15m mâu thuẫn — giảm điểm nhưng KHÔNG block hoàn toàn
            # Cho phép scalp nếu CVD + OB đủ mạnh
            long_score -= 1
            short_score -= 1
            reasons.append("5m vs 15m mâu thuẫn — giảm điểm (scalp mode)")
        elif overall == "strong_bullish":
            long_score += 2
            short_score -= 3  # PHẠT NẶNG: SHORT trong uptrend rõ ràng
            reasons.append("Nến 5m + 15m đều bullish — uptrend mạnh (SHORT bị phạt -3)")
        elif overall == "strong_bearish":
            short_score += 2
            long_score -= 3  # PHẠT NẶNG: LONG trong downtrend rõ ràng
            reasons.append("Nến 5m + 15m đều bearish — downtrend mạnh (LONG bị phạt -3)")
        elif overall == "weak_bullish":
            long_score += 1
            short_score -= 1  # Phạt nhẹ SHORT khi có dấu hiệu bullish
            reasons.append("1/2 khung bullish — SHORT bị phạt nhẹ")
        elif overall == "weak_bearish":
            short_score += 1
            long_score -= 1  # Phạt nhẹ LONG khi có dấu hiệu bearish
            reasons.append("1/2 khung bearish — LONG bị phạt nhẹ")

        # Price vs EMA21: phạt thêm khi đi ngược trend
        price_vs_ema = candle_data.get("m1_price_vs_ema21", "unknown")
        if price_vs_ema == "above":
            short_score -= 1  # Giá trên EMA21 → SHORT rủi ro
            reasons.append("Giá trên EMA21 — SHORT bị phạt -1")
        elif price_vs_ema == "below":
            long_score -= 1  # Giá dưới EMA21 → LONG rủi ro
            reasons.append("Giá dưới EMA21 — LONG bị phạt -1")

    # 1. CVD
    if cvd_data["bias"] == "bullish":
        long_score += 2
        reasons.append("CVD bullish — lực mua mạnh")
    elif cvd_data["bias"] == "bearish":
        short_score += 2
        reasons.append("CVD bearish — lực bán mạnh")

    # 2. Orderbook imbalance
    if ob_data["bias"] == "bullish":
        long_score += 2
        reasons.append(f"Orderbook imbalance {ob_data['imbalance']:.2f} — nhiều bid")
    elif ob_data["bias"] == "bearish":
        short_score += 2
        reasons.append(f"Orderbook imbalance {ob_data['imbalance']:.2f} — nhiều ask")

    # 3. Funding rate
    if funding_data["signal"] in ("high_short", "extreme_short"):
        long_score += 1
        reasons.append("Funding âm — short crowded, long có lợi")
    elif funding_data["signal"] in ("high_long", "extreme_long"):
        short_score += 1
        reasons.append("Funding cao — long crowded, short có lợi")

    # 4. OI — nếu OI tăng quá nhanh, giảm signal
    if oi_data["signal"] == "danger":
        long_score -= 1
        short_score -= 1
        reasons.append(f"OI thay đổi {oi_data['oi_change_pct']}% — squeeze risk cao")
    elif oi_data["signal"] == "warning":
        reasons.append(f"OI thay đổi {oi_data['oi_change_pct']}% — cần cẩn thận")

    # Quyết định
    min_score = 4  # Cần candle + orderflow đồng thuận (CVD+OB+candle)
    min_margin = 2  # Hướng thắng phải dẫn >= 2 điểm để tránh tín hiệu yếu

    if long_score >= min_score and (long_score - short_score) >= min_margin and long_score > short_score:
        return {
            "has_signal": True,
            "direction": "LONG",
            "strength": long_score,
            "reasons": reasons,
        }
    elif short_score >= min_score and (short_score - long_score) >= min_margin and short_score > long_score:
        return {
            "has_signal": True,
            "direction": "SHORT",
            "strength": short_score,
            "reasons": reasons,
        }
    else:
        return {
            "has_signal": False,
            "direction": "NONE",
            "strength": max(long_score, short_score),
            "reasons": reasons if reasons else ["Không đủ tín hiệu"],
        }
