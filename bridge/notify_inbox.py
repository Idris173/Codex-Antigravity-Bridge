"""Wake a selected Antigravity conversation through its native sidecar API."""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import bridge_server as bridge
if __package__:
    from .native_desktop import NativeDesktopError, desktop_rpc, wake_workspace_request
else:
    from native_desktop import NativeDesktopError, desktop_rpc, wake_workspace_request
from bridge_server import (
    BUS_DIR,
    _atomic_write_json,
    _read_json_safe,
    tool_check_codex_inbox,
)

def queued_at_ms(item):
    if isinstance(item.get("queued_at_ms"), int):
        return item["queued_at_ms"]
    try:
        return int(dt.datetime.fromisoformat(item["submitted_at"]).timestamp() * 1000)
    except (KeyError, TypeError, ValueError):
        return int(item["request_id"].split("_")[1])


def native_call(agentapi, *args):
    proc = subprocess.run([agentapi, *args], check=True, timeout=30, capture_output=True,
                          encoding="utf-8", errors="replace")
    response = json.loads(proc.stdout)
    if response.get("error"):
        raise ValueError("Antigravity rejected the native request")
    return response["response"]


def _verify_native_route(agentapi, key, route):
    expected = route.get("expected_project_id", "")
    if not bridge.PROJECT_ID.fullmatch(expected):
        raise bridge.WorkspaceRouteError("ANTIGRAVITY_PROJECT_ID_REQUIRED")
    if not bridge.CONVERSATION_ID.fullmatch(route["conversation_id"]):
        raise bridge.WorkspaceRouteError("INVALID_CONVERSATION_ID")
    bridge._verify_local_project(expected, key)
    reply = native_call(agentapi, "get-conversation-metadata", route["conversation_id"])
    meta = reply["conversationMetadata"]["metadata"]
    project_id = str(meta.get("projectId", "")).lower()
    if project_id != expected:
        raise bridge.WorkspaceRouteError("ANTIGRAVITY_PROJECT_UNCONFIRMED")
    roots = [bridge._workspace_from_uri(uri) for uri in meta.get("workspaceUris", []) if isinstance(uri, str)]
    if not any(root and os.path.normcase(root) == key for root in roots):
        raise bridge.WorkspaceRouteError("ANTIGRAVITY_WORKSPACE_UNCONFIRMED")
    return project_id


def _replace_route(key, route, current):
    path = BUS_DIR / "workspace_routes.json"
    with bridge._mutex(path.with_suffix(".mutex")):
        routes = bridge._workspace_routes()
        if routes.get(key) == route:
            routes[key] = current
            _atomic_write_json(path, routes)


def reconcile_routes(agentapi):
    for key, route in bridge._workspace_routes().items():
        opening = route.get("open_attempt", {}).get("status") in ("opening", "creating")
        if route["status"] != "verifying" and not opening:
            continue
        current = dict(route)
        if opening:
            current["open_attempt"] = {"status": "blocked", "error_code": bridge.NATIVE_PROJECT_SESSION_REQUIRED}
        if route["status"] == "verifying":
            try:
                project_id = _verify_native_route(agentapi, key, route)
                current.update(status="confirmed", project_id=project_id)
                current.pop("error_code", None)
            except bridge.WorkspaceRouteError as error:
                current.update(status="blocked", error_code=str(error))
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                current.update(status="blocked", error_code="ANTIGRAVITY_TARGET_UNCONFIRMED")
        _replace_route(key, route, current)


def _auto_wake(item, route, path):
    """Own the auto-wake attempt once so an unknown execution result is not retried."""
    request_id = item["request_id"]

    def before_send():
        with bridge._mutex(BUS_DIR / "replies.mutex"):
            state = _read_json_safe(path) or {}
            if (state.get("auto_wake_started_at") or state.get("notified_at") or state.get("claimed_at")
                    or not bridge._pending_request(request_id)):
                return False
            state.update(auto_wake_started_at=bridge._now_iso(), auto_wake_status="starting")
            _atomic_write_json(path, state)
            return True

    with bridge._mutex(BUS_DIR / "desktop_provision.mutex"):
        state = _read_json_safe(path) or {}
        if state.get("auto_wake_started_at") or state.get("notified_at") or state.get("claimed_at"):
            return "skip"
        response = wake_workspace_request(
            desktop_rpc(item["desktop_hub"]), Path(route["workspace_path"]), route["project_id"],
            route["conversation_id"], request_id, "", bridge._workspace_from_uri, blank_only=True,
            required_model=item.get("required_model", ""), before_send=before_send,
        )
        if response["status"] in ("sent", "already_notified"):
            with bridge._mutex(BUS_DIR / "replies.mutex"):
                state = _read_json_safe(path) or {}
                state["auto_wake_status"] = response["status"]
                _atomic_write_json(path, state)
        return response["status"]


