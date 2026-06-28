#!/usr/bin/env python3
# 首选项目虚拟环境运行: .venv/bin/python
"""pet-hook — Claude Code 全局 Hook 脚本

将 Claude Code 运行状态映射到桌面宠物动画。

调用方式（由 Claude Code hook 触发）:
    python3 pet_hook.py <event> <status> <priority>

架构：纯 hook 驱动，无常驻进程。
每次 hook 触发时：
  0. SessionStart 时自动检测设备，刷新哨兵文件；其他事件读哨兵文件快速退出
  1. flock 全局锁（防多实例同时写状态文件）
  2. 扫描 ~/.claude/state/pet/instances/ → 清理过期文件
  3. 写入/更新本实例状态文件
  4. 取最后更新的实例状态（后触发者胜）
  5. 状态有变化 → 控制宠物动画
     - off → 杀掉 pet_window 子进程
     - 其他 → 写状态文件，子进程通过轮询自动切换动画
  6. 释放锁，退出
"""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# ── 配置 ───────────────────────────────────────────────

STATE_DIR = Path.home() / ".claude/state/pet"
INSTANCES_DIR = STATE_DIR / "instances"
LOCK_FILE = STATE_DIR / "lock"
CURRENT_STATE_FILE = STATE_DIR / "current_state"
DISABLED_FILE = STATE_DIR / "disabled"

PET_STATE_FILE = STATE_DIR / "pet_state.json"
PET_PID_FILE = STATE_DIR / "pet.pid"
PET_WINDOW_SCRIPT = Path(__file__).resolve().parent / "pet_window.py"
VENV_PYTHON = Path(__file__).resolve().parent / ".venv/bin/python"

# 分状态 TTL（秒）
TTL: dict[str, float] = {
    "working": 15,
    "standby": 120,
    "waiting_user": 300,
}

# 闪烁状态——需要人工介入，不能被其他实例的低优先级状态覆盖
BLINK_STATES: set[str] = {"need_user", "error"}

# 状态 → GIF 文件名（用于 pet_window.py 切换动画）
# off 状态直接杀子进程，不通过文件
STATE_MAP: dict[str, str] = {
    "off":           "clawd-sleeping.gif",
    "working":       "clawd-typing.gif",
    "standby":       "clawd-idle.gif",
    "waiting_user":  "clawd-happy.gif",
    "need_user":     "clawd-notification.gif",
    "error":         "clawd-error.gif",
}

# Debounce：从 working 切到 standby 的最小间隔（秒）
DEBOUNCE_WORKING = 3.0

# StopFailure 冷却：同一 session 连续 StopFailure 最小间隔（秒）
# 防止 API 故障时的事件风暴反复覆盖用户恢复操作
STOPFAILURE_DEBOUNCE = 5.0

PROJECT_DIR = Path(__file__).resolve().parent

# 日志配置
LOG_FILE = STATE_DIR / "hook.log"
NO_LOG_FILE = STATE_DIR / "no-log"
MAX_LOG_SIZE = 100 * 1024  # 100KB


