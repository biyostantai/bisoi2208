# -*- coding: utf-8 -*-
"""
okx_client.py â€” OKX REST API Client

Há»— trá»£:
- Láº¥y dá»¯ liá»‡u thá»‹ trÆ°á»ng (ticker, orderbook, trades, funding, OI)
- Äáº·t lá»‡nh market + TP/SL
- Quáº£n lĂ½ vá»‹ tháº¿
- Demo mode (simulated trading)
"""

import hashlib
import hmac
import base64
import json
import math
import time
from datetime import datetime, timezone
import requests
import config


class OKXClient:
    """OKX REST API client vá»›i HMAC signing."""

    def __init__(self):
        self.api_key = config.OKX_API_KEY
        self.secret_key = config.OKX_SECRET_KEY
        self.passphrase = config.OKX_PASSPHRASE
        self.base_url = config.OKX_BASE_URL
        self.demo = config.OKX_DEMO
        self.session = requests.Session()
        self._ensure_net_mode()

    def _ensure_net_mode(self):
        """
        Ă‰p account sang net_mode (long_short_mode = false).
        Net mode: chá»‰ cho phĂ©p 1 hÆ°á»›ng táº¡i 1 thá»i Ä‘iá»ƒm.
        NgÄƒn cháº·n viá»‡c má»Ÿ vá»‹ tháº¿ Ä‘áº£o chiá»u khi TP/SL trigger.
        """
        try:
            result = self._get("/api/v5/account/config")
            data = result.get("data", [{}])
            if data:
                pos_mode = data[0].get("posMode", "")
                if pos_mode != "net_mode":
                    set_result = self._post("/api/v5/account/set-position-mode",
                                           {"posMode": "net_mode"})
                    if set_result.get("code") == "0":
                        print("âœ… ÄĂ£ chuyá»ƒn sang net_mode â€” ngÄƒn Ä‘áº·t lá»‡nh Ä‘áº£o chiá»u")
                    else:
                        print(f"â ï¸ KhĂ´ng chuyá»ƒn Ä‘Æ°á»£c net_mode: {set_result}")
                else:
                    print("âœ… Account Ä‘Ă£ á»Ÿ net_mode")
        except Exception as e:
            print(f"â ï¸ Lá»—i check net_mode: {e}")

    # â”€â”€ signing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """Táº¡o HMAC SHA256 signature cho OKX API."""
        message = timestamp + method.upper() + path + body
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        """Táº¡o headers cho request."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
             f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"

        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.demo:
            headers["x-simulated-trading"] = "1"
        return headers

    def _get(self, path: str, params: dict = None) -> dict:
        """GET request."""
        url = self.base_url + path
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if query:
                path = path + "?" + query
                url = self.base_url + path

        headers = self._headers("GET", path)
        resp = self.session.get(url, headers=headers, timeout=15)
        return resp.json()

    def _post(self, path: str, data: dict) -> dict:
        """POST request."""
        body = json.dumps(data)
        headers = self._headers("POST", path, body)
        url = self.base_url + path
        resp = self.session.post(url, headers=headers, data=body, timeout=15)
        return resp.json()

    # â”€â”€ market data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def get_ticker(self, inst_id: str) -> dict:
        """Láº¥y ticker (giĂ¡ hiá»‡n táº¡i)."""
        result = self._get("/api/v5/market/ticker", {"instId": inst_id})
        data = result.get("data", [])
        if data:
            return data[0]
        return {}

    def get_orderbook(self, inst_id: str, depth: int = 20) -> dict:
        """Láº¥y orderbook."""
        result = self._get("/api/v5/market/books", {
            "instId": inst_id,
            "sz": str(depth),
        })
        data = result.get("data", [])
        if data:
            return data[0]  # {"asks": [...], "bids": [...]}
        return {"asks": [], "bids": []}

    def get_trades(self, inst_id: str, limit: int = 100) -> list:
        """Láº¥y trades gáº§n Ä‘Ă¢y."""
        result = self._get("/api/v5/market/trades", {
            "instId": inst_id,
            "limit": str(limit),
        })
        return result.get("data", [])

    def get_funding_rate(self, inst_id: str) -> dict:
        """Láº¥y funding rate hiá»‡n táº¡i."""
        result = self._get("/api/v5/public/funding-rate", {"instId": inst_id})
        data = result.get("data", [])
        if data:
            return data[0]
        return {}

    def get_open_interest(self, inst_id: str) -> dict:
        """Láº¥y Open Interest."""
        # TrĂ­ch instType tá»« instId
        result = self._get("/api/v5/public/open-interest", {
            "instType": "SWAP",
            "instId": inst_id,
        })
        data = result.get("data", [])
        if data:
            return data[0]
        return {}

    def get_instrument(self, inst_id: str) -> dict:
        """Láº¥y thĂ´ng tin instrument (contract size, min size, etc.)."""
        result = self._get("/api/v5/public/instruments", {
            "instType": "SWAP",
            "instId": inst_id,
        })
        data = result.get("data", [])
        if data:
            return data[0]
        return {}

    # â”€â”€ account â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def get_balance(self) -> dict:
        """Láº¥y balance tĂ i khoáº£n."""
        result = self._get("/api/v5/account/balance")
        return result

    def get_positions(self, inst_id: str = None) -> list:
        """Láº¥y vá»‹ tháº¿ hiá»‡n táº¡i."""
        params = {"instType": "SWAP"}
        if inst_id:
            params["instId"] = inst_id
        result = self._get("/api/v5/account/positions", params)
        return result.get("data", [])

    def get_positions_history(self, inst_id: str = None, limit: int = 10) -> list:
        """
        Láº¥y lá»‹ch sá»­ vá»‹ tháº¿ Ä‘Ă£ Ä‘Ă³ng (cĂ³ PnL thá»±c táº¿).
        OKX API: /api/v5/account/positions-history
        """
        params = {"instType": "SWAP", "limit": str(limit)}
        if inst_id:
            params["instId"] = inst_id
        result = self._get("/api/v5/account/positions-history", params)
        return result.get("data", [])

    def get_order_detail(self, inst_id: str, order_id: str) -> dict:
        """
        Láº¥y chi tiáº¿t 1 order (bao gá»“m fill price).
        """
        params = {"instId": inst_id, "ordId": order_id}
        result = self._get("/api/v5/trade/order", params)
        data = result.get("data", [])
        return data[0] if data else {}

    # â”€â”€ trading â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def set_leverage(self, inst_id: str, lever: int, mgn_mode: str = "cross") -> dict:
        """Äáº·t leverage cho instrument."""
        data = {
            "instId": inst_id,
            "lever": str(lever),
            "mgnMode": mgn_mode,
        }
        return self._post("/api/v5/account/set-leverage", data)

    def place_market_order(self, inst_id: str, side: str, size: str,
                           td_mode: str = "cross") -> dict:
        """
        Äáº·t lá»‡nh market.
        side: "buy" (long) hoáº·c "sell" (short)
        size: sá»‘ contracts
        """
        data = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": "market",
            "sz": size,
        }
        result = self._post("/api/v5/trade/order", data)
        return result

    def place_reduce_market_order(self, inst_id: str, side: str, size: str,
                                  td_mode: str = "cross") -> dict:
        """
        Dat lenh market giam vi the (reduce-only).
        side: "sell" de giam LONG, "buy" de giam SHORT.
        """
        data = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": "market",
            "sz": size,
            "reduceOnly": "true",
        }
        return self._post("/api/v5/trade/order", data)

    def normalize_size(self, inst_id: str, raw_size: float) -> str:
        """
        Lam tron size theo lot/min size cua instrument.
        Tra ve chuoi size hop le; tra ve "" neu size khong dat min size.
        """
        if raw_size <= 0:
            return ""
        inst = self.get_instrument(inst_id)
        if not inst:
            return ""

        lot_sz = float(inst.get("lotSz", "1"))
        min_sz = float(inst.get("minSz", "1"))
        if lot_sz <= 0:
            lot_sz = 1.0

        steps = int(raw_size / lot_sz)
        norm = steps * lot_sz
        if norm < min_sz:
            return ""

        # Precision from lot size decimals.
        lot_text = f"{lot_sz:.10f}".rstrip("0").rstrip(".")
        decimals = 0
        if "." in lot_text:
            decimals = len(lot_text.split(".")[1])

        if decimals <= 0:
            return str(int(norm))
        return f"{norm:.{decimals}f}"

    def calc_partial_close_size(self, inst_id: str, current_size: float,
                                fraction: float) -> str:
        """Tinh size dong 1 phan vi the theo fraction va lot size."""
        frac = max(0.0, min(1.0, float(fraction)))
        desired = float(current_size) * frac
        return self.normalize_size(inst_id, desired)

    def format_price(self, inst_id: str, price: float) -> str:
        inst = self.get_instrument(inst_id)
        tick_sz = inst.get('tickSz', '')
        if tick_sz:
            if '.' in tick_sz:
                decimals = len(tick_sz.rstrip('0').split('.')[-1])
            else:
                decimals = 0
        else:
            if price >= 1000: decimals = 2
            elif price >= 100: decimals = 3
            elif price >= 1: decimals = 4
            elif price >= 0.1: decimals = 5
            elif price >= 0.01: decimals = 6
            elif price >= 0.001: decimals = 7
            elif price >= 0.0001: decimals = 8
            else: decimals = 10
        return f'{price:.{decimals}f}'

    def place_tp_sl(self, inst_id: str, side: str, size: str,
                    tp_price: str, sl_price: str,
                    td_mode: str = 'cross') -> dict:
        tp_fmt = self.format_price(inst_id, float(tp_price))
        sl_fmt = self.format_price(inst_id, float(sl_price))
        data = {
            'instId': inst_id,
            'tdMode': td_mode,
            'side': side,
            'ordType': 'oco',
            'sz': size,
            'tpTriggerPx': tp_fmt,
            'tpOrdPx': '-1',
            'slTriggerPx': sl_fmt,
            'slOrdPx': '-1',
            'reduceOnly': 'true',
        }
        result = self._post('/api/v5/trade/order-algo', data)
        return result

    def has_algo_orders(self, inst_id: str) -> bool:
        try:
            # OCO algo orders go to "effective" state, not "pending"
            # Check pending first
            result = self._get('/api/v5/trade/orders-algo-pending', {
                'instType': 'SWAP',
                'instId': inst_id,
            })
            if len(result.get('data', [])) > 0:
                return True
            # Check effective in history
            result2 = self._get('/api/v5/trade/orders-algo-history', {
                'instType': 'SWAP',
                'instId': inst_id,
                'ordType': 'oco',
                'state': 'effective',
            })
            return len(result2.get('data', [])) > 0
        except Exception:
            return True  # Assume exists on error to avoid duplicate re-arm

    def close_position(self, inst_id: str, mgn_mode: str = "cross") -> dict:
        """ÄĂ³ng vá»‹ tháº¿."""
        data = {
            "instId": inst_id,
            "mgnMode": mgn_mode,
        }
        result = self._post("/api/v5/trade/close-position", data)
        # Há»§y táº¥t cáº£ algo orders cĂ²n láº¡i (TP/SL orphans) Ä‘á»ƒ trĂ¡nh má»Ÿ lá»‡nh Ä‘áº£o chiá»u
        self.cancel_algo_orders(inst_id)
        return result

    def cancel_algo_orders(self, inst_id: str):
        """
        Há»§y Táº¤T Cáº¢ algo orders (TP/SL) Ä‘ang pending cho 1 instrument.
        NgÄƒn cháº·n viá»‡c TP/SL orphan trigger má»Ÿ vá»‹ tháº¿ Ä‘áº£o chiá»u.
        """
        try:
            # Láº¥y danh sĂ¡ch algo orders Ä‘ang chá»
            result = self._get("/api/v5/trade/orders-algo-pending", {
                "instType": "SWAP",
                "instId": inst_id,
            })
            orders = result.get("data", [])
            if not orders:
                return

            # Há»§y tá»«ng algo order
            for order in orders:
                algo_id = order.get("algoId", "")
                if algo_id:
                    cancel_data = [{
                        "instId": inst_id,
                        "algoId": algo_id,
                    }]
                    cancel_result = self._post("/api/v5/trade/cancel-algos", cancel_data)
                    if cancel_result.get("code") == "0":
                        print(f"âœ… ÄĂ£ há»§y algo order {algo_id} cho {inst_id}")
                    else:
                        print(f"â ï¸ Lá»—i há»§y algo {algo_id}: {cancel_result}")
        except Exception as e:
            print(f"â ï¸ Lá»—i cancel algo orders {inst_id}: {e}")

    # â”€â”€ helper â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def calc_position_size(self, inst_id: str, amount_usdt: float,
                           leverage: int) -> tuple[str, dict]:
        """
        Tinh so contracts dua tren amount USDT va leverage.
        Returns (size_str, info_dict)
        """
        inst = self.get_instrument(inst_id)
        if not inst:
            return "0", {"error": "Khong lay duoc instrument info"}

        ct_val = float(inst.get("ctVal", "1"))
        min_sz = float(inst.get("minSz", "1"))
        lot_sz = float(inst.get("lotSz", "1"))

        ticker = self.get_ticker(inst_id)
        if not ticker:
            return "0", {"error": "Khong lay duoc gia"}

        price = float(ticker.get("last", "0"))
        if price == 0:
            return "0", {"error": "Gia = 0"}

        # Target margin gross: khong tru phi truoc khi vao lenh.
        position_value = amount_usdt * leverage
        contract_value = ct_val * price
        raw_contracts = position_value / contract_value if contract_value > 0 else 0.0
        step = lot_sz if lot_sz > 0 else (min_sz if min_sz > 0 else 1.0)

        def _round_down(value: float, step_size: float) -> float:
            return math.floor(value / step_size + 1e-12) * step_size

        def _round_up(value: float, step_size: float) -> float:
            return math.ceil(value / step_size - 1e-12) * step_size

        floor_contracts = max(min_sz, _round_down(raw_contracts, step))
        ceil_contracts = max(min_sz, _round_up(raw_contracts, step))

        def _margin_for(contracts: float) -> float:
            return (contracts * contract_value) / leverage

        min_margin_needed = _margin_for(min_sz)
        if min_margin_needed > amount_usdt:
            return "0", {
                "error": (
                    f"Margin toi thieu can {min_margin_needed:.2f} USDT "
                    f"(min {min_sz} contracts x {contract_value:.2f}$/ct / {leverage}x), "
                    f"nhung chi co {amount_usdt} USDT. "
                    f"Can nap them hoac tang leverage."
                ),
                "min_margin_needed": round(min_margin_needed, 2),
                "available": amount_usdt,
            }

        margin_floor = _margin_for(floor_contracts)
        margin_ceil = _margin_for(ceil_contracts)
        sizing_mode = str(getattr(config, "POSITION_MARGIN_TARGET_MODE", "at_least")).lower()

        # at_least: uu tien margin >= target
        # nearest: gan target nhat (co the thap hon mot chut)
        if sizing_mode in ("nearest", "closest"):
            if abs(margin_floor - amount_usdt) <= abs(margin_ceil - amount_usdt):
                n_contracts = floor_contracts
            else:
                n_contracts = ceil_contracts
        elif sizing_mode in ("at_least", "target_or_above", "ceil"):
            n_contracts = ceil_contracts
        else:
            n_contracts = floor_contracts

        actual_margin = _margin_for(n_contracts)
        info = {
            "price": price,
            "ct_val": ct_val,
            "contract_value_usdt": round(contract_value, 4),
            "position_value_usdt": round(position_value, 2),
            "actual_margin_usdt": round(actual_margin, 2),
            "target_margin_usdt": round(amount_usdt, 2),
            "margin_diff_usdt": round(actual_margin - amount_usdt, 4),
            "contracts": n_contracts,
            "min_sz": min_sz,
            "lot_sz": lot_sz,
            "sizing_mode": sizing_mode,
            "floor_contracts": floor_contracts,
            "ceil_contracts": ceil_contracts,
            "margin_floor_usdt": round(margin_floor, 4),
            "margin_ceil_usdt": round(margin_ceil, 4),
        }

        if lot_sz >= 1:
            size_str = str(int(n_contracts))
        else:
            decimals = len(str(lot_sz).split(".")[-1]) if "." in str(lot_sz) else 0
            size_str = f"{n_contracts:.{decimals}f}"

        return size_str, info
    def calc_tp_sl_prices(self, price: float, direction: str,
                          tp_pct: float, sl_pct: float) -> tuple[float, float]:
        """
        Tinh gia TP va SL.
        direction: "LONG" hoac "SHORT"
        LONG:  TP > entry, SL < entry
        SHORT: TP < entry, SL > entry
        """
        def _infer_price_precision(px: float) -> int:
            if px >= 1000:
                return 2
            if px >= 100:
                return 3
            if px >= 1:
                return 4
            if px >= 0.1:
                return 5
            if px >= 0.01:
                return 6
            return 7

        precision = _infer_price_precision(price)
        tick = 10 ** (-precision)

        if direction == "LONG":
            tp = price * (1 + tp_pct / 100)
            sl = price * (1 - sl_pct / 100)
        else:  # SHORT
            tp = price * (1 - tp_pct / 100)
            sl = price * (1 + sl_pct / 100)

        tp = round(tp, precision)
        sl = round(sl, precision)

        # Neu sai huong do lam tron, day len/xuong 1 tick de dam bao logic.
        if direction == "LONG":
            if tp <= price:
                tp = round(price + tick, precision)
            if sl >= price:
                sl = round(price - tick, precision)
        else:
            if tp >= price:
                tp = round(price - tick, precision)
            if sl <= price:
                sl = round(price + tick, precision)

        # Sanity check
        if direction == "LONG" and (tp <= price or sl >= price):
            raise ValueError(
                f"TP/SL logic error LONG: entry={price}, tp={tp}, sl={sl}. "
                f"TP phai > entry, SL phai < entry."
            )
        if direction == "SHORT" and (tp >= price or sl <= price):
            raise ValueError(
                f"TP/SL logic error SHORT: entry={price}, tp={tp}, sl={sl}. "
                f"TP phai < entry, SL phai > entry."
            )

        return tp, sl
    # â”€â”€ láº¥y data cho indicators â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def get_candles(self, inst_id: str, bar: str = "15m", limit: int = 50) -> list:
        """Láº¥y náº¿n OHLCV. bar: '1m','5m','15m','1H','4H','1D'
        OKX tráº£ vá» newest first: [ts, open, high, low, close, vol, ...]"""
        result = self._get("/api/v5/market/candles", {
            "instId": inst_id,
            "bar": bar,
            "limit": str(limit),
        })
        return result.get("data", [])

    def get_market_data(self, inst_id: str) -> dict:
        """
        Láº¥y toĂ n bá»™ dá»¯ liá»‡u thá»‹ trÆ°á»ng cáº§n thiáº¿t cho indicators.
        """
        ticker = self.get_ticker(inst_id)
        orderbook = self.get_orderbook(inst_id, depth=20)
        trades = self.get_trades(inst_id, limit=100)
        funding = self.get_funding_rate(inst_id)
        oi = self.get_open_interest(inst_id)
        candles_1m = self.get_candles(inst_id, bar="1m", limit=120)
        candles_5m = self.get_candles(inst_id, bar="5m", limit=50)
        candles_15m = self.get_candles(inst_id, bar="15m", limit=50)

        # Parse trades
        parsed_trades = []
        for t in trades:
            parsed_trades.append({
                "side": t.get("side", "buy"),
                "sz": t.get("sz", "0"),
                "px": t.get("px", "0"),
            })

        return {
            "ticker": ticker,
            "price": float(ticker.get("last", "0")) if ticker else 0,
            "orderbook": orderbook,
            "bids": orderbook.get("bids", []),
            "asks": orderbook.get("asks", []),
            "trades": parsed_trades,
            "funding_rate": float(funding.get("fundingRate", "0")) if funding else 0,
            "open_interest": float(oi.get("oi", "0")) if oi else 0,
            "candles_1m": candles_1m,
            "candles_5m": candles_5m,
            "candles_15m": candles_15m,
        }

