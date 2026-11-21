"""Regression coverage for opt-in Antigravity desktop provisioning."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from bridge.native_desktop import NativeDesktopError, ensure_workspace, wake_workspace_request


class DesktopHost:
    def __init__(self):
        self.projects = {}
        self.conversations = {}
        self.requests = []
        self.wrong_project = False
        self.static_config = None
        self.inputs = []

    def __call__(self, method, args):
        self.requests.append((method, args))
        if method == "ListProjects":
            return {"projectIds": list(self.projects)}
        if method == "ReadProject":
            project = self.projects.get(args["id"])
            return {"project": project} if project else {"notFoundOnDisk": True}
        if method == "CreateProject":
            project = args["project"]
            self.projects[project["id"]] = project
            return {}
        if method == "GetConversationMetadata":
            metadata = self.conversations.get(args["conversationId"])
            if metadata is None:
                raise NativeDesktopError("NATIVE_DESKTOP_RPC_REJECTED", method, 404)
            return {"metadata": metadata}
        if method == "StartCascade":
            project_id = args["projectEnvConfig"]["projectId"]
            resources = self.projects[project_id]["projectResources"]["resources"]
            cid = args["cascadeId"]
            self.conversations[cid] = {
                "rootConversationId": cid,
                "staticConfig": self.static_config,
                "projectId": str(uuid4()) if self.wrong_project else project_id,
                "workspaceUris": [resource["folderUri"] for resource in resources],
            }
            return {"cascadeId": cid}
        if method == "GetCascadeTrajectory":
            return {"status": "CASCADE_RUN_STATUS_IDLE", "trajectory": {"steps": self.inputs}}
        if method == "GetCascadeModelConfigData":
            return {"clientModelConfigs": [
                {"modelId": "gemini-4-carbon-low", "modelOrAlias": {"model": "MODEL_CARBON"}}
            ]}
        if method == "SendUserCascadeMessage":
            self.inputs.append({"type": "CORTEX_STEP_TYPE_USER_INPUT", "items": args["items"]})
            return {}
        raise AssertionError(method)


class DesktopProvisionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name).resolve()
        self.host = DesktopHost()

    def provision(self, name="Pilot", session_key=""):
        return ensure_workspace(
            self.host, self.workspace, name,
            lambda uri: str(self.workspace) if uri == self.workspace.as_uri() else None, session_key,
        )

    def test_create_and_retry_use_same_native_ids(self):
        first = self.provision()
        self.assertTrue(first["created_project"])
        self.assertTrue(first["created_conversation"])
        second = self.provision()
        self.assertEqual(second["project_id"], first["project_id"])
        self.assertEqual(second["conversation_id"], first["conversation_id"])
        self.assertFalse(second["created_project"])
        self.assertFalse(second["created_conversation"])
        for method in ("CreateProject", "StartCascade"):
            requests = [args for operation, args in self.host.requests if operation == method]
            self.assertEqual(len(requests), 1)
            if method == "StartCascade":
                self.assertNotIn("workspaceUris", requests[0])

    def test_new_session_key_keeps_same_project(self):
        original = self.provision()
        extra = self.provision(session_key="carbon")
        self.assertEqual(extra["project_id"], original["project_id"])
        self.assertNotEqual(extra["conversation_id"], original["conversation_id"])
        self.assertFalse(extra["created_project"])
        self.assertTrue(extra["created_conversation"])
        self.assertEqual(self.provision(session_key="carbon")["conversation_id"], extra["conversation_id"])

    def test_wake_binds_model_and_does_not_repeat_existing_notice(self):
        result = self.provision()
        args = (
            self.host, self.workspace, result["project_id"], result["conversation_id"],
            "req_123456_abcdef", "gemini-4-carbon-low", lambda _: str(self.workspace),
        )
        self.assertEqual(wake_workspace_request(*args)["status"], "sent")
        self.assertEqual(wake_workspace_request(*args)["status"], "already_notified")
        sent = [body for method, body in self.host.requests if method == "SendUserCascadeMessage"]
        self.assertEqual(len(sent), 1)
        planner = sent[0]["cascadeConfig"]["plannerConfig"]
        self.assertEqual(planner["planModel"], "MODEL_CARBON")
        self.assertEqual(planner["requestedModel"], {"model": "MODEL_CARBON"})

    def test_does_not_reuse_an_unrelated_same_name(self):
        other = str(uuid4())
        self.host.projects[other] = {
            "id": other, "name": "Pilot",
            "projectResources": {"resources": [{"folderUri": (self.workspace / "different").as_uri()}]},
        }
        result = self.provision()
        self.assertNotEqual(result["project_id"], other)
        self.assertEqual(len(self.host.projects), 2)

    def test_wrong_project_metadata_fails_closed_and_does_not_retry_create(self):
        self.host.wrong_project = True
        for _ in range(2):
            with self.assertRaises(NativeDesktopError) as failure:
                self.provision()
            self.assertEqual(failure.exception.result["error_code"], "NATIVE_CONVERSATION_UNCONFIRMED")
        self.assertEqual(len(self.host.projects), 1)
        self.assertEqual(len(self.host.conversations), 1)

    def test_static_conversation_without_mcp_is_not_verified(self):
        self.host.static_config = {"mcpServers": [], "codingAgent": {"agenticMode": False}}
        with self.assertRaises(NativeDesktopError) as failure:
            self.provision()
        self.assertEqual(failure.exception.result["error_code"], "NATIVE_CONVERSATION_MCP_UNCONFIRMED")

    def test_ambiguous_project_refuses_to_create(self):
        for _ in range(2):
            project_id = str(uuid4())
            self.host.projects[project_id] = {
                "id": project_id, "name": "Pilot",
                "projectResources": {"resources": [{"folderUri": self.workspace.as_uri()}]},
            }
        with self.assertRaises(NativeDesktopError) as failure:
            self.provision()
        self.assertEqual(failure.exception.result["error_code"], "NATIVE_PROJECT_AMBIGUOUS")
        self.assertFalse(self.host.conversations)
