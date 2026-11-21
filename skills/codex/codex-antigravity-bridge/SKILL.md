---
name: codex-antigravity-bridge
description: >-
  Exchange messages, tasks, and results between Codex and Antigravity when the user asks for cross-app collaboration.
---

# Codex-Antigravity-Bridge — Codex

## 1. Activation Guard (严格按需触发)

- 用户明确请求跨端协作时使用本技能。桥接的无头任务用自然语言传达，结果也用任务所用的语言回复。旧版封包和桌面模式继续使用原有约定。
- 仓库只读派发可在封包前带一行宿主授权指令。封包标记 [RECEIPT_AUTHORIZATION: once] 时，通过当前派发专用的 agy_submit_dispatch_receipt 回传一次结果，并在最终回复中带上返回的 request_id。使用仓库只读沙箱，遵循派发范围。普通派发可通过 agy_submit_for_review 传入封包的 dispatch_id 来关联回执。
- 普通对话按常规流程处理。

### 制定计划前确认双边模型选型

- 使用 `agy_routing_settings(action="get")` 读取 `ask_model_roles_on_plan`、已保存的选型、双侧目录以及 Codex 当前默认值。收到已有 `[ROUTING: ...]` 的施工任务时沿用该任务选型。仅提交评审时按提交规约执行。
- 开始制定新的双边计划且开关为 `true` 时，先展示两侧可用模型、各自推理强度和速度，由用户分别确认决策和施工的宿主、模型、强度与速度。Codex 速度有标准、快速和模型支持时的超快，GPT-6.2-Sol 提供 **超快（Ultrafast）**。Antigravity 可用时以 `swarm(action="models", all=true)` 补充实时配额。
- 用 `agy_routing_settings(action="set")` 保存用户确认的选型。开关为 `false` 时沿用已保存选型。选型会进入任务封包，模型和速度由对应客户端配置。

---

## 2. Fast-Turnaround Rules (极速执行与低延迟检索守则)

在大型游戏或引擎源码仓库（如 Unreal Engine 源码或大型项目工程）工作时：
1. **禁止无边界全盘盲搜**：严禁在大型仓库根目录执行无路径限制的全量递归扫描。优先使用 `[LOCAL_CONTEXT]` 提供的路径与接口上下文，或限定到具体 `Source/<ModuleName>/`、`Plugins/<PluginName>/` 子目录精准定位。
2. **轻量指令秒级直回**：若 `[STEP_GOAL]` 或 `[CONSTRAINTS]` 标明为连通性握手、架构对齐或只读审查，禁止执行无关环境探测或编译流程，直接给出精炼结论。
3. **单步聚焦不越界**：严格只修改 `[INSTRUCTION]` 与 `[CONSTRAINTS]` 授权范围内的文件。

---

## 3. Execution & Engine Redlines (双引擎执行红线)

### A. 当 `[ENGINE: UNREAL]` 时
1. **蓝图与资产序列化保护（最高红线）**：严禁擅自重命名、删除或改变已有 `UPROPERTY`、`UFUNCTION`、`USTRUCT`、`UENUM` 的标识符与类型（防 Blueprint / Level / `DataAsset` 丢引用；若需求强制要求重命名，必须同步提供 `DefaultEngine.ini` 的 `[CoreRedirects]` 配置并在 `[SELF_CHECK]` 醒目说明）。
2. **头文件与 GC 生命周期安全**：`#include "XXX.generated.h"` 必须是头文件最后一个 `#include`；所有持有 `UObject` 派生类的成员指针必须声明 `UPROPERTY()`（UE5+ 优先 `TObjectPtr<T>`）；在 `FTimerManager`、异步加载、Lambda 中引用 `UObject` 必须使用 `TWeakObjectPtr` 并执行 `IsValid()` 检查。
3. **Tick 管控与构建依赖**：非逐帧表现/物理逻辑默认保持 `PrimaryActorTick.bCanEverTick = false`；引入新模块头文件时同步核查 `*.Build.cs`。

### B. 当 `[ENGINE: THREEJS]` 时
1. **快速原型与帧率无关手感**：位移与相机阻尼必须基于 `deltaTime`（平滑插值采用 `1 - Math.exp(-decay * dt)`）；将速度、阻尼、顿帧等手感参数集中收敛到顶层 `TUNING_CONFIG`（或 `lil-gui`）。
2. **渲染帧循环零 GC 分配与显存释放**：严禁在 `requestAnimationFrame` 主循环内每帧 `new THREE.Vector3()`、`Matrix4()`、`Quaternion()`、`Raycaster()`；销毁实体或重置关卡时确保调用 `geometry.dispose()`、`material.dispose()` 并解绑事件监听器。

---

## 4. Codex → Antigravity 反向通信（严禁 `computer-use`）

当需要主动向 Antigravity（反重力）发指令、提交评审或汇报结果时，**必须使用 `antigravity_bridge` MCP 工具**，严禁使用 `computer-use` 截图、移动鼠标或模拟键盘输入：

