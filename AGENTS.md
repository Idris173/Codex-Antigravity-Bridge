# AGENTS.md — Dual-Agent Deterministic Installation Contract

> **For Antigravity AND OpenAI ChatGPT / Codex Agents:**  
> When a user opens or clones this repository and asks you to **install**, **set up**, **verify**, or **uninstall** `codex-director`, you MUST follow the deterministic procedure below so that running it from **Antigravity** or from **ChatGPT / Codex** produces the exact same byte-identical installation state.

## 1. Standard One-Command Installation (Windows)

Do **NOT** manually copy files to guessed paths or hardcode usernames/Python paths. Always run the idempotent standard-library installer from the repository root:

```powershell
python install.py
```

Then immediately run the built-in health check to verify both sides:

```powershell
python install.py --doctor
```

## 2. What `install.py` Deterministically Configures

Regardless of which AI agent invokes `install.py`, it performs the exact same non-destructive steps on the current user's Windows machine:

1. **Deploys Shared Bidirectional Bridge Engine** to `~/.gemini/config/sidecars/chatgpt-bridge/` (`bridge_server.py`, `BridgeFastUia.cs`, `uia_driver.ps1`, `uia_daemon.ps1`) and precompiles `BridgeFastUia.dll` using the local `.NET` toolchain.
2. **Installs Antigravity Director Skill** to `~/.gemini/config/skills/codex-director/` (`SKILL.md` + Unreal Engine & Three.js review checklists).
3. **Installs Codex Worker Skill** to `~/.codex/skills/director-worker/SKILL.md`.
4. **Merges Both MCP Configurations Non-Destructively** (creating timestamped `.bak` backups before modifying existing files and preserving all existing MCP servers):
   - Registers `chatgpt-bridge` in `~/.gemini/config/mcp_config.json` (using the detected absolute path of the active Python 3.11+ interpreter).
   - Registers `[mcp_servers.antigravity_bridge]` (`--role codex`) in `~/.codex/config.toml` and validates the resulting TOML via `tomllib.loads()`.

## 3. Post-Install Handback & Language Matching Rule

- **Strict Language Matching**: Detect the language of the user's prompt. **If the user asks in English, reply strictly in English throughout; if the user asks in Simplified Chinese (中文), reply strictly in Simplified Chinese throughout.**
- After `python install.py --doctor` reports `"ok": true`:
1. Inform the user that both **Antigravity** (`codex-director` + `chatgpt-bridge`) and **ChatGPT/Codex** (`director-worker` + `antigravity_bridge`) are installed identically.
2. Remind the user to **restart the ChatGPT Desktop App once** (if currently running) so Codex loads the newly registered `antigravity_bridge` MCP tools natively.
3. Explain that both skills stay **dormant during normal chats** and only activate when the user explicitly asks Antigravity to direct a ChatGPT/Codex session (or asks Codex to submit work to Antigravity).
