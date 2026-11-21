#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Codex-Antigravity-Bridge (v2.3 Release):
Bidirectional local MCP Server & IPC bus between Antigravity (Tech Director) and OpenAI Codex Desktop (Worker).
- Role 'antigravity' (default): Window gatekeeper, 266ms fast UIA + 35ms lossless JSONL dispatch, and Codex inbox review tools.
- Role 'codex' (--role codex): Native MCP submission/reply tools so Codex NEVER needs `computer-use` GUI clicking.
Zero external third-party dependencies (standard Python 3.11+ on Windows).
"""

from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import msvcrt
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from headless_multi_engine import (  # noqa: E402
    BindingStore,
    HeadlessCodexRunner,
    discover_codex_exe,
    filter_submissions_by_workspace,
    ui_inject_lock,
)
from codex_projects import ProjectError, ensure_project  # noqa: E402

PS_DRIVER = BASE_DIR / "uia_driver.ps1"
PS_DAEMON = BASE_DIR / "uia_daemon.ps1"
LOCK_FILE = BASE_DIR / "session_lock.json"
BUS_DIR = BASE_DIR / "bus"
TO_AGY_FILE = BUS_DIR / "to_antigravity.json"
LATEST_TO_CODEX_FILE = BUS_DIR / "to_codex_latest.json"
REQUEST_ID = re.compile(r"req_[0-9]+_[0-9a-f]{6,32}")
DISPATCH_ID = re.compile(r"[0-9a-f]{32}")
CONVERSATION_ID = re.compile(r"[0-9a-zA-Z-]{32,128}")
PROJECT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
NATIVE_PROJECT_SESSION_REQUIRED = "ANTIGRAVITY_NATIVE_PROJECT_SESSION_REQUIRED"
MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{1,95}")

_WARM_PS_PROC: Optional[subprocess.Popen] = None
_WARM_PS_LOCK = threading.Lock()


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


@atexit.register
def _close_warm_ps() -> None:
    global _WARM_PS_PROC
    proc, _WARM_PS_PROC = _WARM_PS_PROC, None
    if proc is None:
        return
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return
    for pipe in (proc.stdin, proc.stdout):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _read_daemon_ack(stdout, timeout: float) -> None:
    ready = queue.Queue()

    def read_ack():
        try:
            ready.put(json.loads(stdout.readline().lstrip("\ufeff")))
        except (OSError, ValueError) as error:
            ready.put(error)

    threading.Thread(target=read_ack, daemon=True).start()
    try:
        ack = ready.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError("UIA daemon response timed out") from None
    if isinstance(ack, Exception):
        raise ack
    if not isinstance(ack, dict):
        raise ValueError("Invalid UIA daemon acknowledgement")
    if ack.get("daemon_ack") is not True:
        raise ValueError(ack.get("error", "Invalid UIA daemon acknowledgement"))


def _get_warm_ps() -> Optional[subprocess.Popen]:
    global _WARM_PS_PROC
    if _WARM_PS_PROC is not None and _WARM_PS_PROC.poll() is None:
        return _WARM_PS_PROC
    _close_warm_ps()
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


def _binding_store() -> BindingStore:
    return BindingStore(BASE_DIR, legacy_lock_path=LOCK_FILE)


def _headless_runner() -> HeadlessCodexRunner:
    return HeadlessCodexRunner(BASE_DIR)


def _load_lock() -> Optional[Dict[str, Any]]:
    data = _read_json_safe(LOCK_FILE)
    if isinstance(data, dict) and data.get("bound"):
        return data
    active = _binding_store().list_bindings()
    return active[0] if len(active) == 1 else None


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
    allow_placeholder_title: bool = False,
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

        with _WARM_PS_LOCK:
            warm = _get_warm_ps()
            if warm is not None and warm.stdin and warm.stdout:
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
                        "allow_placeholder_title": allow_placeholder_title,
                    },
                    ensure_ascii=False,
                )
                try:
                    warm.stdin.write(req_line + "\n")
                    warm.stdin.flush()
                    _read_daemon_ack(warm.stdout, timeout_sec + 15)
                    result = json.loads(out_file.read_text(encoding="utf-8"))
                    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                        raise ValueError("Invalid UIA driver result")
                    return result
                except (OSError, ValueError) as error:
                    _close_warm_ps()
                    if action in ("dispatch", "new_project_session", "switch_session"):
                        return {
                            "ok": False,
                            "error_code": "DAEMON_EXEC_UNCERTAIN",
                            "dispatch_id": dispatch_id,
                            "message": f"守护进程未能确认动作结果。请核实会话状态后再操作。{error}",
                        }
                    out_file.unlink(missing_ok=True)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                env={**os.environ, "CODEX_ANTIGRAVITY_BRIDGE_ALLOW_PLACEHOLDER_TITLE": str(int(allow_placeholder_title))},
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
    raw_text = re.sub(r"\[DIRECTOR_TASK\].*?(?:\[/DIRECTOR_TASK\]|\Z)", "", raw_text, flags=re.DOTALL)
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
        m = re.search(rf"(?m)^[^\S\r\n]*(?:#+[^\S\r\n]*)?\[{tag}\][^\S\r\n]*\r?$", raw_text)
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
        "summary": sections["summary"] if positions else raw_text.strip(),
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


_SPEED_ALIASES = {"default": "standard", "standard": "standard", "fast": "fast",
                  "priority": "fast", "balanced": "balanced", "ultrafast": "ultrafast"}
_SPEED_LABELS = {"standard": "标准", "balanced": "均衡", "fast": "快速", "ultrafast": "超快"}
_ULTRAFAST_DESCRIPTION = "The fastest available responses for latency-sensitive work."
_PROFILE_SIDES = {"decision": "antigravity", "worker": "codex"}
_PROFILE_FIELDS = ("side", "model", "effort", "speed")


def _load_routing_settings(path: Path) -> Dict[str, Any]:
    stored = _read_json_safe(path)
    stored = stored if isinstance(stored, dict) else {}
    ask = stored.get("ask_model_roles_on_plan")
    settings = {"ask_model_roles_on_plan": ask if isinstance(ask, bool) else True}
    for role, side in _PROFILE_SIDES.items():
        key = f"{role}_profile"
        profile = stored.get(key)
        settings[key] = {"side": side, "model": "", "effort": "", "speed": ""}
        if isinstance(profile, dict):
            settings[key].update({field: profile[field] for field in _PROFILE_FIELDS if isinstance(profile.get(field), str)})
    return settings


def _speed_catalog(*tiers: str, descriptions=None) -> list[dict]:
    descriptions = descriptions or {}
    return [{"id": tier, "label": _SPEED_LABELS[tier], "description": descriptions.get(tier, "")} for tier in tiers]


def _codex_catalog(home: Path) -> List[Dict[str, Any]]:
    cache = _read_json_safe(home / "models_cache.json")
    if not isinstance(cache, dict) or not isinstance(cache.get("models"), list):
        return [
            {
                "slug": slug, "display_name": name, "default_reasoning_level": default,
                "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
                "speed_tiers": _speed_catalog("standard", "fast", *extra,
                    descriptions={"ultrafast": _ULTRAFAST_DESCRIPTION}),
            }
            for slug, name, default, extra in (
                ("gpt-6.2-sol", "GPT-6.2-Sol", "low", ("ultrafast",)),
                ("gpt-6.2-astra", "GPT-6.2-Astra", "low", ()),
            )
        ]

    models = [
        item for item in cache["models"]
        if isinstance(item, dict) and item.get("visibility") == "list" and item.get("slug")
    ]
    models.sort(key=lambda item: item["priority"] if isinstance(item.get("priority"), (int, float)) else float("inf"))
    catalog = []
    for model in models:
        tiers = {"standard"}
        descriptions = {"ultrafast": _ULTRAFAST_DESCRIPTION}
        for tier in model.get("additional_speed_tiers") or []:
            if isinstance(tier, str) and tier in _SPEED_ALIASES:
                tiers.add(_SPEED_ALIASES[tier])
        for tier in model.get("service_tiers") or []:
            if isinstance(tier, dict) and isinstance(tier.get("id"), str) and tier["id"] in _SPEED_ALIASES:
                key = _SPEED_ALIASES[tier["id"]]
                tiers.add(key)
                if tier.get("description"):
                    descriptions[key] = tier["description"]
        catalog.append({
            "slug": model["slug"],
            "display_name": model.get("display_name") or model["slug"],
            "description": model.get("description", ""),
            "default_reasoning_level": model.get("default_reasoning_level", ""),
            "efforts": [level["effort"] for level in model.get("supported_reasoning_levels") or []
                        if isinstance(level, dict) and level.get("effort")],
            "speed_tiers": _speed_catalog(*(tier for tier in ("standard", "fast", "ultrafast") if tier in tiers),
                                        descriptions=descriptions),
        })
    return catalog


def _antigravity_catalog() -> Dict[str, Any]:
    return {
        "models": [
            {"slug": slug, "display_name": name, "family": family, "efforts": ["low", "medium", "high", "xhigh"],
             "speed_tiers": _speed_catalog("standard", "balanced", "fast")}
            for slug, name, family in (
                ("gemini-4-hydrogen", "Gemini 4 Hydrogen", "Gemini 4"),
                ("gemini-4-neon", "Gemini 4 Neon", "Gemini 4"),
                ("gemini-4-carbon", "Gemini 4 Carbon", "Gemini 4"),
                ("gemini-4-argon", "Gemini 4 Argon", "Gemini 4"),
                ("gemini-3.8-flash", "Gemini 3.8 Flash", "Gemini 3.8"),
                ("claude-opus-5.5", "Claude Opus 5.5", "Claude 5.5"),
                ("claude-sonnet-5.5", "Claude Sonnet 5.5", "Claude 5.5"),
                ("orchestrator", "Orchestrator", "Orchestrator"),
            )
        ],
        "quota_hint": '可叠加 swarm(action="models", all=true) 查询反重力侧实时可用性与配额。',
    }


def tool_routing_settings(action: str = "get", ask_model_roles_on_plan: Optional[bool] = None,
                          codex_home: str = "", **profiles: str) -> Dict[str, Any]:
    path = BASE_DIR / "director_settings.json"
    if action == "set":
        with _mutex(path.with_suffix(".mutex")):
            settings = _load_routing_settings(path)
            if ask_model_roles_on_plan is not None:
                settings["ask_model_roles_on_plan"] = ask_model_roles_on_plan
            for role in _PROFILE_SIDES:
                profile = settings[f"{role}_profile"]
                for field in _PROFILE_FIELDS:
                    value = profiles.get(f"{role}_{field}")
                    if value is not None:
                        value = value.strip()
                        profile[field] = _SPEED_ALIASES.get(value, value) if field == "speed" else value
            _atomic_write_json(path, settings)
    else:
        settings = _load_routing_settings(path)

    home = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    try:
        with (home / "config.toml").open("rb") as config_file:
            config = tomllib.load(config_file)
    except (OSError, ValueError):
        config = {}
    return {
        "ok": True,
        "settings": settings,
        "codex_catalog": _codex_catalog(home),
        "codex_current_defaults": {key: config.get(key) for key in ("model", "model_reasoning_effort", "service_tier")},
        "antigravity_catalog": _antigravity_catalog(),
    }


def _routing_tag() -> str:
    settings = _load_routing_settings(BASE_DIR / "director_settings.json")
    parts = []
    for role in _PROFILE_SIDES:
        profile = settings[f"{role}_profile"]
        if profile["model"]:
            parts.append(f'{role}={profile["side"]}:{profile["model"]}(effort={profile["effort"]},speed={profile["speed"]})')
    return " | ".join(parts)


def build_director_envelope(
    engine: str,
    step_goal: str,
    task_instruction: str,
    constraints: str = "",
    local_context: str = "",
    rework_round: int = 0,
    dispatch_id: str = "",
    lang: str = "",
    routing_tag: str = "",
    repository_read_only: bool = False,
    authorize_receipt: bool = False,
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

    lines = []
    if authorize_receipt:
        lines.append(
            "$stop-that-shit change -- Authorize only one agy_submit_dispatch_receipt call for this dispatch. "
            "The repository remains sandboxed read-only. This does not authorize other changes."
        )
    lines.append("[DIRECTOR_TASK]")
    if repository_read_only:
        lines.append("[REPOSITORY_ACCESS: read-only]")
    if authorize_receipt:
        lines.append(
            "[RECEIPT_AUTHORIZATION: once]\n"
            "After completing the task, call agy_submit_dispatch_receipt with your result. "
            "The MCP server binds it to this dispatch. Use the returned request_id in your final summary."
        )
    routing = routing_tag.strip() or _routing_tag()
    if routing:
        lines.append(routing if routing.startswith("[ROUTING:") else f"[ROUTING: {routing}]")
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
        "[PROTOCOL_REMINDER] Activate `codex-antigravity-bridge` skill rules. Reply strictly with sections: "
        f"[SUMMARY], [FILES], [SELF_CHECK], [OPEN_QUESTIONS] (write NONE if no blocking questions). {lang_rule}"
    )
    lines.append("[/DIRECTOR_TASK]")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Antigravity-Side Tool Implementations (Director Role)
# ---------------------------------------------------------------------------

def _is_placeholder_title(title: str) -> bool:
    return not title.strip() or bool(re.search(
        r"^(?:新聊天|新建聊天|New chat|New thread|Headless Session|Codex|ChatGPT|你说：|You said:|ChatGPT 说：|ChatGPT said:|\[SUMMARY\]|\[CONSTRAINTS\]|\d{1,2}:\d{2}(?:\s?[AP]M)?)$"
        r"|开始新聊天|你想让我们在|我们应该在|What should we", title.strip(), re.IGNORECASE,
    ))


def _update_first_title(lock: dict, snapshot: dict) -> Dict[str, Any]:
    title = str(snapshot.get("active_session") or "").strip()
    if lock.get("awaiting_first_title", _is_placeholder_title(lock["session_title"])) and (
        snapshot.get("title_authoritative") and not _is_placeholder_title(title)
    ):
        promoted = _binding_store().promote_first_title(str(lock["binding_id"]), title, authoritative=True)
        if isinstance(promoted, dict):
            lock.update(promoted)
            return lock
        lock.update(session_title=title, awaiting_first_title=False)
        _save_lock(lock)
    return lock


def tool_inspect_app() -> Dict[str, Any]:
    res = _run_ps_driver(action="inspect", timeout_sec=20)
    store = _binding_store()
    active_bindings = store.list_bindings()
    res["current_lock"] = active_bindings[0] if len(active_bindings) == 1 else _load_lock()
    res["active_bindings"] = active_bindings
    res["headless_codex_available"] = discover_codex_exe() is not None
    inbox = tool_check_codex_inbox()
    if inbox.get("has_pending"):
        res["pending_codex_inbox"] = {
            "pending_count": inbox.get("pending_count"),
            "pending_summaries": inbox["pending_summaries"],
            "oldest_pending": inbox["oldest_pending"],
            "latest_pending": inbox["latest_pending"],
        }
    return res


def _resolve_worker_model_profile(
    codex_model: str = "",
    codex_effort: str = "",
    codex_speed: str = "",
) -> Dict[str, str]:
    settings = _load_routing_settings(BASE_DIR / "director_settings.json")
    worker = settings.get("worker_profile") or {}
    default_from_routing = worker if worker.get("side") == "codex" else {}
    return {
        "model": (codex_model or default_from_routing.get("model") or "").strip(),
        "effort": (codex_effort or default_from_routing.get("effort") or "").strip(),
        "speed": (codex_speed or default_from_routing.get("speed") or "").strip(),
    }


def tool_bind_session(
    session_title: str = "",
    engine: str = "general",
    mode: str = "semi_auto",
    auto_apply_files: bool = False,
    max_retries: int = 3,
    hwnd: int = 0,
    create_in_project: str = "",
    switch_to_session: str = "",
    workspace_path: str = "",
    transport: str = "auto",
    codex_session_id: str = "",
    codex_model: str = "",
    codex_effort: str = "",
    codex_speed: str = "",
    reuse_workspace_binding: bool = False,
    ensure_codex_project: bool = False,
) -> Dict[str, Any]:
    proj = (create_in_project or "").strip()
    sw_sess = (switch_to_session or "").strip()
    title = (session_title or "").strip()
    ws_clean = (workspace_path or "").strip()
    transport_norm = (transport or "auto").strip().lower()
    model_profile = _resolve_worker_model_profile(codex_model, codex_effort, codex_speed)
    store = _binding_store()

    # Pure headless binding: works when both apps are minimized/closed and runs N sessions in parallel
    use_headless_direct = (
        transport_norm in ("codex_exec", "headless", "exec")
        or (transport_norm == "auto" and ws_clean and not (proj or sw_sess or title or hwnd))
    )
    if ensure_codex_project and (not use_headless_direct or not ws_clean):
        return {"ok": False, "error_code": "CODEX_PROJECT_REQUIRES_HEADLESS_WORKSPACE"}
    if use_headless_direct:
        if not ws_clean and not codex_session_id:
            return {
                "ok": False,
                "error_code": "EMPTY_WORKSPACE_FOR_HEADLESS",
                "message": "无头并发模式 (codex_exec) 需要提供 workspace_path 或 codex_session_id。",
            }
        if ws_clean:
            resolved = _existing_workspace(ws_clean)
            if resolved is None:
                return {"ok": False, "error_code": "WORKSPACE_NOT_FOUND", "workspace_path": ws_clean}
            ws_clean = resolved
        project = {}
        if ensure_codex_project:
            try:
                executable = discover_codex_exe()
                if executable is None:
                    raise ProjectError("CODEX_EXE_NOT_FOUND")
                project = {"status": "confirmed", **ensure_project(executable, ws_clean)}
            except ProjectError as error:
                project = {"status": "unavailable", "error_code": str(error)}
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                project = {"status": "unavailable", "error_code": "CODEX_PROJECT_API_UNAVAILABLE"}
        headless_title = title or (Path(ws_clean).name if ws_clean else "Headless Codex Session")
        lock_data = store.create_binding(
            session_title=headless_title,
            requested_title=headless_title,
            hwnd=0,
            pid=0,
            process_start=0,
            fingerprint=f"HEADLESS|{ws_clean}|{codex_session_id}",
            is_minimized=True,
            awaiting_first_title=False,
            workspace_path=ws_clean,
            transport="codex_exec",
            codex_session_id=codex_session_id,
            model_profile=model_profile,
            engine=engine,
            mode=mode,
            auto_apply_files=auto_apply_files,
            max_retries=max_retries,
            reuse_workspace=reuse_workspace_binding,
            codex_project_id=project.get("project_id", ""),
        )
        if not lock_data.get("binding_id"):
            return lock_data
        if lock_data.get("reused") and project.get("project_id"):
            store.update_binding(lock_data["binding_id"], codex_project_id=project["project_id"])
            lock_data = {**lock_data, "codex_project_id": project["project_id"]}
        return {
            "ok": True,
            "binding_id": lock_data["binding_id"],
            "transport": "codex_exec",
            "reused": bool(lock_data.get("reused")),
            "project": project,
            "message": f"已建立无头并发会话绑定: '{lock_data['session_title']}' (双端最小化与多会话并发就绪)",
            "lock": lock_data,
        }

    try:
        with ui_inject_lock(BASE_DIR):
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
                        "message": "必须提供 session_title、create_in_project、switch_to_session 或 workspace_path 之一。",
                    }
                inspect_res = _run_ps_driver(action="inspect", target_hwnd=hwnd, timeout_sec=20)
                if not inspect_res.get("ok"):
                    if transport_norm == "auto" and ws_clean and inspect_res.get("error_code") == "APP_NOT_RUNNING":
                        lock_data = store.create_binding(
                            session_title=title,
                            requested_title=title,
                            hwnd=0,
                            is_minimized=True,
                            awaiting_first_title=False,
                            workspace_path=ws_clean,
                            transport="codex_exec",
                            codex_session_id=codex_session_id,
                            model_profile=model_profile,
                            engine=engine,
                            mode=mode,
                            auto_apply_files=auto_apply_files,
                            max_retries=max_retries,
                        )
                        return {
                            "ok": True,
                            "binding_id": lock_data["binding_id"],
                            "transport": "codex_exec",
                            "message": f"桌面窗口未开启，已自动切换无头并发引擎绑定: '{lock_data['session_title']}'",
                            "lock": lock_data,
                        }
                    return inspect_res

                windows = inspect_res.get("windows", [])
                matched_win = None
                for w in windows:
                    if hwnd and int(w.get("hwnd", 0)) != int(hwnd):
                        continue
                    act = str(w.get("active_session", ""))
                    wt = str(w.get("window_title", ""))
                    same_title = act and (title.casefold() in act.casefold() or act.casefold() in title.casefold())
                    if same_title or title.casefold() in wt.casefold():
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
                            "可传入 switch_to_session 自动切换，或传入 workspace_path 启用无头并发会话。"
                        ),
                        "available_windows": available,
                    }
    except TimeoutError:
        return {"ok": False, "error_code": "BRIDGE_BUSY", "message": "UI 切换锁短暂繁忙，请稍后重试。"}

    act_sess = str(matched_win["active_session"])
    lock_data = store.create_binding(
        session_title=act_sess,
        requested_title=sw_sess or proj or title,
        hwnd=int(matched_win["hwnd"]),
        pid=int(matched_win["pid"]),
        process_start=int(matched_win.get("process_start", 0)),
        fingerprint=str(matched_win.get("fingerprint", "")),
        is_minimized=bool(matched_win.get("is_minimized", False)),
        awaiting_first_title=bool(proj or _is_placeholder_title(title) or _is_placeholder_title(act_sess)),
        workspace_path=ws_clean,
        transport="uia" if transport_norm == "uia" else "auto",
        codex_session_id=codex_session_id,
        model_profile=model_profile,
        engine=engine,
        mode=mode,
        auto_apply_files=auto_apply_files,
        max_retries=max_retries,
    )
    return {
        "ok": True,
        "binding_id": lock_data["binding_id"],
        "message": f"已成功锁定 ChatGPT 会话: '{lock_data['session_title']}' (HWND={lock_data['hwnd']})",
        "lock": lock_data,
    }


def _is_headless_binding(lock: Dict[str, Any]) -> bool:
    return lock.get("transport") == "codex_exec" or (
        int(lock.get("hwnd", 0)) == 0 and bool(lock.get("workspace_path") or lock.get("codex_session_id"))
    )


def tool_codex_session_model(binding_id: str, action: str = "get", model: str = "") -> Dict[str, Any]:
    store = _binding_store()
    binding, error = store.get_binding(binding_id)
    if error or not binding:
        return error or {"ok": False, "error_code": "SESSION_NOT_BOUND"}
    if not _is_headless_binding(binding):
        return {"ok": False, "error_code": "CODEX_DESKTOP_MODEL_NOT_SUPPORTED"}
    if action == "set":
        available = {entry["slug"]: entry for entry in _codex_catalog(Path.home() / ".codex")}
        profile = available.get(model)
        if profile is None:
            return {"ok": False, "error_code": "CODEX_MODEL_UNAVAILABLE", "requested_model": model}
        current = binding.get("model_profile") or {}
        updated_profile = {"model": model}
        if profile.get("efforts") and current.get("effort") not in profile["efforts"]:
            updated_profile["effort"] = ""
        speeds = {tier["id"] for tier in profile.get("speed_tiers", [])}
        if speeds and current.get("speed") not in speeds:
            updated_profile["speed"] = ""
        binding = store.update_binding(binding_id, model_profile=updated_profile)
        if not binding:
            return {"ok": False, "error_code": "SESSION_NOT_BOUND"}
    last_run = _headless_runner().read_latest_run(binding_id) or {}
    return {"ok": True, "binding_id": binding_id, "codex_session_id": binding.get("codex_session_id"),
            "next_model": (binding.get("model_profile") or {}).get("model") or None,
            "last_dispatched_model": last_run.get("requested_model"),
            "last_observed_model": last_run.get("observed_model"), "applies_to": "next_headless_turn"}


def tool_dispatch_task(
    step_goal: str,
    task_instruction: str,
    constraints: str = "",
    local_context: str = "",
    is_rework: bool = False,
    timeout_sec: int = 50,
    binding_id: str = "",
    routing_tag: str = "",
    repository_read_only: bool = False,
    authorize_receipt: bool = False,
    reply_format: str = "auto",
    wait_for_result: bool = True,
) -> Dict[str, Any]:
    store = _binding_store()
    lock, err = store.get_binding(binding_id)
    if err is not None or lock is None:
        return err or {"ok": False, "error_code": "SESSION_NOT_BOUND", "message": "未绑定任何会话。"}

    use_headless = _is_headless_binding(lock)
    if not use_headless and (reply_format == "natural" or not wait_for_result):
        return {"ok": False, "error_code": "REQUIRES_HEADLESS_BINDING"}
    if reply_format == "natural" and authorize_receipt:
        return {"ok": False, "error_code": "SCOPED_RECEIPT_REQUIRES_LEGACY"}
    if authorize_receipt and not repository_read_only:
        return {"ok": False, "error_code": "RECEIPT_REQUIRES_READ_ONLY"}
    if repository_read_only and (not use_headless or not lock.get("workspace_path")):
        return {"ok": False, "error_code": "READ_ONLY_REQUIRES_HEADLESS_WORKSPACE"}

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
        retry_val = next_retry
    else:
        retry_val = 0

    dispatch_id = uuid.uuid4().hex
    _atomic_write_json(_dispatch_path(dispatch_id), {
        "dispatch_id": dispatch_id,
        "binding_id": lock["binding_id"],
        "workspace_path": lock.get("workspace_path", ""),
        "dispatched_at": _now_iso(),
        "request_id": f"req_{int(time.time() * 1000)}_{dispatch_id}",
        "repository_read_only": repository_read_only,
        "receipt_authorized": authorize_receipt,
    })
    if not use_headless:
        lock = store.update_binding(str(lock["binding_id"]), retry_count=retry_val, last_dispatch_id=dispatch_id) or lock

    natural = use_headless and reply_format != "legacy" and not authorize_receipt
    envelope = build_director_envelope(
        engine=lock.get("engine", "UNREAL"),
        step_goal=step_goal,
        task_instruction=task_instruction,
        constraints=constraints,
        local_context=local_context,
        rework_round=retry_val,
        dispatch_id=dispatch_id,
        routing_tag=routing_tag,
        repository_read_only=repository_read_only,
        authorize_receipt=authorize_receipt,
    )
    if natural:
        envelope = "\n\n".join(part.strip() for part in (
            step_goal, task_instruction, constraints, local_context,
            "请用与任务相同的语言回复，说明结果、已完成的检查和任何卡点。" if detect_language(step_goal, task_instruction, constraints) == "zh-CN"
            else "Reply in the language of the request. Explain the outcome, checks, and any blockers.",
        ) if part.strip())

    # Cap synchronous MCP wait at 55s by default so host ~70s MCP call timeout never cancels long turns
    safe_sync_timeout = max(5, min(int(timeout_sec), 55)) if wait_for_result else 0

    if use_headless:
        driver_res = _headless_runner().dispatch(
            lock,
            dispatch_id=dispatch_id,
            envelope=envelope,
            timeout_sec=safe_sync_timeout,
            repository_read_only=repository_read_only,
            authorize_receipt=authorize_receipt,
            on_complete=lambda result: _record_execution(dispatch_id, result),
            carry_dispatch_id=natural,
            retry_count=retry_val,
        )
        if driver_res.get("codex_session_id"):
            lock["codex_session_id"] = driver_res["codex_session_id"]
    else:
        driver_res = _run_ps_driver(
            action="dispatch",
            expected_session=str(lock["session_title"]),
            target_hwnd=int(lock["hwnd"]),
            target_pid=int(lock.get("pid", 0)),
            dispatch_id=dispatch_id,
            payload_text=envelope,
            timeout_sec=safe_sync_timeout,
            allow_placeholder_title=lock.get("awaiting_first_title", _is_placeholder_title(lock["session_title"])),
        )
        if driver_res.get("ok") or driver_res.get("timed_out"):
            lock = _update_first_title(lock, driver_res)

    if not use_headless or (not driver_res.get("timed_out") and not _execution_path(dispatch_id).exists()):
        _record_execution(dispatch_id, driver_res)
    lifecycle = tool_dispatch_status(dispatch_id)
    if not driver_res.get("ok") and not driver_res.get("timed_out"):
        return {**driver_res, "binding_id": lock["binding_id"], "dispatch_id": dispatch_id, "lifecycle": lifecycle}

    timed_out = bool(driver_res.get("timed_out"))
    raw_reply = str(driver_res.get("raw_reply", ""))
    parsed = parse_worker_reply(raw_reply)
    telemetry = driver_res.get("telemetry") if isinstance(driver_res.get("telemetry"), dict) else {
        "ui_inject_ms": driver_res.get("ui_inject_ms"),
        "model_ttft_ms": driver_res.get("model_ttft_ms"),
        "model_duration_ms": driver_res.get("model_duration_ms"),
        "pickup_overhead_ms": driver_res.get("pickup_overhead_ms"),
        "total_elapsed_ms": driver_res.get("total_elapsed_ms"),
    }

    return {
        "ok": bool(driver_res.get("ok")) or timed_out,
        "timed_out": timed_out and wait_for_result,
        "status": driver_res.get("status") or ("awaiting_completion" if timed_out else "completed"),
        "reply_confirmed": not timed_out,
        "binding_id": lock["binding_id"],
        "codex_session_id": driver_res.get("codex_session_id", lock.get("codex_session_id", "")),
        "dispatch_id": dispatch_id,
        "lifecycle": lifecycle,
        "turn_id": driver_res.get("turn_id", ""),
        "channel": driver_res.get("channel", "uia"),
        "telemetry": telemetry,
        "requested_model": driver_res.get("requested_model"),
        "observed_model": driver_res.get("observed_model"),
        "message": driver_res.get("message", ""),
        "session_title": lock["session_title"],
        "mode": lock["mode"],
        "engine": lock["engine"],
        "rework_round": retry_val,
        "max_retries": lock["max_retries"],
        "parsed_reply": parsed,
        "raw_reply": raw_reply,
    }


def tool_read_latest(binding_id: str = "") -> Dict[str, Any]:
    store = _binding_store()
    lock, err = store.get_binding(binding_id)
    if err is not None or lock is None:
        return err or {
            "ok": False,
            "error_code": "SESSION_NOT_BOUND",
            "message": "当前未绑定会话，若仅探测状态请调用 bridge_inspect_app。",
        }

    if _is_headless_binding(lock):
        run_state = _headless_runner().read_latest_run(str(lock["binding_id"])) or {}
        last_msg = str(run_state.get("raw_reply") or "")
        is_running = run_state.get("status") in ("running", "running_async")
        return {
            "ok": True,
            "binding_id": lock["binding_id"],
            "channel": "codex_headless_exec",
            "status": run_state.get("status", "idle"),
            "dispatch_id": run_state.get("dispatch_id", ""),
            "codex_session_id": run_state.get("codex_session_id", lock.get("codex_session_id", "")),
            "snapshot": {
                "active_session": lock["session_title"],
                "last_message": last_msg,
                "is_generating": is_running,
                "is_minimized": True,
            },
            "parsed_reply": parse_worker_reply(last_msg),
        }

    res = _run_ps_driver(
        action="read_latest",
        expected_session=str(lock["session_title"]),
        target_hwnd=int(lock["hwnd"]),
        target_pid=int(lock.get("pid", 0)),
        timeout_sec=20,
        allow_placeholder_title=lock.get("awaiting_first_title", _is_placeholder_title(lock["session_title"])),
    )
    if res.get("ok") and "snapshot" in res:
        _update_first_title(lock, res["snapshot"])
        last_msg = str(res["snapshot"].get("last_message", ""))
        res["parsed_reply"] = parse_worker_reply(last_msg)
    return res


def tool_create_project_session(project_name: str, hwnd: int = 0) -> Dict[str, Any]:
    proj = (project_name or "").strip()
    if not proj:
        return {"ok": False, "error_code": "EMPTY_PROJECT", "message": "必须指定目标工程名称 (project_name)。"}
    with ui_inject_lock(BASE_DIR):
        return _run_ps_driver(action="new_project_session", project_name=proj, target_hwnd=hwnd, timeout_sec=20)


def tool_unbind_session(binding_id: str = "") -> Dict[str, Any]:
    return _binding_store().remove_binding(binding_id)


# ---------------------------------------------------------------------------
# Concurrent-Safe Multi-Request Inbox Bus (16+ parallel requests safe)
# ---------------------------------------------------------------------------

def _dispatch_path(dispatch_id: str) -> Path:
    return BUS_DIR / "dispatches" / f"{dispatch_id}.json"


def _execution_path(dispatch_id: str) -> Path:
    return _dispatch_path(dispatch_id).with_suffix(".execution.json")


def _load_dispatch(dispatch_id: str) -> Optional[Dict[str, Any]]:
    return _read_json_safe(_dispatch_path(dispatch_id)) if DISPATCH_ID.fullmatch(dispatch_id) else None


def _request_path(request_id: str) -> Path:
    return BUS_DIR / "requests" / f"{request_id}.json"


def _record_execution(dispatch_id: str, result: Dict[str, Any]) -> None:
    if result.get("timed_out") or result.get("error_code") == "DAEMON_EXEC_UNCERTAIN":
        status = "unconfirmed"
    else:
        status = "completed" if result.get("ok") else "failed"
    record = {"status": status, "observed_at": _now_iso()}
    if status in ("completed", "failed"):
        record["completed_at"] = result.get("completed_at") or record["observed_at"]
    for field in ("codex_session_id", "turn_id", "error_code", "raw_reply", "telemetry",
                  "requested_model", "observed_model", "project_attachment"):
        if result.get(field):
            record[field] = result[field]
    _atomic_write_json(_execution_path(dispatch_id), record)


def tool_dispatch_status(dispatch_id: str) -> Dict[str, Any]:
    dispatch = _load_dispatch(dispatch_id)
    if not dispatch:
        return {"ok": False, "error_code": "UNKNOWN_DISPATCH_ID", "dispatch_id": dispatch_id}

    request_id = dispatch["request_id"]
    request = _read_json_safe(_request_path(request_id))
    reply = _read_json_safe(_reply_path(request_id)) if request else None
    receipt: Dict[str, Any] = {"status": "awaiting"}
    if request:
        receipt = {
            "status": "acknowledged" if reply else "queued",
            "request_id": request_id,
            "queued_at": request["submitted_at"],
        }
        if reply:
            receipt["acknowledged_at"] = reply["replied_at"]
            receipt["decision"] = reply["decision"]
    elif dispatch["repository_read_only"] and not dispatch["receipt_authorized"]:
        receipt["status"] = "not_authorized"

    return {
        "ok": True,
        **dispatch,
        "execution": _read_json_safe(_execution_path(dispatch_id)) or {"status": "awaiting_completion"},
        "receipt": receipt,
    }


def _reply_path(request_id: str) -> Path:
    return BUS_DIR / f"to_codex_{request_id}.json"


def _existing_workspace(value: str) -> Optional[str]:
    if not value or not Path(value).is_absolute():
        return None
    try:
        path = Path(value).resolve(strict=True)
        return str(path) if path.is_dir() else None
    except (OSError, RuntimeError):
        return None


def _workspace_routes() -> Dict[str, Any]:
    return _read_json_safe(BUS_DIR / "workspace_routes.json") or {}


def _matching_workspace_route(
    submission: Dict[str, Any], routes: Iterable[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    return max(
        (route for route in routes if filter_submissions_by_workspace([submission], route["workspace_path"])),
        key=lambda route: len(route["workspace_path"]),
        default=None,
    )


class WorkspaceRouteError(ValueError):
    pass


def _workspace_from_uri(uri: str) -> Optional[str]:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme != "file":
        return None
    path = unquote(parts.path)
    if parts.netloc:
        path = f"//{parts.netloc}{path}"
    elif len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return _existing_workspace(path)


def _verify_local_project(project_id: str, workspace_key: str) -> None:
    path = BASE_DIR.parent.parent / "projects" / f"{project_id}.json"
    if not path.is_file():
        return
    project = _read_json_safe(path)
    if not isinstance(project, dict) or str(project.get("id", "")).lower() != project_id:
        raise WorkspaceRouteError("ANTIGRAVITY_PROJECT_UNCONFIRMED")
    if "sidecar" in project or project.get("isWorkspaceOnly") is True:
        raise WorkspaceRouteError("ANTIGRAVITY_SIDECAR_PROJECT_FORBIDDEN")
    container = project.get("projectResources")
    resources = container.get("resources") if isinstance(container, dict) else None
    if not isinstance(resources, list):
        raise WorkspaceRouteError("ANTIGRAVITY_WORKSPACE_UNCONFIRMED")
    for resource in resources:
        uri = resource.get("folderUri") if isinstance(resource, dict) else None
        root = _workspace_from_uri(uri) if isinstance(uri, str) else None
        if root and os.path.normcase(root) == workspace_key:
            return
    raise WorkspaceRouteError("ANTIGRAVITY_WORKSPACE_UNCONFIRMED")


def _project_verified_route(route: Dict[str, Any]) -> bool:
    expected = route.get("expected_project_id", "")
    return bool(expected and route.get("status") == "confirmed" and route.get("project_id") == expected)


def tool_workspace_pair(
    action: str, workspace_path: str, conversation_id: str = "", project_id: str = "",
) -> Dict[str, Any]:
    workspace = _existing_workspace(workspace_path)
    if workspace is None:
        return {"ok": False, "error_code": "WORKSPACE_NOT_FOUND"}
    key = os.path.normcase(workspace)
    path = BUS_DIR / "workspace_routes.json"
    with _mutex(path.with_suffix(".mutex")):
        routes = _workspace_routes()
        route = routes.get(key)
        if action == "get":
            if route and route["status"] == "confirmed" and not _project_verified_route(route):
                route = {**route, "status": "blocked", "error_code": "ANTIGRAVITY_PROJECT_UNCONFIRMED"}
            return {"ok": True, "workspace_path": workspace, "route": route}
        if action == "open":
            confirmed_project = route["project_id"] if route and _project_verified_route(route) else ""
            return {
                "ok": False,
                "error_code": NATIVE_PROJECT_SESSION_REQUIRED,
                "workspace_path": workspace,
                "project_id": confirmed_project,
                "message": "Open a conversation in the intended native Antigravity project, then link it with both IDs.",
            }
        if action == "link":
            if not CONVERSATION_ID.fullmatch(conversation_id):
                return {"ok": False, "error_code": "INVALID_CONVERSATION_ID"}
            if not PROJECT_ID.fullmatch(project_id):
                return {"ok": False, "error_code": "ANTIGRAVITY_PROJECT_ID_REQUIRED"}
            project_id = project_id.lower()
            try:
                _verify_local_project(project_id, key)
            except WorkspaceRouteError as error:
                return {"ok": False, "error_code": str(error)}
            if (route and route.get("conversation_id") == conversation_id
                    and route.get("expected_project_id") == project_id
                    and (_project_verified_route(route) or route["status"] == "verifying")):
                return {"ok": True, "route": route}
            route = {"workspace_path": workspace, "conversation_id": conversation_id,
                     "expected_project_id": project_id, "status": "verifying",
                     "since_ms": int(time.time() * 1000)}
        else:
            return {"ok": False, "error_code": "INVALID_ACTION"}
        routes[key] = route
        _atomic_write_json(path, routes)
    return {"ok": True, "route": route}


def _delivery_path(request_id: str) -> Path:
    return BUS_DIR / "deliveries" / f"{request_id}.json"


def _pending_request(request_id: str) -> Optional[Dict[str, Any]]:
    if not REQUEST_ID.fullmatch(request_id) or _reply_path(request_id).is_file():
        return None
    item = _read_json_safe(_request_path(request_id)) or _read_json_safe(TO_AGY_FILE)
    if item and item.get("request_id") == request_id and item.get("status") == "pending":
        return item
    return None


def _visible_request(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not item or not (item.get("target_conversation_id") or item.get("required_model")):
        return item
    return {key: item.get(key) for key in (
        "request_id", "dispatch_id", "status", "submitted_at", "queued_at_ms", "workspace_path",
        "target_conversation_id", "target_project_id", "required_model",
    )}


def _delivery_status(request_id: str) -> Dict[str, Any]:
    saved = _read_json_safe(_delivery_path(request_id)) or {}
    return {key: value for key, value in saved.items() if key not in ("claim_proof", "reply_proof")}


def _valid_host_proof(delivery: Dict[str, Any], purpose: str, proof: str) -> bool:
    grant = delivery.get(f"{purpose}_proof", {})
    return bool(proof and grant.get("value") == proof and grant.get("expires_at", 0) >= time.time())


def tool_claim_codex_request(request_id: str, model_proof: str = "") -> Dict[str, Any]:
    if not REQUEST_ID.fullmatch(request_id):
        return {"ok": False, "error_code": "INVALID_REQUEST_ID"}
    with _mutex(BUS_DIR / "replies.mutex"):
        item = _pending_request(request_id)
        if not item:
            return {"ok": False, "error_code": "UNKNOWN_REQUEST_ID"}
        state = _read_json_safe(_delivery_path(request_id)) or {}
        if not item.get("required_model"):
            if item.get("target_conversation_id"):
                if not _valid_host_proof(state, "claim", model_proof):
                    return {"ok": False, "error_code": "ANTIGRAVITY_TARGET_UNVERIFIED", "request_id": request_id}
                state.pop("claim_proof", None)
                state.setdefault("claimed_at", _now_iso())
                state["status"] = "claimed"
                _atomic_write_json(_delivery_path(request_id), state)
            return {"ok": True, "submission": item}
        if state.get("claimed_at"):
            return {"ok": False, "error_code": "ANTIGRAVITY_REQUEST_ALREADY_CLAIMED"}
        if not _valid_host_proof(state, "claim", model_proof):
            return {"ok": False, "error_code": "ANTIGRAVITY_MODEL_UNVERIFIED", "request_id": request_id}
        active_path = _active_model_path(item["target_conversation_id"])
        if _active_guarded_request(item["target_conversation_id"]):
            state.pop("claim_proof")
            state["status"] = "notified" if state.get("notified_at") else "queued"
            _atomic_write_json(_delivery_path(request_id), state)
            return {"ok": False, "error_code": "ANTIGRAVITY_CONVERSATION_BUSY"}
        grant = state.pop("claim_proof")
        state.update(status="claimed", claimed_at=_now_iso(), execution_model=grant["model"])
        _atomic_write_json(_delivery_path(request_id), state)
        _atomic_write_json(active_path, {"request_id": request_id})
        return {"ok": True, "submission": item, "execution_model": state["execution_model"]}


def _active_model_path(conversation_id: str) -> Path:
    return BUS_DIR / "deliveries" / f"active_{conversation_id}.json"


def _active_guarded_request(conversation_id: str) -> Optional[Dict[str, Any]]:
    active = _read_json_safe(_active_model_path(conversation_id)) or {}
    item = _pending_request(active.get("request_id", ""))
    if item and item.get("target_conversation_id") == conversation_id:
        return item
    return None


def _clear_active_guard(item: Dict[str, Any]) -> None:
    if not item.get("required_model"):
        return
    path = _active_model_path(item["target_conversation_id"])
    if (_read_json_safe(path) or {}).get("request_id") == item["request_id"]:
        path.unlink(missing_ok=True)


def codex_cancel_guarded_review(request_id: str) -> Dict[str, Any]:
    with _mutex(BUS_DIR / "replies.mutex"):
        item = _pending_request(request_id)
        if not item or not item.get("required_model"):
            return {"ok": False, "error_code": "UNKNOWN_GUARDED_REQUEST"}
        state = _read_json_safe(_delivery_path(request_id)) or {}
        if state.get("claimed_at") and state.get("status") != "blocked":
            return {"ok": False, "error_code": "ANTIGRAVITY_REQUEST_ALREADY_CLAIMED"}
        _atomic_write_json(_request_path(request_id), {**item, "status": "cancelled"})
        if state:
            state.pop("claim_proof", None)
            state.pop("reply_proof", None)
            state.update(status="cancelled", cancelled_at=_now_iso())
            _atomic_write_json(_delivery_path(request_id), state)
        _clear_active_guard(item)
        return {"ok": True, "request_id": request_id, "status": "cancelled"}


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


def tool_check_codex_inbox(request_id: str = "", workspace_path: str = "") -> Dict[str, Any]:
    req_id = (request_id or "").strip()
    if req_id:
        item = _pending_request(req_id)
        candidates = [item] if item else []
    else:
        candidates = _pending_submissions()
    pending = [_visible_request(item) for item in filter_submissions_by_workspace(candidates, workspace_path)]
    submission = pending[0] if pending else None
    return {
        "ok": True,
        "has_pending": bool(pending),
        "pending_count": len(pending),
        "submission": submission,
        "oldest_pending": pending[0] if pending else None,
        "latest_pending": pending[-1] if pending else None,
        "pending_summaries": [
            {key: item.get(key, "") for key in ("request_id", "dispatch_id", "submitted_at", "queued_at_ms",
                                               "workspace_path", "step_summary", "required_model", "target_conversation_id",
                                               "target_project_id")}
            for item in pending
        ],
        "pending_request_ids": [item["request_id"] for item in pending],
    }


def tool_reply_to_codex(
    request_id: str = "",
    decision: str = "APPROVED",
    review_feedback: str = "",
    next_instruction: str = "",
    model_proof: str = "",
) -> Dict[str, Any]:
    if decision not in ("APPROVED", "REWORK_REQUIRED", "ESCALATE_TO_PRODUCER"):
        return {"ok": False, "error_code": "INVALID_DECISION", "message": f"非法评审结论: {decision}"}

    _ensure_bus_dir()
    with _mutex(BUS_DIR / "replies.mutex"):
        req_id = (request_id or "").strip()
        if not req_id:
            pending = _pending_submissions()
            if not pending:
                return {"ok": False, "error_code": "NO_PENDING_REQUESTS", "message": "当前没有待评审单。"}
            if len(pending) > 1:
                return {
                    "ok": False, "error_code": "AMBIGUOUS_PENDING_REQUESTS",
                    "message": "存在多张待评审单，请指定 request_id。",
                    "pending_request_ids": [item["request_id"] for item in pending],
                }
            req_id = pending[0]["request_id"]
        if not REQUEST_ID.fullmatch(req_id):
            return {"ok": False, "error_code": "UNKNOWN_REQUEST_ID", "request_id": req_id}

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

        request = _pending_request(req_id)
        if request is None:
            return {"ok": False, "error_code": "UNKNOWN_REQUEST_ID", "request_id": req_id}

        state = _read_json_safe(_delivery_path(req_id)) or {}
        if request.get("required_model"):
            if not state.get("claimed_at") or not _valid_host_proof(state, "reply", model_proof):
                return {"ok": False, "error_code": "ANTIGRAVITY_MODEL_UNVERIFIED", "request_id": req_id}
        elif request.get("target_conversation_id") and not _valid_host_proof(state, "reply", model_proof):
            return {"ok": False, "error_code": "ANTIGRAVITY_TARGET_UNVERIFIED", "request_id": req_id}

        reply_payload = {
            "ok": True,
            "request_id": req_id,
            "replied_at": _now_iso(),
            **fields,
        }
        if request.get("dispatch_id"):
            reply_payload["dispatch_id"] = request["dispatch_id"]
        if request.get("target_conversation_id"):
            reply_payload["target_conversation_id"] = request["target_conversation_id"]
        if request.get("target_project_id"):
            reply_payload["target_project_id"] = request["target_project_id"]
        if request.get("required_model"):
            reply_payload["execution_model"] = state["execution_model"]
            reply_payload["reply_model"] = state["reply_proof"]["model"]
            reply_payload["required_model"] = request["required_model"]
        _atomic_write_json(path, reply_payload)
        _atomic_write_json(LATEST_TO_CODEX_FILE, reply_payload)
        if state:
            state.pop("reply_proof", None)
            state.update(status="replied", replied_at=reply_payload["replied_at"])
            _atomic_write_json(_delivery_path(req_id), state)

        req_file = _request_path(req_id)
        req_doc = _read_json_safe(req_file)
        if isinstance(req_doc, dict):
            req_doc["status"] = "replied"
            req_doc["replied_at"] = reply_payload["replied_at"]
            _atomic_write_json(req_file, req_doc)
        _clear_active_guard(request)

    return {
        "ok": True,
        "message": f"已通过本地 IPC 总线向 Codex 返回批复 (request_id={req_id}, decision={decision})。",
        "reply": reply_payload,
    }


# ---------------------------------------------------------------------------
# Codex-Side Tool Implementations (Worker Role: ZERO computer-use needed!)
# ---------------------------------------------------------------------------

def _queue_dispatch_receipt(dispatch: Dict[str, Any], submission: Dict[str, Any]) -> Dict[str, Any]:
    request_id = dispatch["request_id"]
    path = _request_path(request_id)
    submission = {
        **submission,
        "queued_at_ms": int(time.time() * 1000),
        "request_id": request_id,
        "dispatch_id": dispatch["dispatch_id"],
        "workspace_path": dispatch["workspace_path"] or submission["workspace_path"],
    }
    with _mutex(BUS_DIR / "replies.mutex"):
        existing = _read_json_safe(path)
        if existing:
            fields = ("step_summary", "self_check", "open_questions", "changed_files", "code_or_diff")
            if any(existing.get(field) != submission.get(field) for field in fields):
                return {"ok": False, "error_code": "RECEIPT_ALREADY_SUBMITTED", "request_id": request_id}
        else:
            _atomic_write_json(path, submission)
    return {
        "ok": True,
        "dispatch_id": dispatch["dispatch_id"],
        "request_id": request_id,
        "status": "queued",
        "already_queued": bool(existing),
    }


def codex_tool_submit_dispatch_receipt(scope: str, step_summary: str, self_check: str = "") -> Dict[str, Any]:
    dispatch = _load_dispatch(scope)
    if not dispatch or not dispatch.get("receipt_authorized"):
        return {"ok": False, "error_code": "RECEIPT_NOT_AUTHORIZED"}
    if not step_summary.strip():
        return {"ok": False, "error_code": "EMPTY_RECEIPT"}
    return _queue_dispatch_receipt(dispatch, {
        "status": "pending",
        "submitted_at": _now_iso(),
        "workspace_path": dispatch["workspace_path"],
        "step_summary": step_summary,
        "changed_files": "",
        "code_or_diff": "",
        "self_check": self_check,
        "open_questions": "NONE",
    })


def codex_tool_submit_for_review(
    step_summary: str,
    changed_files: str = "",
    code_or_diff: str = "",
    self_check: str = "",
    open_questions: str = "NONE",
    workspace_path: str = "",
    wait_for_reply: bool = True,
    wait_timeout_sec: int = 240,
    dispatch_id: str = "",
    required_model: str = "",
    target_conversation_id: str = "",
) -> Dict[str, Any]:
    dispatch = _load_dispatch(dispatch_id) if dispatch_id else None
    if dispatch_id and not dispatch:
        return {"ok": False, "error_code": "UNKNOWN_DISPATCH_ID", "dispatch_id": dispatch_id}
    if dispatch and dispatch["repository_read_only"]:
        return {"ok": False, "error_code": "SCOPED_RECEIPT_REQUIRED", "dispatch_id": dispatch_id}
    if dispatch and workspace_path and dispatch["workspace_path"] and not filter_submissions_by_workspace(
        [dispatch], workspace_path,
    ):
        return {"ok": False, "error_code": "DISPATCH_WORKSPACE_MISMATCH", "dispatch_id": dispatch_id}

    resolved_workspace = _existing_workspace(workspace_path or os.getcwd())
    route = None
    if resolved_workspace and not dispatch:
        route = _matching_workspace_route({"workspace_path": resolved_workspace}, _workspace_routes().values())
    if not dispatch and route and not _project_verified_route(route):
        return {"ok": False, "error_code": route.get("error_code", "ANTIGRAVITY_PROJECT_UNCONFIRMED")}
    if required_model or target_conversation_id:
        if dispatch:
            return {"ok": False, "error_code": "MODEL_REQUIREMENT_NOT_SUPPORTED_FOR_DISPATCH_RECEIPTS"}
        if not MODEL_ID.fullmatch(required_model):
            return {"ok": False, "error_code": "INVALID_ANTIGRAVITY_MODEL"}
        if not workspace_path or not route or route["conversation_id"] != target_conversation_id:
            return {"ok": False, "error_code": "ANTIGRAVITY_TARGET_UNCONFIRMED"}

    req_id = dispatch["request_id"] if dispatch else f"req_{int(time.time() * 1000)}_{uuid.uuid4().hex[:12]}"
    submission = {
        "request_id": req_id,
        "status": "pending",
        "submitted_at": _now_iso(),
        "queued_at_ms": int(time.time() * 1000),
        "workspace_path": resolved_workspace or workspace_path or os.getcwd(),
        "step_summary": step_summary,
        "changed_files": changed_files,
        "code_or_diff": code_or_diff,
        "self_check": self_check,
        "open_questions": open_questions,
    }
    if not dispatch and route:
        submission.update(target_conversation_id=route["conversation_id"], target_project_id=route["project_id"])
    if required_model:
        submission["required_model"] = required_model
    if dispatch:
        queued = _queue_dispatch_receipt(dispatch, submission)
        if not queued["ok"]:
            return queued
    else:
        _atomic_write_json(_request_path(req_id), submission)
        queued = {
            "ok": True,
            "request_id": req_id,
            "status": "queued",
            "message": "已投递至 Antigravity 技术总监收件箱（非阻塞模式）。稍后可调用 agy_fetch_director_reply 获取评审结论。",
        }

    if not wait_for_reply or required_model:
        return {**queued, **({"required_model": required_model, "target_conversation_id": target_conversation_id}
                            if required_model else {})}

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
        **({"dispatch_id": dispatch_id} if dispatch else {}),
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
            if rep.get("required_model"):
                return {**rep, "status": "replied", "delivery": _delivery_status(req_id)}
            return rep
        item = _pending_request(req_id)
        if item is None:
            record = _read_json_safe(_request_path(req_id))
            if record and record.get("status") == "cancelled":
                return {"ok": True, "request_id": req_id, "status": "cancelled"}
            return {"ok": False, "request_id": req_id, "error_code": "UNKNOWN_REQUEST_ID"}
        delivery = _delivery_status(req_id)
        if item.get("required_model"):
            delivery = {"status": "queued", "required_model": item["required_model"],
                        "target_conversation_id": item["target_conversation_id"], **delivery}
        return {"ok": True, "request_id": req_id, "status": "pending", "delivery": delivery,
                "message": f"请求 {req_id} 仍在等待批复。"}
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

def _routing_tool(name: str) -> dict:
    return {
        "name": name,
        "description": "读取双侧模型、推理强度和速度目录，或保存计划阶段的决策和施工选型。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "set"], "default": "get"},
                "ask_model_roles_on_plan": {"type": "boolean"},
                "codex_home": {"type": "string"},
                **{
                    f"{role}_{field}": (
                        {"type": "string", "enum": list(_PROFILE_SIDES.values())}
                        if field == "side" else {"type": "string"}
                    )
                    for role in _PROFILE_SIDES for field in _PROFILE_FIELDS
                },
            },
            "additionalProperties": False,
        },
    }


AGY_MCP_TOOLS: List[Dict[str, Any]] = [
    _routing_tool("bridge_routing_settings"),
    {
        "name": "bridge_codex_session_model",
        "description": "读取或为指定无头 Codex 会话设置下一轮模型。运行中的轮次继续使用自己的模型。",
        "inputSchema": {"type": "object", "properties": {
            "binding_id": {"type": "string"},
            "action": {"type": "string", "enum": ["get", "set"]},
            "model": {"type": "string"},
        }, "required": ["binding_id"], "additionalProperties": False},
    },
    {
        "name": "bridge_claim_codex_request",
        "description": "按编号领取入箱任务。已配对任务须由宿主钩子核验目标会话，指定模型的任务还须核验模型。",
        "inputSchema": {"type": "object", "properties": {
            "request_id": {"type": "string"},
            "model_proof": {"type": "string", "description": "由 Antigravity Hook 填充。"},
        }, "required": ["request_id"], "additionalProperties": False},
    },
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
            "多会话并发加锁与快连工具：支持无头并发引擎 (codex_exec，双端最小化可用、多工程并行) "
            "或桌面窗口绑定 (支持 session_title / create_in_project / switch_to_session)。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_title": {"type": "string", "description": "当前激活会话标题。"},
                "create_in_project": {"type": "string", "description": "一步在指定侧边栏工程下新建会话并锁定。"},
                "switch_to_session": {"type": "string", "description": "一步切换到侧边栏已有会话并锁定。"},
                "workspace_path": {"type": "string", "description": "本地工程路径（传入即可启用 codex.exe 无头并发与最小化支持）。"},
                "transport": {"type": "string", "enum": ["auto", "codex_exec", "uia"]},
                "codex_session_id": {"type": "string", "description": "可选：续接已有的 Codex 会话 ID。"},
                "codex_model": {"type": "string", "description": "可选：无头模式指定模型（如 gpt-6.2-sol）。"},
                "codex_effort": {"type": "string", "description": "可选：无头模式指定推理强度（如 high / xhigh / ultra）。"},
                "codex_speed": {"type": "string", "description": "可选：无头模式指定速度（standard / fast / ultrafast）。"},
                "engine": {"type": "string", "enum": ["unreal", "threejs", "general"]},
                "mode": {"type": "string", "enum": ["semi_auto", "autopilot"]},
                "auto_apply_files": {"type": "boolean"},
                "max_retries": {"type": "integer"},
                "hwnd": {"type": "integer"},
                "reuse_workspace_binding": {"type": "boolean"},
                "ensure_codex_project": {"type": "boolean", "description": "显式允许用实验版 App Server 创建或复用桌面项目。"},
            },
        },
    },
    {
        "name": "bridge_dispatch_task",
        "description": "向指定 binding_id 会话无阻塞并发派发指令（支持无头 codex.exe 双端最小化并发与 35ms JSONL 轮询直连）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "step_goal": {"type": "string"},
                "task_instruction": {"type": "string"},
                "constraints": {"type": "string"},
                "local_context": {"type": "string"},
                "routing_tag": {"type": "string"},
                "binding_id": {"type": "string"},
                "is_rework": {"type": "boolean"},
                "timeout_sec": {"type": "integer"},
                "reply_format": {"type": "string", "enum": ["auto", "legacy", "natural"]},
                "wait_for_result": {"type": "boolean"},
                "repository_read_only": {"type": "boolean", "description": "无头 Codex 使用 read-only 沙箱。"},
                "authorize_receipt": {"type": "boolean", "description": "为这个只读任务授权写入一条 MCP 回执。"},
            },
            "required": ["step_goal", "task_instruction"],
        },
    },
    {
        "name": "bridge_dispatch_status",
        "description": "按派发 ID 核对执行、回执入箱和 Antigravity 批复确认状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"dispatch_id": {"type": "string"}},
            "required": ["dispatch_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bridge_read_latest",
        "description": "不发消息，按 binding_id 读取对应会话最新回复（支持多会话并发与后台异步完成回收）。",
        "inputSchema": {
            "type": "object",
            "properties": {"binding_id": {"type": "string"}},
            "additionalProperties": False,
        },
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
        "description": "查看 Codex 收件箱。已配对任务仅显示摘要字段，正文通过 bridge_claim_codex_request 领取。",
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}, "workspace_path": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "bridge_reply_to_codex",
        "description": "通过双边 MCP 总线在 40ms 内直接向等待中的 Codex 返回评审结论与下一步修改指令（零 UI 粘贴）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "收件箱中的 request_id（仅有一张待审单时可留空）。"},
                "decision": {
                    "type": "string",
                    "enum": ["APPROVED", "REWORK_REQUIRED", "ESCALATE_TO_PRODUCER"],
                    "description": "评审决定：通过 / 打回修改 / 需交由游戏制作人拍板。",
                },
                "review_feedback": {"type": "string", "description": "具体的代码或几何/架构审查意见。"},
                "next_instruction": {"type": "string", "description": "指示 Codex 立即执行的下一步指令。"},
                "model_proof": {"type": "string", "description": "由 Antigravity Hook 填充。"},
            },
            "required": ["decision", "review_feedback"],
        },
    },
]

CODEX_MCP_TOOLS: List[Dict[str, Any]] = [
    _routing_tool("agy_routing_settings"),
    {
        "name": "agy_workspace_pair",
        "description": "按真实目录和原生项目 ID 配对已有 Antigravity 会话。link 需提供 project_id 与 conversation_id，get 查询宿主核验结果。open 返回原生项目会话要求。",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["get", "link", "open"]},
            "workspace_path": {"type": "string"},
            "conversation_id": {"type": "string"},
            "project_id": {"type": "string", "description": "从预期 Antigravity 原生项目独立取得的项目 UUID。"},
        }, "required": ["action", "workspace_path"], "additionalProperties": False},
    },
    {
        "name": "agy_cancel_guarded_review",
        "description": "取消尚未领取或因模型不匹配而阻塞的指定模型请求。",
        "inputSchema": {"type": "object", "properties": {"request_id": {"type": "string"}},
                        "required": ["request_id"], "additionalProperties": False},
    },
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
                "self_check": {"type": "string", "description": "按 codex-antigravity-bridge 规范执行的引擎红线自检说明。"},
                "open_questions": {"type": "string", "description": "需技术总监或制作人裁决的问题，无则填 NONE。"},
                "workspace_path": {"type": "string", "description": "当前工程根目录路径。"},
                "wait_for_reply": {
                    "type": "boolean",
                    "description": "是否阻塞等待 Antigravity 实时写回评审结论（默认 true）。",
                },
                "dispatch_id": {"type": "string", "description": "可选。对应普通派发的 DISPATCH_ID。"},
                "required_model": {"type": "string", "description": "必须执行本请求的 Antigravity 模型，由宿主钩子校验。"},
                "target_conversation_id": {"type": "string", "description": "已按工作区验证的 Antigravity 会话 ID。"},
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
        "annotations": {"readOnlyHint": True},
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


DISPATCH_RECEIPT_TOOL = {
    "name": "agy_submit_dispatch_receipt",
    "description": "向 Antigravity 写入当前只读派发的一条授权回执。工作区和派发 ID 由服务器绑定。",
    "inputSchema": {
        "type": "object",
        "properties": {"step_summary": {"type": "string"}, "self_check": {"type": "string"}},
        "required": ["step_summary"],
        "additionalProperties": False,
    },
}


def _scoped_codex_tools(scope: str) -> List[Dict[str, Any]]:
    tools = [tool for tool in CODEX_MCP_TOOLS if tool["name"] == "agy_fetch_director_reply"]
    dispatch = _load_dispatch(scope)
    if dispatch and dispatch.get("receipt_authorized"):
        tools.append(DISPATCH_RECEIPT_TOOL)
    return tools


def _dispatch_mcp_tool(role: str, name: str, args: Dict[str, Any], receipt_scope: str = "") -> Dict[str, Any]:
    if role == "codex" and receipt_scope:
        tools = _scoped_codex_tools(receipt_scope)
        if name not in {tool["name"] for tool in tools}:
            return {"ok": False, "error_code": "DISPATCH_SCOPE_FORBIDS_TOOL"}
        arg_err = _validate_args(tools, name, args)
        if arg_err:
            return arg_err
        if name == DISPATCH_RECEIPT_TOOL["name"]:
            return codex_tool_submit_dispatch_receipt(receipt_scope, **args)
        dispatch = _load_dispatch(receipt_scope)
        if not dispatch or args.get("request_id", dispatch["request_id"]) != dispatch["request_id"]:
            return {"ok": False, "error_code": "DISPATCH_SCOPE_MISMATCH"}
        return codex_tool_fetch_director_reply(dispatch["request_id"])

    tools = CODEX_MCP_TOOLS if role == "codex" else AGY_MCP_TOOLS
    arg_err = _validate_args(tools, name, args)
    if arg_err is not None:
        return arg_err

    if name == ("agy_routing_settings" if role == "codex" else "bridge_routing_settings"):
        return tool_routing_settings(**args)

    if role == "codex":
        if name == "agy_workspace_pair":
            return tool_workspace_pair(**args)
        if name == "agy_cancel_guarded_review":
            return codex_cancel_guarded_review(args["request_id"])
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
                dispatch_id=args.get("dispatch_id") or os.environ.get("CODEX_BRIDGE_DISPATCH_ID", ""),
                required_model=args.get("required_model", ""),
                target_conversation_id=args.get("target_conversation_id", ""),
            )
        if name == "agy_fetch_director_reply":
            return codex_tool_fetch_director_reply(request_id=args.get("request_id", ""))
        if name == "agy_bridge_status":
            return codex_tool_bridge_status()
        return {"ok": False, "error_code": "UNKNOWN_CODEX_TOOL", "message": f"未知 Codex 侧工具: {name}"}

    if name == "bridge_inspect_app":
        return tool_inspect_app()
    if name == "bridge_codex_session_model":
        return tool_codex_session_model(**args)
    if name == "bridge_bind_session":
        return tool_bind_session(
            session_title=args.get("session_title", ""),
            engine=args.get("engine", "general"),
            mode=args.get("mode", "semi_auto"),
            auto_apply_files=bool(args.get("auto_apply_files", False)),
            max_retries=int(args.get("max_retries", 3)),
            hwnd=int(args.get("hwnd", 0)),
            create_in_project=args.get("create_in_project", ""),
            switch_to_session=args.get("switch_to_session", ""),
            workspace_path=args.get("workspace_path", ""),
            transport=args.get("transport", "auto"),
            codex_session_id=args.get("codex_session_id", ""),
            codex_model=args.get("codex_model", ""),
            codex_effort=args.get("codex_effort", ""),
            codex_speed=args.get("codex_speed", ""),
            reuse_workspace_binding=bool(args.get("reuse_workspace_binding", False)),
            ensure_codex_project=bool(args.get("ensure_codex_project", False)),
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
            routing_tag=args.get("routing_tag", ""),
            repository_read_only=bool(args.get("repository_read_only", False)),
            authorize_receipt=bool(args.get("authorize_receipt", False)),
            reply_format=args.get("reply_format", "auto"),
            wait_for_result=bool(args.get("wait_for_result", True)),
        )
    if name == "bridge_dispatch_status":
        return tool_dispatch_status(args["dispatch_id"])
    if name == "bridge_read_latest":
        return tool_read_latest(binding_id=args.get("binding_id", ""))
    if name == "bridge_create_project_session":
        return tool_create_project_session(
            project_name=args.get("project_name", ""),
            hwnd=int(args.get("hwnd", 0)),
        )
    if name == "bridge_unbind_session":
        return tool_unbind_session(binding_id=args.get("binding_id", ""))
    if name == "bridge_check_codex_inbox":
        return tool_check_codex_inbox(request_id=args.get("request_id", ""), workspace_path=args.get("workspace_path", ""))
    if name == "bridge_claim_codex_request":
        return tool_claim_codex_request(**args)
    if name == "bridge_reply_to_codex":
        return tool_reply_to_codex(
            request_id=args.get("request_id", ""),
            decision=args.get("decision", "APPROVED"),
            review_feedback=args.get("review_feedback", ""),
            next_instruction=args.get("next_instruction", ""),
            model_proof=args.get("model_proof", ""),
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
    receipt_scope = os.environ.get("CODEX_BRIDGE_RECEIPT_SCOPE", "") if role == "codex" else ""
    tools_list = CODEX_MCP_TOOLS if role == "codex" else AGY_MCP_TOOLS
    if receipt_scope:
        tools_list = _scoped_codex_tools(receipt_scope)

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
                result_obj = _dispatch_mcp_tool(role, t_name, t_args, receipt_scope)
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

    parser = argparse.ArgumentParser(description="Codex-Antigravity-Bridge CLI")
    sub = parser.add_subparsers(dest="subcmd", required=True)

    sub.add_parser("inspect")
    sub.add_parser("read_latest")
    sub.add_parser("unbind")
    sub.add_parser("check_inbox")

    p_wait = sub.add_parser("wait_inbox")
    p_wait.add_argument("--timeout", type=int, default=600)
    p_wait.add_argument("--only-new", action="store_true", help="Ignore requests already pending when the wait starts.")

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
        seen = {item["request_id"] for item in _pending_submissions()} if args.only_new else set()
        deadline = time.monotonic() + max(5, args.timeout)
        res = {"ok": False, "timed_out": True, "message": "等待超时，未收到 Codex 新提交。"}
        while time.monotonic() < deadline:
            pending = _pending_submissions()
            submission = next((item for item in pending if item["request_id"] not in seen), None)
            if submission:
                res = {"ok": True, "event": "NEW_CODEX_SUBMISSION", "submission": _visible_request(submission), "pending_count": len(pending)}
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
