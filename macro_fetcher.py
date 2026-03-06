# -*- coding: utf-8 -*-
"""
macro_fetcher.py - Fetch macro data for AI intraday analysis.

Sources:
1. Fear & Greed Index - Alternative.me
2. BTC Dominance + Market Cap - CoinGecko
3. BTC 24h trend - CoinGecko
"""

import json
import logging
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("fubot.macro")

# Cache macro data
_macro_cache: dict = {}
_cache_ts = 0.0
_CACHE_TTL = int(os.getenv("MACRO_CACHE_TTL_SEC", "1800"))
_cache_lock = threading.Lock()


def _fetch_json(url: str, timeout: int = 8) -> dict | None:
    """Fetch JSON from a public URL."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "FuBot/1.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"Macro fetch failed ({url[:60]}...): {e}")
        return None


def _get_fear_greed() -> dict:
    """Fear & Greed Index from Alternative.me."""
    data = _fetch_json("https://api.alternative.me/fng/?limit=1")
    if data and data.get("data"):
        fg = data["data"][0]
        value = int(fg.get("value", 50))
        classification = fg.get("value_classification", "Neutral")
        return {
            "fear_greed_value": value,
            "fear_greed_label": classification,
            "fear_greed_signal": (
                "EXTREME_FEAR"
                if value <= 20
                else "FEAR"
                if value <= 40
                else "NEUTRAL"
                if value <= 60
                else "GREED"
                if value <= 80
                else "EXTREME_GREED"
            ),
        }
    return {
        "fear_greed_value": 50,
        "fear_greed_label": "Neutral",
        "fear_greed_signal": "NEUTRAL",
    }


def _get_global_market() -> dict:
    """BTC dominance + market cap from CoinGecko."""
    data = _fetch_json("https://api.coingecko.com/api/v3/global")
    if data and data.get("data"):
        mkt = data["data"]
        btc_dom = round(mkt.get("market_cap_percentage", {}).get("btc", 0), 1)
        eth_dom = round(mkt.get("market_cap_percentage", {}).get("eth", 0), 1)
        total_mcap = round(mkt.get("total_market_cap", {}).get("usd", 0) / 1e9, 1)
        mcap_change = round(mkt.get("market_cap_change_percentage_24h_usd", 0), 2)
        total_vol = round(mkt.get("total_volume", {}).get("usd", 0) / 1e9, 1)
        return {
            "btc_dominance": btc_dom,
            "eth_dominance": eth_dom,
            "total_market_cap_B": total_mcap,
            "market_cap_change_24h": mcap_change,
            "total_volume_24h_B": total_vol,
            "market_trend": (
                "BULLISH" if mcap_change > 2 else "BEARISH" if mcap_change < -2 else "NEUTRAL"
            ),
        }
    return {
        "btc_dominance": 0,
        "eth_dominance": 0,
        "total_market_cap_B": 0,
        "market_cap_change_24h": 0,
        "total_volume_24h_B": 0,
        "market_trend": "UNKNOWN",
    }


def _get_btc_trend() -> dict:
    """BTC 24h trend from CoinGecko."""
    data = _fetch_json(
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true"
    )
    if data and data.get("bitcoin"):
        btc = data["bitcoin"]
        price = btc.get("usd", 0)
        change = round(btc.get("usd_24h_change", 0), 2)
        return {
            "btc_price": price,
            "btc_change_24h": change,
            "btc_trend": (
                "STRONG_UP"
                if change > 3
                else "UP"
                if change > 1
                else "STRONG_DOWN"
                if change < -3
                else "DOWN"
                if change < -1
                else "FLAT"
            ),
        }
    return {"btc_price": 0, "btc_change_24h": 0, "btc_trend": "UNKNOWN"}


def get_macro_data(force_refresh: bool = False) -> dict:
    """Get macro data with TTL cache; fetch sources in parallel when stale."""
    global _macro_cache, _cache_ts

    now = time.time()
    with _cache_lock:
        if (not force_refresh) and _macro_cache and (now - _cache_ts) < _CACHE_TTL:
            return _macro_cache

    logger.info("📰 Fetching macro data...")

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_fg = pool.submit(_get_fear_greed)
        fut_market = pool.submit(_get_global_market)
        fut_btc = pool.submit(_get_btc_trend)
        fg = fut_fg.result()
        market = fut_market.result()
        btc = fut_btc.result()

    merged = {**fg, **market, **btc}
    with _cache_lock:
        _macro_cache = merged
        _cache_ts = now

    logger.info(
        f"📰 Macro: F&G={fg['fear_greed_value']} ({fg['fear_greed_label']}), "
        f"BTC dom={market['btc_dominance']}%, "
        f"MCap change={market['market_cap_change_24h']}%, "
        f"BTC {btc['btc_trend']} ({btc['btc_change_24h']:+.1f}%)"
    )

    return merged


def format_macro_for_prompt() -> str:
    """Format macro data into prompt-friendly text."""
    m = get_macro_data()
    return (
        f"Fear & Greed Index: {m['fear_greed_value']}/100 ({m['fear_greed_label']}) - {m['fear_greed_signal']}\n"
        f"BTC Price: ${m['btc_price']:,.0f} ({m['btc_change_24h']:+.1f}% 24h) - Trend: {m['btc_trend']}\n"
        f"BTC Dominance: {m['btc_dominance']}% | ETH Dominance: {m['eth_dominance']}%\n"
        f"Total Market Cap: ${m['total_market_cap_B']}B ({m['market_cap_change_24h']:+.1f}% 24h)\n"
        f"24h Volume: ${m['total_volume_24h_B']}B\n"
        f"Market Sentiment: {m['market_trend']}"
    )
