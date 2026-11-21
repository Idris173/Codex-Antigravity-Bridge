"""Opt-in desktop project provisioning against the installed Antigravity host."""

import json
import os
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import NAMESPACE_URL, UUID, uuid5


class NativeDesktopError(ValueError):
    def __init__(self, code, operation, http_status=None, project_id=None, conversation_id=None):
        self.result = {"ok": False, "error_code": code, "operation": operation}
        for key, value in (("http_status", http_status), ("project_id", project_id), ("conversation_id", conversation_id)):
            if value is not None:
                self.result[key] = value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


HUB_NAME = re.compile(r"hub_[1-9][0-9]{0,4}\.json")
MODEL_EFFORT = re.compile(r"-(?:tiered-)?(low|medium|high|extra|xhigh|max)$")


def model_matches(expected, observed):
    if not isinstance(observed, str):
        return False
    if observed == expected:
        return True
    suffix = MODEL_EFFORT.search(observed)
    return bool(
        suffix and re.fullmatch(r"gemini-4-[a-z0-9]+", expected) and observed[:suffix.start()] == expected
    )


def _hub_rpc(path, owners):
    try:
        hub = json.loads(path.read_bytes())
        port, token, pid = hub["httpPort"], hub["csrfToken"], hub["pid"]
        if (type(port) is not int or not 0 < port <= 65535 or port != int(path.stem[4:])
                or not isinstance(token, str) or not token or "\n" in token or "\r" in token):
            raise ValueError()
        if type(pid) is not int or owners.get(port) != pid:
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError):
        raise NativeDesktopError("INVALID_DESKTOP_HUB", "connect") from None

    uri = f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/"
    opener = build_opener(ProxyHandler({}), NoRedirect())
    headers = {"Content-Type": "application/json", "Connect-Protocol-Version": "1", "x-codeium-csrf-token": token}

    def call(method, payload):
        request = Request(uri + method, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with opener.open(request, timeout=2 if method == "ListProjects" else 8) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except HTTPError as error:
            status = error.code
            error.close()
            raise NativeDesktopError("NATIVE_DESKTOP_RPC_REJECTED", method, status) from None
        except (URLError, OSError, ValueError):
            raise NativeDesktopError("NATIVE_DESKTOP_RPC_UNCONFIRMED", method) from None

    return call


def _desktop_loopback_owners():
    # Match the rendezvous PID to a live language-server listener before sending its token.
    command = """
$targetIds = @(Get-Process -Name language_server -ErrorAction SilentlyContinue).Id
if ($targetIds.Count -gt 0) {
    Get-NetTCPConnection -LocalAddress 127.0.0.1 -State Listen -ErrorAction Stop |
        Where-Object { $_.OwningProcess -in $targetIds } |
        Select-Object LocalPort,OwningProcess | ConvertTo-Json -Compress
}
"""
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, encoding="utf-8", check=True, timeout=8,
        )
        results = json.loads(completed.stdout or "[]")
        if isinstance(results, dict):
            results = [results]
        return {row["LocalPort"]: row["OwningProcess"] for row in results}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise NativeDesktopError("DESKTOP_HUB_DISCOVERY_FAILED", "connect") from None


def _hub_candidates(directory, owners):
    live = []
    for path in sorted(directory.glob("hub_*.json")):
        if not HUB_NAME.fullmatch(path.name) or int(path.stem[4:]) not in owners:
            continue
        try:
            call = _hub_rpc(path, owners)
            if not isinstance(call("ListProjects", {}).get("projectIds", []), list):
                continue
        except NativeDesktopError:
            continue
        if live:
            raise NativeDesktopError("DESKTOP_HUB_AMBIGUOUS", "connect")
        live.append(call)
    if not live:
        raise NativeDesktopError("DESKTOP_HUB_NOT_FOUND", "connect")
    return live[0]


def desktop_rpc(hub_name):
    directory = Path.home() / ".gemini/antigravity/daemon"
    if hub_name == "auto":
        return _hub_candidates(directory, _desktop_loopback_owners())
    if not HUB_NAME.fullmatch(hub_name):
        raise NativeDesktopError("INVALID_DESKTOP_HUB", "connect")
    return _hub_rpc(directory / hub_name, _desktop_loopback_owners())


