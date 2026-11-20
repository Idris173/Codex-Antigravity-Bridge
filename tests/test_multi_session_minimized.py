#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Isolated unit & concurrency tests for `bridge.headless_multi_engine`:
1. True concurrent dual-session execution via `HeadlessCodexRunner.dispatch`
2. Dual-minimized (`is_minimized=True`, `hwnd=0`) first-turn `exec` & second-turn `exec resume` + CLI flags
3. Non-destructive async timeout (`timeout_sec=1`) + background completion recovery via `read_latest_run`
4. Multi-binding anti-crosstalk (`AMBIGUOUS_ACTIVE_BINDINGS`), `promote_first_title`, `ui_inject_lock`,
   and case-insensitive `filter_submissions_by_workspace` routing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge.headless_multi_engine import (  # noqa: E402
    BindingStore,
    HeadlessCodexRunner,
    discover_codex_exe,
    filter_submissions_by_workspace,
    normalize_speed_tier,
    ui_inject_lock,
)


FAKE_CODEX_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # -*- coding: utf-8 -*-
    import json
    import os
    import pathlib
    import re
    import sys
    import time
    import uuid

    def main() -> int:
        argv = sys.argv[1:]
        prompt = sys.stdin.read()
        started_at = time.time()

        out_file = ""
        for idx, tok in enumerate(argv):
            if tok in ("-o", "--output-last-message") and idx + 1 < len(argv):
                out_file = argv[idx + 1]

        is_resume = False
        session_id = ""
        for idx, tok in enumerate(argv):
            if tok == "exec" and idx + 1 < len(argv) and argv[idx + 1] == "resume":
                is_resume = True
                if idx + 2 < len(argv):
                    session_id = argv[idx + 2]
                break

        if not session_id:
            m_sess = re.search(r"\\[FORCE_SESSION_ID:([^\\]]+)\\]", prompt)
            session_id = m_sess.group(1).strip() if m_sess else f"sess_{uuid.uuid4().hex[:10]}"

        m_delay = re.search(r"\\[SLEEP_SEC:([0-9.]+)\\]", prompt)
        delay_sec = float(m_delay.group(1)) if m_delay else 0.05

        m_disp = re.search(r"\\[DISPATCH_ID:([^\\]]+)\\]", prompt)
        dispatch_id = m_disp.group(1).strip() if m_disp else "unknown_dispatch"

        m_tag = re.search(r"\\[SESSION_TAG:([^\\]]+)\\]", prompt)
        session_tag = m_tag.group(1).strip() if m_tag else "default_tag"

        turn_id = f"turn_{dispatch_id[:8]}"

        # Immediately emit session & turn start events before sleeping
        print(json.dumps({"type": "thread.started", "thread_id": session_id}), flush=True)
        print(json.dumps({"type": "turn.started", "turn_id": turn_id}), flush=True)

        time.sleep(delay_sec)
        ended_at = time.time()

        log_dir = os.environ.get("FAKE_CODEX_LOG_DIR", "")
        if log_dir:
            log_path = pathlib.Path(log_dir) / f"call_{dispatch_id}_{uuid.uuid4().hex[:6]}.json"
            log_path.write_text(
                json.dumps(
                    {
                        "argv": argv,
                        "is_resume": is_resume,
                        "session_id": session_id,
                        "dispatch_id": dispatch_id,
                        "session_tag": session_tag,
                        "prompt": prompt,
                        "cwd": os.getcwd(),
                        "started_at": started_at,
                        "ended_at": ended_at,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        reply_text = (
            f"[SUMMARY]\\nCompleted {session_tag} ({'resume' if is_resume else 'fresh'}) "
            f"for dispatch={dispatch_id} session={session_id}\\n"
            f"[FILES]\\nNONE\\n"
            f"[SELF_CHECK]\\nPASS\\n"
            f"[OPEN_QUESTIONS]\\nNONE"
        )

        if out_file:
            out_p = pathlib.Path(out_file)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(reply_text, encoding="utf-8")

        print(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_1", "type": "agent_message", "text": reply_text},
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        print(
            json.dumps(
                {
                    "type": "turn.completed",
                    "turn_id": turn_id,
                    "usage": {"input_tokens": 128, "output_tokens": 64},
                }
            ),
            flush=True,
        )
        return 0

    if __name__ == "__main__":
        raise SystemExit(main())
    """
)


class HeadlessMultiSessionMinimizedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="codex_multi_minimized_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.base_dir = self.root / "bridge_state"
        self.log_dir = self.root / "stub_logs"
        self.ws_a = self.root / "Workspace_Unreal_A"
        self.ws_b = self.root / "Workspace_ThreeJS_B"
        for d in (self.base_dir, self.log_dir, self.ws_a, self.ws_b):
            d.mkdir(parents=True, exist_ok=True)

        self.stub_codex = self.root / "fake_codex.py"
        self.stub_codex.write_text(FAKE_CODEX_SCRIPT, encoding="utf-8")

        self._prev_log_env = os.environ.get("FAKE_CODEX_LOG_DIR")
        os.environ["FAKE_CODEX_LOG_DIR"] = str(self.log_dir)
        self.addCleanup(self._restore_env)

        self.store = BindingStore(self.base_dir)
        self.runner = HeadlessCodexRunner(self.base_dir, codex_exe=self.stub_codex)

    def _restore_env(self) -> None:
        if self._prev_log_env is None:
            os.environ.pop("FAKE_CODEX_LOG_DIR", None)
        else:
            os.environ["FAKE_CODEX_LOG_DIR"] = self._prev_log_env

    def _read_stub_logs(self) -> list[dict]:
        records = []
        for p in sorted(self.log_dir.glob("call_*.json")):
            records.append(json.loads(p.read_text(encoding="utf-8")))
        return records

    def test_same_binding_rejects_a_turn_until_the_first_finishes(self):
        binding = self.store.create_binding(session_title="one session", workspace_path=str(self.ws_a),
                                            transport="codex_exec")
        first_id = "a" * 32
        first = self.runner.dispatch(binding, first_id, "[SLEEP_SEC:0.75]", timeout_sec=0)
        self.assertEqual(first["status"], "running_async")
        other = HeadlessCodexRunner(self.base_dir, codex_exe=self.stub_codex)
        second = other.dispatch(binding, "b" * 32, "Do not start now", timeout_sec=0)
        self.assertEqual(second["error_code"], "BINDING_BUSY")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            completed = self.runner.read_latest_run(binding["binding_id"])
            if completed.get("reply_confirmed"):
                break
            time.sleep(0.05)
        self.assertEqual(completed["dispatch_id"], first_id)
        self.assertTrue(completed["reply_confirmed"])
        third = other.dispatch(binding, "c" * 32, "The binding is free", timeout_sec=3)
        self.assertTrue(third["reply_confirmed"])
        self.assertTrue(third["resumed"])

    def test_true_concurrent_dual_sessions(self) -> None:
        """Two bound sessions dispatched concurrently run in parallel with zero blocking and zero cross-talk."""
        bind_a = self.store.create_binding(
            session_title="UE5 Combat Session A",
            workspace_path=str(self.ws_a),
            transport="codex_exec",
            is_minimized=True,
            hwnd=0,
            engine="unreal",
        )
        bind_b = self.store.create_binding(
            session_title="ThreeJS Shader Session B",
            workspace_path=str(self.ws_b),
            transport="codex_exec",
            is_minimized=True,
            hwnd=0,
            engine="threejs",
        )

        barrier = threading.Barrier(2)
        results: dict[str, dict] = {}
        errors: list[BaseException] = []

        def _worker(label: str, binding: dict, dispatch_id: str) -> None:
            try:
                envelope = (
                    f"[SESSION_TAG:{label}] [DISPATCH_ID:{dispatch_id}] [SLEEP_SEC:0.42]\n"
                    f"Execute parallel task for {label}."
                )
                barrier.wait(timeout=5.0)
                results[label] = self.runner.dispatch(
                    binding=binding,
                    dispatch_id=dispatch_id,
                    envelope=envelope,
                    timeout_sec=15,
                )
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        t_a = threading.Thread(target=_worker, args=("SESSION_A", bind_a, "disp_alpha_001"))
        t_b = threading.Thread(target=_worker, args=("SESSION_B", bind_b, "disp_bravo_002"))

        wall_start = time.perf_counter()
        t_a.start()
        t_b.start()
        t_a.join(timeout=15.0)
        t_b.join(timeout=15.0)
        wall_elapsed = time.perf_counter() - wall_start

        self.assertEqual(errors, [])
        self.assertIn("SESSION_A", results)
        self.assertIn("SESSION_B", results)

        res_a = results["SESSION_A"]
        res_b = results["SESSION_B"]

        for res, expected_disp, expected_tag in (
            (res_a, "disp_alpha_001", "SESSION_A"),
            (res_b, "disp_bravo_002", "SESSION_B"),
        ):
            self.assertTrue(res["ok"], res)
            self.assertFalse(res["timed_out"], res)
            self.assertTrue(res["reply_confirmed"], res)
            self.assertNotEqual(res.get("error_code"), "BRIDGE_BUSY")
            self.assertEqual(res["channel"], "codex_headless_exec")
            self.assertEqual(res["dispatch_id"], expected_disp)
            self.assertIn(f"dispatch={expected_disp}", res["raw_reply"])
            self.assertIn(expected_tag, res["raw_reply"])
            self.assertTrue(res["codex_session_id"])

        self.assertNotEqual(res_a["codex_session_id"], res_b["codex_session_id"])

        # Verify overlapping execution windows in subprocess logs AND faster-than-serial wall clock
        logs = {entry["dispatch_id"]: entry for entry in self._read_stub_logs()}
        log_a = logs["disp_alpha_001"]
        log_b = logs["disp_bravo_002"]
        overlap = min(log_a["ended_at"], log_b["ended_at"]) - max(log_a["started_at"], log_b["started_at"])
        self.assertGreater(
            overlap,
            0.15,
            f"Expected true parallel overlap >0.15s, got overlap={overlap:.3f}s (wall={wall_elapsed:.3f}s)",
        )
        self.assertLess(
            wall_elapsed,
            0.80,
            f"Expected concurrent wall time well below serial ~0.84s+, got {wall_elapsed:.3f}s",
        )

    def test_dual_minimized_first_exec_and_resume_cli_flags(self) -> None:
        """Minimized binding (`is_minimized=True`, `hwnd=0`) captures `codex_session_id` and switches to `exec resume`."""
        self.assertEqual(discover_codex_exe(self.stub_codex), self.stub_codex.resolve())
        self.assertEqual(normalize_speed_tier("standard"), "default")
        self.assertEqual(normalize_speed_tier("标准"), "default")
        self.assertEqual(normalize_speed_tier("default"), "default")
        self.assertEqual(normalize_speed_tier("fast"), "fast")
        self.assertEqual(normalize_speed_tier("快速"), "fast")
        self.assertEqual(normalize_speed_tier("priority"), "fast")
        self.assertEqual(normalize_speed_tier("ultrafast"), "ultrafast")
        self.assertEqual(normalize_speed_tier("超快"), "ultrafast")

        binding = self.store.create_binding(
            session_title="Minimized Headless Worker",
            workspace_path=str(self.ws_a),
            transport="codex_exec",
            is_minimized=True,
            hwnd=0,
            model_profile={
                "model": "gpt-6.2-sol",
                "effort": "high",
                "speed": "超快",
            },
        )
        self.assertEqual(binding["model_profile"]["speed"], "ultrafast")

        # Turn 1: fresh `exec`
        env_1 = (
            "[SESSION_TAG:MIN_TURN_1] [DISPATCH_ID:disp_turn_1] "
            "[FORCE_SESSION_ID:sess_minimized_fixed_777] [SLEEP_SEC:0.05]\n"
            "首轮无头派发测试（双窗口最小化）"
        )
        res_1 = self.runner.dispatch(binding, "disp_turn_1", env_1, timeout_sec=10)
        self.assertTrue(res_1["ok"], res_1)
        self.assertFalse(res_1["resumed"])
        self.assertEqual(res_1["codex_session_id"], "sess_minimized_fixed_777")
        self.assertIn("MIN_TURN_1 (fresh)", res_1["raw_reply"])

        # BindingStore should automatically persist the captured `codex_session_id`
        reloaded, err = self.store.get_binding(binding["binding_id"])
        self.assertIsNone(err)
        assert reloaded is not None
        self.assertEqual(reloaded["codex_session_id"], "sess_minimized_fixed_777")

        # Turn 2: automatic `exec resume <codex_session_id>` using reloaded binding from store
        env_2 = (
            "[SESSION_TAG:MIN_TURN_2] [DISPATCH_ID:disp_turn_2] [SLEEP_SEC:0.05]\n"
            "第二轮自动续接已有会话"
        )
        res_2 = self.runner.dispatch(reloaded, "disp_turn_2", env_2, timeout_sec=10)
        self.assertTrue(res_2["ok"], res_2)
        self.assertTrue(res_2["resumed"])
        self.assertEqual(res_2["codex_session_id"], "sess_minimized_fixed_777")
        self.assertIn("MIN_TURN_2 (resume)", res_2["raw_reply"])

        logs = {entry["dispatch_id"]: entry for entry in self._read_stub_logs()}
        argv_1 = logs["disp_turn_1"]["argv"]
        argv_2 = logs["disp_turn_2"]["argv"]

        for argv in (argv_1, argv_2):
            self.assertIn("-m", argv)
            self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.2-sol")
            self.assertIn('model_reasoning_effort="high"', argv)
            self.assertIn('service_tier="ultrafast"', argv)
            self.assertIn("--json", argv)
            self.assertIn("--skip-git-repo-check", argv)
            self.assertIn("-s", argv)
            self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
            self.assertIn("-C", argv)
            self.assertEqual(argv[argv.index("-C") + 1], str(self.ws_a))
            self.assertEqual(argv[-1], "-")

        self.assertIn("exec", argv_1)
        self.assertNotIn("resume", argv_1)
        self.assertIn("首轮无头派发测试（双窗口最小化）", logs["disp_turn_1"]["prompt"])

        exec_idx = argv_2.index("exec")
        self.assertEqual(
            argv_2[exec_idx : exec_idx + 3],
            ["exec", "resume", "sess_minimized_fixed_777"],
        )
        self.assertIn("第二轮自动续接已有会话", logs["disp_turn_2"]["prompt"])

    def test_read_only_scope_is_sent_on_new_and_resumed_codex_commands(self) -> None:
        runner = HeadlessCodexRunner(self.base_dir, self.stub_codex)
        approval = 'mcp_servers.antigravity_bridge.tools.agy_submit_dispatch_receipt.approval_mode="approve"'
        for session_id in ("", "existing-session"):
            with self.subTest(session_id=session_id):
                options = {
                    "model": "", "effort": "", "speed": "", "workspace_path": str(self.ws_a),
                    "codex_session_id": session_id, "last_msg_file": self.base_dir / "last.txt",
                    "read_only_dispatch_id": "a" * 32,
                }
                args = runner._build_command(self.stub_codex, **options)
                self.assertEqual(args[args.index("-s") + 1], "read-only")
                self.assertIn(f'mcp_servers.antigravity_bridge.env.CODEX_BRIDGE_RECEIPT_SCOPE="{"a" * 32}"', args)
                self.assertNotIn(approval, args)
                approved = runner._build_command(self.stub_codex, authorize_receipt=True, **options)
                self.assertEqual(approved[approved.index("-s") + 1], "read-only")
                self.assertIn(approval, approved)

    def test_async_timeout_background_continuation_and_read_latest_run(self) -> None:
        """Long turns exceeding `timeout_sec=1` return `running_async` without killing the child, then recover cleanly."""
        binding = self.store.create_binding(
            session_title="Async Long Task Session",
            workspace_path=str(self.ws_b),
            transport="codex_exec",
            is_minimized=True,
            hwnd=0,
        )

        envelope = (
            "[SESSION_TAG:LONG_ASYNC_JOB] [DISPATCH_ID:disp_long_999] "
            "[FORCE_SESSION_ID:sess_async_999] [SLEEP_SEC:1.6]\n"
            "Heavy shader compilation task."
        )
        res = self.runner.dispatch(binding, "disp_long_999", envelope, timeout_sec=1)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["timed_out"], res)
        self.assertFalse(res["reply_confirmed"], res)
        self.assertEqual(res["status"], "running_async")
        self.assertEqual(res["channel"], "codex_headless_exec")
        self.assertEqual(res["dispatch_id"], "disp_long_999")

        # Wait for background daemon thread to finish collecting without touching any window
        deadline = time.monotonic() + 5.0
        latest = None
        while time.monotonic() < deadline:
            latest = self.runner.read_latest_run(binding["binding_id"])
            if latest and latest.get("status") == "completed":
                break
            time.sleep(0.08)

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertTrue(latest["ok"], latest)
        self.assertFalse(latest["timed_out"], latest)
        self.assertTrue(latest["reply_confirmed"], latest)
        self.assertEqual(latest["status"], "completed")
        self.assertEqual(latest["dispatch_id"], "disp_long_999")
        self.assertEqual(latest["codex_session_id"], "sess_async_999")
        self.assertIn("LONG_ASYNC_JOB", latest["raw_reply"])
        self.assertIn("dispatch=disp_long_999", latest["raw_reply"])
        self.assertEqual(list(self.store.bindings_dir.glob(".tmp_last_msg_*")), [])

    def test_multi_binding_anti_crosstalk_title_promotion_and_workspace_routing(self) -> None:
        """Verify `AMBIGUOUS_ACTIVE_BINDINGS`, `promote_first_title`, legacy lock sync, `ui_inject_lock`, and inbox filter."""
        # 0 bindings -> SESSION_NOT_BOUND
        doc, err = self.store.get_binding()
        self.assertIsNone(doc)
        self.assertEqual(err["error_code"], "SESSION_NOT_BOUND")

        # 1 binding with placeholder title -> awaiting_first_title=True + legacy session_lock.json mirrored
        b1 = self.store.create_binding(
            session_title="新聊天",
            requested_title="ProjectA",
            workspace_path=str(self.ws_a),
            hwnd=1001,
        )
        self.assertTrue(b1["awaiting_first_title"])
        self.assertTrue(self.store.legacy_lock_path.exists())

        single, err = self.store.get_binding()
        self.assertIsNone(err)
        self.assertEqual(single["binding_id"], b1["binding_id"])

        # Non-authoritative or placeholder title must NOT tighten awaiting_first_title
        unchanged_1 = self.store.promote_first_title(b1["binding_id"], "Unverified Window Title", authoritative=False)
        self.assertEqual(unchanged_1["session_title"], "新聊天")
        self.assertTrue(unchanged_1["awaiting_first_title"])

        unchanged_2 = self.store.promote_first_title(b1["binding_id"], "New chat", authoritative=True)
        self.assertEqual(unchanged_2["session_title"], "新聊天")
        self.assertTrue(unchanged_2["awaiting_first_title"])

        # Authoritative real title tightens once and stays locked against later drift
        promoted = self.store.promote_first_title(b1["binding_id"], "UE5 GAS 连招与受击系统", authoritative=True)
        self.assertEqual(promoted["session_title"], "UE5 GAS 连招与受击系统")
        self.assertFalse(promoted["awaiting_first_title"])

        locked_after = self.store.promote_first_title(b1["binding_id"], "Other Random Chat", authoritative=True)
        self.assertEqual(locked_after["session_title"], "UE5 GAS 连招与受击系统")

        # Add 2nd active binding -> omitted binding_id MUST reject with AMBIGUOUS_ACTIVE_BINDINGS
        b2 = self.store.create_binding(
            session_title="ThreeJS PostProcessing",
            workspace_path=str(self.ws_b),
            hwnd=2002,
            engine="threejs",
        )
        amb_doc, amb_err = self.store.get_binding()
        self.assertIsNone(amb_doc)
        self.assertEqual(amb_err["error_code"], "AMBIGUOUS_ACTIVE_BINDINGS")
        self.assertEqual(len(amb_err["active_bindings"]), 2)
        active_ids = {item["binding_id"] for item in amb_err["active_bindings"]}
        self.assertEqual(active_ids, {b1["binding_id"], b2["binding_id"]})

        # Unknown binding_id -> STALE_BINDING
        stale_doc, stale_err = self.store.get_binding("nonexistent_binding_token")
        self.assertIsNone(stale_doc)
        self.assertEqual(stale_err["error_code"], "STALE_BINDING")

        # Removing b1 leaves only b2 -> legacy lock restored for b2, implicit get_binding works again
        rm_1 = self.store.remove_binding(b1["binding_id"])
        self.assertTrue(rm_1["ok"])
        self.assertEqual(rm_1["remaining_count"], 1)
        self.assertTrue(self.store.legacy_lock_path.exists())
        only_b2, err = self.store.get_binding()
        self.assertIsNone(err)
        self.assertEqual(only_b2["binding_id"], b2["binding_id"])

        # Removing all cleans session_lock.json
        rm_all = self.store.remove_binding()
        self.assertTrue(rm_all["ok"])
        self.assertFalse(self.store.legacy_lock_path.exists())

        # Verify workspace inbox routing using the platform's path rules.
        ws_a_sibling = self.root / "Workspace_Unreal_A_Other"
        root_spelling = str(self.ws_a)
        child_spelling = str(self.ws_a / "Source" / "Combat")
        if os.name == "nt":
            root_spelling = root_spelling.lower().replace("\\", "/")
            child_spelling = child_spelling.upper()
        submissions = [
            {"request_id": "req_1", "workspace_path": root_spelling + "/"},
            {"request_id": "req_2", "workspace_path": child_spelling},
            {"request_id": "req_3", "workspace_path": str(ws_a_sibling)},
            {"request_id": "req_4", "workspace_path": str(self.ws_b)},
            {"request_id": "req_5", "workspace_path": ""},
        ]
        routed_a = filter_submissions_by_workspace(submissions, str(self.ws_a))
        self.assertEqual([s["request_id"] for s in routed_a], ["req_1", "req_2"])

        routed_b = filter_submissions_by_workspace(submissions, str(self.ws_b))
        self.assertEqual([s["request_id"] for s in routed_b], ["req_4"])

        routed_all = filter_submissions_by_workspace(submissions, "")
        self.assertEqual(len(routed_all), 5)

        # Verify two-phase short `ui_inject_lock` serializes only injection critical section
        inject_events: list[tuple[str, float]] = []
        ev_guard = threading.Lock()

        def _uia_phase_worker(tag: str) -> None:
            with ui_inject_lock(self.base_dir, timeout_sec=5.0):
                t_in = time.perf_counter()
                time.sleep(0.08)
                t_out = time.perf_counter()
                with ev_guard:
                    inject_events.append((tag, t_in))
                    inject_events.append((tag + "_end", t_out))
            # Lock-free generation wait phase
            time.sleep(0.15)

        u1 = threading.Thread(target=_uia_phase_worker, args=("U1",))
        u2 = threading.Thread(target=_uia_phase_worker, args=("U2",))
        u1.start()
        u2.start()
        u1.join(timeout=5.0)
        u2.join(timeout=5.0)
        self.assertEqual(len(inject_events), 4)
        # First lock holder must finish before second lock holder enters
        self.assertLessEqual(inject_events[1][1], inject_events[2][1] + 1e-4)

    def test_cmd_wrapper_and_session_meta_jsonl_fallback(self) -> None:
        """Verify `.cmd` stub compatibility on Windows and `session_meta` / `event_msg` JSONL extraction."""
        session_meta_py = self.root / "meta_codex.py"
        session_meta_py.write_text(
            textwrap.dedent(
                """\
                import json, sys
                _ = sys.stdin.read()
                print(json.dumps({"type": "session_meta", "payload": {"id": "sess_meta_888"}}), flush=True)
                print(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn_meta_1", "last_agent_message": "Reply from session_meta stream"}
                }), flush=True)
                """
            ),
            encoding="utf-8",
        )
        if os.name == "nt":
            wrapper = self.root / "codex_stub.cmd"
            wrapper.write_text(f'@"{sys.executable}" -B "{session_meta_py}" %*\r\n', encoding="utf-8")
            exe_target = wrapper
        else:
            exe_target = session_meta_py

        runner = HeadlessCodexRunner(self.base_dir, codex_exe=exe_target)
        b = self.store.create_binding(
            session_title="Meta Stream Worker",
            workspace_path=str(self.ws_a),
            transport="codex_exec",
            is_minimized=True,
            hwnd=0,
        )
        res = runner.dispatch(b, "disp_meta_01", "hello via stdin", timeout_sec=10)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["codex_session_id"], "sess_meta_888")
        self.assertEqual(res["turn_id"], "turn_meta_1")
        self.assertEqual(res["raw_reply"], "Reply from session_meta stream")

    def test_bridge_server_end_to_end_concurrent_headless_sessions(self) -> None:
        """End-to-end test through `bridge_server` MCP tools with two concurrent headless sessions + routing settings."""
        from unittest import mock
        from bridge import bridge_server

        bus_dir = self.base_dir / "bus"
        with (
            mock.patch.multiple(
                bridge_server,
                BASE_DIR=self.base_dir,
                BUS_DIR=bus_dir,
                TO_AGY_FILE=bus_dir / "to_antigravity.json",
                LATEST_TO_CODEX_FILE=bus_dir / "to_codex_latest.json",
                LOCK_FILE=self.base_dir / "session_lock.json",
            ),
            mock.patch.dict(os.environ, {"CODEX_ANTIGRAVITY_BRIDGE_CODEX_EXE": str(self.stub_codex)}),
        ):
            bridge_server._dispatch_mcp_tool(
                "antigravity",
                "bridge_routing_settings",
                {
                    "action": "set",
                    "codex_home": str(self.root),
                    "decision_side": "antigravity",
                    "decision_model": "gemini-4-carbon",
                    "decision_effort": "medium",
                    "decision_speed": "fast",
                    "worker_side": "codex",
                    "worker_model": "gpt-6.2-sol",
                    "worker_effort": "high",
                    "worker_speed": "ultrafast",
                },
            )
            b1 = bridge_server._dispatch_mcp_tool(
                "antigravity",
                "bridge_bind_session",
                {"engine": "unreal", "mode": "autopilot", "workspace_path": str(self.ws_a)},
            )
            b2 = bridge_server._dispatch_mcp_tool(
                "antigravity",
                "bridge_bind_session",
                {"engine": "threejs", "mode": "autopilot", "workspace_path": str(self.ws_b)},
            )
            self.assertTrue(b1["ok"] and b2["ok"])
            self.assertNotEqual(b1["binding_id"], b2["binding_id"])

            # Ambiguous call without binding_id is blocked
            amb = bridge_server._dispatch_mcp_tool(
                "antigravity",
                "bridge_dispatch_task",
                {"step_goal": "g", "task_instruction": "i"},
            )
            self.assertEqual(amb["error_code"], "AMBIGUOUS_ACTIVE_BINDINGS")

            # Dispatch both concurrently
            outputs: dict[str, dict] = {}
            def run_one(key: str, bid: str, tag: str) -> None:
                outputs[key] = bridge_server._dispatch_mcp_tool(
                    "antigravity",
                    "bridge_dispatch_task",
                    {
                        "binding_id": bid,
                        "step_goal": f"[SESSION_TAG:{tag}] [SLEEP_SEC:0.25] Parallel goal",
                        "task_instruction": "Implement feature",
                    },
                )

            t1 = threading.Thread(target=run_one, args=("a", b1["binding_id"], "E2E_A"))
            t2 = threading.Thread(target=run_one, args=("b", b2["binding_id"], "E2E_B"))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            self.assertTrue(outputs["a"]["ok"] and outputs["b"]["ok"])
            self.assertEqual(outputs["a"]["channel"], "codex_headless_exec")
            self.assertTrue(outputs["a"]["parsed_reply"]["structured_protocol_matched"])
            self.assertIn("E2E_A", outputs["a"]["parsed_reply"]["summary"])
            self.assertIn("E2E_B", outputs["b"]["parsed_reply"]["summary"])

            latest_a = bridge_server._dispatch_mcp_tool(
                "antigravity", "bridge_read_latest", {"binding_id": b1["binding_id"]}
            )
            self.assertTrue(latest_a["ok"])
            self.assertIn("E2E_A", latest_a["parsed_reply"]["summary"])

            # Verify ultrafast + approval_policy="never" reached CLI
            stub_calls = self._read_stub_logs()
            self.assertTrue(any('service_tier="ultrafast"' in c["argv"] and 'approval_policy="never"' in c["argv"] for c in stub_calls))

            self.assertTrue(bridge_server._dispatch_mcp_tool("antigravity", "bridge_unbind_session", {"binding_id": b1["binding_id"]})["ok"])
            self.assertTrue(bridge_server._dispatch_mcp_tool("antigravity", "bridge_unbind_session", {"binding_id": b2["binding_id"]})["ok"])


if __name__ == "__main__":
    unittest.main()
