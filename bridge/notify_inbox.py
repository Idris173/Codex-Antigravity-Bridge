"""Wake a selected Antigravity conversation through its native sidecar API."""

import argparse
import re
import shutil
import subprocess
import time
from pathlib import Path

from bridge_server import (
    BUS_DIR,
    _atomic_write_json,
    _read_json_safe,
    tool_check_codex_inbox,
)


def notify_pending(workspace, conversation_id, since_ms, agentapi):
    pending = tool_check_codex_inbox(workspace_path=workspace)["pending_request_ids"]
    eligible = {request_id for request_id in pending if int(request_id.split("_")[1]) >= since_ms}
    state_file = BUS_DIR / "antigravity_notifications.json"
    previous = _read_json_safe(state_file) or {}
    target = [workspace, conversation_id, since_ms]
    sent = set(previous.get("sent", [])) & eligible if previous.get("target") == target else set()
    request_id = next((item for item in pending if item in eligible - sent), None)
    if request_id:
        prompt = (
            f"Codex bridge inbox notification for {request_id}. "
            "Use chatgpt-bridge bridge_check_codex_inbox with this exact request_id. "
            "Handle the submission within the current permissions and use bridge_reply_to_codex "
            "with the same request_id to report the outcome or a blocker. "
            "If the submission is no longer pending, stop."
        )
        subprocess.run([agentapi, "send-message", conversation_id, prompt], check=True, timeout=30)
        sent.add(request_id)
        print(f"Notified Antigravity conversation for {request_id}", flush=True)
    if sent or previous:
        updated = {"target": target, "sent": sorted(sent)}
        if updated != previous:
            _atomic_write_json(state_file, updated)
    return request_id


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
            notify_pending(str(args.workspace), args.conversation_id, args.since_ms, agentapi)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Antigravity inbox notification failed: {error}", flush=True)
            time.sleep(10)
        else:
            time.sleep(2)


if __name__ == "__main__":
    main()
