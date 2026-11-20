---
name: director-worker
description: >-
  Executes structured tasks when the first non-empty line is [DIRECTOR_TASK], or when the user explicitly requests submission to Antigravity for review.
---

# Director Worker Protocol (`director-worker`)

## 1. Activation Guard (严格按需触发)

- 仅当消息的**首个非空行**是 `[DIRECTOR_TASK]`，或用户明确要求向 Antigravity（反重力）提交评审时启用本技能。
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

- 调用 `agy_submit_for_review` 投递评审材料或请示（无需阻塞等待时设 `wait_for_reply=false` 并保留返回的 `request_id`）。
- 调用 `agy_fetch_director_reply` 按 `request_id` 查询 Antigravity 的评审结论（`APPROVED` / `REWORK_REQUIRED` / `ESCALATE_TO_PRODUCER`）与下一步指令。

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
