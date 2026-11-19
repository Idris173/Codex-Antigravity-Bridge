#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chatgpt-bridge / antigravity_bridge (v2.3 Release):
Bidirectional local MCP Server & IPC bus between Antigravity (Tech Director) and OpenAI Codex Desktop (Worker).
- Role 'antigravity' (default): Window gatekeeper, 266ms fast UIA + 35ms lossless JSONL dispatch, and Codex inbox review tools.
- Role 'codex' (--role codex): Native MCP submission/reply tools so Codex NEVER needs `computer-use` GUI clicking.
Zero external third-party dependencies (standard Python 3.10+ on Windows).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import msvcrt
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
PS_DRIVER = BASE_DIR / "uia_driver.ps1"
PS_DAEMON = BASE_DIR / "uia_daemon.ps1"
LOCK_FILE = BASE_DIR / "session_lock.json"
BUS_DIR = BASE_DIR / "bus"
TO_AGY_FILE = BUS_DIR / "to_antigravity.json"
LATEST_TO_CODEX_FILE = BUS_DIR / "to_codex_latest.json"
REQUEST_ID = re.compile(r"req_[0-9]+_[0-9a-f]{6,32}")

_WARM_PS_PROC: Optional[subprocess.Popen] = None


def _ensure_bus_dir() -> None:
    (BUS_DIR / "requests").mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def _mutex(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + 5.0
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Local bridge mutex is busy") from None
                time.sleep(0.04)
        try:
            yield
        finally:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass


def _serialized_binding(func):
    @wraps(func)
    def call(*args, **kwargs):
        try:
            with _mutex(LOCK_FILE.with_suffix(".mutex")):
                return func(*args, **kwargs)
        except TimeoutError:
            return {"ok": False, "error_code": "BRIDGE_BUSY", "message": "会话绑定正由其他并发调用占用，请稍后重试。"}
    return call


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json_safe(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    for _ in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            time.sleep(0.015)
    return None


def _get_warm_ps() -> Optional[subprocess.Popen]:
    global _WARM_PS_PROC
    if _WARM_PS_PROC is not None and _WARM_PS_PROC.poll() is None:
        return _WARM_PS_PROC
    if not PS_DAEMON.exists():
        return None
    try:
        _WARM_PS_PROC = subprocess.Popen(
            [
                "powershell.exe",
                "-Sta",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(PS_DAEMON),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        return _WARM_PS_PROC
    except Exception:
        _WARM_PS_PROC = None
        return None


def _load_lock() -> Optional[Dict[str, Any]]:
    data = _read_json_safe(LOCK_FILE)
    if isinstance(data, dict) and data.get("bound"):
        return data
    return None


def _save_lock(data: Dict[str, Any]) -> None:
    _atomic_write_json(LOCK_FILE, data)


def _clear_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _run_ps_driver(
    action: str,
    expected_session: str = "",
    project_name: str = "",
    session_name: str = "",
    target_hwnd: int = 0,
    target_pid: int = 0,
    dispatch_id: str = "",
    payload_text: Optional[str] = None,
    timeout_sec: int = 50,
) -> Dict[str, Any]:
    if not PS_DRIVER.exists():
        return {
            "ok": False,
            "error_code": "DRIVER_MISSING",
            "message": f"找不到 UIA 驱动脚本: {PS_DRIVER}",
        }

    with tempfile.TemporaryDirectory(prefix="agy_chatgpt_bridge_") as tmpdir:
        tmp_path = Path(tmpdir)
        out_file = tmp_path / "result.json"
        cmd = [
            "powershell.exe",
            "-Sta",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PS_DRIVER),
            "-Action",
            action,
            "-OutputFile",
            str(out_file),
            "-TimeoutSec",
            str(timeout_sec),
        ]
        if expected_session:
            cmd.extend(["-ExpectedSession", expected_session])
        if project_name:
            cmd.extend(["-ProjectName", project_name])
        if session_name:
            cmd.extend(["-SessionName", session_name])
        if target_hwnd:
            cmd.extend(["-TargetHwnd", str(target_hwnd)])
        if target_pid:
            cmd.extend(["-TargetPid", str(target_pid)])
        if dispatch_id:
            cmd.extend(["-DispatchId", dispatch_id])

        payload_file_str = ""
        if payload_text is not None:
            payload_file = tmp_path / "payload.txt"
            payload_file.write_text(payload_text, encoding="utf-8")
            payload_file_str = str(payload_file)
            cmd.extend(["-PayloadFile", payload_file_str])

        warm = _get_warm_ps()
        if warm is not None and warm.stdin and warm.stdout:
            try:
                req_line = json.dumps(
                    {
                        "action": action,
                        "output_file": str(out_file),
                        "expected_session": expected_session,
                        "project_name": project_name,
                        "session_name": session_name,
                        "target_hwnd": target_hwnd,
                        "target_pid": target_pid,
                        "dispatch_id": dispatch_id,
                        "payload_file": payload_file_str,
                        "timeout_sec": timeout_sec,
                    },
                    ensure_ascii=False,
                )
                warm.stdin.write(req_line + "\n")
                warm.stdin.flush()
                ack = warm.stdout.readline()
                if ack and out_file.exists():
                    return json.loads(out_file.read_text(encoding="utf-8"))
            except Exception:
                global _WARM_PS_PROC
                try:
                    warm.kill()
                except Exception:
                    pass
                _WARM_PS_PROC = None

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec + 25,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error_code": "BRIDGE_PROCESS_TIMEOUT",
                "message": f"UIA 驱动执行超时 (>{timeout_sec + 25}s)。",
            }
        except Exception as exc:
            return {
                "ok": False,
                "error_code": "BRIDGE_EXEC_ERROR",
                "message": f"启动 UIA 驱动失败: {exc}",
            }

        if out_file.exists():
            try:
                return json.loads(out_file.read_text(encoding="utf-8"))
            except Exception as exc:
                return {
                    "ok": False,
                    "error_code": "INVALID_DRIVER_JSON",
                    "message": f"解析 UIA JSON 失败: {exc}",
                    "stdout": proc.stdout[-1000:],
                    "stderr": proc.stderr[-1000:],
                }

        return {
            "ok": False,
            "error_code": "DRIVER_CRASHED",
            "message": "UIA 驱动未生成输出，可能当前环境无桌面会话访问权限。",
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-1000:],
            "stderr": proc.stderr[-1000:],
        }


def parse_worker_reply(raw_text: str) -> Dict[str, Any]:
    sections = {
        "summary": "",
        "files_raw": "",
        "self_check": "",
        "open_questions": "",
    }
    tag_map = [
        ("SUMMARY", "summary"),
        ("FILES", "files_raw"),
        ("SELF_CHECK", "self_check"),
        ("OPEN_QUESTIONS", "open_questions"),
    ]

    positions = []
    for tag, key in tag_map:
        m = re.search(rf"\[{tag}\]", raw_text)
        if m:
            positions.append((m.start(), m.end(), key))

    positions.sort(key=lambda x: x[0])
    structured_detected = len(positions) >= 2

    for idx, (_, content_start, key) in enumerate(positions):
        content_end = positions[idx + 1][0] if idx + 1 < len(positions) else len(raw_text)
        sections[key] = raw_text[content_start:content_end].strip()

    extracted_files = []
    files_source = sections["files_raw"] if structured_detected else raw_text
    file_pattern = re.compile(
        r"(?:###\s*`?([^\n`]+)`?\s*\n+)?```([a-zA-Z0-9_+-]*)\n(.*?)```",
        re.DOTALL,
    )
    for match in file_pattern.finditer(files_source):
        path_hint = (match.group(1) or "").strip()
        lang = (match.group(2) or "").strip()
        code = match.group(3)
        extracted_files.append(
            {
                "path_hint": path_hint,
                "language": lang,
                "code": code,
            }
        )

    oq = re.sub(
        r"(?:\r?\n(?:\d{1,2}:\d{2}(?:\s?[AP]M)?|最新一条回复|Latest reply))+\s*$",
        "",
        sections["open_questions"],
        flags=re.IGNORECASE,
    ).strip()
    has_open_questions = bool(
        oq
        and oq.upper() not in ("NONE", "NONE.", "N/A")
        and not oq.upper().startswith("NONE\n")
        and not oq.startswith("无")
    )

    return {
        "structured_protocol_matched": structured_detected,
        "summary": sections["summary"],
        "files_section": sections["files_raw"],
        "self_check": sections["self_check"],
        "open_questions": oq,
        "has_open_questions": has_open_questions,
        "extracted_code_blocks": extracted_files,
    }


def detect_language(*texts: str) -> str:
    """Detect whether the prompt/conversation language is Chinese ('zh-CN') or English ('en')."""
    combined = " ".join(t for t in texts if t)
    if re.search(r"[\u4e00-\u9fff]", combined):
        return "zh-CN"
    return "en"


def build_director_envelope(
    engine: str,
    step_goal: str,
    task_instruction: str,
    constraints: str = "",
    local_context: str = "",
    rework_round: int = 0,
    dispatch_id: str = "",
    lang: str = "",
) -> str:
    eng_norm = (engine or "UNREAL").strip().upper()
    if eng_norm not in ("UNREAL", "THREEJS", "GENERAL"):
        eng_norm = "GENERAL"

    resolved_lang = (lang or "").strip() or detect_language(step_goal, task_instruction, constraints)
    lang_rule = (
        "Respond strictly in Simplified Chinese (简体中文) for all prose inside sections."
        if resolved_lang.startswith("zh")
        else "Respond strictly in English for all prose inside sections."
    )

    lines = ["[DIRECTOR_TASK]"]
    if dispatch_id:
        lines.append(f"[DISPATCH_ID: {dispatch_id}]")
    lines.extend(
        [
            f"[LANG: {resolved_lang}]",
            f"[ENGINE: {eng_norm}]",
            f"[ROUND: {'REWORK #' + str(rework_round) if rework_round > 0 else 'INITIAL'}]",
            f"[STEP_GOAL] {step_goal.strip()}",
        ]
    )
    if constraints.strip():
        lines.append(f"[CONSTRAINTS]\n{constraints.strip()}")
    lines.append(f"[INSTRUCTION]\n{task_instruction.strip()}")
    if local_context.strip():
        lines.append(f"[LOCAL_CONTEXT]\n{local_context.strip()}")
    lines.append(
        "[PROTOCOL_REMINDER] Activate `director-worker` skill rules. Reply strictly with sections: "
        f"[SUMMARY], [FILES], [SELF_CHECK], [OPEN_QUESTIONS] (write NONE if no blocking questions). {lang_rule}"
    )
    lines.append("[/DIRECTOR_TASK]")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Antigravity-Side Tool Implementations (Director Role)
# ---------------------------------------------------------------------------

def tool_inspect_app() -> Dict[str, Any]:
    res = _run_ps_driver(action="inspect", timeout_sec=20)
    res["current_lock"] = _load_lock()
    inbox = tool_check_codex_inbox()
    if inbox.get("has_pending"):
        res["pending_codex_inbox"] = {
            "pending_count": inbox.get("pending_count"),
            "latest_submission": inbox.get("submission"),
        }
    return res


@_serialized_binding
def tool_bind_session(
    session_title: str = "",
    engine: str = "unreal",
    mode: str = "semi_auto",
    auto_apply_files: bool = False,
    max_retries: int = 3,
    hwnd: int = 0,
    create_in_project: str = "",
    switch_to_session: str = "",
) -> Dict[str, Any]:
    proj = (create_in_project or "").strip()
    sw_sess = (switch_to_session or "").strip()
    title = (session_title or "").strip()

    if proj:
        nav_res = _run_ps_driver(action="new_project_session", project_name=proj, target_hwnd=hwnd, timeout_sec=20)
        if not nav_res.get("ok"):
            return nav_res
        matched_win = nav_res["snapshot"]
        title = title or str(matched_win.get("active_session") or "新聊天")
    elif sw_sess:
        nav_res = _run_ps_driver(action="switch_session", session_name=sw_sess, target_hwnd=hwnd, timeout_sec=20)
        if not nav_res.get("ok"):
            return nav_res
        matched_win = nav_res["snapshot"]
        title = title or sw_sess
    else:
        if not title:
            return {
                "ok": False,
                "error_code": "EMPTY_SESSION_TITLE",
                "message": "必须提供 session_title、create_in_project 或 switch_to_session 之一。",
            }
        inspect_res = _run_ps_driver(action="inspect", timeout_sec=20)
        if not inspect_res.get("ok"):
            return inspect_res

        windows = inspect_res.get("windows", [])
        matched_win = None
        for w in windows:
            if hwnd and int(w.get("hwnd", 0)) != int(hwnd):
                continue
            act = str(w.get("active_session", ""))
            wt = str(w.get("window_title", ""))
            if title.lower() in act.lower() or act.lower() in title.lower() or title.lower() in wt.lower():
                matched_win = w
                break

        if matched_win is None:
            available = [
                {
                    "hwnd": w.get("hwnd"),
                    "active_session": w.get("active_session"),
                    "sidebar_sessions": w.get("sidebar_sessions", [])[:15],
                }
                for w in windows
            ]
            return {
                "ok": False,
                "error_code": "TARGET_SESSION_NOT_ACTIVE",
                "message": (
                    f"当前打开的 ChatGPT 窗口激活会话与 '{title}' 不匹配。"
                    "可传入 switch_to_session 自动切换，或先在窗口点开目标会话。"
                ),
                "available_windows": available,
            }

    if matched_win.get("is_minimized"):
        return {
            "ok": False,
            "error_code": "WINDOW_MINIMIZED",
            "message": "目标 ChatGPT 窗口处于最小化状态，请将其恢复显示后再锁定。",
        }

    binding_id = uuid.uuid4().hex
    lock_data = {
        "bound": True,
        "binding_id": binding_id,
        "hwnd": int(matched_win["hwnd"]),
        "pid": int(matched_win["pid"]),
        "process_start": int(matched_win.get("process_start", 0)),
        "session_title": str(matched_win["active_session"]),
        "requested_title": sw_sess or proj or title,
        "fingerprint": str(matched_win.get("fingerprint", "")),
        "engine": (engine or "unreal").upper(),
        "mode": mode if mode in ("semi_auto", "autopilot") else "semi_auto",
        "auto_apply_files": bool(auto_apply_files),
        "retry_count": 0,
        "max_retries": max(1, min(int(max_retries), 6)),
        "bound_at": _now_iso(),
    }
    _save_lock(lock_data)
    return {
        "ok": True,
        "binding_id": binding_id,
        "message": f"已成功锁定 ChatGPT 会话: '{lock_data['session_title']}' (HWND={lock_data['hwnd']})",
        "lock": lock_data,
    }


@_serialized_binding
def tool_dispatch_task(
    step_goal: str,
    task_instruction: str,
    constraints: str = "",
    local_context: str = "",
    is_rework: bool = False,
    timeout_sec: int = 50,
    binding_id: str = "",
) -> Dict[str, Any]:
    lock = _load_lock()
    if not lock:
        return {
            "ok": False,
            "error_code": "SESSION_NOT_BOUND",
            "message": "门禁拦截：当前尚未绑定任何 ChatGPT 会话！必须先与制作人确认目标会话并调用 bridge_bind_session。",
        }
    if binding_id and lock.get("binding_id") and binding_id != lock.get("binding_id"):
        return {
            "ok": False,
            "error_code": "STALE_BINDING",
            "message": "绑定令牌已过期，请重新确认锁定目标会话。",
        }

    if is_rework:
        next_retry = int(lock.get("retry_count", 0)) + 1
        max_retries = int(lock.get("max_retries", 3))
        if next_retry > max_retries:
            return {
                "ok": False,
                "error_code": "MAX_RETRIES_EXCEEDED",
                "message": (
                    f"熔断拦截：当前步骤连续返工次数已超过上限 ({max_retries} 轮)。"
                    "请立即停止自动派发，向制作人汇报当前卡点与方案分歧！"
                ),
                "lock": lock,
            }
        lock["retry_count"] = next_retry
    else:
        lock["retry_count"] = 0

    dispatch_id = uuid.uuid4().hex
    lock["last_dispatch_id"] = dispatch_id
    _save_lock(lock)

    envelope = build_director_envelope(
        engine=lock.get("engine", "UNREAL"),
        step_goal=step_goal,
        task_instruction=task_instruction,
        constraints=constraints,
        local_context=local_context,
        rework_round=int(lock.get("retry_count", 0)),
        dispatch_id=dispatch_id,
    )

    # Cap synchronous MCP wait at 55s by default so host ~70s MCP call timeout never cancels long turns
    safe_sync_timeout = max(5, min(int(timeout_sec), 55))

    driver_res = _run_ps_driver(
        action="dispatch",
        expected_session=str(lock["session_title"]),
        target_hwnd=int(lock["hwnd"]),
        target_pid=int(lock.get("pid", 0)),
        dispatch_id=dispatch_id,
        payload_text=envelope,
        timeout_sec=safe_sync_timeout,
    )

    if not driver_res.get("ok") and not driver_res.get("timed_out"):
        return {**driver_res, "dispatch_id": dispatch_id}

    raw_reply = str(driver_res.get("raw_reply", ""))
    parsed = parse_worker_reply(raw_reply)

    return {
        "ok": driver_res.get("ok", False),
        "timed_out": driver_res.get("timed_out", False),
        "dispatch_id": dispatch_id,
        "turn_id": driver_res.get("turn_id", ""),
        "channel": driver_res.get("channel", "uia"),
        "telemetry": {
            "ui_inject_ms": driver_res.get("ui_inject_ms"),
            "model_ttft_ms": driver_res.get("model_ttft_ms"),
            "model_duration_ms": driver_res.get("model_duration_ms"),
            "pickup_overhead_ms": driver_res.get("pickup_overhead_ms"),
            "total_elapsed_ms": driver_res.get("total_elapsed_ms"),
        },
        "message": driver_res.get("message", ""),
        "session_title": lock["session_title"],
        "mode": lock["mode"],
        "engine": lock["engine"],
        "rework_round": lock["retry_count"],
        "max_retries": lock["max_retries"],
        "parsed_reply": parsed,
        "raw_reply": raw_reply,
    }


def tool_read_latest() -> Dict[str, Any]:
    lock = _load_lock()
    if not lock:
        return {
            "ok": False,
            "error_code": "SESSION_NOT_BOUND",
            "message": "当前未绑定会话，若仅探测状态请调用 bridge_inspect_app。",
        }
    res = _run_ps_driver(
        action="read_latest",
        expected_session=str(lock["session_title"]),
        target_hwnd=int(lock["hwnd"]),
        target_pid=int(lock.get("pid", 0)),
        timeout_sec=20,
    )
    if res.get("ok") and "snapshot" in res:
        last_msg = str(res["snapshot"].get("last_message", ""))
        res["parsed_reply"] = parse_worker_reply(last_msg)
    return res


@_serialized_binding
def tool_create_project_session(project_name: str, hwnd: int = 0) -> Dict[str, Any]:
    proj = (project_name or "").strip()
    if not proj:
        return {"ok": False, "error_code": "EMPTY_PROJECT", "message": "必须指定目标工程名称 (project_name)。"}
    return _run_ps_driver(action="new_project_session", project_name=proj, target_hwnd=hwnd, timeout_sec=20)


@_serialized_binding
def tool_unbind_session(binding_id: str = "") -> Dict[str, Any]:
    prev = _load_lock()
    if binding_id and prev and prev.get("binding_id") and binding_id != prev.get("binding_id"):
        return {"ok": False, "error_code": "STALE_BINDING", "message": "绑定令牌不匹配，未解除新绑定。"}
    _clear_lock()
    return {
        "ok": True,
        "message": "已解除 ChatGPT 会话绑定锁。",
        "previous_lock": prev,
    }


# ---------------------------------------------------------------------------
# Concurrent-Safe Multi-Request Inbox Bus (16+ parallel requests safe)
# ---------------------------------------------------------------------------

def _reply_path(request_id: str) -> Path:
    return BUS_DIR / f"to_codex_{request_id}.json"


def _pending_submissions() -> List[Dict[str, Any]]:
    _ensure_bus_dir()
    pending: Dict[str, Dict[str, Any]] = {}
    candidates = [TO_AGY_FILE, *(BUS_DIR / "requests").glob("req_*.json")]
    for path in candidates:
        item = _read_json_safe(path)
        if not isinstance(item, dict):
            continue
        req_id = str(item.get("request_id", ""))
        if item.get("status") == "pending" and REQUEST_ID.fullmatch(req_id) and not _reply_path(req_id).exists():
            pending[req_id] = item
    return sorted(pending.values(), key=lambda x: (x.get("submitted_at", ""), x["request_id"]))


def tool_check_codex_inbox(request_id: str = "") -> Dict[str, Any]:
    pending = _pending_submissions()
    req_id = (request_id or "").strip()
    if req_id:
        submission = next((item for item in pending if item["request_id"] == req_id), None)
    else:
        submission = pending[0] if pending else None
    return {
        "ok": True,
        "has_pending": bool(pending),
        "pending_count": len(pending),
        "submission": submission,
        "pending_request_ids": [item["request_id"] for item in pending],
    }


def tool_reply_to_codex(
    request_id: str = "",
    decision: str = "APPROVED",
    review_feedback: str = "",
    next_instruction: str = "",
) -> Dict[str, Any]:
    if decision not in ("APPROVED", "REWORK_REQUIRED", "ESCALATE_TO_PRODUCER"):
        return {"ok": False, "error_code": "INVALID_DECISION", "message": f"非法评审结论: {decision}"}

    _ensure_bus_dir()
    with _mutex(BUS_DIR / "replies.mutex"):
        pending = _pending_submissions()
        req_id = (request_id or "").strip()
        if not req_id and pending:
            req_id = pending[0]["request_id"]
        if not REQUEST_ID.fullmatch(req_id):
            return {"ok": False, "error_code": "INVALID_REQUEST_ID", "message": "缺少有效的 request_id。"}

        path = _reply_path(req_id)
        fields = {
            "decision": decision,
            "review_feedback": review_feedback,
            "next_instruction": next_instruction,
        }
        if path.exists():
            existing = _read_json_safe(path)
            if isinstance(existing, dict) and all(existing.get(k) == v for k, v in fields.items()):
                return {"ok": True, "already_replied": True, "reply": existing}
            return {"ok": False, "error_code": "ALREADY_REPLIED", "request_id": req_id}

        reply_payload = {
            "ok": True,
            "request_id": req_id,
            "replied_at": _now_iso(),
            **fields,
        }
        _atomic_write_json(path, reply_payload)
        _atomic_write_json(LATEST_TO_CODEX_FILE, reply_payload)

        req_file = BUS_DIR / "requests" / f"{req_id}.json"
        req_doc = _read_json_safe(req_file)
        if isinstance(req_doc, dict):
            req_doc["status"] = "replied"
            req_doc["replied_at"] = reply_payload["replied_at"]
            _atomic_write_json(req_file, req_doc)

    return {
        "ok": True,
        "message": f"已通过本地 IPC 总线向 Codex 返回批复 (request_id={req_id}, decision={decision})。",
        "reply": reply_payload,
    }


# ---------------------------------------------------------------------------
# Codex-Side Tool Implementations (Worker Role: ZERO computer-use needed!)
# ---------------------------------------------------------------------------

def codex_tool_submit_for_review(
    step_summary: str,
    changed_files: str = "",
    code_or_diff: str = "",
    self_check: str = "",
    open_questions: str = "NONE",
    workspace_path: str = "",
    wait_for_reply: bool = True,
    wait_timeout_sec: int = 240,
) -> Dict[str, Any]:
    req_id = f"req_{int(time.time() * 1000)}_{uuid.uuid4().hex[:12]}"
    submission = {
        "request_id": req_id,
        "status": "pending",
        "submitted_at": _now_iso(),
        "workspace_path": workspace_path or os.getcwd(),
        "step_summary": step_summary,
        "changed_files": changed_files,
        "code_or_diff": code_or_diff,
        "self_check": self_check,
        "open_questions": open_questions,
    }
    _ensure_bus_dir()
    _atomic_write_json(BUS_DIR / "requests" / f"{req_id}.json", submission)

    if not wait_for_reply:
        return {
            "ok": True,
            "request_id": req_id,
            "status": "queued",
            "message": "已投递至 Antigravity 技术总监收件箱（非阻塞模式）。稍后可调用 agy_fetch_director_reply 获取评审结论。",
        }

    reply_file = _reply_path(req_id)
    deadline = time.monotonic() + max(5, min(int(wait_timeout_sec), 290))
    while time.monotonic() < deadline:
        if reply_file.exists():
            rep = _read_json_safe(reply_file)
            if rep:
                return rep
        time.sleep(0.04)

    return {
        "ok": True,
        "request_id": req_id,
        "status": "waiting_director",
        "timed_out": True,
        "message": (
            f"请求已写入总线 (request_id={req_id})，等待 {wait_timeout_sec}s 暂未收到同步批复。"
            "严禁使用 computer-use 点击屏幕！请告知用户提醒 Antigravity 审阅，或稍后调用 agy_fetch_director_reply 查询。"
        ),
    }


def codex_tool_fetch_director_reply(request_id: str = "") -> Dict[str, Any]:
    req_id = (request_id or "").strip()
    if req_id:
        if not REQUEST_ID.fullmatch(req_id):
            return {"ok": False, "error_code": "INVALID_REQUEST_ID", "message": "request_id 格式无效。"}
        rep = _read_json_safe(_reply_path(req_id))
        if isinstance(rep, dict) and rep.get("request_id") == req_id:
            return rep
        return {"ok": True, "request_id": req_id, "status": "pending", "message": f"请求 {req_id} 仍在等待批复。"}
    latest = _read_json_safe(LATEST_TO_CODEX_FILE)
    if latest:
        return latest
    return {"ok": True, "status": "empty", "message": "当前尚无来自 Antigravity 的历史批复记录。"}


def codex_tool_bridge_status() -> Dict[str, Any]:
    return {
        "ok": True,
        "bus_dir": str(BUS_DIR),
        "antigravity_session_lock": _load_lock(),
        "pending_inbox": tool_check_codex_inbox(),
        "message": "antigravity_bridge 双向 MCP 总线正常在线（无需使用 computer-use）。",
    }


# ---------------------------------------------------------------------------
# MCP Tool Schemas by Role
# ---------------------------------------------------------------------------

AGY_MCP_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "bridge_inspect_app",
        "description": (
            "门禁检测工具：检查本机 ChatGPT 桌面应用是否已开启，读取当前激活会话标题、"
            "侧边栏全部工程/会话列表、最近对话摘要及当前锁状态。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "bridge_bind_session",
        "description": (
            "会话加锁与一步快连工具：支持锁定当前会话，或通过 create_in_project / switch_to_session "
            "一步原子完成新建/切换并加锁。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_title": {"type": "string", "description": "当前激活会话标题。"},
                "create_in_project": {"type": "string", "description": "一步在指定侧边栏工程下新建会话并锁定。"},
                "switch_to_session": {"type": "string", "description": "一步切换到侧边栏已有会话并锁定。"},
                "engine": {"type": "string", "enum": ["unreal", "threejs", "general"]},
                "mode": {"type": "string", "enum": ["semi_auto", "autopilot"]},
                "auto_apply_files": {"type": "boolean"},
                "max_retries": {"type": "integer"},
                "hwnd": {"type": "integer"},
            },
            "required": ["engine", "mode"],
        },
    },
    {
        "name": "bridge_dispatch_task",
        "description": "向已锁定会话极速派发指令，通过 35ms 本地 JSONL 零延迟无损通道（按 dispatch_id 精确归属）回收结果。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "step_goal": {"type": "string"},
                "task_instruction": {"type": "string"},
                "constraints": {"type": "string"},
                "local_context": {"type": "string"},
                "binding_id": {"type": "string"},
                "is_rework": {"type": "boolean"},
                "timeout_sec": {"type": "integer"},
            },
            "required": ["step_goal", "task_instruction"],
        },
    },
    {
        "name": "bridge_read_latest",
        "description": "不发消息，仅重新读取已锁定会话最新回复。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "bridge_create_project_session",
        "description": "在指定侧边栏工程下新建空白会话。",
        "inputSchema": {
            "type": "object",
            "properties": {"project_name": {"type": "string"}, "hwnd": {"type": "integer"}},
            "required": ["project_name"],
        },
    },
    {
        "name": "bridge_unbind_session",
        "description": "解除当前会话绑定锁。",
        "inputSchema": {
            "type": "object",
            "properties": {"binding_id": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "bridge_check_codex_inbox",
        "description": "读取 Codex 通过双边 MCP 主动发来的待评审代码包或请示消息。",
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "bridge_reply_to_codex",
        "description": "通过双边 MCP 总线在 40ms 内直接向等待中的 Codex 返回评审结论与下一步修改指令（零 UI 粘贴）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "收件箱中的 request_id（留空则自动回复最早挂起单）。"},
                "decision": {
                    "type": "string",
                    "enum": ["APPROVED", "REWORK_REQUIRED", "ESCALATE_TO_PRODUCER"],
                    "description": "评审决定：通过 / 打回修改 / 需交由游戏制作人拍板。",
                },
                "review_feedback": {"type": "string", "description": "具体的代码或几何/架构审查意见。"},
                "next_instruction": {"type": "string", "description": "指示 Codex 立即执行的下一步指令。"},
            },
            "required": ["decision", "review_feedback"],
        },
    },
]

