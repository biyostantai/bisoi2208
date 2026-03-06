@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
title FuBot AI Trading System v6.0
color 0A

echo =========================================
echo   FuBot AI Trading Bot v6.0
echo   6-Layer Dual-Model Anti-Echo
echo   GPT + DeepSeek V3 System
echo =========================================
echo.

where py >nul 2>&1
if %errorlevel% neq 0 (
    where python >nul 2>&1
    if %errorlevel% neq 0 (
        echo [ERROR] Python not found.
        echo Install from: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    set PYTHON_CMD=python
) else (
    set PYTHON_CMD=py
)

echo [1/4] Check dependencies...
%PYTHON_CMD% -m pip install -q requests python-dotenv "python-telegram-bot[job-queue]" 2>nul
if %errorlevel% neq 0 (
    echo [WARN] Dependency install had warnings, continuing...
)
echo      [OK] Dependencies

echo [2/4] Check .env...
if not exist ".env" (
    echo [ERROR] Missing .env file.
    echo Create it from .env.example first.
    pause
    exit /b 1
)
echo      [OK] .env found

cd /d "%~dp0"

echo [3/4] Configure OpenRouter DeepSeek-only mode...
if /I "%OPENROUTER_DEEPSEEK_ONLY%"=="" set OPENROUTER_DEEPSEEK_ONLY=1
if /I "%OPENROUTER_DEEPSEEK_ONLY%"=="1" (
    set AI_FORCE_DEEPSEEK_ONLY=1
    if "!OPENROUTER_BASE_URL!"=="" set OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
    if "!OPENROUTER_DEEPSEEK_MODEL!"=="" set OPENROUTER_DEEPSEEK_MODEL=deepseek/deepseek-v3.2
    echo !OPENROUTER_BASE_URL! | findstr /I "openrouter.ai/deepseek/" >nul
    if !errorlevel!==0 (
        set OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
        set OPENROUTER_DEEPSEEK_MODEL=deepseek/deepseek-v3.2
    )
    set DEEPSEEK_BASE_URL=!OPENROUTER_BASE_URL!
    set DEEPSEEK_MODEL=!OPENROUTER_DEEPSEEK_MODEL!
    if not "!OPENROUTER_API_KEY!"=="" (
        set DEEPSEEK_API_KEY=!OPENROUTER_API_KEY!
    ) else (
        if /I "!GPT_API_KEY:~0,6!"=="sk-or-" (
            set DEEPSEEK_API_KEY=!GPT_API_KEY!
        ) else (
            if /I "!DEEPSEEK_API_KEY:~0,6!"=="sk-or-" set DEEPSEEK_API_KEY=!DEEPSEEK_API_KEY!
        )
    )
    if not "!DEEPSEEK_API_KEY!"=="" set GPT_API_KEY=!DEEPSEEK_API_KEY!
    set GPT_BASE_URL=!DEEPSEEK_BASE_URL!
    set GPT_MODEL=!DEEPSEEK_MODEL!
    set AI_FAST_3L_MODE=1
    set AI_GPT_ONLY_MODE=0
    set GPT_L3_ENSEMBLE=1
    set GPT_L3_QUORUM=1
    if "!OPENROUTER_X_TITLE!"=="" set OPENROUTER_X_TITLE=fubot
    echo      [OK] OpenRouter DeepSeek-only ON: !DEEPSEEK_MODEL!
    echo      [OK] Fast profile ON: FAST3L=1, GPT-only=0, L3 ensemble=1/1
) else (
    echo      [INFO] OpenRouter DeepSeek-only OFF
)

echo [3.5/4] LLM preflight...
if /I not "%SKIP_LLM_PREFLIGHT%"=="1" (
    %PYTHON_CMD% llm_preflight.py
    if %errorlevel% neq 0 (
        echo [ERROR] LLM preflight failed. Check API key/base/model.
        pause
        exit /b 1
    )
) else (
    echo      [WARN] SKIP_LLM_PREFLIGHT=1, preflight skipped
)

echo [4/4] Start bot...
echo.
echo -----------------------------------------
echo Bot is running. Press Ctrl+C to stop.
echo -----------------------------------------
echo.

%PYTHON_CMD% main.py

echo.
echo Bot stopped.
pause
