#!/usr/bin/env python3
"""install.py — 安装 Claude Code 桌面宠物 hook 到 settings.json

将 pet hook 安全合并到 ~/.claude/settings.json：
- 保留用户已有的所有配置（其他 hook、env、permissions 等）
- 每个 pet hook 放在独立的 group 中，不干扰其他 hook
- 重复运行安全（自动更新路径）

用法:
    python3 install.py          # 安装
    python3 install.py --dry-run  # 预览将要写入的 hook 配置
"""

import json
import os
import sys
from pathlib import Path

DRY_RUN = "--dry-run" in sys.argv or "--dryrun" in sys.argv

# ── 配置 ───────────────────────────────────────────────

# 自动探测项目目录（无论从哪里执行 install.py）
PROJECT_DIR = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_DIR / ".venv/bin/python"
HOOK_SCRIPT = Path.home() / ".claude/scripts/pet-hook"
SETTINGS_FILE = Path.home() / ".claude/settings.json"

# 定义桌面宠物的 hook 事件，每个事件一个独立 group
# 注意：每个事件的 group 只包含 pet hook 本身
HOOK_DEFS = [
    # (事件名, matcher, 状态, 优先级)
    ("SessionStart",     None,               "standby",     5),
    ("UserPromptSubmit", None,               "working",     4),
    ("PreToolUse",       None,               "working",     4),
    ("PostToolUse",      None,               "working",     4),
    ("Stop",             None,               "waiting_user", 5),
    ("PermissionRequest",None,               "need_user",   2),
    ("PermissionDenied", None,               "standby",     3),
    ("Elicitation",      None,               "need_user",   3),
    ("StopFailure",      None,               "error",       1),
    ("SessionEnd",       None,               "off",         999),
]


def build_hook_group(event: str, matcher: str | None, status: str, priority: int) -> dict:
    """生成单个 pet hook group。

    返回值形如:
        {"hooks": [{"type": "command", "async": True, "command": "..."}], "matcher": "..."}

    注意 matcher 只在 Notification 事件中出现。
    """
    python_path = str(VENV_PYTHON)
    hook_path = str(HOOK_SCRIPT)

    group: dict = {
        "hooks": [
            {
                "type": "command",
                "async": True,
                "command": f"{python_path} {hook_path} {event} {status} {priority}",
            }
        ]
    }
    if matcher:
        group["matcher"] = matcher

    return group


def is_pet_hook(hook: dict) -> bool:
    """判断单个 hook 是否属于桌面宠物。"""
    return "pet-hook" in hook.get("command", "")


def merge_settings(settings: dict) -> dict:
    """将桌面宠物 hook 安全合并到 settings 中。

    合并策略：
      对每个宠物管理的事件：
        1. 确保该事件的数组存在
        2. 从已有 group 中移除所有 pet hook（一次性完成，避免
           同一事件多个 matcher 时互相覆盖）
        3. 如果某个 group 的所有 hook 都被移除，删除该 group
        4. 追加新的 pet group（每个 matcher 一个独立 group）
      对不管理的事件：完全不动
      对 hooks 以外的字段（env、permissions 等）：完全不动
    """
    if "hooks" not in settings:
        settings["hooks"] = {}

    # 按事件分组，避免同一个事件多个条目时互相覆盖
    from collections import defaultdict
    grouped: dict[str, list[tuple[str | None, str, int]]] = defaultdict(list)
    for event, matcher, status, priority in HOOK_DEFS:
        grouped[event].append((matcher, status, priority))

    for event, entries in grouped.items():
        existing_groups = settings["hooks"].get(event, [])

        # 一次性移除所有旧的 pet hook（避免逐条处理互相覆盖）
        cleaned_groups = []
        for group in existing_groups:
            non_pet = [h for h in group.get("hooks", []) if not is_pet_hook(h)]
            if non_pet:
                new_group = dict(group)
                new_group["hooks"] = non_pet
                cleaned_groups.append(new_group)

        # 一次性追加该事件的所有新 group
        for matcher, status, priority in entries:
            cleaned_groups.append(build_hook_group(event, matcher, status, priority))

        settings["hooks"][event] = cleaned_groups

    return settings


def read_settings() -> dict:
    """读取 settings.json，不存在则返回空 dict。"""
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_settings(settings: dict):
    """原子写入 settings.json。"""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(SETTINGS_FILE)


def main():
    print("🐱 桌面宠物 hook 安装")
    print(f"   项目目录: {PROJECT_DIR}")
    print(f"   Python:   {VENV_PYTHON}")
    print(f"   Hook脚本: {HOOK_SCRIPT}")
    print()

    existing = read_settings()

    # 统计现状
    existing_hook_events = set(existing.get("hooks", {}).keys())
    if existing_hook_events:
        print(f"   现有 settings.json 中有 {len(existing_hook_events)} 个 hook 事件: {', '.join(sorted(existing_hook_events))}")

    merged = merge_settings(existing)

    if DRY_RUN:
        print("\n--- 将要写入的 hooks 配置 (dry-run) ---")
        print(json.dumps(merged.get("hooks", {}), indent=2, ensure_ascii=False))
        return

    write_settings(merged)
    print("✅ hooks 配置已写入 ~/.claude/settings.json")
    print("   - 每个 pet hook 在独立的 group 中，不干扰其他 hook")
    print("   - 已有配置保持原样")


if __name__ == "__main__":
    main()
