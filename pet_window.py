#!/usr/bin/env python3
"""桌面宠物窗口 — macOS 原生 AppKit 实现

由 pet_hook.py 管理生命周期。
通过轮询 pet_state.json 切换动画。
启动时自动杀旧进程保证单例。

用法:
    python3 pet_window.py <gif_file>    # hook 启动
    python3 pet_window.py --test        # 测试模式：循环展示所有状态
"""

import json
import os
import signal
import sys
from pathlib import Path

# ── 配置 ───────────────────────────────────────────────

STATE_DIR = Path.home() / ".claude/state/pet"
STATE_FILE = STATE_DIR / "pet_state.json"
PID_FILE = STATE_DIR / "pet.pid"
POSITION_FILE = STATE_DIR / "pet_position.json"
POLL_INTERVAL = 0.5       # 轮询间隔（秒）

PROJECT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_DIR / "assets"

WINDOW_W, WINDOW_H = 200, 200

# ── AppKit 导入 ────────────────────────────────────────

from AppKit import (
    NSApplication, NSWindow, NSImageView, NSImage, NSColor,
    NSBorderlessWindowMask,
    NSFloatingWindowLevel, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary, NSScreen,
    NSApplicationActivationPolicyAccessory,
    NSImageScaleProportionallyUpOrDown,
)
from Foundation import NSURL, NSMakeRect, NSPoint, NSTimer
import objc
import time as _time

# ── 可拖动 ImageView（让无边框窗口能通过 GIF 图像拖动） ──

class DraggableImageView(NSImageView):
    """NSImageView 子类：mouseDownCanMoveWindow 返回 True，
    让无边框窗口可以通过点击 GIF 图像来拖动窗口。"""
    def mouseDownCanMoveWindow(self):
        return True


def _resolve_gif(gif_name: str) -> str | None:
    for base in [ASSETS_DIR, Path("/Users/zs/工作/py/clawd-on-desk/assets/gif")]:
        path = base / gif_name
        if path.exists():
            return str(path)
    return None


def _load_position() -> tuple[float, float]:
    try:
        data = json.loads(POSITION_FILE.read_text(encoding="utf-8"))
        return float(data.get("x", -1)), float(data.get("y", -1))
    except (FileNotFoundError, json.JSONDecodeError):
        return -1, -1


def _save_position(x: float, y: float):
    try:
        POSITION_FILE.write_text(json.dumps({"x": x, "y": y}, ensure_ascii=False))
    except OSError:
        pass


def _default_origin() -> NSPoint:
    x, y = _load_position()
    if x >= 0 and y >= 0:
        return NSPoint(x, y)
    screen = NSScreen.mainScreen()
    if screen is None:
        return NSPoint(100, 100)
    visible = screen.visibleFrame()
    return NSPoint(
        visible.origin.x + visible.size.width - WINDOW_W - 20,
        visible.origin.y + visible.size.height - WINDOW_H - 40,
    )


def _make_image_view(gif_path: str) -> DraggableImageView | None:
    iv = DraggableImageView.alloc().initWithFrame_(
        NSMakeRect(0, 0, WINDOW_W, WINDOW_H)
    )
    img = NSImage.alloc().initWithContentsOfURL_(NSURL.fileURLWithPath_(gif_path))
    if img is None:
        return None
    iv.setImage_(img)
    iv.setAnimates_(True)
    iv.setImageScaling_(NSImageScaleProportionallyUpOrDown)
    return iv


class PetWindow:
    """桌面宠物窗口：NSWindow + NSImageView，可拖动。"""

    def __init__(self, initial_gif: str):
        origin = _default_origin()
        rect = NSMakeRect(origin.x, origin.y, WINDOW_W, WINDOW_H)

        self._win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSBorderlessWindowMask, 2, False
        )
        self._win.setOpaque_(False)
        self._win.setBackgroundColor_(NSColor.clearColor())
        self._win.setHasShadow_(False)
        self._win.setLevel_(NSFloatingWindowLevel)
        self._win.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces |
            NSWindowCollectionBehaviorStationary
        )
        self._win.setMovableByWindowBackground_(True)

        self._current_gif = initial_gif
        view = _make_image_view(initial_gif)
        self._current_view = view
        if view:
            self._win.contentView().addSubview_(view)
        self._win.makeKeyAndOrderFront_(None)

    def swap_to(self, gif_path: str):
        if gif_path == self._current_gif:
            return
        if self._current_view:
            self._current_view.removeFromSuperview()
        view = _make_image_view(gif_path)
        if view:
            self._win.contentView().addSubview_(view)
            self._current_view = view
            self._current_gif = gif_path

    def save_position(self):
        frame = self._win.frame()
        _save_position(frame.origin.x, frame.origin.y)

    def close(self):
        self.save_position()
        self._win.close()


