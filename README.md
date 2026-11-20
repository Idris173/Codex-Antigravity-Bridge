# Codex-Antigravity-Bridge

English | [简体中文](./README.zh-CN.md)

A general-purpose local MCP communication bridge between **OpenAI Codex** (ChatGPT Desktop App / Codex CLI) and **Google Antigravity** on Windows.

Repository: [https://github.com/Idris173/Codex-Antigravity-Bridge](https://github.com/Idris173/Codex-Antigravity-Bridge)

---

## Overview

`Codex-Antigravity-Bridge` connects the Antigravity MCP server (`chatgpt-bridge`) and the Codex MCP server (`antigravity_bridge`) through a local Python sidecar and shared inbox.

- **On-Demand & Dormant by Default**: Installed skills stay completely silent during normal chats and activate only when you explicitly ask Antigravity and Codex to collaborate or exchange messages.
- **Dual Transport (Headless Concurrent CLI + Fast UIA)**:
  - **Headless Multi-Session (`codex_exec`)**: Drives the bundled `codex.exe exec / resume` CLI in the background with isolated `binding_id` records—supporting multiple workspaces in parallel even when both desktop apps are minimized, with zero focus changes or clipboard writes.
  - **Desktop Window (`uia`)** uses Windows UI Automation to send requests to a Codex window and reads the local session log to match results by `dispatch_id`.
- **Codex → Antigravity** sends requests and results to the local inbox through MCP. Antigravity replies using the same `request_id`.
- **Language Auto-Matching**: English prompts keep the entire coordination loop in English; Simplified Chinese prompts keep the entire loop in Simplified Chinese.
- **Model, Effort & Speed Routing**: Query available models, reasoning effort levels, and speed tiers (`Standard`, `Fast`, and `Ultrafast` where supported, such as `GPT-6.2-Sol`) across both hosts via `bridge_routing_settings` / `agy_routing_settings`.

---

## Requirements

- **Windows 10 / 11**
- **Python 3.11+** (standard library only; zero third-party packages)
- **Google Antigravity**
- **OpenAI ChatGPT Desktop App** (with Codex enabled)

---

## Installation & Health Check

Running `install.py` from either Antigravity or ChatGPT/Codex produces the exact same installation state (see [AGENTS.md](./AGENTS.md)):

```powershell
git clone https://github.com/Idris173/Codex-Antigravity-Bridge.git
cd Codex-Antigravity-Bridge
python install.py
python install.py --doctor
```

What `install.py` configures non-destructively:

1. Copies the bridge runtime (`bridge_server.py`, `headless_multi_engine.py`, `notify_inbox.py`, `BridgeFastUia.cs`, `uia_driver.ps1`, `uia_daemon.ps1`) to `~/.gemini/config/sidecars/chatgpt-bridge/` and precompiles `BridgeFastUia.dll`.
2. Installs the on-demand skills to `~/.gemini/config/skills/codex-antigravity-bridge/` and `~/.codex/skills/codex-antigravity-bridge/`.
3. Creates timestamped `.bak` backups before non-destructively merging `chatgpt-bridge` into `~/.gemini/config/mcp_config.json` and `[mcp_servers.antigravity_bridge]` into `~/.codex/config.toml` (preserving all existing MCP servers).

> **First-Install Note**: If the ChatGPT Desktop App is already running during first install, **restart the ChatGPT Desktop App once** so Codex loads the `antigravity_bridge` MCP server.

---

## Bidirectional Usage

### 1. Antigravity → Codex (Bind, Dispatch & Collect)

#### Headless Multi-Session Mode (Recommended for Local Workspaces)

Runs concurrently across multiple workspaces and works even when both Antigravity and ChatGPT Desktop are minimized:

```json
// 1) Bind a workspace session (Tool: bridge_bind_session)
{
  "workspace_path": "C:/path/to/workspace",
  "transport": "codex_exec",
  "codex_model": "gpt-6.2-sol",
  "codex_effort": "high",
  "codex_speed": "ultrafast",
  "engine": "general",
  "mode": "autopilot"
}

// 2) Dispatch a task using the returned binding_id (Tool: bridge_dispatch_task)
{
  "binding_id": "<binding_id>",
  "step_goal": "Summarize workspace setup",
  "task_instruction": "Read README.md and list the installation requirements.",
  "repository_read_only": true
}
```

- If a long-running task exceeds the synchronous wait window (`timed_out: true`), it continues in the background; check progress or retrieve the result with `bridge_dispatch_status(dispatch_id="<dispatch_id>")` or `bridge_read_latest(binding_id="<binding_id>")`.
- For read-only verification tasks that are authorized to write back one receipt, pass `"repository_read_only": true` and `"authorize_receipt": true` to `bridge_dispatch_task`.
- Release a session binding when finished with `bridge_unbind_session(binding_id="<binding_id>")`.

#### Desktop Window Mode

Inspect open ChatGPT windows with `bridge_inspect_app()`, bind a target project or conversation via `bridge_bind_session(create_in_project="<project>", engine="general", mode="semi_auto")` or `switch_to_session="<session>"`, and send tasks with `bridge_dispatch_task(binding_id="<binding_id>", ...)`.

---

### 2. Codex → Antigravity (Submit & Reply by `request_id`)

#### Step A — Codex Submits a Message or Change Summary

Codex calls `agy_submit_for_review` on the `antigravity_bridge` MCP server (or `agy_submit_dispatch_receipt` during an authorized read-only dispatch):

```json
// Tool: agy_submit_for_review
{
  "workspace_path": "C:/path/to/workspace",
  "step_summary": "Completed parser update and unit test coverage.",
  "changed_files": "src/parser.py, tests/test_parser.py",
  "self_check": "Ran python -B -m unittest discover -s tests -v; all tests passed.",
  "open_questions": "NONE",
  "wait_for_reply": false
}
```

- When `wait_for_reply` is `true` (default), the call blocks on the local IPC bus and returns Antigravity's reply as soon as it arrives.
- When `wait_for_reply` is `false`, the call returns `"status": "queued"` and a unique `request_id`. Give that ID to Codex to look up the reply later.

#### Step B — Antigravity Checks the Inbox & Replies by `request_id`

From Antigravity, read pending submissions (optionally scoped by `workspace_path` or `request_id`) and write the response back to the **same `request_id`** via `chatgpt-bridge` MCP tools:

```json
// 1) Inspect inbox (Tool: bridge_check_codex_inbox)
{
  "request_id": "req_1700000000000_a1b2c3d4e5f6",
  "workspace_path": "C:/path/to/workspace"
}

// 2) Reply to that exact request_id (Tool: bridge_reply_to_codex)
{
  "request_id": "req_1700000000000_a1b2c3d4e5f6",
  "decision": "APPROVED",
  "review_feedback": "Verified changes and test results.",
  "next_instruction": "Proceed to the next task."
}
```

> When more than one request is pending in the inbox, passing `request_id` to `bridge_reply_to_codex` is required so replies never target the wrong submission.

You can also inspect, wait on, or reply to inbox requests from the CLI:

```powershell
# Wait only for new submissions arriving after the listener starts
python "$HOME\.gemini\config\sidecars\chatgpt-bridge\bridge_server.py" cli wait_inbox --timeout 600 --only-new

# Inspect current pending submissions
python "$HOME\.gemini\config\sidecars\chatgpt-bridge\bridge_server.py" cli check_inbox

# Reply to a specific request_id
python "$HOME\.gemini\config\sidecars\chatgpt-bridge\bridge_server.py" cli reply_codex `
  --request-id req_1700000000000_a1b2c3d4e5f6 `
  --decision APPROVED `
  --feedback "Verified changes and test results." `
  --next "Proceed to the next task."
```

---

### 3. Model, Effort & Speed Routing

Call `bridge_routing_settings` in Antigravity or `agy_routing_settings` in Codex to read or save model, reasoning effort, and speed preferences. Supported speed tiers depend on the model.

---

## Optional Antigravity Conversation Notifications

By default, inbox checks are on-demand or polled via `cli wait_inbox`. If you want new Codex inbox submissions from a specific workspace (and its subdirectories) to automatically wake a chosen Antigravity conversation:

1. Run `install.py` **from the target workspace directory** with `--agy-conversation-id`:

   ```powershell
   cd C:\path\to\target-workspace
   python "C:\path\to\Codex-Antigravity-Bridge\install.py" --agy-conversation-id "CONVERSATION_ID"
   ```

2. **Requirements & Behavior**:
   - Configures `~/.gemini/config/sidecars/chatgpt-bridge/sidecar.json` and enables `sidecars.chatgpt-bridge` in `~/.gemini/config/config.json`.
   - Uses Antigravity's sidecar `agentapi send-message` (requires `agentapi` to be available on the Antigravity sidecar `PATH`) to notify `<conversation-id>` when a new eligible `request_id` arrives for that workspace.
   - The notified Antigravity conversation reads the submission via `bridge_check_codex_inbox` and replies with `bridge_reply_to_codex` under its existing permissions.
   - Restart Antigravity if the sidecar does not start immediately after the configuration change.
   - To pause automatic notifications, set `sidecars.chatgpt-bridge.enabled` to `false` in `~/.gemini/config/config.json`.

---

## Uninstall & Testing

To remove the suite while preserving unrelated MCP servers, user-added files, and saved routing preferences:

```powershell
python install.py --uninstall
```

*(If `BridgeFastUia.dll` is currently loaded by a running host process, close the host and rerun `python install.py --uninstall` to complete cleanup.)*

To run the isolated test suite (uses temporary `--home-dir` directories without touching host configuration):

```powershell
python -B -m unittest discover -s tests -v
```

---

## Repository Layout

```text
Codex-Antigravity-Bridge/
├── AGENTS.md                   # Deterministic setup guide for both Antigravity & Codex
├── README.md                   # English documentation
├── README.zh-CN.md             # Simplified Chinese documentation
├── install.py                  # Standard-library installer / --doctor / --uninstall
├── bridge/                     # Bidirectional MCP server, headless engine, sidecar notifier & UIA
│   ├── bridge_server.py
│   ├── headless_multi_engine.py
│   ├── notify_inbox.py
│   ├── BridgeFastUia.cs
│   ├── uia_driver.ps1
│   └── uia_daemon.ps1
├── skills/
│   ├── antigravity/codex-antigravity-bridge/  # Installed to ~/.gemini/config/skills/codex-antigravity-bridge/
│   └── codex/codex-antigravity-bridge/        # Installed to ~/.codex/skills/codex-antigravity-bridge/
└── tests/                      # Isolated automated test suite
```

## License

MIT © [Idris173](https://github.com/Idris173)
