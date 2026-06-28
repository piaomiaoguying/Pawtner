# 🪄 Pawtner — 让 AI 有了实体感官

> **不用看屏幕，就知道 Claude Code 在干嘛。**
>
> 桌面宠物实时反应 Claude Code 运行状态——AI 思考时播放打字动画、卡在权限等你说"yes"时闪烁提醒、报错了发出警报动画。把 AI 的数字灵魂装进桌面宠物里。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)

---

## ✨ 为什么你需要 Pawtner

Claude Code 在终端里跑着，你是不是经常这样：

- 🔄 切到别的窗口干别的事，不知道 AI 还在跑还是已经卡住了
- 😵 开着好几个 Terminal 窗口，不知道哪个在等你回复
- 🔕 后台跑任务，隔几分钟切回去看一眼——大多数时候啥也没发生

**Pawtner 把 AI 的状态从屏幕里拽了出来。** 看一眼桌面上的宠物，你就知道一切。

## 🎬 怎么玩

一行命令：

```bash
curl -sSL https://raw.githubusercontent.com/piaomiaoguying/lumos/master/install.sh | bash
```

然后正常用 Claude Code。宠物会自己动。

## 💡 动画语言

| 动画 | GIF | 内部状态 | 含义 |
|------|-----|---------|------|
| 打字 | ![](assets/clawd-typing.gif) | `working` | **AI 在干活** — 切出去摸鱼，宠物在打字就放心 |
| 空闲 | ![](assets/clawd-idle.gif) | `standby` | **AI 空闲等你** — 它输出完了，该你看了 |
| 通知 | ![](assets/clawd-notification.gif) | `need_user` | **需要你介入** — 权限确认、表单输入、多选题等 |
| 开心 | ![](assets/clawd-happy.gif) | `waiting_user` | **干完活了** — 模型完成响应，等你下一轮指令 |
| 错误 | ![](assets/clawd-error.gif) | `error` | **出错了** — API 限流/认证失败，快去救 |
| 睡眠 | ![](assets/clawd-sleeping.gif) | `off` | **没有活跃会话** — 下班了，宠物也睡了 |

## 🔗 Hook 事件 → 状态映射

Claude Code 的每个生命周期事件触发一个 hook，hook 写入实例状态后经由聚合引擎决定最终动画。以下是完整映射：

| Hook 事件 | 含义 | 注册状态 | 智能修正 | 最终状态 | 动画 | 优先级 |
|-----------|------|---------|---------|---------|------|--------|
| `SessionStart` | 新会话开始 | `standby` | — | `standby` | ![](assets/clawd-idle.gif) | 5 |
| `UserPromptSubmit` | 用户发送消息 | `working` | — | `working` | ![](assets/clawd-typing.gif) | 4 |
| `PreToolUse` (通用) | 模型调用工具前 | `working` | — | `working` | ![](assets/clawd-typing.gif) | 4 |
| `PreToolUse` + `AskUserQuestion` | 模型向用户提问 | `working` | → `need_user` | `need_user` | ![](assets/clawd-notification.gif) | 2 |
| `PostToolUse` (通用) | 模型调用工具后 | `working` | — | `working` | ![](assets/clawd-typing.gif) | 4 |
| `PostToolUse` + `AskUserQuestion` | 用户已回答问题 | `working` | — (保持) | `working` | ![](assets/clawd-typing.gif) | 4 |
| `Stop` | 模型完成响应 | `waiting_user` | — | `waiting_user` | ![](assets/clawd-happy.gif) | 5 |
| `PermissionRequest` | 请求用户批准工具 | `need_user` | — | `need_user` | ![](assets/clawd-notification.gif) | 2 |
| `PermissionDenied` | 用户拒绝了权限 | `standby` | — | `standby` | ![](assets/clawd-idle.gif) | 3 |
| `Elicitation` | MCP 工具请求输入 | `need_user` | — | `need_user` | ![](assets/clawd-notification.gif) | 3 |
| `StopFailure` | 响应生成失败 | `error` | — | `error` | ![](assets/clawd-error.gif) | 1 |
| `SessionEnd` | 会话结束 | `off` | — | `off` | ![](assets/clawd-sleeping.gif) | 999 |

**关于智能修正**：`AskUserQuestion` 触发两个 hook——`PreToolUse` 时模型还没收到回答（此时改状态为 `need_user`，闪烁提醒），`PostToolUse` 时用户已提交回答（改为 `working`，模型正在思考）。这个转换由 `pet_hook.py` 内部的 `tool_name` 检测完成。

**聚合规则**（多实例多会话并发）：
- **后触发者胜** — 取 `updated_at` 最新的实例状态（不做优先级聚合）
- **闪烁锁** — `need_user` / `error` 一旦确立，只有同实例或更高优先级闪烁能覆盖
- **TTL 过期** — `working` 15s、`standby` 120s、`waiting_user` 300s 后自动清理
- **Debounce** — `working` → `standby` 至少间隔 3 秒，防止瞬间抖动
- **SessionStart 抑制** — 已有其他会话活跃时，新会话不切 `standby`

## 🧬 桌面宠物，不是 GIF 表情包

这是你桌面上一个**活的、在动的宠物**——无边框、透明背景、始终置顶。它随着你的 AI 工作状态变换动画，像《攻壳机动队》里的塔奇克马有了灵魂。

采用 macOS 原生 AppKit 实现，**零外部依赖**、GPU 加速渲染、空闲 CPU 接近 0%。

## 🏗️ 架构

```
Claude Code Hook (async)
    ↓
pet_hook.py  ← 纯 hook 驱动，无常驻进程
    │
    ├─ SessionStart → 清除禁用哨兵
    ├─ 多实例聚合引擎 → 后触发者胜
    ├─ 闪烁锁 → need_user/error 不受低优先级干扰
    ├─ flock 全局锁 → 防并发冲突
    └─ pet_window.py (子进程) → GIF 动画桌面宠物
```

**近乎 0 CPU 占用，无常驻守护进程。** Hook 触发时才跑，跑完即退；宠物子进程 GPU 加速渲染，空闲动画接近零资源消耗。多实例安全。

## 🛠️ 命令

```bash
./install.sh                          # 一键安装
./toggle_pet.sh             # 开关宠物（自动检测运行状态）
./toggle_pet.sh off         # 关掉宠物
.venv/bin/python pet_window.py --test  # 测试所有状态动画
```

## 📜 许可

MIT

---

<p align="center">
  <i>Pawtner — 让 AI 从终端里走出来。</i><br>
  <sub>不是科幻，是你的桌面宠物。</sub>
</p>
