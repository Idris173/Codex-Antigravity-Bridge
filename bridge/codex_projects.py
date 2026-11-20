"""Opt-in Codex desktop project operations through the native App Server."""

import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading


class ProjectError(Exception):
    pass


def _project_api(executable, operation):
    with subprocess.Popen(
        [str(executable), "app-server", "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as child:
        messages = queue.Queue()

        def collect():
            for line in child.stdout:
                messages.put(line)
            messages.put(None)

        threading.Thread(target=collect, daemon=True).start()
        next_id = 0

        def send(body):
            child.stdin.write(json.dumps({"jsonrpc": "2.0", **body}) + "\n")
            child.stdin.flush()

        def call(method, params):
            nonlocal next_id
            next_id += 1
            send({"id": next_id, "method": method, "params": params})
            while True:
                try:
                    line = messages.get(timeout=6)
                except queue.Empty as error:
                    raise ProjectError("CODEX_PROJECT_API_TIMEOUT") from error
                if line is None:
                    raise ProjectError("CODEX_PROJECT_API_UNAVAILABLE")
                reply = json.loads(line)
                if reply.get("id") != next_id or "method" in reply:
                    continue
                if "result" not in reply:
                    raise ProjectError("CODEX_PROJECT_API_REJECTED")
                return reply["result"]

        try:
            call("initialize", {"clientInfo": {"name": "codex-antigravity-bridge", "version": "1"},
                                "capabilities": {"experimentalApi": True}})
            send({"method": "initialized"})
            return operation(call)
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def ensure_project(executable, workspace):
    normalized = os.path.normcase(str(Path(workspace).resolve(strict=True)))

    def match_or_create(call):
        matches = []
        cursor = None
        while True:
            page = call("project/list", {"cursor": cursor})
            for project in page["data"]:
                roots = {os.path.normcase(str(Path(root["path"]).resolve())) for root in project["roots"]}
                if roots == {normalized}:
                    matches.append(project)
            cursor = page.get("nextCursor")
            if not cursor:
                break
        if len(matches) > 1:
            raise ProjectError("AMBIGUOUS_CODEX_PROJECT")
        if matches:
            return {"project_id": matches[0]["id"], "reused": True}
        key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        project = call("project/create", {
            "idempotencyKey": key, "name": Path(workspace).name, "roots": [{"path": workspace}],
        })["project"]
        return {"project_id": project["id"], "reused": False}

    return _project_api(executable, match_or_create)


def attach_thread(executable, project_id, session_id):
    return _project_api(executable, lambda call: call(
        "thread/metadata/update", {"threadId": session_id, "projectId": project_id},
    ))