def _log(session_id: str, event: str, status: str, result: str):
    """追加一行日志，超限时自动截断。失败不影响主逻辑。"""
    if NO_LOG_FILE.exists():
        return
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        short_id = session_id[:8]
        line = f"{now} {short_id} {event} {status} {result}\n"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
        if LOG_FILE.stat().st_size > MAX_LOG_SIZE:
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
            LOG_FILE.write_text("\n".join(lines[-500:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def _ensure_dirs():
    INSTANCES_DIR.mkdir(parents=True, exist_ok=True)


# stdin 解析结果缓存——sys.stdin 只能读一次，解析后缓存供后续使用
_stdin_cache: tuple[str, str] | None = None


def _parse_stdin() -> tuple[str, str]:
    """从 stdin 解析 session_id 和 tool_name。只读一次，结果缓存。

    Claude Code 将 hook 事件数据以 JSON 格式写入 stdin。
    返回 (session_id, tool_name)，tool_name 可能为空字符串。
    """
    global _stdin_cache
    if _stdin_cache is not None:
        return _stdin_cache
    tool_name = ""
    try:
        raw = sys.stdin.read()
        if raw and raw.strip():
            data = json.loads(raw)
            sid = data.get("session_id") or data.get("sessionId")
            if sid:
                session_id = str(sid)
            else:
                session_id = f"ppid{os.getppid()}-{int(time.time() * 1000)}"
            tool_name = str(data.get("tool_name") or data.get("toolName") or "")
        else:
            session_id = f"ppid{os.getppid()}-{int(time.time() * 1000)}"
    except (json.JSONDecodeError, OSError):
        session_id = f"ppid{os.getppid()}-{int(time.time() * 1000)}"
    env_sid = os.environ.get("CLAUDE_SESSION_ID")
    if env_sid:
        session_id = env_sid
    _stdin_cache = (session_id, tool_name)
    return _stdin_cache


def _get_session_id() -> str:
    sid, _ = _parse_stdin()
    return sid


def _cleanup_expired(now: float):
    try:
        for path in INSTANCES_DIR.iterdir():
            if not path.suffix == ".json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                path.unlink(missing_ok=True)
                continue
            max_age = TTL.get(data.get("status", ""))
            if max_age is None:
                continue          # 不在 TTL 表里的状态不清理（need_user / error）
            if now - data.get("updated_at", 0) > max_age:
                path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass


def _write_instance(session_id: str, status: str, priority: int, project: str):
    """原子写入实例文件：先写临时文件，再 rename。

    每个进程使用独立的 tmp 文件名（加 PID 和时间戳），完全消除
    pre-flock 并发写的竞态窗口。两个进程同时 rename 同一个 target
    是不可控的，但至少各自的 tmp 不会互相覆盖——rename 是原子的，
    最终 target 一定是其中一个完整有效 JSON，不会是损坏文件。
    """
    target = INSTANCES_DIR / f"{session_id}.json"
    tmp = INSTANCES_DIR / f".{session_id}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    # claude_pid 仅作调试元数据写入，不参与任何灯光逻辑决策。
    # 闪烁锁基于实例文件内容（status 是否为 BLINK_STATE）而非 PID 判断。
    content = json.dumps({
        "session_id": session_id,
        "project": project,
        "status": status,
        "priority": priority,
        "updated_at": time.time(),
        "claude_pid": os.getppid(),
    }, ensure_ascii=False)
    tmp.write_text(content, encoding="utf-8")
    try:
        tmp.replace(target)
    except OSError:
        tmp.unlink(missing_ok=True)


def _delete_instance(session_id: str):
    (INSTANCES_DIR / f"{session_id}.json").unlink(missing_ok=True)


def _read_current_state() -> dict | None:
    try:
        return json.loads(CURRENT_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_current_state(state: dict):
    CURRENT_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _aggregate() -> tuple[str, int, str]:
    """扫描所有实例文件，返回 最后更新 的实例状态。

    后触发者胜——不按优先级聚合，谁最后写文件就听谁的。
    无有效实例时返回 ("off", sys.maxsize, "")。
    """
    best_status = "off"
    best_priority = sys.maxsize
    best_session_id = ""
    best_updated = 0.0

    try:
        for path in INSTANCES_DIR.iterdir():
            if not path.suffix == ".json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            updated = data.get("updated_at", 0)
            if updated > best_updated:
                best_updated = updated
                best_status = data.get("status", "off")
                best_priority = data.get("priority", sys.maxsize)
                best_session_id = data.get("session_id", "")
    except FileNotFoundError:
        pass
    return best_status, best_priority, best_session_id


def _pet_alive() -> bool:
    """检查 pet_window 子进程是否存活。"""
    try:
        pid = int(PET_PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
        return False


def _kill_pet():
    """杀死正在运行的 pet_window 进程。"""
    try:
        old_pid = int(PET_PID_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        old_pid = None
    if old_pid:
        try:
            os.kill(old_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        subprocess.run(
            ["pkill", "-f", "pet_window.py"],
            capture_output=True,
            timeout=2,
        )
    except Exception:
        pass
    PET_PID_FILE.unlink(missing_ok=True)
    PET_STATE_FILE.unlink(missing_ok=True)


def _apply_pet(status: str):
    """根据聚合后的状态，启动/通知桌面宠物子进程。

    - "off" → 杀掉子进程，窗口关闭
    - 其他状态 → 写状态文件；子进程通过 NSTimer 轮询自动切换动画，
      无需 SIGUSR1（默认行为是终止进程，会误杀宠物）。
    """
    if status not in STATE_MAP:
        return

    gif_file = STATE_MAP[status]

    if status == "off":
        _kill_pet()
        return

    # 原子写入状态文件
    state_data = {
        "status": status,
        "gif": gif_file,
        "updated_at": time.time(),
    }
    tmp = PET_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state_data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PET_STATE_FILE)

    # 检查子进程是否存活，不存活则启动
    try:
        pid = int(PET_PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
        # 启动前先清理任何意外残留的旧进程（双重保险）
        _kill_pet()
        # 重新写状态文件（_kill_pet 会 unlink 它）
        tmp = PET_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state_data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(PET_STATE_FILE)
        subprocess.Popen(
            [str(VENV_PYTHON), str(PET_WINDOW_SCRIPT), gif_file],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


# ── 主入口 ────────────────────────────────────────────

def main():
    if len(sys.argv) < 4:
        print(f"用法: {sys.argv[0]} <event> <status> <priority>", file=sys.stderr)
        sys.exit(0)

    event = sys.argv[1]
    status = sys.argv[2]
    priority = int(sys.argv[3])

    # 从 stdin 获取 session_id（需要先调用，后面 get_tool_name 依赖缓存）
    session_id = _get_session_id()

    # ── 智能状态修正：根据 tool_name 覆盖 naive 状态 ──
    # install.py 注册的 hook 没有 matcher（所有 PreToolUse 统一 = working），
    # 这里根据 stdin 中的 tool_name 做精准修正：
    #   - PreToolUse + AskUserQuestion → need_user（模型在提问，用户还没回答）
    #   - PostToolUse   + AskUserQuestion → working（用户已经回答了，模型正在思考）
    # 其他事件保持 install.py 注册的默认状态不变。
    _, tool_name = _parse_stdin()
    if event == "PreToolUse" and tool_name == "AskUserQuestion":
        status = "need_user"
        priority = 2
    elif event == "PostToolUse" and tool_name == "AskUserQuestion":
        status = "working"
        priority = 4

    # SessionStart 时清除禁用哨兵（宠物模式无硬件检测，默认启用）
    if event == "SessionStart":
        DISABLED_FILE.unlink(missing_ok=True)

    # 哨兵文件存在 → 已禁用，直接退出
    if DISABLED_FILE.exists():
        _log(session_id, event, status, "disabled")
        sys.exit(0)
    project = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    _ensure_dirs()

    # ── 先写本实例文件（无需 flock，per-session 原子操作）────
    # 必须在 flock 之前写：PreToolUse 和 PermissionRequest 可能并发，
    # 抢锁输的一方也不能丢状态——实例文件已落盘，下一次聚合就能读到。
    if event == "SessionEnd":
        # SessionEnd 在 flock 内通过 _delete_instance 清理自身实例，
        # 不在此处写入（避免先写 off 又被其他并发 hook 覆盖的竞态）。
        pass
    else:
        _write_instance(session_id, status, priority, project)

    # ── flock（带重试，避免多实例竞争时静默丢弃） ────
    lock_fd = None
    try:
        lock_fd = open(LOCK_FILE, "a+")
    except OSError:
        lock_fd = None

    acquired = False
    if lock_fd is not None:
        for _attempt in range(3):
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError):
                time.sleep(0.5)

    if not acquired:
        _log(session_id, event, status, "flock_busy:skipped")
        if lock_fd:
            lock_fd.close()
        sys.exit(0)

    try:
        now = time.time()

        # 0) 清理过期（在聚合之前，确保不会读到过期数据）
        _cleanup_expired(now)

        # 0.5) StopFailure 去重：同一 session 连续 StopFailure 冷却
        # 防止 API 故障风暴反复覆盖用户恢复后的工作状态。
        # 只抑制同一 session 的重复 StopFailure，真正的新错误仍会触发。
        if event == "StopFailure":
            prev_state = _read_current_state()
            if prev_state and prev_state.get("status") == "error":
                owner = prev_state.get("owner_session_id", "")
                last_error = prev_state.get("updated_at", 0)
                if owner == session_id and now - last_error < STOPFAILURE_DEBOUNCE:
                    _log(session_id, event, status, "debounce:skip_consecutive_error")
                    raise SystemExit(0)

        # 0.6) SessionEnd：删除本实例，让聚合仅反映其他活跃 session
        if event == "SessionEnd":
            _delete_instance(session_id)

        # 1) 聚合
        best_status, best_priority, aggregated_session_id = _aggregate()

        # 1.1) 竞态修复：pre-flock 写可能被并发 hook 覆盖。
        # SessionEnd 已主动删实例，不参与竞态修复。
        if event != "SessionEnd" and best_status != status:
            _write_instance(session_id, status, priority, project)
            best_status, best_priority, aggregated_session_id = _aggregate()

        # 1.5) 日志：记录 SessionStart 的设备检测结果
        if event == "SessionStart":
            _log(session_id, event, status, "auto-detect:enabled")

        # 2.5) SessionStart 抑制：有其他会话正在运行时，不切到 standby
        # 只有之前聚合状态是 off，新会话的 standby 才允许切换
        if event == "SessionStart" and best_status == "standby":
            # 不含本实例重新聚合，判断是否真的"之前是空闲的"
            _delete_instance(session_id)
            prev_status_only, prev_priority, prev_session_id = _aggregate()
            _write_instance(session_id, status, priority, project)
            if prev_status_only != "off":
                _log(session_id, event, status, f"standby-restraint:kept_{prev_status_only}")
                best_status = prev_status_only
                best_priority = prev_priority
                aggregated_session_id = prev_session_id

        # 2.6) 闪烁锁：闪烁状态一旦确立，只能被同一实例、更高优先级闪烁、或锁主已退出的情况覆盖
        # 锁主是否"存活"不由 PID 判断，而是检查其实例文件的 status：
        #   - 实例文件仍为 BLINK_STATE → 锁主还在闪烁状态 → 保护
        #   - 实例文件非 BLINK_STATE（如 off/working）→ 锁主已退出 → 放行
        #   - 实例文件不存在 → 放行
        prev = _read_current_state()
        if prev and prev.get("status") in BLINK_STATES:
            owner_id = prev.get("owner_session_id", "")
            owner_file = INSTANCES_DIR / f"{owner_id}.json"
            # 读锁主实例的内容状态，判断锁是否真的还在
            owner_still_blinking = False
            if owner_file.exists():
                try:
                    owner_data = json.loads(owner_file.read_text(encoding="utf-8"))
                    owner_still_blinking = owner_data.get("status", "") in BLINK_STATES
                except (json.JSONDecodeError, OSError):
                    pass  # 文件损坏 → 视为锁主已退出
            if owner_still_blinking:
                # 锁主还在闪烁 → 只有同一实例或更高优先级闪烁才能覆盖
                if aggregated_session_id == owner_id:
                    pass  # 放行：同一实例
                elif aggregated_session_id != "" and best_status in BLINK_STATES and best_priority < prev.get("priority", sys.maxsize):
                    pass  # 放行：更高优先级闪烁升级（error 覆盖 need_user）
                else:
                    _log(session_id, event, status, f"blink-lock:rejected_{best_status}")
                    best_status = prev["status"]
                    best_priority = prev["priority"]
                    aggregated_session_id = owner_id  # 保持原所有者不变
            # else: 锁主已退出闪烁状态（正常结束 / SessionEnd 写了 off）→ 放行

        # 2.7) 用户主动输入 → 无条件覆盖任何闪烁锁
        # UserPromptSubmit 是"人在用"的明确信号：无论之前的 error/need_user
        # 是哪个 session 触发的、锁主是否还在，用户正在操作就应该反映工作状态。
        # 系统事件（PreToolUse/StopFailure）不应自动覆盖异常，
        # 但人的明确输入行为例外。
        if event == "UserPromptSubmit" and best_status != status:
            _log(session_id, event, status, f"user-override:{prev.get('status','?')}→{status}")
            best_status = status
            best_priority = priority
            aggregated_session_id = session_id

        # 3) Debounce: 本实例刚切到 standby，但之前是 working → 保持 working
        if best_status == "standby":
            prev = _read_current_state()
            if prev and prev.get("status") == "working":
                working_since = prev.get("working_since", 0)
                if now - working_since < DEBOUNCE_WORKING:
                    _log(session_id, event, status, "debounce:kept_working")
                    best_status = "working"
                    best_priority = 4

        # 4) 状态没变 → 跳过（但宠物进程死了要重启）
        prev_check = _read_current_state()
        if prev_check and prev_check.get("status") == best_status:
            # 检查宠物进程是否存活，死了就重启
            if not _pet_alive():
                _log(session_id, event, status, "same:restart_pet")
                _apply_pet(best_status)
            else:
                _log(session_id, event, status, "same")
            raise SystemExit(0)

        # 5) 控制宠物动画
        _apply_pet(best_status)

        # 6) 记录状态 + 日志
        prev_status = prev.get("status", "off") if prev else "off"
        gif_name = STATE_MAP.get(best_status, "?")
        _log(session_id, event, status, f"{prev_status}→{best_status}:{gif_name}")

        state_record = {
            "status": best_status,
            "priority": best_priority,
            "updated_at": now,
        }
        if best_status in BLINK_STATES:
            state_record["owner_session_id"] = aggregated_session_id
        if best_status == "working":
            state_record["working_since"] = prev.get("working_since", now) if prev else now
        _write_current_state(state_record)

    except SystemExit:
        pass
    finally:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        if lock_fd:
            lock_fd.close()


if __name__ == "__main__":
    main()
