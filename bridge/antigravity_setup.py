"""Read the Antigravity installation settings shared by MCP diagnostics and the installer."""

import json
from pathlib import Path
import re


MODEL_HOOK = "codex-antigravity-bridge-model"


def _read_object(path):
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _model_hook_installed(config, script):
    hook = _read_object(config / "hooks.json").get(MODEL_HOOK)
    if not isinstance(hook, dict) or hook.get("enabled") is False or not script.is_file():
        return False

    def command_matches(handler, action):
        command = handler.get("command") if isinstance(handler, dict) else None
        if not isinstance(command, str):
            return False
        suffix = re.search(r'(?:^|\s)(?:"([^"]+)"|(\S+))\s+' + re.escape(action) + r"\s*$", command)
        return bool(suffix and Path(suffix.group(1) or suffix.group(2)) == script)

    invocation = hook.get("PreInvocation")
    pre_tool = hook.get("PreToolUse")
    if not isinstance(invocation, list) or not isinstance(pre_tool, list):
        return False
    if not any(command_matches(handler, "pre-invocation") for handler in invocation):
        return False

    tool_names = ("call_mcp_tool", "bridge_claim_codex_request", "bridge_reply_to_codex")
    for group in pre_tool:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            continue
        matcher = group.get("matcher")
        if not isinstance(matcher, str):
            continue
        try:
            matched = all(re.search(matcher, tool) for tool in tool_names)
        except re.error:
            continue
        if matched and any(command_matches(handler, "pre-tool") for handler in group["hooks"]):
            return True
    return False


def inspect_setup(config, bridge_dir):
    """Report installation evidence without returning commands, config bodies, or credentials."""
    settings = _read_object(config / "config.json").get("sidecars")
    sidecar = settings.get("chatgpt-bridge") if isinstance(settings, dict) else None
    manifest = _read_object(bridge_dir / "sidecar.json")
    args = manifest.get("args")
    script = bridge_dir / "notify_inbox.py"
    sidecar_configured = (
        script.is_file() and isinstance(args, list) and isinstance(manifest.get("command"), str)
        and any(isinstance(arg, str) and Path(arg) == script for arg in args)
    )
    return {
        "sidecar_configured": sidecar_configured,
        "sidecar_enabled_in_config": isinstance(sidecar, dict) and sidecar.get("enabled") is True,
        "sidecar_enabled": sidecar_configured and isinstance(sidecar, dict) and sidecar.get("enabled") is True,
        "model_hook_installed": _model_hook_installed(config, bridge_dir / "agy_model_guard.py"),
    }