def notify_pending(workspace, conversation_id, since_ms, agentapi):
    project_routes = list(bridge._workspace_routes().values())
    installed_root = bridge._existing_workspace(workspace) or os.path.abspath(workspace)
    installed = {"workspace_path": installed_root, "conversation_id": conversation_id, "since_ms": since_ms}
    routes = [*project_routes, installed]  # An explicit route wins if both roots match.
    pending = tool_check_codex_inbox()["pending_summaries"]
    previous = _read_json_safe(BUS_DIR / "antigravity_notifications.json") or {}
    previous_target = previous.get("target")
    last = None
    attempted_conversations = set()
    for item in pending:
        request_id = item["request_id"]
        candidates = project_routes if item.get("target_project_id") else routes
        route = bridge._matching_workspace_route(item, candidates)
        if not route or (route is not installed and not bridge._project_verified_route(route)):
            continue
        if queued_at_ms(item) < route["since_ms"]:
            continue
        cid = route["conversation_id"]
        if cid in attempted_conversations or (item.get("target_conversation_id") and item["target_conversation_id"] != cid):
            continue
        if item.get("target_project_id") and item["target_project_id"] != route.get("project_id"):
            continue
        path = BUS_DIR / "deliveries" / f"{request_id}.json"
        state = _read_json_safe(path) or {}
        if state.get("notified_at") or state.get("claimed_at") or state.get("auto_wake_started_at"):
            continue
        if previous_target == [workspace, cid, since_ms] and request_id in previous.get("sent", []):
            continue
        prompt = (
            f"Codex sent a request. Use chatgpt-bridge to claim {request_id} "
            "with bridge_claim_codex_request. If the tool returns the request, handle it within your "
            "current permissions and reply with bridge_reply_to_codex for the same request. "
            "If verification fails, stop and report the blocker."
            if item.get("target_conversation_id")
            else f"Codex sent a request. Read {request_id} using bridge_check_codex_inbox and "
                 "reply with bridge_reply_to_codex for the same request. If it is no longer pending, stop."
        )
        attempt = bridge._pending_request(request_id)
        if not attempt:
            continue
        auto_wake = attempt.get("auto_wake_blank") is True and route is not installed
        attempted_conversations.add(cid)
        try:
            if route is not installed:
                _verify_native_route(agentapi, os.path.normcase(route["workspace_path"]), route)
            wake = _auto_wake(attempt, route, path) if auto_wake else "not_blank"
            if wake == "skip":
                continue
            if wake == "not_blank":
                response = native_call(agentapi, "send-message", cid, prompt)
                if response["sendMessage"]["recipientId"] != cid:
                    raise ValueError("Antigravity did not confirm the target")
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            code = "ANTIGRAVITY_NOTIFICATION_UNCONFIRMED"
            if isinstance(error, NativeDesktopError):
                code = error.result["error_code"]
            if isinstance(error, bridge.WorkspaceRouteError):
                code = str(error)
                key = os.path.normcase(route["workspace_path"])
                _replace_route(key, route, {**route, "status": "blocked", "error_code": code})
            with bridge._mutex(BUS_DIR / "replies.mutex"):
                state = _read_json_safe(path) or {}
                if not state.get("claimed_at"):
                    state.update(status="notification_failed", error_code=code)
                    if state.get("auto_wake_started_at"):
                        state["auto_wake_status"] = "failed"
                    _atomic_write_json(path, state)
            continue
        with bridge._mutex(BUS_DIR / "replies.mutex"):
            state = _read_json_safe(path) or {}
            if state.get("status") in (None, "notification_failed"):
                state["status"] = "notified"
                state.pop("error_code", None)
            state.update(notified_at=bridge._now_iso(), target_conversation_id=cid)
            _atomic_write_json(path, state)
        print(f"Notified Antigravity conversation for {request_id}", flush=True)
        last = request_id
    return last


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("conversation_id")
    parser.add_argument("since_ms", type=int)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-zA-Z-]{32,128}", args.conversation_id):
        parser.error("invalid conversation ID")
    agentapi = shutil.which("agentapi")
    if agentapi is None:
        parser.error("agentapi must be available on the Antigravity sidecar PATH")

    print("Listening for Codex inbox messages", flush=True)
    while True:
        try:
            reconcile_routes(agentapi)
            notify_pending(str(args.workspace), args.conversation_id, args.since_ms, agentapi)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Antigravity inbox notification failed: {error}", flush=True)
            time.sleep(10)
        else:
            time.sleep(2)


if __name__ == "__main__":
    main()