def resolve_model(catalog, requested="", effort=""):
    """Resolve the host's exact model ID and wire enum. The runtime hook still verifies both calls."""
    if not requested and not effort:
        model = (catalog.get("defaultOverrideModelConfig") or {}).get("modelOrAlias")
        if isinstance(model, dict) and model.get("model"):
            return None, model
        raise NativeDesktopError("NATIVE_MODEL_UNCONFIRMED", "GetCascadeModelConfigData")

    requested = "-".join(requested.strip().lower().split())
    effort = effort.strip().lower().replace("xhigh", "extra")
    if effort not in ("", "low", "medium", "high", "extra", "max"):
        raise NativeDesktopError("NATIVE_MODEL_EFFORT_INVALID", "GetCascadeModelConfigData")
    catalog_models = {}
    for item in catalog.get("clientModelConfigs", []):
        model_id = item.get("modelId")
        model = item.get("modelOrAlias")
        if isinstance(model_id, str) and isinstance(model, dict) and model.get("model"):
            catalog_models[model_id] = model

    if requested in catalog_models and not effort:
        return requested, catalog_models[requested]
    candidates = {}
    for model_id, model in catalog_models.items():
        suffix = MODEL_EFFORT.search(model_id)
        family = model_id[:suffix.start()] if suffix else model_id
        tier = suffix.group(1).replace("xhigh", "extra") if suffix else ""
        family_match = requested in (family, model_id) or family.endswith("-" + requested)
        tier_match = not effort and model_id.endswith("-" + requested)
        if (family_match or tier_match) and (not effort or effort == tier):
            candidates[model_id] = (model, family, tier)
    if not candidates:
        raise NativeDesktopError("NATIVE_MODEL_UNCONFIRMED", "GetCascadeModelConfigData")
    if len(candidates) == 1:
        model_id, (model, _, _) = next(iter(candidates.items()))
        return model_id, model
    families = {family for _, family, _ in candidates.values()}
    medium = [(model_id, model) for model_id, (model, _, tier) in candidates.items() if tier == "medium"]
    if not effort and len(families) == 1 and len(medium) == 1:
        return medium[0]
    error = NativeDesktopError("NATIVE_MODEL_AMBIGUOUS", "GetCascadeModelConfigData")
    error.result["candidate_models"] = sorted(candidates)
    raise error


def ensure_workspace(call, workspace, name, path_from_uri, session_key=""):
    """Read back the host's project and conversation before reporting success."""
    path_key = os.path.normcase(str(workspace))
    folder_uri = workspace.as_uri()

    def matches_workspace(uris):
        if not isinstance(uris, list) or len(uris) != 1 or not isinstance(uris[0], str):
            return False
        root = path_from_uri(uris[0])
        return bool(root and os.path.normcase(root) == path_key)

    def read_project(project_id):
        result = call("ReadProject", {"id": project_id})
        if result.get("notFoundOnDisk") is True:
            return None
        project = result.get("project")
        if not isinstance(project, dict) or project.get("id") != project_id:
            raise NativeDesktopError("NATIVE_PROJECT_UNCONFIRMED", "ReadProject", project_id=project_id)
        return project

    def matches_project(project):
        if not project or project.get("name") != name or "sidecar" in project or project.get("isWorkspaceOnly"):
            return False
        if project.get("archived") or project.get("backendType") not in (None, "BACKEND_TYPE_LOCAL"):
            return False
        container = project.get("projectResources")
        resources = container.get("resources") if isinstance(container, dict) else None
        return (
            isinstance(resources, list) and len(resources) == 1 and isinstance(resources[0], dict)
            and matches_workspace([resources[0].get("folderUri")])
        )

    def read_conversation(conversation_id):
        try:
            result = call("GetConversationMetadata", {"conversationId": conversation_id})
        except NativeDesktopError as error:
            if error.result.get("http_status") == 404:
                return None
            raise
        return result.get("metadata") or {}

    project_ids = call("ListProjects", {}).get("projectIds", [])
    if not isinstance(project_ids, list):
        raise NativeDesktopError("NATIVE_PROJECT_LIST_UNCONFIRMED", "ListProjects")
    matching = [
        project_id for project_id in project_ids
        if isinstance(project_id, str) and matches_project(read_project(project_id))
    ]
    if len(matching) > 1:
        raise NativeDesktopError("NATIVE_PROJECT_AMBIGUOUS", "ReadProject")

    project_id = matching[0] if matching else str(uuid5(NAMESPACE_URL, f"codex-agy-desktop\n{path_key}\n{name}"))
    project = read_project(project_id)
    created_project = project is None
    if created_project:
        call("CreateProject", {
            "backend": "BACKEND_TYPE_LOCAL",
            "project": {"id": project_id, "name": name, "projectResources": {"resources": [{"folderUri": folder_uri}]}},
        })
        project = read_project(project_id)
    if not matches_project(project):
        raise NativeDesktopError("NATIVE_PROJECT_UNCONFIRMED", "ReadProject", project_id=project_id)

    try:
        namespace = UUID(project_id)
    except ValueError:
        raise NativeDesktopError("NATIVE_PROJECT_UNCONFIRMED", "ReadProject", project_id=project_id) from None
    session_name = "codex-agy-desktop-session-v1"
    if session_key:
        session_name += ":" + session_key
    conversation_id = str(uuid5(namespace, session_name))
    try:
        metadata = read_conversation(conversation_id)
        created_conversation = metadata is None
        if created_conversation:
            start = call("StartCascade", {
                "cascadeId": conversation_id,
                "source": "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT",
                "trajectoryType": "CORTEX_TRAJECTORY_TYPE_CASCADE",
                "backend": "BACKEND_TYPE_LOCAL",
                "projectEnvConfig": {"projectId": project_id, "defaultProjectEnvironment": {}},
            })
            if start.get("cascadeId") != conversation_id:
                raise NativeDesktopError("NATIVE_CONVERSATION_UNCONFIRMED", "StartCascade")
            metadata = read_conversation(conversation_id)
        if (not isinstance(metadata, dict) or metadata.get("rootConversationId") != conversation_id
                or metadata.get("parentConversationId") or metadata.get("projectId") != project_id
                or not matches_workspace(metadata.get("workspaceUris"))):
            raise NativeDesktopError("NATIVE_CONVERSATION_UNCONFIRMED", "GetConversationMetadata")
        source = metadata.get("sourceMetadata") or {}
        static = metadata.get("staticConfig")
        if source.get("sidecar") or (static is not None and (
            not static.get("mcpServers") or (static.get("codingAgent") or {}).get("agenticMode") is False
        )):
            raise NativeDesktopError("NATIVE_CONVERSATION_MCP_UNCONFIRMED", "GetConversationMetadata")
    except NativeDesktopError as error:
        error.result.update(project_id=project_id, conversation_id=conversation_id)
        raise
    return {
        "ok": True, "status": "verified", "workspace_path": str(workspace), "project_name": name,
        "project_id": project_id, "conversation_id": conversation_id,
        "created_project": created_project, "created_conversation": created_conversation,
    }


