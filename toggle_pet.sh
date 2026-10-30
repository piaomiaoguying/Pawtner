#!/bin/bash
set -e
# =============================================
# 桌面宠物 Hook 开关脚本
# 用法:
#   ./toggle_pet.sh            # 自动检测设备
#   ./toggle_pet.sh on         # 强制启用
#   ./toggle_pet.sh off        # 强制禁用
#   ./toggle_pet.sh status     # 查看状态
#   ./toggle_pet.sh log-on     # 启用日志
#   ./toggle_pet.sh log-off    # 禁用日志
#   ./toggle_pet.sh log-status # 查看日志状态
#
# 说明:
#   SessionStart 事件会自动检测设备并刷新哨兵文件，
#   此脚本用于手动覆盖或在会话中途切换。
# =============================================

SENTINEL="$HOME/.claude/state/pet/disabled"
STOPPED="$HOME/.claude/state/pet/stopped"
NO_LOG="$HOME/.claude/state/pet/no-log"

enable_hook() {
    if [ -f "$SENTINEL" ]; then
        rm "$SENTINEL"
        echo "✅ 已启用桌面宠物 hook"
    else
        echo "ℹ️  已经是启用状态"
    fi
}

disable_hook() {
    if [ ! -f "$SENTINEL" ]; then
        mkdir -p "$(dirname "$SENTINEL")"
        touch "$SENTINEL"
        echo "❌ 已禁用桌面宠物 hook"
    else
        echo "ℹ️  已经是禁用状态"
    fi
}

device_exists() {
    # 检查 pet_window.py 是否运行中
    python3 -c "
from pathlib import Path
import os
pid_file = Path.home() / '.claude/state/pet/pet.pid'
try:
    pid = int(pid_file.read_text().strip())
    os.kill(pid, 0)
    exit(0)
except Exception:
    exit(1)
" 2>/dev/null
}

enable_log() {
    if [ -f "$NO_LOG" ]; then
        rm "$NO_LOG"
        echo "📋 已启用日志 (hook.log)"
    else
        echo "ℹ️  日志已经是启用状态"
    fi
}

disable_log() {
    if [ ! -f "$NO_LOG" ]; then
        mkdir -p "$(dirname "$NO_LOG")"
        touch "$NO_LOG"
        echo "📋 已禁用日志"
    else
        echo "ℹ️  日志已经是禁用状态"
    fi
}

log_status() {
    if [ -f "$NO_LOG" ]; then
        echo "日志: 📴 已禁用 (no-log 哨兵文件存在)"
        echo "      运行 $0 log-on 启用日志"
    else
        echo "日志: 📝 已启用 (写入 hook.log)"
        local log_file="$HOME/.claude/state/pet/hook.log"
        if [ -f "$log_file" ]; then
            echo "      最近 5 条:"
            tail -5 "$log_file"
        else
            echo "      (暂无日志记录)"
        fi
        echo "      运行 $0 log-off 禁用日志"
    fi
}

CMD="${1:-auto}"

case "$CMD" in
    on)
        enable_hook
        ;;
    off)
        disable_hook
        ;;
    status)
        if [ -f "$STOPPED" ]; then
            echo "状态: 🔴 已持久停用 (pet_ctl.py stop 写入的 stopped 标记)"
            echo "       新开会话也不会复活，恢复用: .venv/bin/python pet_ctl.py start"
        elif [ -f "$SENTINEL" ]; then
            echo "状态: 🟡 本会话已禁用 (SessionStart 会清除 disabled 标记并重新启用)"
        else
            echo "状态: 🟢 已启用 (hook 正常执行)"
        fi
        echo ""
        # 同时显示日志状态
        log_status
        ;;
    log-on)
        enable_log
        ;;
    log-off)
        disable_log
        ;;
    log-status)
        log_status
        ;;
    auto|*)
        if device_exists; then
            echo "🐱 检测到桌面宠物 → 启用"
            enable_hook
        else
            echo "🐱 未检测到桌面宠物 → 禁用"
            disable_hook
        fi
        ;;
esac
