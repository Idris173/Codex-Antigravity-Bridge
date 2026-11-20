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
from urllib.parse import unquote, urlsplit

import bridge_server as bridge
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


def _workspace_from_uri(uri):
    parts = urlsplit(uri)
    if parts.scheme != "file":
        return None
    path = unquote(parts.path)
    if parts.netloc:
        path = f"//{parts.netloc}{path}"
    elif len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return bridge._existing_workspace(path)


def reconcile_routes(agentapi):
    path = BUS_DIR / "workspace_routes.json"
    config_path = bridge.BASE_DIR.parent.parent / "config.json"
    for key, route in bridge._workspace_routes().items():
        opening = route.get("open_attempt", {}).get("status") == "opening"
        if route["status"] != "verifying" and not opening:
            continue
        current = dict(route)
        try:
            if opening:
                settings = _read_json_safe(config_path) or {}
                project = settings.get("sidecars", {}).get("chatgpt-bridge", {}).get("projectId")
                if project != route["project_id"]:
                    raise ValueError("ANTIGRAVITY_SIDECAR_PROJECT_MISMATCH")
                # Reserve creation before crossing the native API. A restarted listener must not create a duplicate.
                with bridge._mutex(path.with_suffix(".mutex")):
                    routes = bridge._workspace_routes()
                    if routes.get(key) != route:
                        continue
                    route = {**route, "open_attempt": {"status": "creating"}}
                    routes[key] = route
                    _atomic_write_json(path, routes)
                reply = native_call(agentapi, "new-conversation", "--title=Codex paired workspace",
                                    "This conversation was opened for a paired Codex workspace. Please wait for a request.")
                current["conversation_id"] = reply["newConversation"]["conversationId"]
                current["since_ms"] = int(time.time() * 1000)
            conversation_id = current["conversation_id"]
            if not bridge.CONVERSATION_ID.fullmatch(conversation_id):
                raise ValueError("INVALID_CONVERSATION_ID")
            reply = native_call(agentapi, "get-conversation-metadata", conversation_id)
            meta = reply["conversationMetadata"]["metadata"]
            roots = [_workspace_from_uri(uri) for uri in meta.get("workspaceUris", []) if isinstance(uri, str)]
            if not any(root and os.path.normcase(root) == key for root in roots):
                raise ValueError("ANTIGRAVITY_WORKSPACE_UNCONFIRMED")
            if current.get("project_id") and current["project_id"] != meta.get("projectId"):
                raise ValueError("ANTIGRAVITY_PROJECT_UNCONFIRMED")
            current.update(status="confirmed", project_id=meta.get("projectId", ""))
            current.pop("error_code", None)
            current.pop("open_attempt", None)
        except ValueError as error:
            reason = str(error)
            current.update(status="blocked", error_code=reason if reason.isupper() else "ANTIGRAVITY_TARGET_UNCONFIRMED")
        except (OSError, KeyError, subprocess.SubprocessError):
            current.update(status="blocked", error_code="ANTIGRAVITY_TARGET_UNCONFIRMED")
        if opening and current["status"] == "blocked":
            current = {**route, "open_attempt": {"status": "blocked", "error_code": current["error_code"]}}
        with bridge._mutex(path.with_suffix(".mutex")):
            routes = bridge._workspace_routes()
            if routes.get(key) == route:
                routes[key] = current
                _atomic_write_json(path, routes)


def notify_pending(workspace, conversation_id, since_ms, agentapi):
    routes = list(bridge._workspace_routes().values())
    routes.append({"workspace_path": workspace, "conversation_id": conversation_id,
                   "since_ms": since_ms, "status": "confirmed"})
    routes.sort(key=lambda route: len(route["workspace_path"]), reverse=True)
    pending = tool_check_codex_inbox()["pending_summaries"]
    previous = _read_json_safe(BUS_DIR / "antigravity_notifications.json") or {}
    previous_target = previous.get("target")
    last = None
    attempted_conversations = set()
    for item in pending:
        request_id = item["request_id"]
        route = next((entry for entry in routes
                      if bridge.filter_submissions_by_workspace([item], entry["workspace_path"])), None)
        if not route or route["status"] != "confirmed" or queued_at_ms(item) < route["since_ms"]:
            continue
        cid = route["conversation_id"]
        if cid in attempted_conversations or (item.get("target_conversation_id") and item["target_conversation_id"] != cid):
            continue
        path = BUS_DIR / "deliveries" / f"{request_id}.json"
        state = _read_json_safe(path) or {}
        if state.get("notified_at") or state.get("claimed_at"):
            continue
        if previous_target == [workspace, cid, since_ms] and request_id in previous.get("sent", []):
            continue
        prompt = (
            f"Codex sent a request. Use chatgpt-bridge to claim {request_id} "
            "with bridge_claim_codex_request. If the tool returns the request, handle it within your "
            "current permissions and reply with bridge_reply_to_codex for the same request. "
            "If model verification fails, stop and report the blocker."
            if item.get("required_model")
            else f"Codex sent a request. Read {request_id} using bridge_check_codex_inbox and "
                 "reply with bridge_reply_to_codex for the same request. If it is no longer pending, stop."
        )
        attempted_conversations.add(cid)
        try:
            response = native_call(agentapi, "send-message", cid, prompt)
            if response["sendMessage"]["recipientId"] != cid:
                raise ValueError("Antigravity did not confirm the target")
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            with bridge._mutex(BUS_DIR / "replies.mutex"):
                state = _read_json_safe(path) or {}
                if not state.get("claimed_at"):
                    state.update(status="notification_failed", error_code="ANTIGRAVITY_NOTIFICATION_UNCONFIRMED")
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
