---
name: codex-antigravity-bridge
description: >-
  Exchange messages, tasks, and results between Antigravity and Codex when the user requests cross-app collaboration.
---

# Codex-Antigravity-Bridge — Antigravity

用户明确要求时，通过本地 `chatgpt-bridge` MCP 与指定 Codex 会话交换任务和结果。用自然语言向另一端说明目标、限制和验收条件，把会话编号和请求编号留给工具调用。

---

## 0. 绝对门禁铁律（Pre-flight Hard Gates，缺一不可）

在向 ChatGPT 发送任何实质开发指令前，**必须同时满足以下三项条件**，缺任何一项必须立即停下向制作人确认：

1. **门禁 1（显式指挥意图）**：制作人在当前对话中明确下达了让你指挥/调度 ChatGPT（或 Codex）的指令。
2. **门禁 2（应用存活确认）**：调用 `bridge_inspect_app` 确认本机 `ChatGPT` 桌面窗口可访问。最小化窗口可绑定，驱动会在后台恢复窗口布局。若返回 `APP_NOT_RUNNING`，请制作人打开应用。
3. **门禁 3（目标会话与需求锁定）**：必须明确你要指挥哪一个具体会话/工程，并与制作人对齐微缩任务契约（Mini-Spec）及运行模式（`semi_auto` 半自动把关 vs `autopilot` 目标托管）。
4. **语言自适应跟随（Language Matching Rule）**：严格检测并跟随制作人（用户）当前提问所使用的语言——**如果用户用中文提问，则你与用户的交互、生成的微缩 Spec、派发给 Codex 的任务指令以及审查总结全程使用简体中文；如果用户用英文提问，则全程使用英文**（保留代码、标识符、命令与原始报错信息原文）。

---

### 制定计划前确认双边模型选型

1. 调用 `bridge_routing_settings(action="get")` 读取 `ask_model_roles_on_plan`、已保存选型、双方模型目录和 Codex 当前默认值。可用 `swarm(action="models", all=true)` 补充反重力侧实时可用性与配额。
2. 开关为 `true` 时，先展示两侧可用模型、各自推理强度及速度，由制作人分别确认决策和施工的宿主、模型、强度与速度。Codex 有标准、快速和模型支持时的超快档，GPT-6.2-Sol 提供 **超快（Ultrafast）**。
3. 调用 `bridge_routing_settings(action="set")` 保存确认结果。开关为 `false` 时沿用已保存选型。派发时选型会进入 `[ROUTING: ...]`，模型和速度由对应客户端配置。

## 1. 极速连接与会话复用规约（Fast-Track Connection SOP）

首次调用懒加载 MCP 工具前，按需读取 `<appDataDir>/mcp/chatgpt-bridge/*.json` Schema：

### 路径 A：制作人未指明具体会话时（先探后锁）
1. 调用 `bridge_inspect_app` 获取当前打开会话与侧边栏工程/会话列表；
2. 向制作人展示候选会话 + **3~5 行微缩 Spec** + 确认模式（`半自动把关` / `目标托管`）；
3. 制作人确认后，调用 `bridge_bind_session` 完成锁定并记录返回的 `binding_id`。

### 路径 B：制作人已明确指定「在某工程新建会话」、「切到某已有会话」或「多会话/双端最小化并发」时（一步快连）
- 与制作人确认需求无误后，直接调用一次 `bridge_bind_session`：
  - **多会话并行 / 双端最小化无感模式（推荐本地工程使用）**：传 `workspace_path="<本地工程绝对路径>", transport="codex_exec"`（可选传 `codex_model`、`codex_effort`、`codex_speed`，默认自动继承 `bridge_routing_settings` 保存的施工选型），直接通过内置无头 `codex.exe exec / resume` 运行，即使反重力与 ChatGPT 桌面端全部最小化、或多工程同时施工也零冲突；
  - 新建窗口工程会话传：`create_in_project="<工程名>"`；
  - 切换窗口已有会话传：`switch_to_session="<会话名>"`；
- 一步原子完成加锁并记录专属 `binding_id`，随后携带该 `binding_id` 调用 `bridge_dispatch_task`。

- 若制作人明确授权只读任务回传一次结果，通过无头绑定派发，传入 `repository_read_only=true` 和 `authorize_receipt=true`。仓库使用只读沙箱，Codex 只能用派发专用的回执工具写入该请求。
- 用 `bridge_dispatch_status(dispatch_id=...)` 查询派发、执行完成、回执入箱和批复状态。读到并核对结果后，以同一 `request_id` 调用 `bridge_reply_to_codex`，记录收件确认。

