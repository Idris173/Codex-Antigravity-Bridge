# 游戏开发双引擎代码审查清单（Unreal Engine & Three.js）

在 `codex-antigravity-bridge` 工作流中，每次收到 ChatGPT/Codex 执行端返回的 `[FILES]` 与 `[SELF_CHECK]` 后，按当前绑定引擎逐项核查以下红线。若发现违规，生成具体、可定位到行/函数的修改建议打回返工。

---

## 一、 Unreal Engine (UE5 C++ / 蓝图协同) 审查红线

### 1. 资产与序列化兼容（最高优先级：防坏蓝图/地图）
- **禁止静默重命名或删除已有反射成员**：检查是否修改了已有 `UPROPERTY`、`UFUNCTION`、`USTRUCT`、`UENUM` 的名称或底层类型。此类修改会直接导致派生 Blueprint、关卡实例或 `DataAsset` 丢失配置数据。
- **CoreRedirects 兜底**：若需求明确要求重命名已序列化字段/类，必须同步提供 `Config/DefaultEngine.ini` 中的 `[CoreRedirects]` 条目，并在向制作人汇报时醒目提示重新保存相关资产。

### 2. 编译规范与头文件卫生（防 UHT / UBT 低级报错）
- **`.generated.h` 位置铁律**：在任何包含 `UCLASS` / `USTRUCT` / `UENUM` 的 `.h` 头文件中，`#include "XXX.generated.h"` **必须是最后一个 `#include`**，否则 Unreal Header Tool (UHT) 必报错。
- **前向声明优先**：`.h` 中尽量使用前向声明（`class UCameraComponent;`），将具体头文件 `#include` 放入 `.cpp`，缩短编译耗时并防循环依赖。
- **`*.Build.cs` 模块同步**：引入跨模块类型时（如 `EnhancedInput`、`UMG`、`Niagara`、`GameplayTags`、`GameplayAbilities`、`AIModule`、`NavigationSystem`），核查是否说明或同步补充了 `Build.cs` 依赖模块。

### 3. GC 内存安全与异步生命周期（防崩溃）
- **反射指针托管**：所有持有 `UObject` 派生对象的类成员变量必须加 `UPROPERTY()`（UE5 规范优先使用 `TObjectPtr<UXxx>`），严禁裸指针无保护持有导致被 GC 回收后悬空。
- **异步/延时安全**：`FTimerManager::SetTimer`、异步资源加载（`FStreamableManager`）、委托 `BindLambda` 中，严禁直接裸捕获 `this` 后解引用；必须使用 `TWeakObjectPtr<ThisClass>` 并在回调入口检查 `if (!WeakThis.IsValid()) return;`。

### 4. 帧率与运行时性能卫生
- **Tick 节制**：默认保持 `PrimaryActorTick.bCanEverTick = false`。除非涉及逐帧插值、相机跟随或连续物理检测，一律建议改用事件委托或固定间隔 Timer。
- **热路径禁忌**：严禁在 `Tick` 循环中调用 `UGameplayStatics::GetAllActorsOfClass`、高频同步 `LoadObject`、复杂未缓存的字符串拼接或大量临时 `TArray` 重分配。

---

## 二、 Three.js 玩法原型审查红线

### 1. 手感可调性与帧率无关性（核心玩法体验）
- **帧率无关运动/阻尼（防 60Hz vs 144Hz 手感不一致）**：位移、旋转、相机平滑跟随必须乘 `deltaTime`（并做 `Math.min(dt, 0.1)` 防切屏大跳变）；平滑阻尼插值严禁写死 `lerp(target, 0.1)`，应使用帧率无关公式 `1 - Math.exp(-decay * dt)`。
- **调参集中外露**：移动速度、跳跃初速、重力倍率、镜头距离/FOV、顿帧时长等玩法手感参数，必须集中提取至顶部 `TUNING_CONFIG` 常量对象（或挂载 `lil-gui` 调试面板），方便制作人边玩边拉参数。

### 2. 渲染主循环零 GC 分配（防周期性掉帧卡顿）
- **禁止帧内 `new` 数学对象**：严查 `requestAnimationFrame` / `update(dt)` 内部每帧 `new THREE.Vector3()`、`Quaternion()`、`Matrix4()`、`Euler()`、`Color()`、`Raycaster()`。必须在模块或类顶层预分配临时复用变量（如 `_tmpVecA`, `_tmpQuat`）。

### 3. WebGL 显存与事件清理（防热重载越跑越卡）
- **显式释放 GPU 资源**：子弹/特效/敌人销毁或关卡重置时，核查是否对不再复用的非共享资源执行了 `geometry.dispose()`、`material.dispose()` 及贴图 `dispose()`（高频生成销毁物优先使用 `InstancedMesh` 或对象池）。
- **监听器成对解绑**：注册在 `window` / `document` 上的 `resize`、`keydown`、`keyup`、`pointermove` 必须具备清理入口，防止多次重启原型叠加重复输入响应。

---

## 三、 升级请示制作人（停止自动返工）的判定标准

出现以下任一情形时，**禁止自行替制作人做决定**，必须暂停自动循环并向制作人汇报：
1. `[OPEN_QUESTIONS]` 非 `NONE`，且涉及游戏核心手感、数值曲线、镜头表现或交互规则取舍；
2. 方案需要修改现有 Unreal 蓝图父类结构、动画蓝图接口或存档数据结构；
3. 连续返工达到上限（默认 3 轮）仍存在未收敛的技术冲突；
4. 代码已通过本审查清单（及本地编译/运行检查），进入需要制作人进引擎实机试玩手感的节点。
