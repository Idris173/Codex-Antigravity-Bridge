# ChatGPT 侧配套技能（`director-worker`）挂载说明

安装脚本 `install.py` 已自动将执行端配套技能部署至本机标准 Codex 技能目录：
- `~/.codex/skills/director-worker/SKILL.md`

## 两种窗口形态的生效说明

1. **ChatGPT 桌面应用内的本地 Codex 会话（推荐）**  
   自动扫描并识别 `~/.codex/skills/director-worker/SKILL.md` 与 `~/.codex/config.toml` 中的 `antigravity_bridge` MCP 服务（首次安装后重启一次 ChatGPT 桌面端即可生效）。

2. **纯云端普通 ChatGPT 聊天窗口**  
   - **保底机制（零配置可跑）**：`chatgpt-bridge` 中转接口每次发送 `[DIRECTOR_TASK]` 时已自动内嵌四段式回传提示。
   - **完整挂载方式**：在 ChatGPT 桌面应用侧边栏 **Skills** 面板或专属 **Project Instructions** 中粘贴 `~/.codex/skills/director-worker/SKILL.md` 的内容即可。
