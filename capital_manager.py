# -*- coding: utf-8 -*-
"""
capital_manager.py — Quản lý vốn & rủi ro hàng ngày

Quy tắc:
- Không giới hạn số kèo/ngày — AI lọc chất lượng
- Cooldown 10 phút sau mỗi lệnh, 15 phút sau thua
- Bảo toàn vốn là ưu tiên số 1
"""

import json
import os
import time
from datetime import date, datetime, timedelta
import config


class CapitalManager:
    """Quản lý vốn, theo dõi trades trong ngày."""

    LOG_FILE = "daily_log.json"
    HISTORY_FILE = "trade_history.json"

    def __init__(self):
        self.today = date.today().isoformat()
        self.trades = []           # danh sách trades trong ngày
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.consecutive_losses = 0  # thua liên tiếp
        self.last_trade_time = 0.0   # timestamp lệnh cuối
        self.last_trade_was_loss = False
        self._load()

    # ── persistence ─────────────────────────────────────────
    def _load(self):
        """Load log ngày hiện tại (nếu có)."""
        if not os.path.exists(self.LOG_FILE):
            return
        try:
            with open(self.LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") != self.today:
                # ngày mới → reset
                return
            self.trades = data.get("trades", [])
            self.wins = data.get("wins", 0)
            self.losses = data.get("losses", 0)
            self.total_pnl = data.get("total_pnl", 0.0)
            self.consecutive_losses = data.get("consecutive_losses", 0)
            self.last_trade_time = data.get("last_trade_time", 0.0)
            self.last_trade_was_loss = data.get("last_trade_was_loss", False)
        except Exception:
            pass

    def _save(self):
        """Lưu log ngày hiện tại."""
        data = {
            "date": self.today,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
            "consecutive_losses": self.consecutive_losses,
            "last_trade_time": self.last_trade_time,
            "last_trade_was_loss": self.last_trade_was_loss,
        }
        with open(self.LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _save_history(self):
        """Lưu kết quả ngày vào history (gọi khi kết thúc ngày)."""
        history = []
        if os.path.exists(self.HISTORY_FILE):
            try:
                with open(self.HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []
        # Cập nhật hoặc thêm entry cho ngày này
        entry = {
            "date": self.today,
            "wins": self.wins,
            "losses": self.losses,
            "total_trades": len(self.trades),
            "total_pnl": round(self.total_pnl, 4),
        }
        # Replace nếu đã có entry cho ngày này
        history = [h for h in history if h.get("date") != self.today]
        history.append(entry)
        # Giữ tối đa 30 ngày gần nhất
        history = sorted(history, key=lambda x: x["date"])[-30:]
        with open(self.HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    def _get_yesterday_stats(self) -> dict | None:
        """Lấy thống kê ngày hôm qua từ history."""
        if not os.path.exists(self.HISTORY_FILE):
            return None
        try:
            with open(self.HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            for h in history:
                if h.get("date") == yesterday:
                    return h
        except Exception:
            pass
        return None

    def _calc_max_trades_today(self):
        """Không giới hạn số kèo — AI lọc chất lượng."""
        pass

    # ── kiểm tra ────────────────────────────────────────────
    def _daily_stop_loss_hit(self) -> tuple[bool, float]:
        threshold = float(getattr(config, "DAILY_STOP_LOSS_USDT", 0.0))
        if threshold >= 0:
            return False, threshold
        return float(self.total_pnl) <= threshold, threshold

    def can_trade(self) -> tuple[bool, str]:
        """
        Kiểm tra có được phép trade tiếp không.
        Returns (allowed, reason)
        """
        # Reset nếu sang ngày mới
        today = date.today().isoformat()
        if today != self.today:
            if len(self.trades) > 0:
                self._save_history()
            self.today = today
            self.trades = []
            self.wins = 0
            self.losses = 0
            self.total_pnl = 0.0
            self.consecutive_losses = 0
            self.last_trade_time = 0.0
            self.last_trade_was_loss = False
            self._save()

        hit_daily_stop, daily_threshold = self._daily_stop_loss_hit()
        if hit_daily_stop:
            return (
                False,
                f"Daily stop-loss hit: PnL {self.total_pnl:+.4f} <= {daily_threshold:+.4f} USDT",
            )

        # Chan trade khi vuot nguong thua
        if config.MAX_LOSSES_PER_DAY > 0 and self.losses >= config.MAX_LOSSES_PER_DAY:
            return (
                False,
                f"Dung trade: da dat {self.losses} lenh thua trong ngay "
                f"(max {config.MAX_LOSSES_PER_DAY})",
            )

        if (
            config.MAX_CONSECUTIVE_LOSSES > 0
            and self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES
        ):
            return (
                False,
                f"Dung trade: thua lien tiep {self.consecutive_losses} lenh "
                f"(max {config.MAX_CONSECUTIVE_LOSSES})",
            )

        # Cooldown sau lệnh trước
        if self.last_trade_time > 0:
            now = time.time()
            cooldown = config.COOLDOWN_AFTER_LOSS if self.last_trade_was_loss else config.COOLDOWN_AFTER_TRADE
            elapsed = now - self.last_trade_time
            remaining = cooldown - elapsed
            if remaining > 0:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                return False, f"Cooldown còn {mins}p{secs}s ({'sau thua' if self.last_trade_was_loss else 'sau lệnh'})"

        return True, "OK"

    # ── ghi nhận kết quả ────────────────────────────────────
    def record_trade(self, symbol: str, direction: str, entry: float,
                     exit_price: float, pnl: float, pnl_pct: float,
                     extra: dict | None = None):
        """Ghi nhận kết quả 1 trade."""
        trade = {
            "time": datetime.now().isoformat(),
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "exit": exit_price,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
        }
        if extra:
            for key, value in extra.items():
                if value is None:
                    continue
                trade[key] = value
        self.trades.append(trade)

        if pnl >= 0:
            self.wins += 1
            self.consecutive_losses = 0
            self.last_trade_was_loss = False
        else:
            self.losses += 1
            self.consecutive_losses += 1
            self.last_trade_was_loss = True

        self.last_trade_time = time.time()
        self.total_pnl += pnl
        self._save()
        self._save_history()  # cập nhật history sau mỗi trade

    # ── thống kê ────────────────────────────────────────────
    def daily_stats(self) -> dict:
        """Trả về thống kê ngày."""
        total = len(self.trades)
        winrate = (self.wins / total * 100) if total > 0 else 0
        return {
            "date": self.today,
            "total_trades": total,
            "wins": self.wins,
            "losses": self.losses,
            "winrate": round(winrate, 1),
            "total_pnl": round(self.total_pnl, 4),
        }

    def stats_text(self) -> str:
        """Format thống kê dạng text."""
        s = self.daily_stats()
        return (
            f"📊 Thống kê ngày {s['date']}\n"
            f"Trades: {s['total_trades']} (không giới hạn)\n"
            f"Win: {s['wins']} | Loss: {s['losses']}\n"

            f"Winrate: {s['winrate']}%\n"
            f"PnL: {s['total_pnl']:+.4f} USDT"
        )
