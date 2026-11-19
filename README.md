# Codex Director (`codex-director`)

**Antigravity (Tech Director) ↔ ChatGPT / Codex (Worker) Bidirectional Local MCP & Game Dev Skill Suite**  
面向游戏制作者（Unreal Engine & Three.js）的 **Antigravity（反重力技术总监）↔ ChatGPT / Codex（执行主程）** 本机双边对称 MCP 与按需技能套件。

Repository: [https://github.com/Idris173/codex-director](https://github.com/Idris173/codex-director)

---

## Quick Start (English)

### Requirements
- **Windows 10 / 11**, **Python 3.11+**, **Antigravity**, and **ChatGPT Desktop App** (with Codex mode).

### One-Command Identical Install (via Antigravity OR ChatGPT/Codex)
Clone this repo or tell **either** Antigravity or ChatGPT/Codex:
> *"Clone `https://github.com/Idris173/codex-director` and install this skill suite for me."*

Both agents automatically read `AGENTS.md` and run the deterministic installer:

```powershell
git clone https://github.com/Idris173/codex-director.git
cd codex-director
python install.py
python install.py --doctor
```

- **Language Auto-Matching**: Ask in English → both agents coordinate and reply in English throughout; ask in Chinese → both agents coordinate and reply in Chinese throughout.
- **Note**: Restart the ChatGPT Desktop App once after first install so Codex loads the native `antigravity_bridge` MCP tools. To uninstall cleanly: `python install.py --uninstall`.

---

## 快速上手（简体中文）

### 核心特性
1. **三重门禁，平时完全休眠**：日常聊天零干扰；仅在**你明确下达指挥指令 + ChatGPT 桌面端已开启 + 目标工程/会话已锁定**时激活。
2. **中英双语全链路自适应**：用中文提问则全程（微缩 Spec、派发指令、审查报告）一直使用中文；用英文提问则全程一直使用英文；同时兼容中/英文界面的 ChatGPT 桌面客户端。
3. **双边对称 MCP 极速直连（无需 `computer-use`）**：
   - **正向（Antigravity → Codex）**：预编译 C# `CacheRequest` 热管道（探测 `~260ms`）+ 本地 `.codex/sessions/rollout-*.jsonl` `35ms` 零延迟无损流截获（按 `dispatch_id` 精确归属）。
   - **反向（Codex → Antigravity）**：Codex 原生挂载 `antigravity_bridge` MCP，走本地并发安全文件总线（`<40ms` IPC），无需调用 `computer-use` 截屏或抢鼠标。
4. **Unreal Engine & Three.js 专属保护**：检测键鼠空闲间隙投递并毫秒级备份还原系统剪贴板（保护复制的 Unreal 蓝图节点），内置 UE5/UE6 序列化防丢引用与 Three.js 帧率无关/零 GC 审查红线。

### 一键对等安装（反重力或 GPT 运行结果完全一致）

无论你的朋友在 **反重力（Antigravity）** 还是在 **ChatGPT（Codex）** 中操作，只需对任意一方说：

> “克隆或打开 `https://github.com/Idris173/codex-director`，帮我安装并验证这套双边技能。”

或者直接在终端执行：

```powershell
git clone https://github.com/Idris173/codex-director.git
cd codex-director
python install.py
python install.py --doctor
```

> **首次安装提示**：安装脚本会自动备份并无损合并原有的 `~/.gemini/config/mcp_config.json` 与 `~/.codex/config.toml`（保留你已有的全部其他 MCP 服务）。若安装时 ChatGPT 桌面应用已打开，请**重启一次 ChatGPT 桌面应用**以加载原生 `antigravity_bridge` 工具。

---

## Repository Layout / 目录结构

```text
codex-director/
├── AGENTS.md                   # Deterministic setup guide for both Antigravity & Codex
├── README.md                   # Bilingual documentation (EN / zh-CN)
├── install.py                  # Zero-dependency installer / --doctor / --uninstall
├── bridge/                     # Bidirectional MCP server + C# / PowerShell fast UIA engine
│   ├── bridge_server.py
│   ├── BridgeFastUia.cs
│   ├── uia_driver.ps1
│   └── uia_daemon.ps1
├── skills/
│   ├── antigravity/codex-director/  # Installed to ~/.gemini/config/skills/codex-director/
│   └── codex/director-worker/       # Installed to ~/.codex/skills/director-worker/
└── tests/                      # Isolated automated test suite
```

## License

MIT © [Idris173](https://github.com/Idris173)
