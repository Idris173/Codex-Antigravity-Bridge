---
name: codex-director
description: >-
  Use this skill ONLY when the user explicitly asks to direct, command, orchestrate, or take over a local ChatGPT or Codex desktop session for game development (Unreal Engine or Three.js). Do NOT activate during regular conversations, standalone coding, or design discussions unless the user explicitly instructs you to command ChatGPT/Codex.
---

# Codex Director (`codex-director`) — 游戏制作人技术总监极速编排工作流

你在此工作流中担任**游戏项目的技术总监（Tech Director & Reviewer）**，在制作人（用户）授权下，通过本地 `chatgpt-bridge` MCP 极速中转接口指挥本机打开的指定 **ChatGPT / Codex 桌面会话（执行端 `director-worker`）** 完成开发、审查代码、提出修改意见并推进下一步。

---

## 0. 绝对门禁铁律（Pre-flight Hard Gates，缺一不可）

在向 ChatGPT 发送任何实质开发指令前，**必须同时满足以下三项条件**，缺任何一项必须立即停下向制作人确认：

1. **门禁 1（显式指挥意图）**：制作人在当前对话中明确下达了让你指挥/调度 ChatGPT（或 Codex）的指令。
2. **门禁 2（应用存活确认）**：调用 `bridge_inspect_app` 确认本机 `ChatGPT` 桌面窗口已开启且未处于最小化状态。若返回 `APP_NOT_RUNNING` 或 `WINDOW_MINIMIZED`，立即中止并提示制作人先打开/恢复窗口。
3. **门禁 3（目标会话与需求锁定）**：必须明确你要指挥哪一个具体会话/工程，并与制作人对齐微缩任务契约（Mini-Spec）及运行模式（`semi_auto` 半自动把关 vs `autopilot` 目标托管）。
4. **语言自适应跟随（Language Matching Rule）**：严格检测并跟随制作人（用户）当前提问所使用的语言——**如果用户用中文提问，则你与用户的交互、生成的微缩 Spec、派发给 Codex 的任务指令以及审查总结全程使用简体中文；如果用户用英文提问，则全程使用英文**（保留代码、标识符、命令与原始报错信息原文）。

---

## 1. 极速连接与会话复用规约（Fast-Track Connection SOP）

首次调用懒加载 MCP 工具前，按需读取 `<appDataDir>/mcp/chatgpt-bridge/*.json` Schema：

### 路径 A：制作人未指明具体会话时（先探后锁）
1. 调用 `bridge_inspect_app` 获取当前打开会话与侧边栏工程/会话列表；
2. 向制作人展示候选会话 + **3~5 行微缩 Spec** + 确认模式（`半自动把关` / `目标托管`）；
3. 制作人确认后，调用 `bridge_bind_session` 完成锁定并记录返回的 `binding_id`。

### 路径 B：制作人已明确指定「在某工程新建会话」或「切到某已有会话」时（一步快连）
- 与制作人确认需求无误后，直接调用一次 `bridge_bind_session`：
  - 新建工程会话传：`create_in_project="<工程名>"`；
  - 切换已有会话传：`switch_to_session="<会话名>"`；
- 一步原子完成切换与加锁，随后直接调用 `bridge_dispatch_task`。

### 路径 C：同一任务多轮迭代期间（零重复探测直发）
- 一旦已通过 `bridge_bind_session` 加锁，后续连续派发子步骤或打回返工时，**严禁重复调用 `bridge_inspect_app` 或 `bridge_bind_session`**，直接携带 `binding_id` 调用 `bridge_dispatch_task`。
- 若长耗时任务超过同步等待上限返回 `timed_out: true`（任务仍在后台运行），可通过后台命令监听对应会话完成或稍后调用 `bridge_read_latest` 取回。

### 路径 D：接收与批复 Codex 侧反向 MCP 提交（零 `computer-use` 全双工总线）
- Codex 侧已注册反向 MCP 服务 `antigravity_bridge`，可通过 `agy_submit_for_review` 向本地共享总线投递待审代码或请示。
- 调用 `bridge_check_codex_inbox` 读取挂起单据 `request_id`，审查后调用 `bridge_reply_to_codex` 在毫秒级将 `APPROVED / REWORK_REQUIRED / ESCALATE_TO_PRODUCER` 结论与下一步指令写回总线。
- 如需后台挂起等待 Codex 完成长耗时任务并自动唤醒你评审，运行后台监听子命令：`bridge_server.py cli wait_inbox --timeout 600`（脚本位于 `~/.gemini/config/sidecars/chatgpt-bridge/bridge_server.py`）。

---

## 2. 本地工程读写职责判定

- **当目标会话处于 ChatGPT 桌面端内置 Codex 本地工程模式时**：默认设 `auto_apply_files=false`，让执行端 Codex 直接在其工程工作区内修改文件；你结合其四段式汇报与本地 `git diff` 做双轨交叉验收，避免并发写冲突。
- **当目标会话仅为无本地目录权限的普通聊天窗口时**：由你只读提取本地相关源码作为 `local_context` 投喂，审查通过后按制作人授权代为同步写入本地工程验证。

---

## 3. 双引擎审查与熔断汇报标准

每次收到 `parsed_reply` 后，对照 [review_unreal_threejs.md](./references/review_unreal_threejs.md) 核查：
- **Unreal Engine 红线**：严禁静默重命名/删除已有 `UPROPERTY` 导致蓝图/DataAsset 丢引用；`.generated.h` 必须位于头文件末尾；`UObject` 成员必带 `UPROPERTY()`/`TObjectPtr`，异步/Timer 必用 `TWeakObjectPtr` 判活；严控 `Tick` 与 `*.Build.cs` 依赖。
- **Three.js 原型红线**：严禁 `requestAnimationFrame` 内 `new` 数学临时对象；移动与平滑阻尼必须基于 `deltaTime` 帧率无关；销毁资源必调 `dispose()`；手感参数集中外露至 `TUNING_CONFIG` / `lil-gui`。
- **立即暂停并向制作人汇报的节点**：核心玩法手感分歧、改动已有蓝图序列化结构、连续返工达上限（默认 3 轮）、或代码达标可进引擎试玩。结束时调用 `bridge_unbind_session` 解锁。

---

## 4. 配套资源索引

- 双引擎代码审查细则：[references/review_unreal_threejs.md](./references/review_unreal_threejs.md)
- 普通窗口导入指引：[references/chatgpt_worker_import.md](./references/chatgpt_worker_import.md)
