"""Isolated installer, MCP transport, and review bus tests."""

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

import install


ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(Path(sys.executable).resolve())


class TemporaryHomeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="codex director 隔离 ")
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
        self.assertEqual(set(health["checks"]["antigravity_mcp"]["tools"]), install.TOOLS[install.AGY])
        self.assertEqual(set(health["checks"]["codex_mcp"]["tools"]), install.TOOLS[install.CODEX])

        module = runpy.run_path(str(self.layout.server))
        for prompt, language in (("Verify the mechanism", "en"), ("验证通信机制", "zh-CN")):
            with self.subTest(language=language):
                self.assertEqual(module["detect_language"](prompt), language)
                envelope = module["build_director_envelope"]("general", prompt, prompt)
                self.assertIn(f"\n[LANG: {language}]\n", envelope)

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
        self.assertFalse(self.layout.bridge.exists())
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


class MergeAndLocaleTests(TemporaryHomeTest):
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
            '[mcp_servers."antigravity_bridge"."env"]\nPYTHONUTF8 = "0"\nEXISTING = "keep"\n'
        )
        for source in (inline, quoted):
            with self.subTest(source=source.splitlines()[0]):
                original = tomllib.loads(source)
                updated = install.edit_toml(source.encode(), self.layout.server, entry, {})
                self.assertIn(b"[mcp_servers.antigravity_bridge.env]", updated)
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


if __name__ == "__main__":
    unittest.main()
