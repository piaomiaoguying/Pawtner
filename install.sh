#!/bin/bash
set -e
# =============================================
# 桌面宠物一键安装脚本
#
# 用法: ./install.sh          # 安装
#       python3 install.py --dry-run  # 预览 hook 配置
#
# 做了什么:
#   1. 创建虚拟环境
#   2. 创建符号链接 ~/.claude/scripts/pet-hook
#   3. 安全合并 hook 到 ~/.claude/settings.json（不干扰已有配置）
#   4. 创建运行时目录
# =============================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
SETTINGS_DIR="$HOME/.claude/scripts"
STATE_DIR="$HOME/.claude/state/pet"
HOOK_SYMLINK="$SETTINGS_DIR/pet-hook"

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

step()  { echo -e "${BLUE}→${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }

# ── 1. 虚拟环境 ──────────────────────────────────────
step "创建 Python 虚拟环境..."
if [ ! -f "$VENV_DIR/bin/python" ]; then
    python3 -m venv --system-site-packages "$VENV_DIR"
    ok "虚拟环境创建完成 (--system-site-packages)"
else
    # 检查是否已有 --system-site-packages
    if "$VENV_DIR/bin/python" -c "import sys; exit(0 if sys.base_prefix != sys.prefix else 1)" 2>/dev/null; then
        ok "虚拟环境已存在"
    else
        rm -rf "$VENV_DIR"
        python3 -m venv --system-site-packages "$VENV_DIR"
        ok "虚拟环境重建为 --system-site-packages"
    fi
fi

step "创建符号链接..."
mkdir -p "$SETTINGS_DIR"
ln -sf "$SCRIPT_DIR/pet_hook.py" "$HOOK_SYMLINK"
ok "符号链接: $HOOK_SYMLINK → $SCRIPT_DIR/pet_hook.py"

step "复制桌面宠物动画资源..."

step "复制桌面宠物动画资源..."
mkdir -p "$SCRIPT_DIR/assets"

# GIF 动画资源已在项目 assets/ 目录中，无需额外安装
if [ -d "$SCRIPT_DIR/assets" ]; then
    ok "动画资源目录已就绪 ($(ls "$SCRIPT_DIR/assets"/*.gif 2>/dev/null | wc -l | tr -d ' ') 个 GIF)"
else
    echo "  ⚠️  assets/ 目录不存在，请手动复制 GIF 文件"
fi

# ── 3. settings.json ─────────────────────────────────
step "合并 hook 到 ~/.claude/settings.json..."
"$VENV_DIR/bin/python" "$SCRIPT_DIR/install.py"
ok "hook 配置完成"

# ── 4. 运行时目录 ────────────────────────────────────
step "创建运行时目录..."
mkdir -p "$STATE_DIR/instances"
ok "运行时目录: $STATE_DIR"

# ── 5. 完成 ─────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " 🐱 安装完成"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  下次启动新的 Claude Code 会话时生效"
echo ""
echo "  开关工具:"
echo "    ./toggle_pet.sh on      强制启用"
echo "    ./toggle_pet.sh off     强制禁用"
echo "    ./toggle_pet.sh status  查看状态"
echo ""
echo "  测试宠物:"
echo "    .venv/bin/python pet_window.py --test"
echo ""
echo "  查看日志:"
echo "    ./toggle_pet.sh log-on"
echo "    tail -f ~/.claude/state/pet/hook.log"