def wake_workspace_request(call, workspace, project_id, conversation_id, request_id, model_id,
                           path_from_uri, *, blank_only=False, required_model="", before_send=None):
    """Inject the claim notice into a verified desktop conversation."""
    metadata = call("GetConversationMetadata", {"conversationId": conversation_id}).get("metadata") or {}
    uris = metadata.get("workspaceUris") or []
    workspace_key = os.path.normcase(str(workspace))
    roots = (path_from_uri(uri) for uri in uris if isinstance(uri, str))
    if (str(metadata.get("projectId", "")).lower() != project_id.lower()
            or metadata.get("rootConversationId") != conversation_id or metadata.get("parentConversationId")
            or not any(root and os.path.normcase(root) == workspace_key for root in roots)):
        raise NativeDesktopError("NATIVE_CONVERSATION_UNCONFIRMED", "GetConversationMetadata")

    trajectory = call("GetCascadeTrajectory", {"cascadeId": conversation_id, "disableRehydration": True})
    inputs = [
        step for step in (trajectory.get("trajectory") or {}).get("steps", [])
        if step.get("type") == "CORTEX_STEP_TYPE_USER_INPUT"
    ]
    if any(request_id in json.dumps(step) for step in inputs):
        return {"ok": True, "status": "already_notified", "conversation_id": conversation_id, "request_id": request_id}
    if blank_only and inputs:
        return {"ok": True, "status": "not_blank", "conversation_id": conversation_id, "request_id": request_id}
    if trajectory.get("status") != "CASCADE_RUN_STATUS_IDLE":
        raise NativeDesktopError("NATIVE_CONVERSATION_BUSY", "GetCascadeTrajectory")

    resolved, model = resolve_model(call("GetCascadeModelConfigData", {}), model_id or required_model)
    if required_model and not model_matches(required_model, resolved):
        raise NativeDesktopError("ANTIGRAVITY_MODEL_MISMATCH", "GetCascadeModelConfigData")
    planner_config = {"google": {}, "requestedModel": model, "planModel": model["model"]}
    message = (
        f"Codex sent a request. Use chatgpt-bridge to claim {request_id} with bridge_claim_codex_request. "
        "If the tool returns the request, answer within its scope and reply through bridge_reply_to_codex "
        "for that exact ID. Operate only within the request scope and your existing permissions."
    )
    if before_send is not None and not before_send():
        return {"ok": True, "status": "skip", "conversation_id": conversation_id, "request_id": request_id}
    call("SendUserCascadeMessage", {
        "cascadeId": conversation_id,
        "items": [{"text": message}],
        "cascadeConfig": {"plannerConfig": planner_config},
        "clientMessageId": str(uuid5(UUID(conversation_id), request_id + ":" + model["model"])),
    })
    return {"ok": True, "status": "sent", "conversation_id": conversation_id, "request_id": request_id}
