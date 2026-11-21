"""Isolated installer, MCP transport, and review bus tests."""

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest import mock

import install


ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(Path(sys.executable).resolve())


class TemporaryHomeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="codex antigravity bridge 隔离 ")
        self.addCleanup(temp.cleanup)
        self.layout = install.Layout(Path(temp.name).resolve())

    def cli(self, *args, good=True, language="en"):
        completed = subprocess.run(
            [PYTHON, "-B", str(ROOT / "install.py"), "--home-dir", str(self.layout.home), "--lang", language, *args],
            cwd=ROOT,
            env={**os.environ, **install.UTF8_ENV},
            capture_output=True, encoding="utf-8", timeout=75,
        )
        self.assertEqual(
            completed.returncode, 0 if good else 1,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        return completed

    def seed(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def tool(self, entry, role, name, arguments):
        result = install.exchange_mcp(entry, role, "tools/call", {"name": name, "arguments": arguments})
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][0]["text"])


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Requires Windows PowerShell and UI Automation assemblies")
class InstallerAndBusTests(TemporaryHomeTest):
    def test_upgrade_archives_legacy_skill_directories(self):
        legacy_directories = []
        for (_, destination), name in zip(self.layout.skills, ("codex-director", "director-worker")):
            legacy = destination.with_name(name)
            self.seed(legacy / "SKILL.md", f"---\nname: {name}\n---\n".encode())
            self.seed(legacy / "local-notes.md", b"keep local edits")
            legacy_directories.append(legacy)

        self.cli()
        for legacy in legacy_directories:
            self.assertFalse(legacy.exists())
            archived, = self.layout.bridge.glob(f"{legacy.name}.*.bak")
            self.assertEqual((archived / "local-notes.md").read_bytes(), b"keep local edits")

        self.assertNotIn("Backup:", self.cli().stdout)
        health = json.loads(self.cli("--doctor").stdout)
        self.assertTrue(health["ok"], health)

    def test_distributed_lifecycle(self):
        gemini = {
            "mcpServers": {"friend-gemini": {"command": "friend", "args": ["--with spaces"]}},
            "settings": {"theme": "原始"},
        }
        gemini_original = json.dumps(gemini, ensure_ascii=False).encode("utf-8")
        codex_original = '''# Keep this comment and the multiline text.
model = "friend-model"
instructions = """
[mcp_servers.antigravity_bridge]
This is part of a string.
"""
[mcp_servers."friend.codex"]
command = "friend"
[mcp_servers."friend.codex".env]
MODE = "user-managed"

[[projects]]
name = "Existing"
'''.replace("\n", "\r\n").encode("utf-8")
        codex = tomllib.loads(codex_original.decode("utf-8"))
        self.seed(self.layout.gemini, gemini_original)
        self.seed(self.layout.codex, codex_original)

        first = self.cli("--python-exe", PYTHON, language="zh")
        self.assertIn("已安装", first.stdout)
        self.assertEqual(first.stderr, "")
        self.assertEqual([file.read_bytes() for file in self.layout.gemini.parent.glob("mcp_config.json.*.bak")], [gemini_original])
        self.assertEqual([file.read_bytes() for file in self.layout.codex.parent.glob("config.toml.*.bak")], [codex_original])

        gemini_installed = self.layout.gemini.read_bytes()
        codex_installed = self.layout.codex.read_bytes()
        gemini_config = json.loads(gemini_installed)
        codex_config = tomllib.loads(codex_installed.decode("utf-8"))
        director = gemini_config["mcpServers"].pop(install.AGY)
        worker = codex_config["mcp_servers"].pop(install.CODEX)
        self.assertEqual(gemini_config, gemini)
        self.assertEqual(codex_config, codex)
        self.assertEqual(director["command"], PYTHON)
        self.assertEqual(worker["command"], PYTHON)
        self.assertEqual(director["args"], [str(self.layout.server), "--role", "antigravity"])
        self.assertEqual(worker["args"], [str(self.layout.server), "--role", "codex"])
        self.assertEqual(worker["env"], install.UTF8_ENV)
        self.assertEqual(worker["startup_timeout_sec"], 15)
        self.assertEqual(worker["tool_timeout_sec"], 310)
        self.assertTrue(codex_installed.startswith(codex_original))

        dll = self.layout.bridge / "BridgeFastUia.dll"
        original_dll = dll.read_bytes()
        dll_mtime = dll.stat().st_mtime_ns
        second = self.cli()
        self.assertIn("Installed both", second.stdout)
        self.assertNotIn("Backup:", second.stdout)
        self.assertEqual(self.layout.gemini.read_bytes(), gemini_installed)
        self.assertEqual(self.layout.codex.read_bytes(), codex_installed)
        self.assertEqual(dll.read_bytes(), original_dll)
        self.assertEqual(dll.stat().st_mtime_ns, dll_mtime)
        self.assertEqual(len(list(self.layout.gemini.parent.glob("mcp_config.json.*.bak"))), 1)
        self.assertEqual(len(list(self.layout.codex.parent.glob("config.toml.*.bak"))), 1)

        health = json.loads(self.cli("--doctor").stdout)
        self.assertTrue(health["ok"], health)
        self.assertTrue(all(check["ok"] for check in health["checks"].values()), health)
        for source, destination in self.layout.skills:
            skill = health["checks"][f"{source.parent.name}_skill"]
            self.assertEqual(Path(skill["path"]), destination)
            self.assertEqual(destination.name, "codex-antigravity-bridge")
        self.assertEqual(set(health["checks"]["antigravity_mcp"]["tools"]), install.TOOLS[install.AGY])
        self.assertEqual(set(health["checks"]["codex_mcp"]["tools"]), install.TOOLS[install.CODEX])

        module = runpy.run_path(str(self.layout.server))
        for prompt, language in (("Verify the mechanism", "en"), ("验证通信机制", "zh-CN")):
            with self.subTest(language=language):
                self.assertEqual(module["detect_language"](prompt), language)
                envelope = module["build_director_envelope"]("general", prompt, prompt)
                self.assertIn(f"\n[LANG: {language}]\n", envelope)
                self.assertIn("Activate `codex-antigravity-bridge`", envelope)

        routing_args = {"codex_home": str(self.layout.codex.parent)}
        configured = self.tool(worker, install.CODEX, "agy_routing_settings", {
            **routing_args, "action": "set", "worker_model": "gpt-6.2-sol", "worker_speed": "ultrafast"
        })
        director_routing = self.tool(director, install.AGY, "bridge_routing_settings", routing_args)
        self.assertEqual(director_routing["settings"], configured["settings"])
        self.assertEqual(director_routing["codex_current_defaults"]["model"], "friend-model")
        self.cli()
        self.assertEqual(self.tool(director, install.AGY, "bridge_routing_settings", routing_args)["settings"],
                         configured["settings"])

        queued = self.tool(worker, install.CODEX, "agy_submit_for_review",
                           {"step_summary": "验收双边链路", "wait_for_reply": False})
        request_id = queued["request_id"]
        self.assertEqual(queued["status"], "queued")
        inbox = self.tool(director, install.AGY, "bridge_check_codex_inbox", {"request_id": request_id})
        self.assertEqual(inbox["submission"]["request_id"], request_id)
        self.assertEqual(inbox["submission"]["step_summary"], "验收双边链路")
        self.tool(director, install.AGY, "bridge_reply_to_codex", {
            "request_id": request_id, "decision": "APPROVED", "review_feedback": "往返验证完成。",
        })
        reply = self.tool(worker, install.CODEX, "agy_fetch_director_reply", {"request_id": request_id})
        self.assertEqual(reply["request_id"], request_id)
        self.assertEqual(reply["decision"], "APPROVED")
        self.assertEqual(reply["review_feedback"], "往返验证完成。")

        self.cli("--uninstall")
        self.assertEqual(json.loads(self.layout.gemini.read_bytes()), gemini)
        self.assertEqual(tomllib.loads(self.layout.codex.read_text(encoding="utf-8")), codex)
        self.assertEqual(json.loads((self.layout.bridge / "director_settings.json").read_bytes()), configured["settings"])
        self.assertEqual(list(self.layout.bridge.iterdir()), [self.layout.bridge / "director_settings.json"])
        self.assertTrue(all(not destination.exists() for _, destination in self.layout.skills))
        self.cli("--uninstall")
        self.assertEqual(json.loads(self.layout.gemini.read_bytes()), gemini)
        self.assertEqual(tomllib.loads(self.layout.codex.read_text(encoding="utf-8")), codex)

    def test_empty_home_and_invalid_library_repair(self):
        self.cli()
        dll = self.layout.bridge / "BridgeFastUia.dll"
        dll.write_bytes(b"not an assembly")
        report = json.loads(self.cli("--doctor", good=False, language="zh").stdout)
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["uia_dll"]["ok"])
        self.assertTrue(report["checks"]["antigravity_mcp"]["ok"])
        self.assertTrue(report["checks"]["codex_mcp"]["ok"])
        self.cli()
        self.assertTrue(json.loads(self.cli("--doctor").stdout)["ok"])
        self.cli("--uninstall")
        self.assertFalse(self.layout.gemini.exists())
        self.assertFalse(self.layout.codex.exists())
        self.assertFalse(self.layout.bridge.exists())

    def test_malformed_config_aborts_before_writing(self):
        initial = b"[mcp_servers\n"
        self.seed(self.layout.codex, initial)
        malformed = self.cli(good=False)
        self.assertIn("Error", malformed.stderr)
        self.assertEqual(self.layout.codex.read_bytes(), initial)
        self.assertFalse(self.layout.gemini.exists())
        self.assertFalse(self.layout.bridge.exists())
        self.assertFalse(list(self.layout.codex.parent.glob("*.bak")))

    def test_blank_mcp_config_is_initialized_and_backed_up(self):
        self.seed(self.layout.gemini, b"")

        self.cli()

        config = json.loads(self.layout.gemini.read_bytes())
        self.assertEqual(config["mcpServers"][install.AGY]["args"],
                         [str(self.layout.server), "--role", "antigravity"])
        backup, = self.layout.gemini.parent.glob("mcp_config.json.*.bak")
        self.assertEqual(backup.read_bytes(), b"")
        self.assertNotIn("Backup:", self.cli().stdout)
        health = json.loads(self.cli("--doctor").stdout)
        self.assertTrue(health["ok"], health)

    def test_library_replacement_can_be_deferred(self):
        self.cli()
        dll = self.layout.bridge / "BridgeFastUia.dll"
        original = dll.read_bytes()
        replace = Path.replace

        def locked(path, target):
            if target == dll:
                raise PermissionError("fixture simulates an in-use DLL")
            return replace(path, target)

        with mock.patch.object(Path, "replace", locked):
            self.assertFalse(install.compile_dll(self.layout, changed=True))
        self.assertEqual(dll.read_bytes(), original)
        self.assertFalse(list(self.layout.bridge.glob(".compile-*")))

    def test_warm_driver_keeps_invalid_window_target_across_actions(self):
        self.cli()
        module = runpy.run_path(str(self.layout.server))
        driver = module["_run_ps_driver"]
        try:
            for action in ("inspect", "dispatch", "read_latest", "new_project_session", "switch_session"):
                with self.subTest(action=action):
                    result = driver(action, target_hwnd=-1, timeout_sec=2, allow_placeholder_title=True)
                    self.assertEqual(result["error_code"], "TARGET_WINDOW_NOT_FOUND", result)
                    self.assertEqual(result["hwnd"], -1)
                    warm = driver.__globals__["_WARM_PS_PROC"]
                    self.assertIsNotNone(warm)
                    self.assertIsNone(warm.poll())
        finally:
            module["_close_warm_ps"]()