### 路径 C：同一任务多轮迭代期间（零重复探测直发）
- 一旦已通过 `bridge_bind_session` 加锁，后续连续派发子步骤或打回返工时，**严禁重复调用 `bridge_inspect_app` 或 `bridge_bind_session`**，直接携带 `binding_id` 调用 `bridge_dispatch_task`。
- 无头模式可用 `wait_for_result=false` 取得派发编号，然后用 `bridge_dispatch_status` 读取同一任务的结果。同一绑定仍在运行时等待该任务结束再发下一轮。
- 复用同一目录可在 `bridge_bind_session` 传 `reuse_workspace_binding=true`。若用户要求将会话归入 Codex 桌面项目，可传 `ensure_codex_project=true`，并核对项目和会话挂接状态。
- 通过 `bridge_codex_session_model` 查询或设置目标无头 Codex 会话下一轮的模型。派发后查看本轮请求和宿主提供的模型证据，运行中的轮次继续使用其原选型。

### 路径 D：接收与批复 Codex 侧反向 MCP 提交（零 `computer-use` 全双工总线）
- Codex 侧已注册反向 MCP 服务 `antigravity_bridge`，可通过 `agy_submit_for_review` 向本地共享总线投递待审代码或请示。
- 调用 `bridge_check_codex_inbox` 查看收件箱，可传 `workspace_path` 按工程筛选。收到已配对请求时，在当前会话调用 `bridge_claim_codex_request` 领取正文，审查后将同一 `request_id` 连同结论和下一步指令传给 `bridge_reply_to_codex`。等待宿主钩子自动完成会话身份与模型校验，**切勿自行填写 `model_proof`**。
- 处理原生项目配对请求时，先独立确定目标项目名称、真实根目录与原生项目 UUID，再查宿主返回的会话归属。对带 `target_project_id` 和 `target_conversation_id` 的请求，只有当前会话的宿主元数据同时吻合这两个 ID 和工程根目录才处理，并用同一 `request_id` 回执。无法取得归属证据或发现不符时停止处理并报告原因。
- **常规桌面路径**使用目标原生项目内的桌面会话。Windows 实测 `agy --project=<project_uuid>` 新建的 CLI 会话位于独立的 `antigravity-cli` 存储，桌面 Sidecar 无法读取。未启用实验性工具时，请用户在对应原生项目中手动新开桌面会话并发送首条消息。项目尚不存在时，请用户先用对应名称和同一根目录创建项目。保留原会话，不使用 UI 驱动或额外 Sidecar 项目代替。[Antigravity 官方项目文档](https://antigravity.google/docs/projects#opening-a-session-in-a-specific-project)
- **实验性桌面创建与唤醒**默认关闭，使用本机 Antigravity 未公开的桌面 RPC。用户明确授权后，Codex 可用 `agy_provision_desktop_workspace` 创建或复用原生项目及会话，用 `session_key` 在同一项目内新开会话，并在配对核验后提交任务。Antigravity 2.34.0 已通过隔离实测。
- 已更新的 Sidecar 可在 Codex 提交时授权的 `auto_wake_blank=true` 下唤醒已配对的空白会话。唤醒结果未确认时，先核对原会话和原请求再决定是否补唤醒。会话启动后，仍通过 `bridge_claim_codex_request` 领取，并用同一 `request_id` 回写。用 `bridge_inspect_app(workspace_path=...)` 只读检查 Sidecar、钩子和配对。
- 宿主钩子会核验领取和回执的目标会话。若请求指定了模型，钩子还会核验实际调用的模型。核验失败时停止处理并报告原因。

### 接收已有桌面会话的切模请求

1. 通过 `bridge_claim_codex_request` 领取发给当前配对会话的切模请求。核对用户明确授权和准确模型 ID。必要时用内置 `swarm(action="models", all=true)` 检查模型可用性。遇到目标不符或模型不可用时报告原因。
2. 用 `swarm(action="status")` 检查当前会话。尚无 Swarm 时，用 `swarm(action="title", title="Model selection")` 创建看板并初始化当前会话的协调者，无需调用 `add`。已有 Swarm 时，只有确认当前会话是该协调者才能继续。使用 `swarm(action="model", name="coordinator", model=...)` 切换自身，按原 `request_id` 用 `bridge_reply_to_codex` 回告工具的实际结果。
3. 切模回执只记录工具结果。等 Codex 发来单独的指定模型任务，再经 `bridge_claim_codex_request` 由宿主检查实际模型。
- 用 `bridge_server.py cli wait_inbox --timeout 600 --only-new` 等待监听开始后的新提交。脚本位于 `~/.gemini/config/sidecars/chatgpt-bridge/bridge_server.py`。

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
- 普通窗口导入指引：[references/codex_skill_setup.md](./references/codex_skill_setup.md)
