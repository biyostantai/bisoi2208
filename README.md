# FuBot v6.0

Bot trading futures OKX tự động với BTC Trend Filter.

## Quick Start (VPS)

```bash
# 1. Clone repo
git clone https://github.com/biyostantai/bisoi2208.git
cd bisoi2208

# 2. Copy và điền API keys
cp .env.example .env
nano .env

# 3. Chạy bot (1 lệnh)
bash start.sh
```

## Chạy nền (không tắt khi đóng SSH)

```bash
nohup bash start.sh > fubot_output.log 2>&1 &
```

## Xem log

```bash
tail -f fubot.log
```

## Dừng bot

```bash
pkill -f "python3 main.py"
```

## Cấu hình

Tất cả config trong file `.env`. Xem `.env.example` để biết các tham số.

### Coins hiện tại
SUI, SOL, DOGE, WLD, AVAX, INJ, TIA, PENDLE

### Tính năng chính
- BTC Trend Filter: chỉ trade theo hướng BTC
- Partial TP + Break-even tại 50%
- Per-coin cooldown sau SL (600s)
- AI Score Gate 8.5/10
- Max 3 lệnh cùng lúc