CODEX_MCP_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "agy_submit_for_review",
        "description": (
            "向 Antigravity 技术总监原生提交本步工作成果、代码改动或疑问以获取评审与下一步指令。"
            "使用此 MCP 工具即可直接毫秒级跨进程通信，严禁使用 computer-use 截图或控制鼠标键盘！"
        ),
        "annotations": {
            "title": "Submit Work to Antigravity Director",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "step_summary": {"type": "string", "description": "本步完成情况摘要。"},
                "changed_files": {"type": "string", "description": "新增或修改的文件路径列表。"},
                "code_or_diff": {"type": "string", "description": "关键代码片段或 git diff 内容。"},
                "self_check": {"type": "string", "description": "按 director-worker 规范执行的引擎红线自检说明。"},
                "open_questions": {"type": "string", "description": "需技术总监或制作人裁决的问题，无则填 NONE。"},
                "workspace_path": {"type": "string", "description": "当前工程根目录路径。"},
                "wait_for_reply": {
                    "type": "boolean",
                    "description": "是否阻塞等待 Antigravity 实时写回评审结论（默认 true）。",
                },
                "wait_timeout_sec": {
                    "type": "integer",
                    "description": "等待批复超时秒数（默认 240 秒）。",
                },
            },
            "required": ["step_summary"],
        },
    },
    {
        "name": "agy_fetch_director_reply",
        "description": "查询 Antigravity 技术总监对指定 request_id 或最新一次提交的评审批复与下一步指令。",
        "annotations": {
            "title": "Fetch Antigravity Director Reply",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "可选：提交时返回的 request_id。"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "agy_bridge_status",
        "description": "检查与 Antigravity 双边 MCP 总线的连接状态。",
        "annotations": {
            "title": "Check Antigravity Bridge Status",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _validate_args(tools: List[Dict[str, Any]], name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    schema = next((t["inputSchema"] for t in tools if t["name"] == name), None)
    if schema is None:
        return None
    expected_types = {"string": str, "boolean": bool, "integer": int}
    props = schema.get("properties", {})
    missing = [k for k in schema.get("required", ()) if k not in args]
    invalid = [
        k
        for k, v in args.items()
        if (k in props and "type" in props[k] and type(v) is not expected_types[props[k]["type"]])
        or (k in props and "enum" in props[k] and v not in props[k]["enum"])
        or (k not in props and schema.get("additionalProperties") is False)
    ]
    if missing or invalid:
        return {
            "ok": False,
            "error_code": "INVALID_ARGUMENTS",
            "message": "工具参数与声明的 inputSchema 不符。",
            "missing": missing,
            "invalid": invalid,
        }
    return None


def _dispatch_mcp_tool(role: str, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    tools = CODEX_MCP_TOOLS if role == "codex" else AGY_MCP_TOOLS
    arg_err = _validate_args(tools, name, args)
    if arg_err is not None:
        return arg_err

    if role == "codex":
        if name == "agy_submit_for_review":
            return codex_tool_submit_for_review(
                step_summary=args.get("step_summary", ""),
                changed_files=args.get("changed_files", ""),
                code_or_diff=args.get("code_or_diff", ""),
                self_check=args.get("self_check", ""),
                open_questions=args.get("open_questions", "NONE"),
                workspace_path=args.get("workspace_path", ""),
                wait_for_reply=bool(args.get("wait_for_reply", True)),
                wait_timeout_sec=int(args.get("wait_timeout_sec", 240)),
            )
        if name == "agy_fetch_director_reply":
            return codex_tool_fetch_director_reply(request_id=args.get("request_id", ""))
        if name == "agy_bridge_status":
            return codex_tool_bridge_status()
        return {"ok": False, "error_code": "UNKNOWN_CODEX_TOOL", "message": f"未知 Codex 侧工具: {name}"}

    if name == "bridge_inspect_app":
        return tool_inspect_app()
    if name == "bridge_bind_session":
        return tool_bind_session(
            session_title=args.get("session_title", ""),
            engine=args.get("engine", "unreal"),
            mode=args.get("mode", "semi_auto"),
            auto_apply_files=bool(args.get("auto_apply_files", False)),
            max_retries=int(args.get("max_retries", 3)),
            hwnd=int(args.get("hwnd", 0)),
            create_in_project=args.get("create_in_project", ""),
            switch_to_session=args.get("switch_to_session", ""),
        )
    if name == "bridge_dispatch_task":
        return tool_dispatch_task(
            step_goal=args.get("step_goal", ""),
            task_instruction=args.get("task_instruction", ""),
            constraints=args.get("constraints", ""),
            local_context=args.get("local_context", ""),
            is_rework=bool(args.get("is_rework", False)),
            timeout_sec=int(args.get("timeout_sec", 50)),
            binding_id=args.get("binding_id", ""),
        )
    if name == "bridge_read_latest":
        return tool_read_latest()
    if name == "bridge_create_project_session":
        return tool_create_project_session(
            project_name=args.get("project_name", ""),
            hwnd=int(args.get("hwnd", 0)),
        )
    if name == "bridge_unbind_session":
        return tool_unbind_session(binding_id=args.get("binding_id", ""))
    if name == "bridge_check_codex_inbox":
        return tool_check_codex_inbox(request_id=args.get("request_id", ""))
    if name == "bridge_reply_to_codex":
        return tool_reply_to_codex(
            request_id=args.get("request_id", ""),
            decision=args.get("decision", "APPROVED"),
            review_feedback=args.get("review_feedback", ""),
            next_instruction=args.get("next_instruction", ""),
        )
    return {"ok": False, "error_code": "UNKNOWN_TOOL", "message": f"未知工具: {name}"}


def _read_rpc_message() -> Tuple[Optional[Dict[str, Any]], str]:
    stdin_buf = sys.stdin.buffer
    while True:
        line = stdin_buf.readline()
        if not line:
            return None, "eof"
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.lower().startswith(b"content-length:"):
            length = int(stripped.split(b":", 1)[1].strip())
            while True:
                hline = stdin_buf.readline()
                if not hline or hline in (b"\r\n", b"\n"):
                    break
            body = stdin_buf.read(length)
            return json.loads(body.decode("utf-8")), "header"

        return json.loads(stripped.decode("utf-8")), "line"


def _write_rpc_message(payload: Dict[str, Any], framing: str) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    out = sys.stdout.buffer
    if framing == "header":
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
        out.write(header)
        out.write(raw)
    else:
        out.write(raw + b"\n")
    out.flush()


def run_mcp_stdio_loop(role: str = "antigravity") -> None:
    server_name = "antigravity_bridge" if role == "codex" else "chatgpt-bridge"
    tools_list = CODEX_MCP_TOOLS if role == "codex" else AGY_MCP_TOOLS

    while True:
        try:
            msg, framing = _read_rpc_message()
        except Exception as exc:
            sys.stderr.write(f"[{server_name}] parse error: {exc}\n")
            sys.stderr.flush()
            continue

        if msg is None:
            break

        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params")

        if msg_id is None:
            continue

        if method == "initialize":
            p_dict = params if isinstance(params, dict) else {}
            client_proto = p_dict.get("protocolVersion") or "2024-11-05"
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": client_proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": server_name, "version": "2.3.0"},
                },
            }
            _write_rpc_message(resp, framing)
        elif method == "ping":
            _write_rpc_message({"jsonrpc": "2.0", "id": msg_id, "result": {}}, framing)
        elif method == "tools/list":
            _write_rpc_message(
                {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools_list}},
                framing,
            )
        elif method == "tools/call":
            if (
                not isinstance(params, dict)
                or not isinstance(params.get("name"), str)
                or not isinstance(params.get("arguments", {}), dict)
            ):
                _write_rpc_message(
                    {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "Invalid params"}},
                    framing,
                )
                continue
            t_name = params["name"]
            t_args = params.get("arguments", {})
            try:
                result_obj = _dispatch_mcp_tool(role, t_name, t_args)
            except OSError:
                result_obj = {"ok": False, "error_code": "IO_ERROR", "message": "本地桥接 I/O 操作失败。"}
            is_err = not bool(result_obj.get("ok", True))
            _write_rpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result_obj, ensure_ascii=False, indent=2),
                            }
                        ],
                        "isError": is_err,
                    },
                },
                framing,
            )
        else:
            _write_rpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                },
                framing,
            )


