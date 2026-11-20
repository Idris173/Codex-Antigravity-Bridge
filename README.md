# Codex-Antigravity-Bridge

English | [简体中文](./README.zh-CN.md)

A general-purpose local MCP communication bridge between **OpenAI Codex** (ChatGPT Desktop App / Codex CLI) and **Google Antigravity**. Windows supports headless and desktop window operation. This branch adds a macOS headless preview.

Repository: [https://github.com/Idris173/Codex-Antigravity-Bridge](https://github.com/Idris173/Codex-Antigravity-Bridge)

[Prompt guide](./docs/bridge-prompts.html). Download the HTML file and open it in your browser to copy examples in English or Simplified Chinese.

---

## Overview

`Codex-Antigravity-Bridge` connects the Antigravity MCP server (`chatgpt-bridge`) and the Codex MCP server (`antigravity_bridge`) through a local Python sidecar and shared inbox.

- **On-Demand & Dormant by Default**: Installed skills stay completely silent during normal chats and activate only when you explicitly ask Antigravity and Codex to collaborate or exchange messages.
- **Dual Transport (Headless Concurrent CLI + Fast UIA)**:
  - **Headless Multi-Session (`codex_exec`)** uses `codex exec / resume` in the background. Each binding keeps its own workspace and session.
  - **Desktop Window (`uia`)** uses Windows UI Automation to send requests to a Codex window and reads the local session log to match results by `dispatch_id`.
- **Codex → Antigravity** sends requests and results to the local inbox through MCP. Antigravity replies using the same `request_id`.
- **Language Auto-Matching**: English prompts keep the entire coordination loop in English; Simplified Chinese prompts keep the entire loop in Simplified Chinese.
- **Model, Effort & Speed Routing**: Query available models, reasoning effort levels, and speed tiers (`Standard`, `Fast`, and `Ultrafast` where supported, such as `GPT-6.2-Sol`) across both hosts via `bridge_routing_settings` / `agy_routing_settings`.

---

## Requirements

- **Windows 10 / 11** or **macOS** on this preview branch
- **Python 3.11+** (standard library only; zero third-party packages)
- **Google Antigravity**
- **OpenAI Codex**. On macOS, install and sign in to the [Codex CLI](https://learn.chatgpt.com/docs/cli) and confirm that `codex --version` works in your terminal. You can use the Codex CLI or the ChatGPT desktop app to send bridge requests.

---

## Installation & Health Check

Run the platform's installer command from the repository root. [Installation contract](./AGENTS.md)

### Windows

```powershell
git clone https://github.com/Idris173/Codex-Antigravity-Bridge.git
cd Codex-Antigravity-Bridge
python install.py
python install.py --doctor
```

### macOS headless preview

From a terminal where `codex --version` works, clone the preview branch and run the installer.

```sh
git clone --branch macos-headless https://github.com/Idris173/Codex-Antigravity-Bridge.git
cd Codex-Antigravity-Bridge
python3 install.py
python3 install.py --doctor
```

Reopen Antigravity and your Codex client after the first install. Open the same project folder on both sides and use its absolute path, such as `/Users/you/Projects/example`.

1. From Antigravity, ask it to have Codex read the project's README and explicitly allow a single result receipt. Use the headless workspace binding for this task.
2. From Codex, ask it to send a message to Antigravity and report the reply. Antigravity can check its inbox manually. To enable automatic notifications, use the [optional notification setup](#optional-antigravity-conversation-notifications) with `python3` from the target workspace.

The health check verifies the installed CLI command and both MCP connections. The POSIX tests use an isolated home and a stub Codex CLI. A full test with the live desktop clients on a Mac remains pending.

What `install.py` configures non-destructively:

1. Copies the Python bridge runtime to `~/.gemini/config/sidecars/chatgpt-bridge/`. On Windows it also deploys the UI Automation files and precompiles `BridgeFastUia.dll`. On macOS it records the Codex CLI path and terminal `PATH` for Antigravity's background process.
2. Installs the on-demand skills to `~/.gemini/config/skills/codex-antigravity-bridge/` and `~/.codex/skills/codex-antigravity-bridge/`.
3. Creates timestamped `.bak` backups before non-destructively merging `chatgpt-bridge` into `~/.gemini/config/mcp_config.json` and `[mcp_servers.antigravity_bridge]` into `~/.codex/config.toml` (preserving all existing MCP servers).

If Antigravity's MCP configuration is blank, the installer saves it as a `.bak` file and initializes valid JSON. If the file has content and contains invalid JSON, installation stops before writing configuration.

> **First-Install Note**: If the ChatGPT Desktop App is already running during first install, **restart the ChatGPT Desktop App once** so Codex loads the `antigravity_bridge` MCP server.

---

## Bidirectional Usage

Ask either app in plain language, for example “Open a paired session for this folder and have Codex check the setup guide.” The bridge passes session and request identifiers through its tools. Headless Codex requests and replies use natural language by default. Desktop window mode and authorized one-time receipts for read-only tasks keep the legacy envelope.

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

#### Desktop Window Mode (Windows)

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
   - Configures `~/.gemini/config/sidecars/chatgpt-bridge/sidecar.json`, enables `sidecars.chatgpt-bridge` in `~/.gemini/config/config.json`, and registers the `codex-antigravity-bridge-model` lifecycle hook in `~/.gemini/config/hooks.json` (`--doctor` verifies this hook when the sidecar is present).
   - Uses Antigravity's sidecar `agentapi send-message` (requires `agentapi` on the Antigravity sidecar `PATH`) to notify `<conversation-id>` when a new eligible `request_id` arrives for that workspace.
   - Use `bridge_check_codex_inbox` to read ordinary requests and `bridge_claim_codex_request` to claim requests that require a model. Reply to either through `bridge_reply_to_codex` under the conversation's existing permissions.
   - Restart Antigravity if the sidecar or hook does not load immediately after the configuration change.
   - To pause automatic notifications, set `sidecars.chatgpt-bridge.enabled` to `false` in `~/.gemini/config/config.json`.

### Pair a Workspace

- **Codex → Antigravity (`agy_workspace_pair`)**:
  1. Ensure the target folder is open as a workspace/project in Antigravity, then call `agy_workspace_pair(action="link", workspace_path="C:/path/to/workspace", conversation_id="<conversation_id>")`.
  2. Poll `agy_workspace_pair(action="get", workspace_path="C:/path/to/workspace")` until `route.status` is `"confirmed"` via native `agentapi get-conversation-metadata`. Multiple confirmed folders can route to separate Antigravity conversations.
  3. Calling `action="open"` attempts to create a new paired Antigravity conversation only when `route.project_id` is confirmed and matches `sidecars.chatgpt-bridge.projectId` in `~/.gemini/config/config.json` (not set by default during standard `--agy-conversation-id` install). The existing `"confirmed"` route continues receiving requests while `route.open_attempt` reports `"opening"`, `"creating"`, or `"blocked"` (`error_code`), and switches only after the new conversation's workspace metadata is verified.
- **Antigravity → Codex (`bridge_bind_session`)**:
  1. Call `bridge_bind_session(workspace_path="C:/path/to/workspace", reuse_workspace_binding=true)` to reuse an active headless `codex_exec` binding for the same resolved folder.
  2. Optionally pass `ensure_codex_project=true` to reuse or create a sidebar project via the experimental Codex App Server (`project` status on bind, `project_attachment` on the completed run). If the experimental API is unavailable, headless execution still runs in `workspace_path`.
  3. For non-blocking headless turns, pass `wait_for_result=false` to `bridge_dispatch_task` and read the exact turn outcome via `bridge_dispatch_status(dispatch_id="<dispatch_id>")`.

### Session Model Control

| Target | Available Controls | Scope & Boundaries |
| :--- | :--- | :--- |
| **Codex headless binding (`codex_exec`)** | `bridge_codex_session_model(binding_id, action="get" \| "set", model="<slug>")` (or initial `codex_model` on `bridge_bind_session`) | Applies to the **next headless turn** (`codex.exe exec` / `exec resume`), preserving session history across model changes and reporting `next_model`, `last_dispatched_model`, and `last_observed_model`. Does **not** alter a turn already in progress. |
| **Codex desktop window (`uia`)** | Not supported via bridge (`CODEX_DESKTOP_MODEL_NOT_SUPPORTED`) | The bridge cannot remotely change the ChatGPT/Codex desktop app's model picker. *(Host-native Codex CLI / App Server session APIs exist independently of this bridge's desktop UIA mode.)* |
| **Antigravity existing conversation** | Request a model switch through the conversation's built-in Swarm tool, then require the model through `agy_submit_for_review` | The conversation keeps its ID and context. The host hook checks the conversation and actual model at both claim and reply. Use the required model to resume a blocked request or cancel it from Codex. |
| **Antigravity new conversation** | Host-native `agentapi new-conversation --model=<model_id>` | Tested locally with `gemini-4-neon-medium`. The host falls back to the `flash` tier if the requested model is unavailable. The bridge's `agy_workspace_pair(action="open")` has no `model` parameter. |

#### Switch the model of an existing Antigravity conversation

1. Confirm that `agy_workspace_pair(action="get")` shows a confirmed route to the target conversation and that Sidecar notifications are active. Once the user requests a switch, send an ordinary `agy_submit_for_review` request. Put the target conversation ID and exact model ID in the message and omit the `required_model` and `target_conversation_id` arguments so the current model can perform the switch.
2. In the target conversation, check `swarm(action="status")`. If it has no Swarm, call `swarm(action="title", title="Model selection")` to initialize its board and coordinator. Once the conversation is confirmed as the coordinator, call `swarm(action="model", name="coordinator", model="gemini-4-neon-medium")`. This uses the original conversation and needs no worker agents. Reply with the tool result using the same request ID.
3. After the switch reply arrives, send a separate request that requires the exact model and the same conversation ID. Check `execution_model` and `reply_model` in the reply to verify the actual model. An isolated desktop conversation was tested switching from Hydrogen High to Neon Medium, and a separate Sidecar-triggered turn ran on Neon Medium.

#### Require a model for a request

From Codex, call `agy_submit_for_review` with these arguments. A request that requires a model immediately returns a `request_id`.

```json
{
  "workspace_path": "C:/path/to/workspace",
  "target_conversation_id": "<confirmed_conversation_id>",
  "required_model": "gemini-4-neon-medium",
  "step_summary": "Verify edge-case state transitions."
}
```

In the target Antigravity conversation, use `bridge_claim_codex_request` and then `bridge_reply_to_codex` with the same `request_id`. The host hook supplies the model proof automatically.

From Codex, use `agy_fetch_director_reply` with that ID to check `delivery.status` or read the reply. You can call `agy_cancel_guarded_review` before the request is claimed or while model verification blocks it.

From Antigravity, call `bridge_codex_session_model` to set the next headless turn's model.

```json
{
  "binding_id": "<binding_id>",
  "action": "set",
  "model": "gpt-6.2-astra"
}
```

[Antigravity Sidecars (`agentapi`)](https://antigravity.google/docs/sidecars#agentapi) · [Antigravity Swarm](https://antigravity.google/docs/swarm/) · [Antigravity Lifecycle Hooks (`PreToolUse` / `PreInvocation`)](https://antigravity.google/docs/hooks#pretooluse)

---

## Uninstall & Testing

To remove the suite while preserving unrelated MCP servers, user-added files, and saved routing preferences:

```powershell
python install.py --uninstall
```

*(If `BridgeFastUia.dll` is currently loaded by a running host process, close the host and rerun `python install.py --uninstall` to complete cleanup.)*

On macOS, use `python3 install.py --uninstall`.

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
│   ├── agy_model_guard.py
│   ├── codex_projects.py
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