class MergeAndLocaleTests(TemporaryHomeTest):
    def test_blank_mcp_config_with_whitespace_or_bom(self):
        entry = install.registry_entry(Path(PYTHON), self.layout.server, "antigravity")
        for original in (b" \r\n\t", b"\xef\xbb\xbf \n"):
            with self.subTest(original=original):
                updated = install.edit_json(original, self.layout.server, entry, {})
                self.assertEqual(json.loads(updated), {"mcpServers": {install.AGY: entry}})
        with self.assertRaises(json.JSONDecodeError):
            install.edit_json(b'{"mcpServers":', self.layout.server, entry, {})

    def test_opt_in_antigravity_notifications_preserve_other_sidecars(self):
        initial = {"sidecars": {"unrelated": {"enabled": True}}, "other": "keep"}
        self.seed(self.layout.antigravity_config, install.json_bytes(initial))
        conversation_id = "12345678-1234-1234-1234-123456789abc"
        edits = install.antigravity_notification_edits(self.layout, Path(PYTHON), ROOT, conversation_id)
        for path, old, new in edits:
            install.write_config(path, old, new)
        manifest = json.loads(self.layout.antigravity_sidecar.read_bytes())
        self.assertEqual(manifest["args"][2:4], [str(ROOT), conversation_id])
        settings = json.loads(self.layout.antigravity_config.read_bytes())
        self.assertEqual(settings["sidecars"][install.AGY], {"enabled": True})
        for _, old, new in install.antigravity_notification_edits(self.layout, Path(PYTHON), ROOT, conversation_id):
            self.assertEqual(old, new)
        for path, old, new in install.antigravity_notification_edits(self.layout):
            install.write_config(path, old, new)
        self.assertFalse(self.layout.antigravity_sidecar.exists())
        self.assertEqual(json.loads(self.layout.antigravity_config.read_bytes()), initial)

    def test_uninstall_keeps_custom_files_and_completes_after_unlock(self):
        _, edits = install.plans(self.layout, Path(PYTHON))
        for path, _, updated in edits:
            self.seed(path, updated)
        self.seed(self.layout.state, install.json_bytes({
            "gemini_existed": False, "gemini_map_existed": False,
            "codex_existed": False, "codex_map_existed": False,
        }))
        (self.layout.bridge / "director_settings.json").write_text("{}", encoding="utf-8")
        install.copy_assets(self.layout)
        dll = self.layout.bridge / "BridgeFastUia.dll"
        self.seed(dll, b"fixture")
        director, worker = [destination for _, destination in self.layout.skills]
        extras = [director / "references/custom.md", worker / "custom.md",
                  self.layout.bridge / "custom.py", self.layout.bridge / "bus/requests/custom.json"]
        for path in extras:
            self.seed(path, b"user data")

        unlink = Path.unlink

        def locked(path, *args, **kwargs):
            if path == dll:
                raise PermissionError("fixture physically locks DLL")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", locked):
            with self.assertRaisesRegex(PermissionError, "BridgeFastUia.dll"):
                install.uninstall(self.layout)
        self.assertTrue(self.layout.state.is_file())
        self.assertTrue(self.layout.server.is_file())
        self.assertTrue(self.layout.codex.is_file())
        self.assertTrue(dll.is_file())

        install.uninstall(self.layout)
        for path in (self.layout.state, self.layout.server, self.layout.codex, self.layout.gemini,
                     dll, director / "SKILL.md", worker / "SKILL.md"):
            self.assertFalse(path.exists(), path)
        for path in extras:
            self.assertEqual(path.read_bytes(), b"user data")
        self.assertEqual((self.layout.bridge / "director_settings.json").read_text(encoding="utf-8"), "{}")
        self.assertEqual(install.uninstall(self.layout), [])

    def test_inline_toml_parent_and_quoted_managed_tables(self):
        command = str(self.layout.server)
        python = Path(PYTHON)
        entry = install.registry_entry(python, self.layout.server, "codex")
        existing = {
            "sample.dot": {"command": "keep", "env": {"URL": "https://example.com", "when": "today"}},
            install.CODEX: {"command": "old", "args": [command, "--role", "codex"]},
        }
        inline = "mcp_servers = " + install.toml_value(existing) + '\nother = "keep me"\n'
        quoted = (
            '[mcp_servers."sample.dot"]\ncommand = "keep"\n'
            '[mcp_servers."antigravity_bridge"] # from an earlier install\n'
            'command = "old"\nargs = ' + install.toml_value(existing[install.CODEX]["args"]) + '\n'
            '"A.B" = "keep top level"\n'
            '[mcp_servers."antigravity_bridge"."env"]\nPYTHONUTF8 = "0"\nEXISTING = "keep"\n"A.B" = "keep env"\n'
        )
        for source in (inline, quoted):
            with self.subTest(source=source.splitlines()[0]):
                original = tomllib.loads(source)
                updated = install.edit_toml(source.encode(), self.layout.server, entry, {})
                self.assertIn(b"[mcp_servers.antigravity_bridge.env]", updated)
                if source == quoted:
                    self.assertIn(b'"A.B" = "keep top level"', updated)
                    self.assertIn(b'"A.B" = "keep env"', updated)
                parsed = tomllib.loads(updated.decode())
                previous = original["mcp_servers"].pop(install.CODEX)
                expected_entry = {**previous, **entry, "env": {**previous.get("env", {}), **entry["env"]}}
                self.assertEqual(parsed["mcp_servers"].pop(install.CODEX), expected_entry)
                self.assertEqual(parsed, original)
                self.assertEqual(install.edit_toml(updated, self.layout.server, entry, {}), updated)
                removed = install.edit_toml(updated, self.layout.server, None, {})
                self.assertEqual(tomllib.loads(removed.decode()), original)

    def test_unrelated_reserved_name_is_preserved(self):
        original = b'{"mcpServers":{"chatgpt-bridge":{"command":"another service","args":["unrelated"]}}}'
        self.assertEqual(install.edit_json(original, self.layout.server, None, {}), original)
        with self.assertRaisesRegex(ValueError, "different service"):
            install.edit_json(original, self.layout.server, install.registry_entry(Path(PYTHON), self.layout.server, "antigravity"), {})

    def test_cli_uses_locale_or_explicit_language(self):
        with mock.patch.dict(os.environ, {"LC_ALL": "zh_CN.UTF-8"}, clear=True):
            self.assertEqual(install.detect_language(), "zh")
            self.assertEqual(install.detect_language("en"), "en")
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=True):
            self.assertEqual(install.detect_language(), "en")
            self.assertEqual(install.detect_language("zh"), "zh")