# ── 轮询驱动 ─────────────────────────────────────────

class PollDriver:
    """NSTimer 每 POLL_INTERVAL 秒读 pet_state.json，状态变则切换/退出。

    文件格式: {"status": "...", "gif": "clawd-xxx.gif", "updated_at": ...}
    当 status=="off" 时退出（与 hook 中 _kill_pet 配合：hook 删 PID 文件后
    pet 自身也退出）。

    实际上 status=="off" 时 hook 直接 _kill_pet，但万一 hook 没杀掉，
    这里的轮询作为兜底：读到 status=="off" 就 self-quit。
    """

    def __init__(self, pet: PetWindow):
        self._pet = pet
        self._last_update: float = 0
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_INTERVAL, self, "poll:", None, True
        )

    def poll_(self, timer):
        # 每次轮询都保存位置，确保拖动后位置不丢失
        self._pet.save_position()
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            # 状态文件不存在 → hook 清理了 → 退出
            self._pet.close()
            NSApplication.sharedApplication().terminate_(None)
            return

        ts = data.get("updated_at", 0)
        if ts <= self._last_update:
            return
        self._last_update = ts

        status = data.get("status", "")
        if status == "off":
            self._pet.close()
            NSApplication.sharedApplication().terminate_(None)
            return

        gif = data.get("gif", "")
        if gif:
            path = _resolve_gif(gif)
            if path:
                self._pet.swap_to(path)


# ── 单例 ─────────────────────────────────────────────

def _kill_existing():
    try:
        old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return
    try:
        os.kill(old_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # 给旧进程一点时间退出
    _time.sleep(0.2)
    # 如果还没退，强杀
    try:
        os.kill(old_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# ── 主入口 ────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <gif_file>  或  {sys.argv[0]} --test", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--test":
        _run_test()
        return

    path = _resolve_gif(sys.argv[1])
    if path is None:
        print(f"找不到 GIF 文件: {sys.argv[1]}", file=sys.stderr)
        sys.exit(1)

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # 单例：杀旧进程
    _kill_existing()

    # 忽略 SIGUSR1——hook 用此信号通知状态文件更新，但本进程
    # 已通过 NSTimer 轮询 pet_state.json，不需要信号触发。
    # 忽略它，防止默认行为（进程终止）导致宠物被误杀。
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)

    ns_app = NSApplication.sharedApplication()
    ns_app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    pet = PetWindow(path)
    PID_FILE.write_text(str(os.getpid()))
    sys.stdout.flush()

    # 启动文件轮询
    driver = PollDriver(pet)

    try:
        ns_app.run()
    except KeyboardInterrupt:
        pass
    finally:
        pet.save_position()
        PID_FILE.unlink(missing_ok=True)


# ── 测试模式 ────────────────────────────────────────

class _TestRunner:
    def __init__(self, pet: PetWindow, paths: list[str], labels: list[str]):
        self.pet = pet
        self.paths = paths
        self.labels = labels
        self.idx = 0

    def nextState_(self, timer):
        self.idx += 1
        if self.idx < len(self.paths):
            print(f"  [{self.idx+1}/{len(self.paths)}] {self.labels[self.idx]}")
            self.pet.swap_to(self.paths[self.idx])
        else:
            timer.invalidate()
            print("=" * 50)
            print("测试完成 ✅ 窗口将在 2 秒后关闭")
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                2.0, NSApplication.sharedApplication(), "terminate:", None, False
            )


def _run_test():
    all_states = [
        ("standby", "clawd-idle.gif"),
        ("working", "clawd-typing.gif"),
        ("waiting_user", "clawd-happy.gif"),
        ("need_user", "clawd-notification.gif"),
        ("error", "clawd-error.gif"),
        ("off", "clawd-sleeping.gif"),
    ]

    print("桌面宠物测试模式 — 依次展示所有状态动画 (3s 间隔)")
    print("=" * 50)

    paths, labels = [], []
    for status, gif in all_states:
        path = _resolve_gif(gif)
        if path:
            paths.append(path)
            labels.append(status)
            print(f"  ✓ {status}: {gif}")
        else:
            print(f"  ✗ {status}: {gif} 找不到")

    if not paths:
        print("无可用 GIF 文件")
        return

    ns_app = NSApplication.sharedApplication()
    ns_app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    pet = PetWindow(paths[0])
    print()

    _TestRunner(pet, paths, labels)
    try:
        ns_app.run()
    except KeyboardInterrupt:
        print("退出")
    finally:
        pet.save_position()
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
