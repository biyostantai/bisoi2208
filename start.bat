@echo off
chcp 65001 >nul 2>&1
title FuBot AI Trading System v6.0
color 0A

echo ══════════════════════════════════════════
echo     🤖 FuBot AI Trading Bot v6.0
echo     6-Layer Dual-Model Anti-Echo
echo     GPT + DeepSeek V3 System
echo ══════════════════════════════════════════
echo.

:: Kiểm tra Python
where py >nul 2>&1
if %errorlevel% neq 0 (
    where python >nul 2>&1
    if %errorlevel% neq 0 (
        echo ❌ Không tìm thấy Python!
        echo    Tải tại: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    set PYTHON_CMD=python
) else (
    set PYTHON_CMD=py
)

echo [1/3] Kiểm tra dependencies...
%PYTHON_CMD% -m pip install -q requests python-dotenv "python-telegram-bot[job-queue]" 2>nul
if %errorlevel% neq 0 (
    echo ⚠️  Cài dependencies thất bại, thử tiếp...
)
echo      ✅ Dependencies OK

echo [2/3] Kiểm tra file .env...
if not exist ".env" (
    echo ❌ Thiếu file .env — Tạo file .env với API keys trước!
    pause
    exit /b 1
)
echo      ✅ Config OK

echo [3/3] Khởi động bot...
echo.
echo ──────────────────────────────────────────
echo  Bot đang chạy... Nhấn Ctrl+C để dừng
echo ──────────────────────────────────────────
echo.

cd /d "%~dp0"
%PYTHON_CMD% main.py

echo.
echo Bot đã dừng.
pause
