#!/usr/bin/env python3
"""pet_ctl.py — 桌面宠物进程管理与卸载

用法:
    python3 pet_ctl.py status                 # 查看进程 / 抑制标记 / hook 安装状态
    python3 pet_ctl.py stop                   # 结束宠物进程并持久抑制（新会话也不复活）
    python3 pet_ctl.py start                  # 解除抑制，恢复宠物
    python3 pet_ctl.py uninstall              # 仅移除 pet hook，保留其他所有 hook
    python3 pet_ctl.py uninstall --dry-run    # 预览将要删除/保留的内容
    python3 pet_ctl.py uninstall --keep-state # 保留 ~/.claude/state/pet 运行数据

与 toggle_pet.sh 的区别:
    toggle_pet.sh off  只写 disabled 哨兵，SessionStart 会把它清掉 → 下一个新会话自动复活
    pet_ctl.py stop    额外写 stopped 哨兵，pet_hook.py 不会清除 → 跨会话保持关闭
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── 配置 ───────────────────────────────────────────────

HOME = Path.home()
STATE_DIR = HOME / ".claude/state/pet"
PID_FILE = STATE_DIR / "pet.pid"
PET_STATE_FILE = STATE_DIR / "pet_state.json"
DISABLED_FILE = STATE_DIR / "disabled"   # toggle_pet.sh 写的会话级标记
STOPPED_FILE = STATE_DIR / "stopped"     # pet_ctl 写的持久标记
HOOK_LINK = HOME / ".claude/scripts/pet-hook"

DEFAULT_SETTINGS = [
    HOME / ".claude/settings.json",
    HOME / ".claude/settings.local.json",
]
HOOK_MARKER = "pet-hook"
WINDOW_SCRIPT = "pet_window.py"

GREEN, RED, BLUE, YELLOW, NC = (
    "\033[0;32m", "\033[0;31m", "\033[0;34m", "\033[0;33m", "\033[0m"
)


# ── 进程工具 ───────────────────────────────────────────

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _is_pet_pid(pid: int) -> bool:
    """校验 PID 确实是 pet_window（防 PID 复用误杀）。"""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return False
    return WINDOW_SCRIPT in out


def _pet_pids() -> list[int]:
    pids: set[int] = set()
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        if _alive(pid) and _is_pet_pid(pid):
            pids.add(pid)
    except (FileNotFoundError, ValueError):
        pass
    try:
        out = subprocess.run(
            ["pgrep", "-f", WINDOW_SCRIPT],
            capture_output=True, text=True, timeout=3,
        ).stdout
        for line in out.split():
            try:
                pid = int(line)
            except ValueError:
                continue
            if pid != os.getpid() and _is_pet_pid(pid):
                pids.add(pid)
    except Exception:
        pass
    return sorted(pids)


def _kill_pets(timeout: float = 3.0) -> list[int]:
    """SIGTERM → 等待 → SIGKILL。返回被处理的 PID 列表。"""
    pids = _pet_pids()
    if not pids:
        PID_FILE.unlink(missing_ok=True)
        return []

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.time() + timeout
    while time.time() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.1)

    for pid in pids:
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    time.sleep(0.1)
    PID_FILE.unlink(missing_ok=True)
    return pids


def _write_off_state():
    """让宠物轮询自己读到 off 而退出（进程被 kill 之外的兜底）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {"status": "off", "gif": "clawd-sleeping.gif", "updated_at": time.time()}
    tmp = PET_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(PET_STATE_FILE)


# ── hook 配置工具 ──────────────────────────────────────

def _is_pet_hook(hook: dict) -> bool:
    return HOOK_MARKER in str(hook.get("command", ""))