- 把用户的自然语言意图传给 `agy_submit_for_review`，设 `wait_for_reply=false`，把返回的 `request_id` 留给工具调用。用 `agy_fetch_director_reply` 询问同一请求的投递状态与结果。
- 当用户指定 Antigravity 工程时，先确定 Codex 工程的真实根目录和名称，再找到同一根目录、对应名称的 Antigravity 原生项目。从该项目独立取得预期项目 UUID。候选会话返回的项目 ID 只用于比对，不能反填成预期 ID。
- 桌面桥接使用目标原生项目里的桌面会话。若没有可用路由，优先复用用户指定的会话。实验性创建工具不可用时，请用户在同一目录对应的原生项目中新开桌面会话、发送首条消息并提供会话 ID。CLI 会话和额外 Sidecar 项目无法代替目标原生会话。 [Antigravity 项目文档](https://antigravity.google/docs/projects#opening-a-session-in-a-specific-project)
- 用 `agy_workspace_pair(action="link", workspace_path=..., project_id=..., conversation_id=...)` 登记会话。`get` 显示 `route.status="confirmed"` 且项目与会话 ID 均符合预期后，才可提交该项目的请求。旧配对若只校验过目录，须重新 `link`。
- 用同一 `request_id` 核对目标会话回执中的项目和会话 ID。宿主钩子会在领取正文和写回回执时核验会话身份。
- 用户指定必须使用的 Antigravity 模型时，将 `required_model` 和已确认的 `target_conversation_id` 作为工具参数。路由偏好不代表实际执行。只有宿主确认后任务正文才可领取。发现模型不符时保留等待状态并汇报，用户要求取消时调用 `agy_cancel_guarded_review`。

### 实验性创建和唤醒原生桌面会话

仅在用户授权创建或启动原生桌面会话，且 MCP 工具列表暴露相应工具时使用。原型默认关闭，依赖已实测的本机 Antigravity 桌面内部接口。开发试验可为单独运行的桥接进程设置 `CODEX_AGY_EXPERIMENTAL_HUB=hub_<HTTP端口>.json`，使用当前本机宿主的运行时文件名。

1. 调用 `agy_provision_desktop_workspace(workspace_path, project_name)`。要在同一项目下新开会话时传入不同的 `session_key`。原样重试使用相同的 key。保留工具回读核验后的 `project_id` 与 `conversation_id`。
2. 用这两个 ID 调用 `agy_workspace_pair(action="link", ...)`，等待 `get` 确认路由。在为同一目录切换路由前，确认上一单已领取或已有回执。调用 `agy_submit_for_review` 并记录 `request_id`。
3. 若空白会话停留在 `notified` 且未开始领取，调用 `agy_wake_desktop_workspace(workspace_path, project_id, conversation_id, request_id)` 唤醒同一会话和同一单。指定模型时，唤醒的 `model_id` 使用宿主目录中的准确档位，并与提交的 `required_model` 一致，例如 `gemini-4-carbon-medium`。
4. 用 `agy_fetch_director_reply` 追踪领取与回写。收到回执后核对请求、项目和会话 ID。指定模型时还要核对 `execution_model` 与 `reply_model`。唤醒返回 `sent` 只确认宿主接收消息。

### 切换 Antigravity 已有桌面会话的模型

1. 用户明确要求切换会话模型时，用 `agy_workspace_pair(action="get")` 确认工作区路由指向用户指定的会话。用 `agy_submit_for_review` 发送一条普通切模请求，在正文写明目标会话 ID、准确模型 ID 及用户授权。该请求只做切模，省略 `required_model` 和 `target_conversation_id` 工具参数，让当前模型能先调用宿主工具。
2. 目标会话用内置 `swarm(action="status")` 检查协调者。普通会话尚无 Swarm 时，用 `swarm(action="title", title="Model selection")` 初始化当前会话的协调者记录，再用 `swarm(action="model", name="coordinator", model=...)` 自切。已有协调者时复用。该流程会为首次切换的会话创建 Swarm 看板，无需添加成员。
3. 用 `agy_fetch_director_reply` 等待切模回执。成功后再向同一会话发送带 `required_model` 和 `target_conversation_id` 的任务或只读验证。用回执的 `execution_model` 和 `reply_model` 确认实际模型。切换失败或宿主仍报告不符时，保留任务的模型要求并汇报实际原因。

---

## 5. Language Matching & Mandatory Response Format (语言自适应与强制四段式协议)

- **语言严格跟随（Language Matching）**：
  - 若收到的指令包含 `[LANG: zh-CN]` 或指令正文为中文，回复正文**全程使用简体中文**；
  - 若收到的指令包含 `[LANG: en]` 或指令正文为英文，回复正文**全程使用英文（English）**；
  - 四个顶级协议标签名（`[SUMMARY]`、`[FILES]`、`[SELF_CHECK]`、`[OPEN_QUESTIONS]`）及空问题标记 `NONE` 始终保持大写英文，以确保跨语言解析器 100% 兼容。

每次针对 `[DIRECTOR_TASK]` 的回复，**必须且只能**按以下四个顶级标签组织内容：

```markdown
[SUMMARY]
- 简明列出本步完成的核心改动，或针对上一轮审查意见的具体修复点（2~5 条）。

[FILES]
### `相对路径/文件名.后缀`
```语言标识符
// 若已在本地 Codex 工作区直接修改文件，列出修改路径与关键变更；
// 若为无本地写权限会话，提供完整可编译落地的代码段（严禁在函数内部用省略号破坏可编译性）。
```

[SELF_CHECK]
- 资产/兼容性检查：...
- 性能/内存检查：...
- 构建/依赖提示：...

[OPEN_QUESTIONS]
（若遇到玩法手感取舍或设计歧义在此列出；若无任何阻塞疑问，必须严格只写一行：NONE）
```