def run_cli(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="chatgpt-bridge / antigravity_bridge CLI")
    sub = parser.add_subparsers(dest="subcmd", required=True)

    sub.add_parser("inspect")
    sub.add_parser("read_latest")
    sub.add_parser("unbind")
    sub.add_parser("check_inbox")

    p_wait = sub.add_parser("wait_inbox")
    p_wait.add_argument("--timeout", type=int, default=600)

    p_rep = sub.add_parser("reply_codex")
    p_rep.add_argument("--request-id", default="")
    p_rep.add_argument("--decision", required=True, choices=["APPROVED", "REWORK_REQUIRED", "ESCALATE_TO_PRODUCER"])
    p_rep.add_argument("--feedback", required=True)
    p_rep.add_argument("--next", default="")

    args = parser.parse_args(argv)

    if args.subcmd == "inspect":
        res = tool_inspect_app()
    elif args.subcmd == "read_latest":
        res = tool_read_latest()
    elif args.subcmd == "unbind":
        res = tool_unbind_session()
    elif args.subcmd == "check_inbox":
        res = tool_check_codex_inbox()
    elif args.subcmd == "wait_inbox":
        deadline = time.monotonic() + max(5, args.timeout)
        res = {"ok": False, "timed_out": True, "message": "等待超时，未收到 Codex 新提交。"}
        while time.monotonic() < deadline:
            pending = _pending_submissions()
            if pending:
                res = {"ok": True, "event": "NEW_CODEX_SUBMISSION", "submission": pending[0], "pending_count": len(pending)}
                break
            time.sleep(0.05)
    elif args.subcmd == "reply_codex":
        res = tool_reply_to_codex(
            request_id=args.request_id,
            decision=args.decision,
            review_feedback=args.feedback,
            next_instruction=args.next,
        )
    else:
        res = {"ok": False, "message": "Unknown subcmd"}

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        sys.exit(run_cli(sys.argv[2:]))

    mcp_role = "antigravity"
    if "--role" in sys.argv:
        idx = sys.argv.index("--role")
        if idx + 1 < len(sys.argv):
            mcp_role = sys.argv[idx + 1].strip().lower()
    run_mcp_stdio_loop(role=mcp_role)