def _strip_pet_hooks(settings: dict) -> tuple[dict, int]:
    """移除所有 pet hook，返回 (新 hooks 段, 移除条数)。

    只删除 command 含 pet-hook 的 hook；同一 group 里的其他 hook 原样保留；
    group 被删空才删 group；事件被删空才删事件键。其他顶层字段完全不动。
    """
    hooks = settings.get("hooks") or {}
    new_hooks: dict = {}
    removed = 0

    for event, groups in hooks.items():
        kept_groups = []
        for group in groups:
            inner = group.get("hooks")
            if not isinstance(inner, list):
                kept_groups.append(group)      # 结构异常时不擅自改动
                continue
            non_pet = [h for h in inner if not _is_pet_hook(h)]
            removed += len(inner) - len(non_pet)
            if non_pet:
                kept_groups.append({**group, "hooks": non_pet})
        if kept_groups:
            new_hooks[event] = kept_groups

    return new_hooks, removed


def _count_pet_hooks(settings: dict) -> int:
    return sum(
        1
        for groups in (settings.get("hooks") or {}).values()
        for group in groups
        for hook in (group.get("hooks") or [])
        if _is_pet_hook(hook)
    )


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        print(f"{RED}✗{NC} {path} 不是合法 JSON，跳过：{e}")
        return None


