# Codex-Antigravity-Bridge

[English](./README.md) | 简体中文

面向 Windows 本地环境的 **OpenAI Codex**（ChatGPT 桌面应用 / Codex CLI）与 **Google Antigravity** 通用双向 MCP 通信桥接方案。

仓库地址：[https://github.com/Idris173/Codex-Antigravity-Bridge](https://github.com/Idris173/Codex-Antigravity-Bridge)

---

## 项目概览

`Codex-Antigravity-Bridge` 通过本地 Python 后台程序和共享收件箱，连接 Antigravity 的 `chatgpt-bridge` MCP 服务和 Codex 的 `antigravity_bridge` MCP 服务。

- **按需唤醒，日常休眠**：双端配套技能在普通对话中保持静默，仅在你明确要求跨端协作或收发消息时启用。
- **双通道传输（无头多会话并发 + 快速 UI 自动化）**：
  - **无头多会话模式（`codex_exec`）**：直接驱动 Codex 内置的 `codex.exe exec / resume` 后台进程，按独立 `binding_id` 隔离会话；支持多工作区同时并行运行，即使 Antigravity 与 ChatGPT 桌面端全部处于最小化状态也可无感收发（零焦点切换、零剪贴板写入）。
  - **桌面窗口模式（`uia`）**使用 Windows UI 自动化向 Codex 窗口发送请求，并从本地会话日志按 `dispatch_id` 匹配结果。
- **Codex → Antigravity** 通过 MCP 将请求和结果投递到本地收件箱，Antigravity 使用同一 `request_id` 回执。
- **中英双语自适应**：英文提问全程以英文协同与回复；简体中文提问全程以简体中文协同与回复。
- **模型、推理强度与速度选型**：通过 `bridge_routing_settings` / `agy_routing_settings` 查询双端可用模型、推理强度与速度档位（`标准 Standard`、`快速 Fast` 及模型支持时的 `超快 Ultrafast`，如 `GPT-6.2-Sol`），支持在开始新计划前确认两侧模型选型。

---

## 环境要求

- **Windows 10 / 11**
- **Python 3.11+**（仅使用标准库，零第三方依赖）
- **Google Antigravity**
- **OpenAI ChatGPT 桌面应用**（开启 Codex 功能）

---

## 安装与健康检查

无论从 Antigravity 还是从 ChatGPT / Codex 执行安装脚本，都会生成字节级一致的安装结果（详见 [AGENTS.md](./AGENTS.md)）：

```powershell
git clone https://github.com/Idris173/Codex-Antigravity-Bridge.git
cd Codex-Antigravity-Bridge
python install.py
python install.py --doctor
```

`install.py` 会执行以下非破坏性配置：

1. 将桥接运行时（`bridge_server.py`、`headless_multi_engine.py`、`notify_inbox.py`、`agy_model_guard.py`、`codex_projects.py`、`BridgeFastUia.cs`、`uia_driver.ps1`、`uia_daemon.ps1`）部署至 `~/.gemini/config/sidecars/chatgpt-bridge/`，并使用本机 `.NET` 工具链预编译 `BridgeFastUia.dll`。
2. 将双侧按需技能分别安装至 `~/.gemini/config/skills/codex-antigravity-bridge/` 与 `~/.codex/skills/codex-antigravity-bridge/`。
3. 在修改配置前自动生成带时间戳的 `.bak` 备份，并将 `chatgpt-bridge` 与 `[mcp_servers.antigravity_bridge]` 无损合并至 `~/.gemini/config/mcp_config.json` 和 `~/.codex/config.toml`（完整保留已有的全部其他 MCP 服务）。

> **首次安装提示**：若首次安装时 ChatGPT 桌面应用已处于打开状态，请**重启一次 ChatGPT 桌面应用**以加载新注册的 `antigravity_bridge` MCP 工具。

---

## 双向收发用法

可以直接对任一端说“在这个目录开一个配对会话，让 Codex 核对安装指南”。桥接工具负责传递会话和请求编号。无头 Codex 默认用自然语言收发。桌面窗口模式和授权回传一次结果的只读派发沿用旧封包。

### 1. Antigravity → Codex（会话绑定、任务派发与结果回收）

#### 无头多会话模式（推荐本地工作区使用）

支持多工作区并发执行，且在双端桌面窗口最小化时仍可后台无感运行：

```json
// 1) 绑定本地工作区会话（工具名：bridge_bind_session）
{
  "workspace_path": "C:/path/to/workspace",
  "transport": "codex_exec",
  "codex_model": "gpt-6.2-sol",
  "codex_effort": "high",
  "codex_speed": "ultrafast",
  "engine": "general",
  "mode": "autopilot"
}

// 2) 携带返回的 binding_id 派发任务（工具名：bridge_dispatch_task）
{
  "binding_id": "<binding_id>",
  "step_goal": "整理工作区安装要求",
  "task_instruction": "阅读 README.md 并列出安装要求。",
  "repository_read_only": true
}
```

- 若长耗时任务超出单次同步等待窗口（返回 `timed_out: true`），后台进程会继续运行；可稍后调用 `bridge_dispatch_status(dispatch_id="<dispatch_id>")` 或 `bridge_read_latest(binding_id="<binding_id>")` 查询状态并取回完整回复。
- 对于仅需只读核查并授权回写一条回执的任务，可在 `bridge_dispatch_task` 中传入 `"repository_read_only": true` 与 `"authorize_receipt": true`。
- 任务结束后调用 `bridge_unbind_session(binding_id="<binding_id>")` 释放绑定。

#### 桌面窗口模式

先调用 `bridge_inspect_app()` 查看当前打开的 ChatGPT 窗口与侧边栏项目/会话列表，再通过 `bridge_bind_session(create_in_project="<项目名>", engine="general", mode="semi_auto")` 或 `switch_to_session="<会话名>"` 锁定目标会话，随后携带 `binding_id` 调用 `bridge_dispatch_task`。

---

### 2. Codex → Antigravity（投递请求与按 `request_id` 回执）

#### 步骤 A — Codex 提交消息或变更摘要

Codex 调用 `antigravity_bridge` MCP 服务中的 `agy_submit_for_review`（在受权只读派发下也可调用 `agy_submit_dispatch_receipt`）：

```json
// 工具名：agy_submit_for_review
{
  "workspace_path": "C:/path/to/workspace",
  "step_summary": "已完成解析器更新并补充单元测试。",
  "changed_files": "src/parser.py, tests/test_parser.py",
  "self_check": "运行 python -B -m unittest discover -s tests -v 全部通过。",
  "open_questions": "NONE",
  "wait_for_reply": false
}
```

- 当 `wait_for_reply` 为 `true`（默认）时，调用会在本地总线上阻塞等待，并在收到回复后立即返回结果。
- 当 `wait_for_reply` 为 `false` 时，调用会返回 `"status": "queued"` 和唯一的 `request_id`。之后可以请 Codex 按该编号查询回执。

#### 步骤 B — Antigravity 读取收件箱并按 `request_id` 回执

在 Antigravity 中通过 `chatgpt-bridge` MCP 工具按 `workspace_path` 或精确 `request_id` 读取待处理单据，并向**同一个 `request_id`** 写回处理结果：

```json
// 1) 读取收件箱（工具名：bridge_check_codex_inbox）
{
  "request_id": "req_1700000000000_a1b2c3d4e5f6",
  "workspace_path": "C:/path/to/workspace"
}

// 2) 按同一 request_id 写回回执（工具名：bridge_reply_to_codex）
{
  "request_id": "req_1700000000000_a1b2c3d4e5f6",
  "decision": "APPROVED",
  "review_feedback": "已核对变更内容与单元测试结果。",
  "next_instruction": "继续执行下一步。"
}
```

> 当收件箱中同时存在多张待处理单据时，`bridge_reply_to_codex` 必须显式指定 `request_id`，以防误回其他单据。

也可以使用命令行子命令执行监听、查看与按 `request_id` 回执：

```powershell
# 仅等待监听启动之后新到达的提交
python "$HOME\.gemini\config\sidecars\chatgpt-bridge\bridge_server.py" cli wait_inbox --timeout 600 --only-new

# 查看当前待处理收件箱
python "$HOME\.gemini\config\sidecars\chatgpt-bridge\bridge_server.py" cli check_inbox

# 按指定 request_id 写回回执
python "$HOME\.gemini\config\sidecars\chatgpt-bridge\bridge_server.py" cli reply_codex `
  --request-id req_1700000000000_a1b2c3d4e5f6 `
  --decision APPROVED `
  --feedback "已核对变更内容与测试结果。" `
  --next "继续执行下一步。"
```

---

### 3. 模型、推理强度与速度选型

在 Antigravity 调用 `bridge_routing_settings`，或在 Codex 调用 `agy_routing_settings`，可以读取或保存模型、推理强度和速度设置。可用速度取决于模型。

---

## 可选功能：Antigravity 会话自动通知

默认情况下，收件箱通过按需调用或 `cli wait_inbox` 读取。如果你希望某个工作区（及其子目录）收到新的 Codex 收件箱请求时自动唤醒指定的 Antigravity 会话：

1. **切换到需要关联的目标工作区目录**，带 `--agy-conversation-id` 运行安装脚本：

   ```powershell
   cd C:\path\to\target-workspace
   python "C:\path\to\Codex-Antigravity-Bridge\install.py" --agy-conversation-id "CONVERSATION_ID"
   ```

2. **适用条件与运行机制**：
   - 安装器会写入 `~/.gemini/config/sidecars/chatgpt-bridge/sidecar.json`、在 `~/.gemini/config/config.json` 中启用 `sidecars.chatgpt-bridge`，并在 `~/.gemini/config/hooks.json` 中注册 `codex-antigravity-bridge-model` 生命周期钩子（存在 Sidecar 时 `--doctor` 会同步校验该钩子）。
   - 该功能依赖 Antigravity Sidecar 运行环境 `PATH` 中的 `agentapi` 命令；当该工作区到达新的待处理 `request_id` 时，后台监听器通过 `agentapi send-message <conversation-id> ...` 通知目标会话。
   - 普通请求通过 `bridge_check_codex_inbox` 读取，指定模型的受保护请求通过 `bridge_claim_codex_request` 领取，并在会话现有权限范围内调用 `bridge_reply_to_codex` 按原 `request_id` 回复。
   - 若修改配置后 Sidecar 或钩子未立即生效，请重启一次 Antigravity。
   - 如需暂停自动通知，将 `~/.gemini/config/config.json` 中的 `sidecars.chatgpt-bridge.enabled` 设为 `false` 即可。

### 配对工作区

- **Codex → Antigravity（`agy_workspace_pair`）**：
  1. 先确保目标文件夹已在 Antigravity 中作为工作区/项目打开，然后调用 `agy_workspace_pair(action="link", workspace_path="C:/path/to/workspace", conversation_id="<conversation_id>")`。
  2. 调用 `agy_workspace_pair(action="get", workspace_path="C:/path/to/workspace")` 查询状态，直至原生 `agentapi get-conversation-metadata` 核验目录通过并变为 `route.status == "confirmed"`。多个已确认目录可分别路由到各自的 Antigravity 会话。
  3. 调用 `action="open"` 尝试新建配对会话时，要求 `route.project_id` 已确认且与 `~/.gemini/config/config.json` 中 `sidecars.chatgpt-bridge.projectId` 一致（标准 `--agy-conversation-id` 安装默认不写入 `projectId`）。创建期间原 `"confirmed"` 路由保持收件不中断，可通过 `action="get"` 的 `route.open_attempt` 查看进度（`"opening"` / `"creating"`）或失败原因（`"blocked"` 与 `error_code`），仅在新会话通过目录与项目核验后切换。
- **Antigravity → Codex（`bridge_bind_session`）**：
  1. 调用 `bridge_bind_session(workspace_path="C:/path/to/workspace", reuse_workspace_binding=true)` 可按规范化真实路径复用已有的无头 `codex_exec` 会话绑定。
  2. 可选传入 `ensure_codex_project=true`，通过实验版 Codex App Server 复用或创建桌面侧边栏项目（绑定时返回 `project` 状态，首轮完成后在运行记录中写入 `project_attachment`）；若实验接口不可用，无头任务仍正常在 `workspace_path` 内执行。
  3. 无头派发可在 `bridge_dispatch_task` 中传 `wait_for_result=false` 立即返回 `dispatch_id`，随后用 `bridge_dispatch_status(dispatch_id="<dispatch_id>")` 按编号取回该轮结果。

### 会话模型控制与核验

| 目标场景 | 可用操作 | 生效范围与能力边界 |
| :--- | :--- | :--- |
| **Codex 无头绑定（`codex_exec`）** | `bridge_codex_session_model(binding_id, action="get" \| "set", model="<slug>")`（或绑定时传 `codex_model`） | 作用于该绑定的**下一轮无头执行**（`codex.exe exec` / `exec resume`），续接同一会话时保留上下文，并返回 `next_model`、`last_dispatched_model` 与 `last_observed_model`。**无法**改变正在运行中的轮次。 |
| **Codex 桌面窗口（`uia`）** | 本桥接工具不支持（返回 `CODEX_DESKTOP_MODEL_NOT_SUPPORTED`） | 无法通过本桥接工具远程修改 ChatGPT / Codex 桌面窗口的模型选择器。*（Codex 原生 CLI / App Server 自身的会话接口属于宿主独立能力，独立于本仓库桌面 UIA 模式。）* |
| **Antigravity 已有会话** | 通过会话内置的 Swarm 切换模型，再通过 `agy_submit_for_review` 要求使用该模型 | 保留原会话 ID 与上下文。宿主钩子在领取和回执时都核验会话与实际模型。请求因模型不符而阻塞时，可用指定模型恢复，也可从 Codex 取消。 |
| **Antigravity 新建会话** | 宿主原生 `agentapi new-conversation --model=<model_id>` | 本机已用 `gemini-4-neon-medium` 验证。目标模型不可用时宿主会回退至 `flash` 档。桥接工具 `agy_workspace_pair(action="open")` 没有 `model` 参数。 |

#### 切换 Antigravity 已有会话的模型

1. 用 `agy_workspace_pair(action="get")` 确认工作区已配对到目标会话，并启用 Sidecar 通知。用户明确要求切换时，通过 `agy_submit_for_review` 发送一条普通请求。把目标会话 ID 和准确模型 ID 写在正文里，省略 `required_model` 和 `target_conversation_id` 参数，让当前模型先执行切换。
2. 在目标会话中调用 `swarm(action="status")`。如果尚无 Swarm，用 `swarm(action="title", title="Model selection")` 初始化看板和协调者。确认当前会话是协调者后，调用 `swarm(action="model", name="coordinator", model="gemini-4-neon-medium")`。操作在原会话内进行，无需添加成员。按原请求编号回告工具结果。
3. 收到切模回执后，向同一会话另发一条要求使用准确模型的请求。检查回执中的 `execution_model` 与 `reply_model`，核验实际模型。隔离桌面会话已实测从 Hydrogen High 切到 Neon Medium，随后一条独立的 Sidecar 请求实际运行在 Neon Medium。

#### 为任务指定模型

在 Codex 中调用 `agy_submit_for_review` 并传入以下参数。指定模型的请求会立即返回 `request_id`。

```json
{
  "workspace_path": "C:/path/to/workspace",
  "target_conversation_id": "<confirmed_conversation_id>",
  "required_model": "gemini-4-neon-medium",
  "step_summary": "核对状态流转边界。"
}
```

在目标 Antigravity 会话中，用同一个 `request_id` 调用 `bridge_claim_codex_request` 领取，再用 `bridge_reply_to_codex` 回执。宿主钩子会自动提供模型校验凭证。

在 Codex 中用同一编号调用 `agy_fetch_director_reply`，查看 `delivery.status` 或读取回执。请求尚未领取或因模型校验阻塞时，可以调用 `agy_cancel_guarded_review` 取消。

在 Antigravity 中调用 `bridge_codex_session_model`，设置该绑定下一轮无头执行所用的模型。

```json
{
  "binding_id": "<binding_id>",
  "action": "set",
  "model": "gpt-6.2-astra"
}
```

[Antigravity Sidecar 接口（`agentapi`）](https://antigravity.google/docs/sidecars#agentapi) · [Antigravity Swarm](https://antigravity.google/docs/swarm/) · [Antigravity 生命周期钩子（`PreToolUse` / `PreInvocation`）](https://antigravity.google/docs/hooks#pretooluse)

---

## 卸载与隔离测试

卸载本套件并完整保留其他 MCP 服务、用户自定义文件及已保存的选型配置：

```powershell
python install.py --uninstall
```

*（若卸载时 `BridgeFastUia.dll` 正被运行中的宿主进程占用，请先退出对应宿主进程，再重新运行 `python install.py --uninstall` 完成收尾清理。）*

运行自动化测试套件（全程使用临时 `--home-dir`，不修改任何真实宿主配置）：

```powershell
python -B -m unittest discover -s tests -v
```

---

## 目录结构

```text
Codex-Antigravity-Bridge/
├── AGENTS.md                   # Antigravity 与 Codex 双端确定性安装规约
├── README.md                   # 英文文档 (English)
├── README.zh-CN.md             # 简体中文文档
├── install.py                  # 标准库安装 / --doctor 诊断 / --uninstall 卸载脚本
├── bridge/                     # 双向 MCP 服务、无头并发引擎、Sidecar 通知与 UIA 组件
│   ├── bridge_server.py
│   ├── headless_multi_engine.py
│   ├── notify_inbox.py
│   ├── agy_model_guard.py
│   ├── codex_projects.py
│   ├── BridgeFastUia.cs
│   ├── uia_driver.ps1
│   └── uia_daemon.ps1
├── skills/
│   ├── antigravity/codex-antigravity-bridge/  # 安装至 ~/.gemini/config/skills/codex-antigravity-bridge/
│   └── codex/codex-antigravity-bridge/        # 安装至 ~/.codex/skills/codex-antigravity-bridge/
└── tests/                      # 隔离自动化测试套件
```

## 许可证

MIT © [Idris173](https://github.com/Idris173)
