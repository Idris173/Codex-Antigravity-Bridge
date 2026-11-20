# AGENTS.md — Dual-Agent Deterministic Installation Contract

> **For Antigravity AND OpenAI ChatGPT / Codex Agents:**  
> When a user asks to install, set up, verify, or uninstall `Codex-Antigravity-Bridge`, use the repository installer. Use the same interpreter and, on macOS, the same terminal environment when repeating an installation.

## 1. Installation and health check

Run the idempotent installer and health check from the repository root.

### Windows

```powershell
python install.py
python install.py --doctor
```

### macOS headless preview

Install and sign in to the [official Codex CLI](https://learn.chatgpt.com/docs/cli). Use the terminal where `codex --version` succeeds.

```sh
python3 install.py
python3 install.py --doctor
```

## 2. What `install.py` Deterministically Configures

The installer performs the following steps for the current user.

1. **Deploys the shared bridge** to `~/.gemini/config/sidecars/chatgpt-bridge/`. Windows also gets the UI Automation files and compiled DLL. On macOS the installer records the Codex CLI path and terminal `PATH` for the Antigravity MCP process.
2. **Installs the Antigravity skill** to `~/.gemini/config/skills/codex-antigravity-bridge/` (`SKILL.md` + Unreal Engine & Three.js review checklists).
3. **Installs the Codex skill** to `~/.codex/skills/codex-antigravity-bridge/SKILL.md`.
4. **Merges Both MCP Configurations Non-Destructively** (creating timestamped `.bak` backups before modifying existing files and preserving all existing MCP servers):
   - Registers `chatgpt-bridge` in `~/.gemini/config/mcp_config.json` (using the detected absolute path of the active Python 3.11+ interpreter).
   - Registers `[mcp_servers.antigravity_bridge]` (`--role codex`) in `~/.codex/config.toml` and validates the resulting TOML via `tomllib.loads()`.

## 3. Post-Install Handback & Language Matching Rule

- **Strict Language Matching**: Detect the language of the user's prompt. **If the user asks in English, reply strictly in English throughout; if the user asks in Simplified Chinese (中文), reply strictly in Simplified Chinese throughout.**
- After `--doctor` reports `"ok": true`:
1. Inform the user that both **Antigravity** (`codex-antigravity-bridge` + `chatgpt-bridge`) and **ChatGPT/Codex** (`codex-antigravity-bridge` + `antigravity_bridge`) are installed identically.
2. Remind the user to **restart the ChatGPT Desktop App once** (if currently running) so Codex loads the newly registered `antigravity_bridge` MCP tools natively.
3. Explain that both skills stay **dormant during normal chats** and only activate when the user explicitly asks Antigravity to direct a ChatGPT/Codex session (or asks Codex to submit work to Antigravity).

## 4. Optional Antigravity conversation notifications

From the workspace to connect, run `python <bridge-repo>/install.py --agy-conversation-id <conversation-id>`. This enables the Antigravity App sidecar for new Codex messages from that workspace and its subdirectories. It uses Antigravity's `agentapi send-message` to notify the selected conversation. The conversation reads the request and replies through MCP under its existing permissions. Restart Antigravity if the sidecar does not start after the configuration change. To pause notifications, set `sidecars.chatgpt-bridge.enabled` to `false` in `~/.gemini/config/config.json`.

Use `python3` for this command on macOS.

The optional installation also adds the bridge model verification hook to the existing Antigravity hook configuration. `--doctor` verifies the installed hook configuration. The hook checks required models on guarded requests at the time they are claimed or answered.