def _write_json_atomic(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


# ── 子命令 ─────────────────────────────────────────────

def cmd_status(_args) -> int:
    print(f"{BLUE}═══ 进程 ═══{NC}")
    pids = _pet_pids()
    if pids:
        for pid in pids:
            print(f"  运行中: PID {pid}")
    else:
        print("  未运行")

    print(f"\n{BLUE}═══ 抑制标记 ═══{NC}")
    print(f"  stopped  (持久，跨会话): {'存在 → 已停用' if STOPPED_FILE.exists() else '无'}")
    print(f"  disabled (会话级):        {'存在 → 本会话已停用' if DISABLED_FILE.exists() else '无'}")

    print(f"\n{BLUE}═══ hook 配置 ═══{NC}")
    total = 0
    for path in DEFAULT_SETTINGS:
        data = _read_json(path)
        if data is None:
            continue
        n = _count_pet_hooks(data)
        total += n
        print(f"  {path}: {n} 条 pet hook")
    if total == 0:
        print("  未安装 pet hook")

    print(f"\n{BLUE}═══ 目录 ═══{NC}")
    print(f"  状态目录: {STATE_DIR} {'(存在)' if STATE_DIR.exists() else '(不存在)'}")
    print(f"  hook 链接: {HOOK_LINK} {'(存在)' if HOOK_LINK.exists() else '(不存在)'}")
    return 0


def cmd_stop(_args) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # 先落抑制标记，再杀进程——避免被杀瞬间有 hook 触发把宠物重新拉起
    STOPPED_FILE.write_text(
        f"stopped by pet_ctl at {datetime.now().isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )
    DISABLED_FILE.touch()
    _write_off_state()

    killed = _kill_pets()
    # 再扫一遍：抑制标记生效前已启动的 hook 可能刚拉起新进程
    killed += [p for p in _kill_pets(timeout=1.0) if p not in killed]

    if killed:
        print(f"{GREEN}✓{NC} 已结束宠物进程: {', '.join(f'PID {p}' for p in killed)}")
    else:
        print(f"{YELLOW}ℹ{NC} 没有正在运行的宠物进程")
    print(f"{GREEN}✓{NC} 已写入持久抑制标记 {STOPPED_FILE}")
    print("  新开 Claude Code 会话也不会复活；恢复用: pet_ctl.py start")
    return 0


def cmd_start(_args) -> int:
    changed = []
    for f in (STOPPED_FILE, DISABLED_FILE):
        if f.exists():
            f.unlink()
            changed.append(f.name)
    if changed:
        print(f"{GREEN}✓{NC} 已移除抑制标记: {', '.join(changed)}")
    else:
        print(f"{YELLOW}ℹ{NC} 没有抑制标记，宠物一直是启用状态")

    if STOPPED_FILE.exists() or DISABLED_FILE.exists():
        print(f"{RED}✗{NC} 仍有标记残留")
        return 1

    print("  宠物会在下一个 hook 事件（如发送消息）时自动出现")
    print("  想立即看到: .venv/bin/python pet_window.py --test")
    return 0


def cmd_uninstall(args) -> int:
    if args.settings:
        targets = [Path(s).expanduser() for s in args.settings]
    else:
        targets = [p for p in DEFAULT_SETTINGS if p.exists()]

    if not targets:
        print(f"{YELLOW}ℹ{NC} 没找到 settings 文件")

    total_removed = 0
    for path in targets:
        data = _read_json(path)
        if data is None:
            continue
        new_hooks, removed = _strip_pet_hooks(data)
        if removed == 0:
            print(f"{YELLOW}ℹ{NC} {path}: 无 pet hook")
            continue

        total_removed += removed
        print(f"\n{BLUE}▶ {path}{NC}")
        print(f"  将移除 pet hook: {removed} 条")
        remaining = {
            event: sum(len(g.get('hooks', [])) for g in groups)
            for event, groups in new_hooks.items()
        }
        if remaining:
            print("  保留的其他 hook:")
            for event, n in remaining.items():
                print(f"    {event:<20} {n} 条")
        else:
            print("  没有其他 hook 需要保留")

        if args.dry_run:
            continue

        backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        print(f"  备份: {backup}")

        if new_hooks:
            data["hooks"] = new_hooks
        else:
            data.pop("hooks", None)
        _write_json_atomic(path, data)
        print(f"  {GREEN}✓{NC} 已写入")

    if args.dry_run:
        print(f"\n{YELLOW}dry-run：未做任何修改{NC}")
        return 0

    if total_removed == 0:
        print(f"\n{YELLOW}ℹ{NC} 没有需要卸载的 pet hook")

    # 链接、状态目录、宠物进程都属于全局安装，只在清理全局 settings 时处理，
    # 避免用 --settings 指向测试文件时误删真实环境
    global_targets = {p.resolve() for p in DEFAULT_SETTINGS}
    touched_global = any(p.resolve() in global_targets for p in targets)
    if not touched_global:
        print(f"{YELLOW}ℹ{NC} 未清理链接/状态目录/进程（仅 --settings 指定的非默认文件被处理）")
        print(f"\n{GREEN}✓{NC} 卸载完成。其他 hook 未改动。")
        return 0

    if HOOK_LINK.exists() or HOOK_LINK.is_symlink():
        HOOK_LINK.unlink()
        print(f"{GREEN}✓{NC} 已删除 hook 链接: {HOOK_LINK}")

    killed = _kill_pets()
    if killed:
        print(f"{GREEN}✓{NC} 已结束宠物进程: {', '.join(f'PID {p}' for p in killed)}")

    if args.keep_state:
        print(f"{YELLOW}ℹ{NC} 保留状态目录: {STATE_DIR}")
    elif STATE_DIR.exists():
        shutil.rmtree(STATE_DIR, ignore_errors=True)
        print(f"{GREEN}✓{NC} 已删除状态目录: {STATE_DIR}")

    print(f"\n{GREEN}✓{NC} 卸载完成。其他 hook 未改动。")
    print("  重新安装: ./install.sh")
    return 0


# ── 入口 ───────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pet_ctl.py",
        description="桌面宠物进程管理与卸载",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="查看状态")
    sub.add_parser("stop", help="结束宠物并持久抑制")
    sub.add_parser("start", help="解除抑制")

    p_un = sub.add_parser("uninstall", help="仅卸载 pet hook")
    p_un.add_argument("--dry-run", action="store_true", help="预览，不修改")
    p_un.add_argument("--keep-state", action="store_true", help="保留状态目录")
    p_un.add_argument("--settings", action="append", metavar="PATH",
                      help="指定 settings 文件（可多次；默认扫描全局与 local）")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return 2

    return {"status": cmd_status, "stop": cmd_stop,
            "start": cmd_start, "uninstall": cmd_uninstall}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