@unittest.skipUnless(os.name == "nt", "The bridge uses the Windows file mutex")
class BridgeLogicTests(TemporaryHomeTest):
    def setUp(self):
        super().setUp()
        from bridge import bridge_server
        self.bridge = bridge_server
        bridge_dir = self.layout.bridge
        bus = bridge_dir / "bus"
        patches = mock.patch.multiple(bridge_server, BASE_DIR=bridge_dir, BUS_DIR=bus,
                                      TO_AGY_FILE=bus / "to_antigravity.json",
                                      LATEST_TO_CODEX_FILE=bus / "to_codex_latest.json",
                                      LOCK_FILE=bridge_dir / "session_lock.json")
        patches.start()
        self.addCleanup(patches.stop)

    def test_notifications_retry_and_only_notify_new_messages_for_bound_workspace(self):
        from bridge import notify_inbox

        bus = self.layout.bridge / "bus"
        workspace = self.layout.home / "bound repository"
        conversation_id = "12345678-1234-1234-1234-123456789abc"
        for request_id, submitted_from in (
            ("req_9_aabbcc", workspace),
            ("req_10_aabbcc", self.layout.home / "another repository"),
            ("req_11_aabbcc", workspace),
        ):
            self.bridge._atomic_write_json(self.bridge._request_path(request_id), {
                "request_id": request_id, "status": "pending", "workspace_path": str(submitted_from),
                "step_summary": "Read it through MCP",
            })
        with mock.patch.multiple(notify_inbox, BUS_DIR=bus, bridge=self.bridge,
                                 tool_check_codex_inbox=self.bridge.tool_check_codex_inbox):
            with mock.patch.object(notify_inbox, "native_call", side_effect=[
                OSError("not ready"), {"sendMessage": {"recipientId": conversation_id}},
            ]) as send:
                args = (str(workspace), conversation_id, 10, "agentapi")
                self.assertIsNone(notify_inbox.notify_pending(*args))
                self.assertEqual(self.bridge._delivery_status("req_11_aabbcc")["status"], "notification_failed")
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(notify_inbox.notify_pending(*args), "req_11_aabbcc")
                self.assertIsNone(notify_inbox.notify_pending(*args))
            self.assertEqual(send.call_count, 2)
            command = send.call_args.args
            self.assertEqual(command[:3], ("agentapi", "send-message", conversation_id))
            self.assertIn("req_11_aabbcc", command[3])
            self.assertNotIn("Read it through MCP", command[3])

    def test_late_receipt_is_notified_by_queued_time_after_workspace_pairing(self):
        from bridge import notify_inbox

        workspace = self.layout.home
        cid = "12345678-1234-1234-1234-123456789abc"
        project_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        request_id = "req_3_aabbcc"
        bridge = self.bridge
        paired = bridge.tool_workspace_pair("link", str(workspace), cid, project_id)
        self.assertEqual(paired["route"]["status"], "verifying")
        bridge._atomic_write_json(bridge._request_path(request_id), {
            "request_id": request_id, "status": "pending", "workspace_path": str(workspace),
            "queued_at_ms": paired["route"]["since_ms"] + 1,
        })
        meta = {"conversationMetadata": {"metadata": {"workspaceUris": [workspace.as_uri()], "projectId": project_id}}}
        with mock.patch.multiple(notify_inbox, BUS_DIR=bridge.BUS_DIR, bridge=bridge,
                                 tool_check_codex_inbox=bridge.tool_check_codex_inbox):
            with mock.patch.object(notify_inbox, "native_call", side_effect=[
                meta, meta, {"sendMessage": {"recipientId": cid}},
            ]):
                notify_inbox.reconcile_routes("agentapi")
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(notify_inbox.notify_pending(str(workspace), cid, 100, "agentapi"), request_id)
        self.assertEqual(bridge.tool_workspace_pair("get", str(workspace))["route"]["status"], "confirmed")
        self.assertEqual(bridge._delivery_status(request_id)["status"], "notified")

    def test_parent_route_does_not_override_the_installed_workspace_target(self):
        from bridge import notify_inbox

        bridge = self.bridge
        workspace = self.layout.home / "installed workspace"
        installed = "12345678-1234-1234-1234-123456789abc"
        parent = "12345678-1234-1234-1234-123456789def"
        bridge._atomic_write_json(bridge.BUS_DIR / "workspace_routes.json", {
            str(self.layout.home): {"workspace_path": str(self.layout.home), "conversation_id": parent,
                                    "since_ms": 0, "status": "confirmed"},
        })
        request_id = "req_10_aabbcc"
        bridge._atomic_write_json(bridge._request_path(request_id), {
            "request_id": request_id, "status": "pending", "workspace_path": str(workspace), "queued_at_ms": 10,
        })
        with mock.patch.multiple(notify_inbox, BUS_DIR=bridge.BUS_DIR, bridge=bridge,
                                 tool_check_codex_inbox=bridge.tool_check_codex_inbox):
            with mock.patch.object(notify_inbox, "native_call",
                                   return_value={"sendMessage": {"recipientId": installed}}) as send:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(notify_inbox.notify_pending(str(workspace), installed, 0, "agentapi"), request_id)
                self.assertEqual(send.call_args.args[2], installed)

    def test_open_blocks_legacy_creation_and_keeps_confirmed_route(self):
        from bridge import notify_inbox

        bridge = self.bridge
        workspace = self.layout.home
        cid = "12345678-1234-1234-1234-123456789abc"
        project_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        route = {"workspace_path": str(workspace), "conversation_id": cid, "status": "confirmed",
                 "project_id": project_id, "expected_project_id": project_id, "since_ms": 0}
        key = os.path.normcase(str(workspace))
        bridge._atomic_write_json(bridge.BUS_DIR / "workspace_routes.json", {
            key: {**route, "open_attempt": {"status": "opening"}},
        })
        with mock.patch.multiple(notify_inbox, BUS_DIR=bridge.BUS_DIR, bridge=bridge,
                                 tool_check_codex_inbox=bridge.tool_check_codex_inbox):
            with mock.patch.object(notify_inbox, "native_call") as native:
                opening = bridge.tool_workspace_pair("open", str(workspace))
                self.assertEqual(opening["error_code"], bridge.NATIVE_PROJECT_SESSION_REQUIRED)
                self.assertEqual(opening["project_id"], project_id)
                notify_inbox.reconcile_routes("agentapi")
                self.assertEqual(bridge.tool_workspace_pair("get", str(workspace))["route"], {
                    **route, "open_attempt": {"status": "blocked", "error_code": bridge.NATIVE_PROJECT_SESSION_REQUIRED},
                })
                native.assert_not_called()

    def test_pair_checks_project_and_directory_and_rechecks_before_delivery(self):
        from bridge import notify_inbox

        bridge = self.bridge
        workspace = self.layout.home
        subfolder = workspace / "Source"
        subfolder.mkdir()
        cid = "12345678-1234-1234-1234-123456789abc"
        project_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        meta = {"workspaceUris": [workspace.as_uri()], "projectId": project_id}
        wrong_project = {**meta, "projectId": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"}
        wrong_root = {**meta, "workspaceUris": []}
        self.assertEqual(bridge.tool_workspace_pair("link", str(workspace), cid)["error_code"],
                         "ANTIGRAVITY_PROJECT_ID_REQUIRED")
        submit = {"workspace_path": str(subfolder), "step_summary": "Read-only test", "wait_for_reply": False}
        with mock.patch.multiple(notify_inbox, BUS_DIR=bridge.BUS_DIR, bridge=bridge,
                                 tool_check_codex_inbox=bridge.tool_check_codex_inbox):
            with mock.patch.object(notify_inbox, "native_call") as native:
                for metadata, code in [
                    (wrong_project, "ANTIGRAVITY_PROJECT_UNCONFIRMED"),
                    (wrong_root, "ANTIGRAVITY_WORKSPACE_UNCONFIRMED"),
                    (meta, None),
                ]:
                    native.return_value = {"conversationMetadata": {"metadata": metadata}}
                    bridge.tool_workspace_pair("link", str(workspace), cid, project_id)
                    self.assertEqual(bridge.codex_tool_submit_for_review(**submit)["error_code"],
                                     "ANTIGRAVITY_PROJECT_UNCONFIRMED")
                    notify_inbox.reconcile_routes("agentapi")
                    route = bridge.tool_workspace_pair("get", str(workspace))["route"]
                    self.assertEqual(route.get("error_code"), code)
                    if code:
                        self.assertEqual(route["status"], "blocked")
                        self.assertEqual(bridge.codex_tool_submit_for_review(**submit)["error_code"], code)
                self.assertEqual(route["status"], "confirmed")
                request_id = bridge.codex_tool_submit_for_review(**submit)["request_id"]
                pending = bridge.tool_check_codex_inbox(request_id)["submission"]
                self.assertEqual(pending["target_project_id"], project_id)
                self.assertEqual(pending["target_conversation_id"], cid)
                guarded = bridge.codex_tool_submit_for_review(**submit, required_model="gemini-4-neon",
                                                              target_conversation_id=cid)
                self.assertTrue(guarded["ok"])

                native.reset_mock()
                native.return_value = {"conversationMetadata": {"metadata": wrong_project}}
                self.assertIsNone(notify_inbox.notify_pending(str(subfolder), cid, 0, "agentapi"))
                self.assertIsNone(notify_inbox.notify_pending(str(subfolder), cid, 0, "agentapi"))
                native.assert_called_once_with("agentapi", "get-conversation-metadata", cid)
                self.assertEqual(bridge._delivery_status(request_id)["error_code"], "ANTIGRAVITY_PROJECT_UNCONFIRMED")
                self.assertEqual(bridge.tool_workspace_pair("get", str(workspace))["route"]["status"], "blocked")
                self.assertEqual(bridge.codex_tool_submit_for_review(**submit)["error_code"],
                                 "ANTIGRAVITY_PROJECT_UNCONFIRMED")

    def test_directory_only_route_blocks_the_installed_target_for_the_same_root(self):
        from bridge import notify_inbox

        bridge = self.bridge
        workspace = self.layout.home
        cid = "12345678-1234-1234-1234-123456789abc"
        subfolder = workspace / "Source"
        subfolder.mkdir()
        queued = bridge.codex_tool_submit_for_review(step_summary="Before upgrade", workspace_path=str(subfolder),
                                                     wait_for_reply=False)
        bridge._atomic_write_json(bridge.BUS_DIR / "workspace_routes.json", {
            os.path.normcase(str(workspace)): {"workspace_path": str(workspace), "conversation_id": cid,
                                              "status": "confirmed", "since_ms": 0},
        })
        route = bridge.tool_workspace_pair("get", str(workspace))["route"]
        self.assertEqual(route["status"], "blocked")
        self.assertFalse(bridge.codex_tool_submit_for_review(step_summary="After upgrade", workspace_path=str(subfolder),
                                                           wait_for_reply=False)["ok"])
        with mock.patch.multiple(notify_inbox, BUS_DIR=bridge.BUS_DIR, bridge=bridge,
                                 tool_check_codex_inbox=bridge.tool_check_codex_inbox):
            with mock.patch.object(notify_inbox, "native_call") as native:
                notify_inbox.reconcile_routes("agentapi")
                self.assertIsNone(notify_inbox.notify_pending(f"{workspace}{os.sep}.", cid, 0, "agentapi"))
                native.assert_not_called()
        self.assertFalse(bridge._delivery_status(queued["request_id"]))

    def test_link_and_reconcile_reject_non_native_projects(self):
        from bridge import notify_inbox

        bridge = self.bridge
        workspace = self.layout.home
        cid = "12345678-1234-1234-1234-123456789abc"
        project_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        project_path = bridge.BASE_DIR.parent.parent / "projects" / f"{project_id}.json"
        project = {"id": project_id, "projectResources": {"resources": [{"folderUri": workspace.as_uri()}]}}
        sidecar = {**project, "sidecar": {"sidecarId": "duplicate"}}
        for bad, error in (
            (sidecar, "ANTIGRAVITY_SIDECAR_PROJECT_FORBIDDEN"),
            ({**project, "isWorkspaceOnly": True}, "ANTIGRAVITY_SIDECAR_PROJECT_FORBIDDEN"),
            ({**project, "projectResources": {"resources": []}}, "ANTIGRAVITY_WORKSPACE_UNCONFIRMED"),
        ):
            bridge._atomic_write_json(project_path, bad)
            self.assertEqual(bridge.tool_workspace_pair("link", str(workspace), cid, project_id)["error_code"], error)
        bridge._atomic_write_json(project_path, project)
        self.assertTrue(bridge.tool_workspace_pair("link", str(workspace), cid, project_id)["ok"])
        bridge._atomic_write_json(project_path, sidecar)
        with mock.patch.multiple(notify_inbox, BUS_DIR=bridge.BUS_DIR, bridge=bridge):
            with mock.patch.object(notify_inbox, "native_call") as native:
                notify_inbox.reconcile_routes("agentapi")
                native.assert_not_called()
        route = bridge.tool_workspace_pair("get", str(workspace))["route"]
        self.assertEqual(route["status"], "blocked")
        self.assertEqual(route["error_code"], "ANTIGRAVITY_SIDECAR_PROJECT_FORBIDDEN")

    def test_paired_request_requires_host_conversation_for_claim_and_reply(self):
        from bridge import agy_model_guard as guard

        bridge = self.bridge
        workspace = self.layout.home
        cid = "12345678-1234-1234-1234-123456789abc"
        project_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        bridge._atomic_write_json(bridge.BUS_DIR / "workspace_routes.json", {
            os.path.normcase(str(workspace)): {"workspace_path": str(workspace), "conversation_id": cid,
                                              "status": "confirmed", "since_ms": 0, "project_id": project_id,
                                              "expected_project_id": project_id},
        })
        summary = "Payload for the paired conversation"
        request_id = bridge.codex_tool_submit_for_review(step_summary=summary, workspace_path=str(workspace),
                                                         wait_for_reply=False)["request_id"]
        self.assertNotIn(summary, json.dumps(bridge.tool_check_codex_inbox(request_id)))
        self.assertEqual(bridge.tool_claim_codex_request(request_id)["error_code"], "ANTIGRAVITY_TARGET_UNVERIFIED")
        self.assertEqual(bridge.tool_reply_to_codex(request_id)["error_code"], "ANTIGRAVITY_TARGET_UNVERIFIED")
        with mock.patch.object(guard, "bridge", bridge):
            for tool in ("bridge_claim_codex_request", "bridge_reply_to_codex"):
                event = {"conversationId": "another", "toolCall": {
                    "name": f"mcp__chatgpt-bridge__{tool}", "args": {"request_id": request_id},
                }}
                self.assertEqual(guard.pre_tool(event)["reason"], "ANTIGRAVITY_TARGET_MISMATCH")
            claim = guard.pre_tool({"conversationId": cid, "toolCall": {
                "name": "mcp__chatgpt-bridge__bridge_claim_codex_request", "args": {"request_id": request_id},
            }})["overwrite"]
            self.assertEqual(bridge.tool_claim_codex_request(**claim)["submission"]["step_summary"], summary)
            reply = guard.pre_tool({"conversationId": cid, "toolCall": {
                "name": "mcp__chatgpt-bridge__bridge_reply_to_codex", "args": {},
            }})["overwrite"]
            self.assertTrue(bridge.tool_reply_to_codex(**reply)["ok"])
        receipt = bridge.codex_tool_fetch_director_reply(request_id)
        self.assertEqual(receipt["target_conversation_id"], cid)
        self.assertEqual(receipt["target_project_id"], project_id)

    def test_model_guard_requires_host_proof_for_claim_and_reply(self):
        from bridge import agy_model_guard as guard, notify_inbox

        bridge = self.bridge
        workspace = self.layout.home
        cid = "12345678-1234-1234-1234-123456789abc"
        project_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        args = {"workspace_path": str(workspace), "required_model": "gemini-4-neon",
                "target_conversation_id": cid, "step_summary": "Guarded task payload"}
        self.assertEqual(bridge.codex_tool_submit_for_review(**args)["error_code"], "ANTIGRAVITY_TARGET_UNCONFIRMED")
        bridge._atomic_write_json(bridge.BUS_DIR / "workspace_routes.json", {
            os.path.normcase(str(workspace)): {"workspace_path": str(workspace), "conversation_id": cid,
                                              "status": "confirmed", "since_ms": 0, "project_id": project_id,
                                              "expected_project_id": project_id},
        })
        sent = bridge.codex_tool_submit_for_review(**args)
        request_id = sent["request_id"]
        inbox = bridge.tool_check_codex_inbox(request_id)
        self.assertNotIn("Guarded task payload", json.dumps(inbox))
        event = {"toolCall": {"name": "mcp__chatgpt-bridge__bridge_claim_codex_request",
                             "args": {"request_id": request_id}},
                 "conversationId": cid, "modelName": "gemini-4-hydrogen-tiered-high"}
        with mock.patch.object(guard, "bridge", bridge):
            self.assertEqual(guard.pre_tool({**event, "conversationId": "another"})["reason"], "ANTIGRAVITY_TARGET_MISMATCH")
            self.assertEqual(guard.pre_tool(event)["reason"], "ANTIGRAVITY_MODEL_MISMATCH")
            self.assertEqual(bridge.codex_tool_fetch_director_reply(request_id)["delivery"]["status"], "blocked")
            self.assertFalse(bridge.tool_claim_codex_request(request_id)["ok"])
            event["modelName"] = "gemini-4-neon-tiered-high"
            wrapped = {**event, "toolCall": {"name": "call_mcp_tool", "args": {
                "ServerName": '"chatgpt-bridge"', "ToolName": '"bridge_claim_codex_request"',
                "Arguments": json.dumps({"request_id": request_id}),
            }}}
            proof = json.loads(guard.pre_tool(wrapped)["overwrite"]["Arguments"])["model_proof"]
            self.assertNotIn(proof, json.dumps(bridge.codex_tool_fetch_director_reply(request_id)))
            second = bridge.codex_tool_submit_for_review(**args)["request_id"]
            second_event = {**event, "toolCall": {**event["toolCall"], "args": {"request_id": second}}}
            raced_proof = guard.pre_tool(second_event)["overwrite"]["model_proof"]
            claim = bridge.tool_claim_codex_request(request_id, proof)
            self.assertEqual(claim["submission"]["step_summary"], args["step_summary"])
            claimed = dict(bridge._delivery_status(request_id))
            self.assertEqual(bridge.tool_claim_codex_request(request_id, proof)["error_code"],
                             "ANTIGRAVITY_REQUEST_ALREADY_CLAIMED")
            duplicate = guard.pre_tool({**event, "modelName": "gemini-4-neon-low"})
            self.assertEqual(duplicate["reason"], "ANTIGRAVITY_REQUEST_ALREADY_CLAIMED")
            self.assertEqual(bridge._delivery_status(request_id), claimed)
            self.assertEqual(bridge.tool_claim_codex_request(second, raced_proof)["error_code"],
                             "ANTIGRAVITY_CONVERSATION_BUSY")
            self.assertEqual(guard.pre_tool(second_event)["reason"], "ANTIGRAVITY_CONVERSATION_BUSY")
            second_state = bridge._read_json_safe(bridge._delivery_path(second))
            self.assertNotIn("claim_proof", second_state)
            self.assertEqual(second_state["status"], "queued")
            self.assertTrue(bridge.codex_cancel_guarded_review(second)["ok"])
            self.assertTrue(bridge._active_model_path(cid).is_file())
            self.assertFalse(bridge.codex_cancel_guarded_review(request_id)["ok"])

            wrong_model = {**event, "modelName": "gemini-4-carbon-medium"}
            self.assertEqual(guard.pre_invocation(wrong_model)["terminationBehavior"], "terminate")
            self.assertEqual(bridge._delivery_status(request_id)["status"], "blocked")
            with mock.patch.multiple(notify_inbox, BUS_DIR=bridge.BUS_DIR, bridge=bridge,
                                     tool_check_codex_inbox=bridge.tool_check_codex_inbox):
                with mock.patch.object(notify_inbox, "native_call") as send:
                    self.assertIsNone(notify_inbox.notify_pending(str(workspace), cid, 0, "agentapi"))
                    send.assert_not_called()
            self.assertEqual(guard.pre_invocation(event), {})
            recovered = bridge._delivery_status(request_id)
            self.assertEqual(recovered["status"], "claimed")
            self.assertNotIn("error_code", recovered)
            self.assertFalse(bridge.tool_reply_to_codex(request_id, review_feedback="Done")["ok"])
            wrapped_reply = {**event, "toolCall": {"name": "call_mcp_tool", "args": {
                "ServerName": '"chatgpt-bridge"', "ToolName": '"bridge_reply_to_codex"',
                "Arguments": json.dumps({"review_feedback": "Done"}),
            }}}
            reply_args = json.loads(guard.pre_tool(wrapped_reply)["overwrite"]["Arguments"])
            self.assertEqual(reply_args["request_id"], request_id)
            reply = bridge.tool_reply_to_codex(**reply_args)
            self.assertEqual(reply["reply"]["execution_model"], event["modelName"])
            self.assertEqual(bridge.codex_tool_fetch_director_reply(request_id)["status"], "replied")
            self.assertFalse(bridge._active_model_path(cid).exists())

            third = bridge.codex_tool_submit_for_review(**args)["request_id"]
            third_event = {**event, "toolCall": {**event["toolCall"], "args": {"request_id": third}}}
            token = guard.pre_tool(third_event)["overwrite"]["model_proof"]
            self.assertTrue(bridge.tool_claim_codex_request(third, token)["ok"])
            self.assertEqual(guard.pre_invocation(wrong_model)["terminationBehavior"], "terminate")
            self.assertTrue(bridge.codex_cancel_guarded_review(third)["ok"])
            self.assertEqual(bridge.codex_tool_fetch_director_reply(third)["status"], "cancelled")
            self.assertFalse(bridge._active_model_path(cid).exists())
            self.assertEqual(guard.pre_invocation(wrong_model), {})

    def test_headless_reuses_exact_workspace_and_defaults_to_natural(self):
        bridge = self.bridge
        initial = bridge.tool_bind_session(workspace_path=str(self.layout.home), reuse_workspace_binding=True)
        reused = bridge.tool_bind_session(workspace_path=str(self.layout.home / "."), reuse_workspace_binding=True)
        self.assertEqual(initial["binding_id"], reused["binding_id"])
        self.assertTrue(reused["reused"])
        with mock.patch.object(bridge.HeadlessCodexRunner, "dispatch", return_value={
            "ok": True, "raw_reply": "完成了。",
        }) as dispatch:
            result = bridge.tool_dispatch_task("查看工作区", "只读查看当前目录。",
                                              binding_id=reused["binding_id"], wait_for_result=False)
        self.assertNotIn("[DIRECTOR_TASK]", dispatch.call_args.kwargs["envelope"])
        self.assertEqual(dispatch.call_args.kwargs["timeout_sec"], 0)
        self.assertEqual(result["parsed_reply"]["summary"], "完成了。")

    def test_codex_model_update_is_per_binding(self):
        bridge = self.bridge
        first = bridge.tool_bind_session(workspace_path=str(self.layout.home))
        second = bridge.tool_bind_session(workspace_path=str(self.layout.home))
        with mock.patch.object(bridge, "_codex_catalog", return_value=[
            {"slug": "gpt-6.2-sol"}, {"slug": "gpt-6.2-astra"},
        ]):
            changed = bridge.tool_codex_session_model(first["binding_id"], "set", "gpt-6.2-astra")
            self.assertEqual(changed["next_model"], "gpt-6.2-astra")
            self.assertIsNone(bridge.tool_codex_session_model(second["binding_id"])["next_model"])
            self.assertEqual(bridge.tool_codex_session_model(first["binding_id"], "set", "unknown")["error_code"],
                             "CODEX_MODEL_UNAVAILABLE")
            self.assertEqual(bridge.tool_codex_session_model(first["binding_id"])["next_model"], "gpt-6.2-astra")

    def test_routing_catalog_reads_isolated_cache_and_persists_profiles(self):
        home = self.layout.codex.parent
        get = {"codex_home": str(home)}
        fallback = self.bridge._dispatch_mcp_tool("codex", "agy_routing_settings", get)
        self.assertTrue(fallback["settings"]["ask_model_roles_on_plan"])
        self.assertFalse((self.layout.bridge / "director_settings.json").exists())
        self.assertEqual([tier["id"] for tier in fallback["codex_catalog"][0]["speed_tiers"]],
                         ["standard", "fast", "ultrafast"])
        self.assertNotIn("[ROUTING:", self.bridge.build_director_envelope("general", "goal", "instructions"))
        self.seed(self.layout.codex, b'model = "gpt-6.2-sol"\nmodel_reasoning_effort = "high"\nservice_tier = "ultrafast"\n')
        cache = {"models": [
            {"slug": "slow", "visibility": "list", "priority": 4, "additional_speed_tiers": ["unknown"]},
            {"slug": "hidden", "visibility": "hide", "priority": 0},
            {"slug": "gpt-6.2-sol", "display_name": "GPT-6.2-Sol", "visibility": "list", "priority": 1,
             "description": "General purpose",
             "default_reasoning_level": "low", "supported_reasoning_levels": [{"effort": "xhigh"}, {"effort": "ultra"}],
             "additional_speed_tiers": ["default", "fast"],
             "service_tiers": [{"id": "priority", "description": "2x speed"},
                               {"id": "ultrafast", "description": "The fastest available responses for latency-sensitive work."}]},
        ]}
        self.seed(home / "models_cache.json", json.dumps(cache).encode())
        configured = self.bridge._dispatch_mcp_tool("antigravity", "bridge_routing_settings", {
            **get, "action": "set", "ask_model_roles_on_plan": False, "decision_side": "codex",
            "decision_model": "gpt-6.2-sol", "decision_effort": "ultra", "decision_speed": "ultrafast",
            "worker_side": "antigravity", "worker_model": "gemini-4-carbon", "worker_effort": "medium",
            "worker_speed": "priority",
        })
        observed = self.bridge._dispatch_mcp_tool("codex", "agy_routing_settings", get)
        self.assertEqual(configured, observed)
        self.assertEqual(configured["settings"], json.loads((self.layout.bridge / "director_settings.json").read_bytes()))
        self.assertFalse(configured["settings"]["ask_model_roles_on_plan"])
        self.assertEqual(configured["codex_current_defaults"], {
            "model": "gpt-6.2-sol", "model_reasoning_effort": "high", "service_tier": "ultrafast",
        })
        sol, slow = configured["codex_catalog"]
        self.assertEqual([sol["slug"], slow["slug"]], ["gpt-6.2-sol", "slow"])
        self.assertEqual(sol["efforts"], ["xhigh", "ultra"])
        self.assertEqual([(tier["id"], tier["label"]) for tier in sol["speed_tiers"]],
                         [("standard", "标准"), ("fast", "快速"), ("ultrafast", "超快")])
        self.assertEqual(sol["speed_tiers"][1]["description"], "2x speed")
        self.assertEqual([tier["id"] for tier in slow["speed_tiers"]], ["standard"])
        self.assertEqual(len(configured["antigravity_catalog"]["models"]), 8)
        self.assertIn('swarm(action="models", all=true)', configured["antigravity_catalog"]["quota_hint"])
        self.assertIn("[ROUTING: decision=codex:gpt-6.2-sol(effort=ultra,speed=ultrafast) | "
                      "worker=antigravity:gemini-4-carbon(effort=medium,speed=fast)]",
                      self.bridge.build_director_envelope("general", "goal", "instructions"))
        self.assertIn("[ROUTING: explicit]", self.bridge.build_director_envelope(
            "general", "goal", "instructions", routing_tag="explicit"))
        self.assertEqual(self.bridge._dispatch_mcp_tool("codex", "agy_routing_settings", {
            **get, "action": "set", "worker_side": "unknown"
        })["error_code"], "INVALID_ARGUMENTS")

    def test_multiple_review_requests_require_id_and_can_be_filtered(self):
        self.assertEqual(self.bridge.tool_reply_to_codex()["error_code"], "NO_PENDING_REQUESTS")
        first_id, second_id = "req_1_aaaaaa", "req_2_bbbbbb"
        for req_id, directory, submitted_at in ((first_id, "One", "2026-01-01"),
                                               (second_id, "Two", "2026-01-02")):
            self.seed(self.bridge.BUS_DIR / "requests" / f"{req_id}.json", json.dumps({
                "request_id": req_id, "status": "pending", "workspace_path": str(self.layout.home / directory),
                "step_summary": directory, "submitted_at": submitted_at,
            }).encode())
        inbox = self.bridge.tool_check_codex_inbox()
        self.assertEqual(inbox["oldest_pending"]["request_id"], first_id)
        self.assertEqual(inbox["latest_pending"]["request_id"], second_id)
        self.assertEqual([item["step_summary"] for item in inbox["pending_summaries"]], ["One", "Two"])
        filtered = self.bridge._dispatch_mcp_tool("antigravity", "bridge_check_codex_inbox", {
            "workspace_path": str(self.layout.home / "nested" / ".." / "tWO")
        })
        self.assertEqual(filtered["pending_request_ids"], [second_id])
        with mock.patch.object(self.bridge, "_run_ps_driver", return_value={"ok": True}):
            self.assertEqual(self.bridge.tool_inspect_app()["pending_codex_inbox"]["latest_pending"]["request_id"], second_id)
        self.assertEqual(self.bridge.tool_reply_to_codex()["error_code"], "AMBIGUOUS_PENDING_REQUESTS")
        invalid = self.bridge.tool_reply_to_codex("req_3_cccccc")
        self.assertEqual(invalid["error_code"], "UNKNOWN_REQUEST_ID")
        self.assertFalse(self.bridge._reply_path("req_3_cccccc").exists())
        self.assertTrue(self.bridge.tool_reply_to_codex(second_id)["ok"])
        self.assertTrue(self.bridge.tool_reply_to_codex(second_id)["already_replied"])
        self.assertEqual(self.bridge.tool_reply_to_codex(second_id, "REWORK_REQUIRED")["error_code"], "ALREADY_REPLIED")
        self.assertEqual(self.bridge.tool_reply_to_codex()["reply"]["request_id"], first_id)

    def test_read_only_dispatch_has_one_scoped_receipt_and_persistent_acknowledgement(self):
        bridge = self.bridge
        workspace = str(self.layout.home)
        binding = bridge.tool_bind_session(workspace_path=workspace, transport="codex_exec")
        with mock.patch.object(bridge.HeadlessCodexRunner, "dispatch", return_value={
            "ok": True, "timed_out": True, "status": "running_async",
        }) as dispatch:
            result = bridge.tool_dispatch_task(
                "核对分支", "根据所附输出汇报分支", binding_id=binding["binding_id"],
                repository_read_only=True, authorize_receipt=True,
            )
        dispatch_id = result["dispatch_id"]
        self.assertEqual(result["lifecycle"]["execution"]["status"], "awaiting_completion")
        self.assertEqual(result["lifecycle"]["receipt"]["status"], "awaiting")
        self.assertTrue(dispatch.call_args.kwargs["repository_read_only"])
        self.assertTrue(dispatch.call_args.kwargs["envelope"].startswith("$stop-that-shit change --"))
        self.assertIn("[RECEIPT_AUTHORIZATION: once]", dispatch.call_args.kwargs["envelope"])
        scoped = bridge._scoped_codex_tools(dispatch_id)
        self.assertEqual({tool["name"] for tool in scoped}, {
            "agy_submit_dispatch_receipt", "agy_fetch_director_reply",
        })
        self.assertEqual(bridge._dispatch_mcp_tool(
            "codex", "agy_submit_for_review", {"step_summary": "generic"}, dispatch_id,
        )["error_code"], "DISPATCH_SCOPE_FORBIDS_TOOL")
        self.assertEqual(bridge.codex_tool_submit_for_review(
            "generic", dispatch_id=dispatch_id, wait_for_reply=False,
        )["error_code"], "SCOPED_RECEIPT_REQUIRED")
        self.assertEqual(bridge._dispatch_mcp_tool(
            "codex", "agy_submit_dispatch_receipt", {"step_summary": "result"},
        )["error_code"], "UNKNOWN_CODEX_TOOL")

        args = {"step_summary": "main tracks origin/main"}
        def submit(arguments):
            return bridge._dispatch_mcp_tool("codex", "agy_submit_dispatch_receipt", arguments, dispatch_id)

        self.assertEqual(submit({**args, "workspace_path": "elsewhere"})["error_code"], "INVALID_ARGUMENTS")
        queued = submit(args)
        request_id = queued["request_id"]
        self.assertFalse(queued["already_queued"])
        self.assertEqual(queued["dispatch_id"], dispatch_id)
        self.assertEqual(submit(args)["request_id"], request_id)
        self.assertTrue(submit(args)["already_queued"])
        self.assertEqual(submit({"step_summary": "second"})["error_code"], "RECEIPT_ALREADY_SUBMITTED")
        self.assertEqual(len(list((bridge.BUS_DIR / "requests").glob("*.json"))), 1)

        dispatch.call_args.kwargs["on_complete"]({
            "ok": True, "codex_session_id": "codex-session", "turn_id": "turn",
        })
        status = bridge.tool_dispatch_status(dispatch_id)
        self.assertEqual(status["execution"]["status"], "completed")
        self.assertEqual(status["receipt"]["status"], "queued")
        inbox = bridge.tool_check_codex_inbox(request_id, workspace)["submission"]
        self.assertEqual(inbox["dispatch_id"], dispatch_id)
        self.assertEqual(inbox["step_summary"], args["step_summary"])
        self.assertEqual(bridge.tool_dispatch_status(dispatch_id)["receipt"]["status"], "queued")
        reply = bridge.tool_reply_to_codex(request_id, review_feedback="确认收到")["reply"]
        self.assertEqual(reply["dispatch_id"], dispatch_id)
        self.assertEqual(bridge._dispatch_mcp_tool(
            "codex", "agy_fetch_director_reply", {}, dispatch_id,
        ), reply)
        self.assertTrue(bridge.tool_unbind_session(binding["binding_id"])["ok"])
        status = bridge.tool_dispatch_status(dispatch_id)
        self.assertEqual(status["receipt"]["status"], "acknowledged")
        self.assertEqual(status["receipt"]["acknowledged_at"], reply["replied_at"])

    def test_read_only_dispatch_does_not_grant_a_receipt_implicitly(self):
        bridge = self.bridge
        binding = bridge.tool_bind_session(workspace_path=str(self.layout.home), transport="codex_exec")
        self.assertEqual(bridge.tool_dispatch_task(
            "goal", "instructions", binding_id=binding["binding_id"], authorize_receipt=True,
        )["error_code"], "RECEIPT_REQUIRES_READ_ONLY")
        with mock.patch.object(bridge.HeadlessCodexRunner, "dispatch", return_value={"ok": True}):
            result = bridge.tool_dispatch_task(
                "goal", "instructions", binding_id=binding["binding_id"], repository_read_only=True,
            )
        self.assertEqual(result["lifecycle"]["receipt"]["status"], "not_authorized")
        self.assertEqual(bridge._dispatch_mcp_tool(
            "codex", "agy_submit_dispatch_receipt", {"step_summary": "result"}, result["dispatch_id"],
        )["error_code"], "DISPATCH_SCOPE_FORBIDS_TOOL")

    def test_wait_only_new_skips_initial_requests(self):
        old = {"request_id": "req_1_aaaaaa"}
        new = {"request_id": "req_2_bbbbbb"}
        stdout = io.StringIO()
        with mock.patch.object(self.bridge, "_pending_submissions", side_effect=[[old], [old, new]]), redirect_stdout(stdout):
            self.bridge.run_cli(["wait_inbox", "--only-new"])
        self.assertEqual(json.loads(stdout.getvalue())["submission"], new)

    def test_protocol_does_not_parse_echo_or_inline_tags(self):
        echo = self.bridge.build_director_envelope("general", "goal", "instructions\n[SUMMARY]\n[FILES]")
        self.assertFalse(self.bridge.parse_worker_reply(echo)["structured_protocol_matched"])
        self.assertFalse(self.bridge.parse_worker_reply("Echo mentioning [SUMMARY] and [FILES]")["structured_protocol_matched"])
        self.assertEqual(self.bridge.parse_worker_reply("[SUMMARY]\nA legacy summary.")["summary"],
                         "A legacy summary.")
        reply = echo + "\r\n## [SUMMARY]\r\nactual reply\r\n[FILES]\r\nbridge/bridge_server.py\r\n[OPEN_QUESTIONS]\r\nNONE\r\n"
        result = self.bridge.parse_worker_reply(reply)
        self.assertTrue(result["structured_protocol_matched"])
        self.assertEqual(result["summary"], "actual reply")
        self.assertFalse(result["has_open_questions"])

    def test_minimized_binding_and_authoritative_first_title(self):
        snapshot = {"active_session": "我们应该在这个项目中做什么", "is_minimized": True, "hwnd": 9, "pid": 20}
        with mock.patch.object(self.bridge, "_run_ps_driver", return_value={"ok": True, "snapshot": snapshot}):
            bound = self.bridge.tool_bind_session(create_in_project="Sample", hwnd=9)
        self.assertTrue(bound["lock"]["awaiting_first_title"])
        with mock.patch.object(self.bridge, "_run_ps_driver", return_value={
            "ok": False, "timed_out": True, "title_authoritative": False, "active_session": "内容预览"
        }) as driver:
            dispatched = self.bridge.tool_dispatch_task("goal", "instructions")
        self.assertTrue(dispatched["ok"])
        self.assertTrue(dispatched["timed_out"])
        self.assertFalse(dispatched["reply_confirmed"])
        self.assertTrue(driver.call_args.kwargs["allow_placeholder_title"])
        self.assertTrue(self.bridge._load_lock()["awaiting_first_title"])

        with mock.patch.object(self.bridge, "_run_ps_driver", return_value={
            "ok": True, "snapshot": {"active_session": "真实标题", "title_authoritative": True}
        }):
            self.bridge.tool_read_latest()
        updated = self.bridge._load_lock()
        self.assertFalse(updated["awaiting_first_title"])
        self.assertEqual(updated["session_title"], "真实标题")

    def test_daemon_ack_reader_has_bounded_wait(self):
        ready = threading.Event()
        stdout = mock.Mock()
        stdout.readline.side_effect = lambda: ready.wait(2) and '{"daemon_ack":true}'
        try:
            with self.assertRaises(TimeoutError):
                self.bridge._read_daemon_ack(stdout, 0.01)
        finally:
            ready.set()

    def test_uncertain_daemon_actions_are_never_replayed(self):
        for action in ("dispatch", "new_project_session", "switch_session"):
            for ack in ("", '{"daemon_ack":false,"error":"failure"}', '{"daemon_ack":true}'):
                with self.subTest(action=action, ack=ack):
                    warm = mock.Mock(stdin=mock.Mock(), stdout=io.StringIO(ack))
                    with mock.patch.object(self.bridge, "_WARM_PS_PROC", warm), \
                         mock.patch.object(self.bridge, "_get_warm_ps", return_value=warm), \
                         mock.patch.object(self.bridge.subprocess, "run") as cold:
                        result = self.bridge._run_ps_driver(action, dispatch_id="original")
                    cold.assert_not_called()
                    warm.stdin.write.assert_called_once()
                    warm.kill.assert_called_once()
                    self.assertEqual(result["error_code"], "DAEMON_EXEC_UNCERTAIN")
                    self.assertEqual(result["dispatch_id"], "original")

        warm = mock.Mock(stdin=mock.Mock(), stdout=io.StringIO())
        with mock.patch.object(self.bridge, "_WARM_PS_PROC", warm), \
             mock.patch.object(self.bridge, "_get_warm_ps", return_value=warm), \
             mock.patch.object(self.bridge, "_read_daemon_ack", side_effect=TimeoutError) as read_ack, \
             mock.patch.object(self.bridge.subprocess, "run") as cold:
            result = self.bridge._run_ps_driver("dispatch", dispatch_id="timed-out", timeout_sec=7)
        read_ack.assert_called_once_with(warm.stdout, 22)
        cold.assert_not_called()
        warm.kill.assert_called_once()
        self.assertEqual((result["error_code"], result["dispatch_id"]), ("DAEMON_EXEC_UNCERTAIN", "timed-out"))

    def test_read_only_driver_can_retry_daemon_failure(self):
        def cold_driver(cmd, **kwargs):
            output = Path(cmd[cmd.index("-OutputFile") + 1])
            output.write_text('{"ok":true}', encoding="utf-8")

        for action in ("inspect", "read_latest"):
            warm = mock.Mock(stdin=mock.Mock(), stdout=io.StringIO())
            with mock.patch.object(self.bridge, "_WARM_PS_PROC", warm), \
                 mock.patch.object(self.bridge, "_get_warm_ps", return_value=warm), \
                 mock.patch.object(self.bridge.subprocess, "run", side_effect=cold_driver) as cold:
                self.assertEqual(self.bridge._run_ps_driver(action), {"ok": True})
            warm.kill.assert_called_once()
            cold.assert_called_once()


if __name__ == "__main__":
    unittest.main()
