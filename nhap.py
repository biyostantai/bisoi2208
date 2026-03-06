# -*- coding: utf-8 -*-
"""
Nhập API key một lần, lưu vào .env local.
Chạy: python nhap.py

File .env KHÔNG bị push lên GitHub (đã có trong .gitignore).
"""

import os
import re
import sys
import io

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")

ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

KEYS_TO_SET = [
    ("GPT_API_KEY",        "OpenRouter API key (bắt đầu bằng sk-or-v1-...)"),
    ("DEEPSEEK_API_KEY",   "Deepseek API key (giống OpenRouter key hoặc key riêng)"),
    ("OPENROUTER_API_KEY", "OpenRouter API key (có thể giống GPT_API_KEY)"),
]


def read_env(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return f.readlines()


def write_env(path, lines):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(lines)


def set_key(lines, key, value):
    pattern = re.compile(r"^" + re.escape(key) + r"\s*=.*$", re.MULTILINE)
    new_line = f"{key}={value}\n"
    for i, line in enumerate(lines):
        if re.match(r"^" + re.escape(key) + r"\s*=", line):
            lines[i] = new_line
            return lines
    # Key chưa có → thêm vào cuối
    lines.append(new_line)
    return lines


def main():
    print("=" * 55)
    print("  FuBot - Nhập API Key (chỉ cần làm 1 lần)")
    print("=" * 55)
    print(f"File sẽ được lưu vào: {ENV_FILE}")
    print("File này KHÔNG bị push lên GitHub.\n")

    lines = read_env(ENV_FILE)

    # Kiểm tra key nào đã có
    existing = {}
    for line in lines:
        m = re.match(r"^([A-Z_]+)\s*=\s*(.+)$", line.strip())
        if m:
            existing[m.group(1)] = m.group(2)

    changed = False
    for key, desc in KEYS_TO_SET:
        current = existing.get(key, "")
        masked = current[:12] + "..." if len(current) > 12 else current
        if current and current not in ("", "your_api_key_here", "sk-my-secret-key-123"):
            print(f"[{key}] Hiện tại: {masked}")
            ans = input(f"  Nhấn Enter để giữ nguyên, hoặc nhập key mới: ").strip()
            if not ans:
                continue
            new_val = ans
        else:
            print(f"\n[{key}] {desc}")
            new_val = input("  Nhập key: ").strip()
            if not new_val:
                print("  Bỏ qua.")
                continue

        lines = set_key(lines, key, new_val)
        changed = True
        print(f"  → Đã lưu {key}")

    # Đảm bảo các config OpenRouter đúng
    lines = set_key(lines, "GPT_BASE_URL", "https://openrouter.ai/api/v1")
    lines = set_key(lines, "GPT_MODEL", "deepseek/deepseek-v3.2")
    lines = set_key(lines, "DEEPSEEK_BASE_URL", "https://openrouter.ai/api/v1")
    lines = set_key(lines, "DEEPSEEK_MODEL", "deepseek/deepseek-v3.2")
    lines = set_key(lines, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    lines = set_key(lines, "OPENROUTER_DEEPSEEK_MODEL", "deepseek/deepseek-v3.2")

    write_env(ENV_FILE, lines)
    print("\n[OK] Đã lưu .env thành công!")
    print("\nBây giờ chạy bot: start.bat hoặc python main.py")
    print("=" * 55)


if __name__ == "__main__":
    main()
