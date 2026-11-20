#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
headless_multi_engine.py:
Multi-session concurrent registry, dual-minimized headless `codex.exe exec` engine,
short-lived UIA injection mutex, and workspace-isolated inbox filtering for codex-director.

Zero external third-party dependencies (Python 3.11+ standard library on Windows).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None  # type: ignore[assignment]

CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000 if os.name == "nt" else 0)

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

PLACEHOLDER_SESSION_TITLES = frozenset(
    {
        "",
        "新聊天",
        "新对话",
        "新线程",
        "新建会话",
        "new chat",
        "new thread",
        "new session",
        "untitled",
        "codex",
        "chatgpt",
    }
)

_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: Dict[str, threading.Lock] = {}


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _get_thread_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(str(path)))
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


@contextmanager
def _file_mutex(path: Path, timeout_sec: float = 5.0) -> Iterator[None]:
    """Cross-thread and cross-process short critical-section file lock using msvcrt.locking."""
    path.parent.mkdir(parents=True, exist_ok=True)
    t_lock = _get_thread_lock(path)
    deadline = time.monotonic() + max(0.05, float(timeout_sec))

    remaining = max(0.01, deadline - time.monotonic())
    if not t_lock.acquire(timeout=remaining):
        raise TimeoutError(f"Mutex timed out acquiring thread lock: {path}")

    try:
        with path.open("a+b") as handle:
            if msvcrt is not None:
                while True:
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"Mutex timed out acquiring file lock: {path}") from None
                        time.sleep(0.02)
            try:
                yield
            finally:
                if msvcrt is not None:
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
    finally:
        t_lock.release()


@contextmanager
def ui_inject_lock(base_dir: Path, timeout_sec: float = 15.0) -> Iterator[None]:
    """
    Two-phase short lock dedicated to UIA ~300ms window switch & input injection.
    Released immediately after input delivery so the subsequent ~55s generation wait runs lock-free.
    """
    mutex_path = Path(base_dir) / "bindings" / "ui_inject.mutex"
    with _file_mutex(mutex_path, timeout_sec=timeout_sec):
        yield


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:6]}")
    tmp.write_text(payload, encoding="utf-8")
    last_err: Optional[Exception] = None
    for attempt in range(6):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_err = exc
            time.sleep(0.015 * (attempt + 1))
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    if last_err is not None:
        raise last_err


def _read_json_safe(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    for _ in range(4):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else None
        except Exception:
            time.sleep(0.015)
    return None


def is_placeholder_title(title: str) -> bool:
    raw = (title or "").strip()
    cleaned = raw.lower()
    if cleaned in PLACEHOLDER_SESSION_TITLES:
        return True
    return bool(
        re.search(
            r"^(?:新建聊天|headless session|你说：|you said:|chatgpt 说：|chatgpt said:|\[summary\]|\[constraints\]|\d{1,2}:\d{2}(?:\s?[ap]m)?)$"
            r"|开始新聊天|你想让我们在|我们应该在|what should we",
            raw,
            re.IGNORECASE,
        )
    )


def normalize_speed_tier(speed: str) -> str:
    """Normalize speed tier aliases into Codex `service_tier` values: default | fast | ultrafast."""
    raw = (speed or "").strip().lower()
    if raw in ("", "default", "standard", "normal", "标准", "默认"):
        return "default"
    if raw in ("fast", "priority", "快速", "快", "优先"):
        return "fast"
    if raw in ("ultrafast", "ultra_fast", "ultra-fast", "超快", "极速"):
        return "ultrafast"
    return "default"


def discover_codex_exe(codex_exe: Optional[Path | str] = None) -> Optional[Path]:
    """
    Dynamically locate Windows OpenAI Codex CLI (`codex.exe`):
    1) Explicit `codex_exe` argument or `CODEX_DIRECTOR_CODEX_EXE` / `CODEX_EXE` environment variable
    2) `shutil.which("codex")` or `shutil.which("codex.exe")`
    3) PowerShell `Get-AppxPackage -Name "OpenAI.Codex"` InstallLocation + `\\app\\resources\\codex.exe`
    4) Fallback scan `C:\\Program Files\\WindowsApps\\OpenAI.Codex_*\\app\\resources\\codex.exe` (newest version first)
    """
    explicit_candidates = [
        codex_exe,
        os.environ.get("CODEX_DIRECTOR_CODEX_EXE"),
        os.environ.get("CODEX_EXE"),
    ]
    for candidate in explicit_candidates:
        if candidate:
            p = Path(candidate)
            if p.is_file():
                return p.resolve()

    for cmd_name in ("codex", "codex.exe"):
        found = shutil.which(cmd_name)
        if found:
            p = Path(found)
            if p.is_file():
                return p.resolve()

    if os.name == "nt" and shutil.which("powershell.exe"):
        ps_script = (
            '(Get-AppxPackage -Name "OpenAI.Codex" '
            "| Sort-Object Version -Descending "
            "| Select-Object -First 1).InstallLocation"
        )
        try:
            proc = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    ps_script,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=CREATE_NO_WINDOW,
            )
            install_loc = (proc.stdout or "").strip()
            if proc.returncode == 0 and install_loc:
                appx_exe = Path(install_loc) / "app" / "resources" / "codex.exe"
                if appx_exe.is_file():
                    return appx_exe
        except Exception:
            pass

    windows_apps = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
    try:
        if windows_apps.exists():
            matches = sorted(
                windows_apps.glob("OpenAI.Codex_*/app/resources/codex.exe"),
                key=lambda item: item.parents[2].name,
                reverse=True,
            )
            for match in matches:
                if match.is_file():
                    return match
    except OSError:
        pass

    return None


class BindingStore:
    """
    Concurrent multi-session binding registry stored under `<base_dir>/bindings/<binding_id>.json`.
    Mirrors to `<base_dir>/session_lock.json` when exactly 1 active binding exists,
    and cleans `<base_dir>/session_lock.json` when unbound (or when >1 bindings are active to prevent cross-talk).
    """

    def __init__(self, base_dir: Path, legacy_lock_path: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir)
        self.bindings_dir = self.base_dir / "bindings"
        self.mutex_path = self.bindings_dir / "bindings.mutex"
        self.legacy_lock_path = Path(legacy_lock_path) if legacy_lock_path is not None else (self.base_dir / "session_lock.json")

    def _is_safe_id(self, binding_id: str) -> bool:
        return bool(binding_id and SAFE_ID_RE.fullmatch(binding_id) and not binding_id.endswith(".last_run"))

    def _binding_path(self, binding_id: str) -> Path:
        if not self._is_safe_id(binding_id):
            raise ValueError(f"Invalid binding_id: {binding_id!r}")
        return self.bindings_dir / f"{binding_id}.json"

    def _last_run_path(self, binding_id: str) -> Path:
        if not self._is_safe_id(binding_id):
            raise ValueError(f"Invalid binding_id: {binding_id!r}")
        return self.bindings_dir / f"{binding_id}.last_run.json"

    def _list_bindings_unlocked(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        if self.bindings_dir.exists():
            for path in self.bindings_dir.glob("*.json"):
                if path.name.endswith(".last_run.json") or path.name.startswith("."):
                    continue
                doc = _read_json_safe(path)
                if isinstance(doc, dict) and doc.get("bound") and doc.get("binding_id"):
                    items.append(doc)

        if not items and self.legacy_lock_path.exists():
            legacy = _read_json_safe(self.legacy_lock_path)
            if isinstance(legacy, dict) and legacy.get("bound") and legacy.get("binding_id"):
                bid = str(legacy["binding_id"])
                if self._is_safe_id(bid):
                    _atomic_write_json(self._binding_path(bid), legacy)
                    items.append(legacy)

        items.sort(key=lambda x: (str(x.get("bound_at", "")), str(x.get("binding_id", ""))))
        return items

    def _sync_legacy_lock_unlocked(self, active: Optional[List[Dict[str, Any]]] = None) -> None:
        records = active if active is not None else self._list_bindings_unlocked()
        if len(records) == 1:
            _atomic_write_json(self.legacy_lock_path, records[0])
        else:
            self.legacy_lock_path.unlink(missing_ok=True)

    @staticmethod
    def _normalize_transport(transport: str) -> str:
        t = (transport or "auto").strip().lower()
        if t in ("headless", "codex", "exec"):
            return "codex_exec"
        if t in ("auto", "codex_exec", "uia"):
            return t
        return "auto"

    @staticmethod
    def _normalize_model_profile(profile: Optional[Dict[str, Any]]) -> Dict[str, str]:
        raw = profile if isinstance(profile, dict) else {}
        model = str(raw.get("model") or "").strip()
        effort = str(raw.get("effort") or raw.get("reasoning_effort") or "").strip()
        speed_raw = str(raw.get("speed") or raw.get("service_tier") or "").strip()
        speed = normalize_speed_tier(speed_raw) if speed_raw else ""
        return {
            "model": model,
            "effort": effort,
            "speed": speed,
        }

    @staticmethod
    def _summarize_binding(doc: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "binding_id": doc.get("binding_id", ""),
            "session_title": doc.get("session_title", ""),
            "workspace_path": doc.get("workspace_path", ""),
            "hwnd": doc.get("hwnd", 0),
            "transport": doc.get("transport", "auto"),
            "codex_session_id": doc.get("codex_session_id", ""),
            "engine": doc.get("engine", ""),
            "bound_at": doc.get("bound_at", ""),
        }

    def create_binding(
        self,
        *,
        session_title: str = "",
        requested_title: str = "",
        hwnd: int = 0,
        pid: int = 0,
        process_start: int = 0,
        fingerprint: str = "",
        is_minimized: bool = False,
        awaiting_first_title: Optional[bool] = None,
        workspace_path: str = "",
        transport: str = "auto",
        codex_session_id: str = "",
        model_profile: Optional[Dict[str, Any]] = None,
        engine: str = "unreal",
        mode: str = "semi_auto",
        auto_apply_files: bool = False,
        retry_count: int = 0,
        max_retries: int = 3,
        binding_id: str = "",
        **extra: Any,
    ) -> Dict[str, Any]:
        bid = (binding_id or "").strip() or uuid.uuid4().hex
        if not self._is_safe_id(bid):
            raise ValueError(f"Invalid binding_id: {bid!r}")

        clean_title = (session_title or "").strip()
        req_title = (requested_title or clean_title).strip()
        if awaiting_first_title is None:
            awaiting_first = is_placeholder_title(clean_title)
        else:
            awaiting_first = bool(awaiting_first_title)

        now = _now_iso()
        record: Dict[str, Any] = {
            "bound": True,
            "binding_id": bid,
            "hwnd": int(hwnd or 0),
            "pid": int(pid or 0),
            "process_start": int(process_start or 0),
            "session_title": clean_title or req_title or "Headless Session",
            "requested_title": req_title,
            "awaiting_first_title": awaiting_first,
            "fingerprint": str(fingerprint or ""),
            "is_minimized": bool(is_minimized),
            "workspace_path": str(workspace_path or "").strip(),
            "transport": self._normalize_transport(transport),
            "codex_session_id": str(codex_session_id or "").strip(),
            "model_profile": self._normalize_model_profile(model_profile),
            "engine": (engine or "unreal").upper(),
            "mode": mode if mode in ("semi_auto", "autopilot") else "semi_auto",
            "auto_apply_files": bool(auto_apply_files),
            "retry_count": max(0, int(retry_count or 0)),
            "max_retries": max(1, min(int(max_retries or 3), 6)),
            "bound_at": now,
            "updated_at": now,
        }
        for key, val in extra.items():
            if key not in record:
                record[key] = val

        with _file_mutex(self.mutex_path):
            _atomic_write_json(self._binding_path(bid), record)
            self._sync_legacy_lock_unlocked()
        return record

    def list_bindings(self) -> List[Dict[str, Any]]:
        with _file_mutex(self.mutex_path):
            return self._list_bindings_unlocked()

    def get_binding(self, binding_id: str = "") -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        bid = (binding_id or "").strip()
        with _file_mutex(self.mutex_path):
            if bid:
                if not self._is_safe_id(bid):
                    return None, {
                        "ok": False,
                        "error_code": "STALE_BINDING",
                        "binding_id": bid,
                        "message": f"无效或已失效的绑定令牌: {bid}",
                    }
                doc = _read_json_safe(self._binding_path(bid))
                if isinstance(doc, dict) and doc.get("bound") and doc.get("binding_id") == bid:
                    return doc, None
                return None, {
                    "ok": False,
                    "error_code": "STALE_BINDING",
                    "binding_id": bid,
                    "message": f"绑定令牌不存在或已解除 (binding_id={bid})，请重新确认目标会话。",
                }

            active = self._list_bindings_unlocked()
            if len(active) == 1:
                return active[0], None
            if len(active) == 0:
                return None, {
                    "ok": False,
                    "error_code": "SESSION_NOT_BOUND",
                    "message": "门禁拦截：当前尚未绑定任何 Codex/ChatGPT 会话！必须先调用 bridge_bind_session。",
                }
            return None, {
                "ok": False,
                "error_code": "AMBIGUOUS_ACTIVE_BINDINGS",
                "active_count": len(active),
                "active_bindings": [self._summarize_binding(item) for item in active],
                "message": (
                    f"防串台拦截：当前同时存在 {len(active)} 个活跃会话绑定，"
                    "调用时必须显式传入目标 binding_id！"
                ),
            }

    def update_binding(self, binding_id: str, **updates: Any) -> Optional[Dict[str, Any]]:
        bid = (binding_id or "").strip()
        if not self._is_safe_id(bid):
            return None
        with _file_mutex(self.mutex_path):
            path = self._binding_path(bid)
            doc = _read_json_safe(path)
            if not isinstance(doc, dict) or not doc.get("bound"):
                return None

            for key, val in updates.items():
                if key in ("binding_id", "bound"):
                    continue
                if key == "transport" and isinstance(val, str):
                    doc["transport"] = self._normalize_transport(val)
                elif key == "model_profile" and isinstance(val, dict):
                    merged = dict(doc.get("model_profile") or {})
                    merged.update(val)
                    doc["model_profile"] = self._normalize_model_profile(merged)
                else:
                    doc[key] = val

            doc["updated_at"] = _now_iso()
            _atomic_write_json(path, doc)
            self._sync_legacy_lock_unlocked()
            return doc

    def promote_first_title(
        self,
        binding_id: str,
        active_title: str,
        authoritative: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Tighten a newly created placeholder session title (e.g. '新聊天' / 'New chat') to its first real generated title
        once `authoritative=True` and `active_title` is non-placeholder. Idempotent once tightened.
        """
        bid = (binding_id or "").strip()
        if not self._is_safe_id(bid):
            return None
        candidate = (active_title or "").strip()

        with _file_mutex(self.mutex_path):
            path = self._binding_path(bid)
            doc = _read_json_safe(path)
            if not isinstance(doc, dict) or not doc.get("bound"):
                return None

            if not authoritative or is_placeholder_title(candidate):
                return doc

            if not doc.get("awaiting_first_title", False):
                return doc

            doc["session_title"] = candidate
            doc["awaiting_first_title"] = False
            doc["title_promoted_at"] = _now_iso()
            doc["updated_at"] = doc["title_promoted_at"]
            _atomic_write_json(path, doc)
            self._sync_legacy_lock_unlocked()
            return doc

    def remove_binding(self, binding_id: str = "") -> Dict[str, Any]:
        bid = (binding_id or "").strip()
        with _file_mutex(self.mutex_path):
            if bid:
                if not self._is_safe_id(bid):
                    return {
                        "ok": False,
                        "error_code": "STALE_BINDING",
                        "binding_id": bid,
                        "message": f"绑定令牌格式无效或不匹配: {bid}",
                    }
                path = self._binding_path(bid)
                prev = _read_json_safe(path)
                if not isinstance(prev, dict) or not prev.get("bound"):
                    return {
                        "ok": False,
                        "error_code": "STALE_BINDING",
                        "binding_id": bid,
                        "message": "绑定令牌不匹配或已解除，未改动其他活跃绑定。",
                    }
                path.unlink(missing_ok=True)
                self._last_run_path(bid).unlink(missing_ok=True)
                remaining = self._list_bindings_unlocked()
                self._sync_legacy_lock_unlocked(remaining)
                return {
                    "ok": True,
                    "binding_id": bid,
                    "previous_lock": prev,
                    "removed": [prev],
                    "remaining_count": len(remaining),
                    "message": f"已解除会话绑定: '{prev.get('session_title', '')}' (binding_id={bid})。",
                }

            active = self._list_bindings_unlocked()
            for item in active:
                item_id = str(item.get("binding_id", ""))
                if self._is_safe_id(item_id):
                    self._binding_path(item_id).unlink(missing_ok=True)
                    self._last_run_path(item_id).unlink(missing_ok=True)
            self.legacy_lock_path.unlink(missing_ok=True)
            return {
                "ok": True,
                "previous_lock": active[0] if len(active) == 1 else None,
                "removed": active,
                "remaining_count": 0,
                "message": f"已解除全部会话绑定 (共清理 {len(active)} 个会话锁)。",
            }


def _extract_event_fields(event: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """
    Extract (session_id, turn_id, assistant_message, error_message) from a single Codex JSONL event object.
    Supports both modern `codex exec --json` events (`thread.started`, `item.completed`, `turn.*`)
    and session rollout envelopes (`session_meta`, `event_msg`).
    """
    ev_type = str(event.get("type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}

    session_id = ""
    for candidate in (
        event.get("thread_id"),
        event.get("session_id"),
        event.get("conversation_id"),
        payload.get("session_id"),
        payload.get("thread_id"),
        payload.get("id") if ev_type == "session_meta" else "",
    ):
        if isinstance(candidate, str) and candidate.strip():
            session_id = candidate.strip()
            break

    turn_id = ""
    turn_obj = event.get("turn") if isinstance(event.get("turn"), dict) else {}
    for candidate in (
        event.get("turn_id"),
        turn_obj.get("id"),
        event.get("id") if ev_type in ("turn.started", "turn.completed") else "",
        payload.get("turn_id"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            turn_id = candidate.strip()
            break

    assistant_msg = ""
    if isinstance(event.get("last_agent_message"), str) and event["last_agent_message"].strip():
        assistant_msg = event["last_agent_message"]
    elif ev_type in ("agent_message", "assistant_message"):
        for key in ("message", "text", "content"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                assistant_msg = val
                break
    elif ev_type == "event_msg":
        p_type = str(payload.get("type") or "")
        if p_type in ("agent_message", "task_complete"):
            for key in ("last_agent_message", "message", "text"):
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    assistant_msg = val
                    break
    elif ev_type == "item.completed":
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_type = str(item.get("type") or "")
        role = str(item.get("role") or "")
        if item_type in ("agent_message", "assistant_message", "message") or role == "assistant":
            if isinstance(item.get("text"), str) and item["text"].strip():
                assistant_msg = item["text"]
            elif isinstance(item.get("message"), str) and item["message"].strip():
                assistant_msg = item["message"]
            elif isinstance(item.get("content"), str) and item["content"].strip():
                assistant_msg = item["content"]
            elif isinstance(item.get("content"), list):
                chunks: List[str] = []
                for part in item["content"]:
                    if isinstance(part, dict):
                        txt = part.get("text") or part.get("output_text") or ""
                        if isinstance(txt, str) and txt:
                            chunks.append(txt)
                if chunks:
                    assistant_msg = "\n".join(chunks)

    error_msg = ""
    if ev_type in ("error", "turn.failed"):
        err_obj = event.get("error")
        if isinstance(err_obj, dict) and isinstance(err_obj.get("message"), str):
            error_msg = err_obj["message"]
        elif isinstance(err_obj, str):
            error_msg = err_obj
        elif isinstance(event.get("message"), str):
            error_msg = event["message"]

    return session_id, turn_id, assistant_msg, error_msg


class HeadlessCodexRunner:
    """
    True multi-session concurrent & dual-minimized execution engine powered by `codex.exe exec`.
    - Zero focus stealing, zero clipboard touching, works while both Antigravity & ChatGPT/Codex windows are minimized.
    - Non-destructive async timeout: if execution exceeds `timeout_sec`, background collector keeps running
      and writes completion state to `<base_dir>/bindings/<binding_id>.last_run.json` for `read_latest_run`.
    """

    def __init__(self, base_dir: Path, codex_exe: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir)
        self.bindings_dir = self.base_dir / "bindings"
        self._explicit_codex_exe = Path(codex_exe) if codex_exe is not None else None
        self._store = BindingStore(self.base_dir)

    @staticmethod
    def normalize_speed_tier(speed: str) -> str:
        return normalize_speed_tier(speed)

    def discover_codex_exe(self) -> Optional[Path]:
        return discover_codex_exe(self._explicit_codex_exe)

    def _last_run_path(self, binding_id: str) -> Path:
        bid = (binding_id or "default").strip() or "default"
        if not SAFE_ID_RE.fullmatch(bid):
            raise ValueError(f"Invalid binding_id: {bid!r}")
        return self.bindings_dir / f"{bid}.last_run.json"

    def _build_command(
        self,
        exe_path: Path,
        *,
        model: str,
        effort: str,
        speed: str,
        workspace_path: str,
        codex_session_id: str,
        last_msg_file: Path,
    ) -> List[str]:
        suffix = exe_path.suffix.lower()
        if suffix == ".py":
            prefix = [sys.executable, "-B", str(exe_path)]
        else:
            prefix = [str(exe_path)]

        top_opts: List[str] = ["-c", 'approval_policy="never"']
        if model:
            top_opts.extend(["-m", model])
        if effort:
            top_opts.extend(["-c", f'model_reasoning_effort="{effort}"'])
        if speed:
            norm_speed = self.normalize_speed_tier(speed)
            top_opts.extend(["-c", f'service_tier="{norm_speed}"'])

        if codex_session_id:
            # Real codex.exe clap grammar accepts `-s` and `-C` on top-level/exec before `resume <SESSION_ID>`
            top_opts.extend(["-s", "workspace-write"])
            if workspace_path:
                top_opts.extend(["-C", workspace_path])
            sub_args = [
                "exec",
                "resume",
                codex_session_id,
                "--json",
                "--skip-git-repo-check",
                "-o",
                str(last_msg_file),
                "-",
            ]
        else:
            sub_args = [
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-s",
                "workspace-write",
            ]
            if workspace_path:
                sub_args.extend(["-C", workspace_path])
            sub_args.extend(["-o", str(last_msg_file), "-"])

        return [*prefix, *top_opts, *sub_args]

    def dispatch(
        self,
        binding: Dict[str, Any],
        dispatch_id: str,
        envelope: str,
        timeout_sec: int = 50,
    ) -> Dict[str, Any]:
        exe_path = self.discover_codex_exe()
        if exe_path is None:
            return {
                "ok": False,
                "timed_out": False,
                "reply_confirmed": False,
                "channel": "codex_headless_exec",
                "dispatch_id": dispatch_id,
                "error_code": "CODEX_EXE_NOT_FOUND",
                "message": "未找到本机 codex.exe 可执行文件，无法使用无头引擎派发。",
            }

        binding_id = str(binding.get("binding_id") or "default").strip() or "default"
        persisted_binding, _ = self._store.get_binding(binding_id) if binding_id != "default" else (None, None)

        codex_session_id = str(
            binding.get("codex_session_id")
            or (persisted_binding or {}).get("codex_session_id")
            or ""
        ).strip()
        workspace_path = str(
            binding.get("workspace_path")
            or (persisted_binding or {}).get("workspace_path")
            or ""
        ).strip()

        raw_profile = binding.get("model_profile") or (persisted_binding or {}).get("model_profile") or {}
        if not isinstance(raw_profile, dict):
            raw_profile = {}
        model = str(raw_profile.get("model") or binding.get("model") or "").strip()
        effort = str(raw_profile.get("effort") or binding.get("effort") or "").strip()
        speed = str(raw_profile.get("speed") or binding.get("speed") or "").strip()

        self.bindings_dir.mkdir(parents=True, exist_ok=True)
        last_run_path = self._last_run_path(binding_id)
        last_msg_file = self.bindings_dir / f".tmp_last_msg_{binding_id}_{dispatch_id}_{uuid.uuid4().hex[:6]}.txt"

        cmd = self._build_command(
            exe_path,
            model=model,
            effort=effort,
            speed=speed,
            workspace_path=workspace_path,
            codex_session_id=codex_session_id,
            last_msg_file=last_msg_file,
        )

        cwd_arg: Optional[str] = None
        if workspace_path and Path(workspace_path).is_dir():
            cwd_arg = workspace_path

        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }

        started_mono = time.monotonic()
        started_iso = _now_iso()

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd_arg,
                env=env,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:
            last_msg_file.unlink(missing_ok=True)
            return {
                "ok": False,
                "timed_out": False,
                "reply_confirmed": False,
                "channel": "codex_headless_exec",
                "binding_id": binding_id,
                "dispatch_id": dispatch_id,
                "error_code": "CODEX_SPAWN_ERROR",
                "message": f"启动无头 codex.exe 失败: {exc}",
            }

        state_lock = threading.Lock()
        done_event = threading.Event()
        shared: Dict[str, Any] = {
            "codex_session_id": codex_session_id,
            "turn_id": "",
            "ttft_ms": None,
            "event_count": 0,
            "usage": None,
            "final_doc": None,
        }

        initial_doc: Dict[str, Any] = {
            "ok": True,
            "status": "running",
            "timed_out": False,
            "reply_confirmed": False,
            "channel": "codex_headless_exec",
            "binding_id": binding_id,
            "dispatch_id": dispatch_id,
            "resumed": bool(codex_session_id),
            "codex_session_id": codex_session_id,
            "turn_id": "",
            "pid": proc.pid,
            "started_at": started_iso,
            "updated_at": started_iso,
            "raw_reply": "",
            "telemetry": {
                "ui_inject_ms": 0,
                "model_ttft_ms": None,
                "model_duration_ms": None,
                "pickup_overhead_ms": 0,
                "total_elapsed_ms": 0,
                "resumed": bool(codex_session_id),
            },
        }
        _atomic_write_json(last_run_path, initial_doc)

        stderr_Holder: List[bytes] = []

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            try:
                stderr_Holder.append(proc.stderr.read())
            except Exception:
                pass
            finally:
                try:
                    proc.stderr.close()
                except Exception:
                    pass

        stderr_thread = threading.Thread(
            target=_drain_stderr,
            name=f"codex-stderr-{dispatch_id[:8]}",
            daemon=True,
        )
        stderr_thread.start()

        def _collect_worker() -> None:
            last_jsonl_reply = ""
            last_error_msg = ""
            plain_stdout_lines: List[str] = []

            try:
                if proc.stdin is not None:
                    try:
                        proc.stdin.write((envelope or "").encode("utf-8"))
                        proc.stdin.flush()
                    except OSError:
                        pass
                    finally:
                        try:
                            proc.stdin.close()
                        except OSError:
                            pass

                if proc.stdout is not None:
                    for raw_bytes in iter(proc.stdout.readline, b""):
                        line = raw_bytes.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except Exception:
                            plain_stdout_lines.append(line)
                            continue

                        if not isinstance(ev, dict):
                            continue

                        s_id, t_id, msg_txt, err_txt = _extract_event_fields(ev)
                        ev_usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else None
                        meta_changed = False

                        with state_lock:
                            shared["event_count"] += 1
                            if shared["ttft_ms"] is None and (
                                msg_txt or t_id or str(ev.get("type", "")).startswith(("item.", "turn."))
                            ):
                                shared["ttft_ms"] = round((time.monotonic() - started_mono) * 1000, 1)
                            if s_id and s_id != shared["codex_session_id"]:
                                shared["codex_session_id"] = s_id
                                meta_changed = True
                            if t_id and t_id != shared["turn_id"]:
                                shared["turn_id"] = t_id
                                meta_changed = True
                            if ev_usage is not None:
                                shared["usage"] = ev_usage
                            cur_sid = shared["codex_session_id"]
                            cur_tid = shared["turn_id"]
                            cur_ttft = shared["ttft_ms"]

                        if msg_txt:
                            last_jsonl_reply = msg_txt
                        if err_txt:
                            last_error_msg = err_txt

                        if meta_changed:
                            if cur_sid and binding_id != "default":
                                self._store.update_binding(binding_id, codex_session_id=cur_sid)
                            binding["codex_session_id"] = cur_sid or binding.get("codex_session_id", "")
                            progress_doc = {
                                **initial_doc,
                                "codex_session_id": cur_sid,
                                "turn_id": cur_tid,
                                "updated_at": _now_iso(),
                                "telemetry": {
                                    **initial_doc["telemetry"],
                                    "model_ttft_ms": cur_ttft,
                                    "total_elapsed_ms": round((time.monotonic() - started_mono) * 1000, 1),
                                },
                            }
                            try:
                                _atomic_write_json(last_run_path, progress_doc)
                            except Exception:
                                pass

                    try:
                        proc.stdout.close()
                    except Exception:
                        pass

                exit_code = proc.wait()
                stderr_thread.join(timeout=2.0)
                finished_mono = time.monotonic()

                file_reply = ""
                if last_msg_file.exists():
                    try:
                        file_reply = last_msg_file.read_text(encoding="utf-8", errors="replace").strip()
                    except Exception:
                        file_reply = ""

                pickup_finished_mono = time.monotonic()
                raw_reply = file_reply or last_jsonl_reply.strip()
                if not raw_reply and exit_code == 0 and plain_stdout_lines:
                    raw_reply = "\n".join(plain_stdout_lines).strip()

                stderr_text = b"".join(stderr_Holder).decode("utf-8", errors="replace").strip()

                with state_lock:
                    final_sid = shared["codex_session_id"]
                    final_tid = shared["turn_id"]
                    final_ttft = shared["ttft_ms"]
                    ev_count = shared["event_count"]
                    usage_info = shared["usage"]

                if final_sid:
                    binding["codex_session_id"] = final_sid
                    if binding_id != "default":
                        self._store.update_binding(binding_id, codex_session_id=final_sid)

                total_ms = round((pickup_finished_mono - started_mono) * 1000, 1)
                model_ms = round((finished_mono - started_mono) * 1000, 1)
                pickup_ms = round(max(0.0, (pickup_finished_mono - finished_mono) * 1000), 1)
                if final_ttft is None:
                    final_ttft = model_ms

                telemetry: Dict[str, Any] = {
                    "ui_inject_ms": 0,
                    "model_ttft_ms": final_ttft,
                    "model_duration_ms": model_ms,
                    "pickup_overhead_ms": pickup_ms,
                    "total_elapsed_ms": total_ms,
                    "exit_code": exit_code,
                    "event_count": ev_count,
                    "resumed": bool(codex_session_id),
                }
                if usage_info is not None:
                    telemetry["usage"] = usage_info

                completed_iso = _now_iso()
                if exit_code == 0:
                    final_doc: Dict[str, Any] = {
                        "ok": True,
                        "status": "completed",
                        "timed_out": False,
                        "reply_confirmed": True,
                        "channel": "codex_headless_exec",
                        "binding_id": binding_id,
                        "dispatch_id": dispatch_id,
                        "resumed": bool(codex_session_id),
                        "codex_session_id": final_sid,
                        "turn_id": final_tid,
                        "raw_reply": raw_reply,
                        "telemetry": telemetry,
                        "started_at": started_iso,
                        "completed_at": completed_iso,
                        "updated_at": completed_iso,
                        "message": "Codex 无头引擎已完成本轮施工并返回回复。",
                    }
                else:
                    err_detail = last_error_msg or stderr_text[-1000:] or f"codex.exe exited with code {exit_code}"
                    final_doc = {
                        "ok": False,
                        "status": "failed",
                        "timed_out": False,
                        "reply_confirmed": False,
                        "error_code": "CODEX_EXEC_FAILED",
                        "channel": "codex_headless_exec",
                        "binding_id": binding_id,
                        "dispatch_id": dispatch_id,
                        "resumed": bool(codex_session_id),
                        "codex_session_id": final_sid,
                        "turn_id": final_tid,
                        "raw_reply": raw_reply,
                        "stderr": stderr_text[-2000:],
                        "telemetry": telemetry,
                        "started_at": started_iso,
                        "completed_at": completed_iso,
                        "updated_at": completed_iso,
                        "message": f"Codex 无头引擎执行异常 (exit_code={exit_code}): {err_detail}",
                    }

                _atomic_write_json(last_run_path, final_doc)
                with state_lock:
                    shared["final_doc"] = final_doc
            finally:
                last_msg_file.unlink(missing_ok=True)
                done_event.set()

        collector = threading.Thread(
            target=_collect_worker,
            name=f"codex-exec-{binding_id[:8]}-{dispatch_id[:8]}",
            daemon=True,
        )
        collector.start()

        wait_budget = max(0.05, float(timeout_sec))
        if done_event.wait(timeout=wait_budget):
            with state_lock:
                final_result = shared["final_doc"]
            if isinstance(final_result, dict):
                return dict(final_result)
            fallback = _read_json_safe(last_run_path)
            if isinstance(fallback, dict):
                return fallback

        # Timed out synchronously: DO NOT terminate the background Codex worker!
        with state_lock:
            snap_sid = shared["codex_session_id"]
            snap_tid = shared["turn_id"]
            snap_ttft = shared["ttft_ms"]
            maybe_done = shared["final_doc"]

        if isinstance(maybe_done, dict):
            return dict(maybe_done)

        elapsed_ms = round((time.monotonic() - started_mono) * 1000, 1)
        async_doc: Dict[str, Any] = {
            "ok": True,
            "timed_out": True,
            "reply_confirmed": False,
            "status": "running_async",
            "channel": "codex_headless_exec",
            "binding_id": binding_id,
            "dispatch_id": dispatch_id,
            "resumed": bool(codex_session_id),
            "codex_session_id": snap_sid,
            "turn_id": snap_tid,
            "pid": proc.pid,
            "raw_reply": "",
            "started_at": started_iso,
            "updated_at": _now_iso(),
            "telemetry": {
                "ui_inject_ms": 0,
                "model_ttft_ms": snap_ttft,
                "model_duration_ms": None,
                "pickup_overhead_ms": 0,
                "total_elapsed_ms": elapsed_ms,
                "resumed": bool(codex_session_id),
            },
            "message": (
                f"同步等待已达 {timeout_sec}s，后台 Codex 无头施工进程仍在继续运行 "
                f"(dispatch_id={dispatch_id})；稍后可通过 read_latest_run 直接取回结果。"
            ),
        }
        return async_doc

    def read_latest_run(self, binding_id: str) -> Optional[Dict[str, Any]]:
        bid = (binding_id or "").strip()
        if not bid:
            active, err = self._store.get_binding("")
            if err or not active:
                return None
            bid = str(active.get("binding_id") or "")
        if not bid or not SAFE_ID_RE.fullmatch(bid):
            return None
        return _read_json_safe(self._last_run_path(bid))


def _normalize_fs_path(raw_path: str) -> str:
    cleaned = (raw_path or "").strip().strip('"').strip("'")
    if not cleaned:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(cleaned)))


def filter_submissions_by_workspace(
    submissions: List[Dict[str, Any]],
    workspace_path: str = "",
) -> List[Dict[str, Any]]:
    """
    Case-insensitive normalized workspace path filter for review inbox submissions.
    Matches exact workspace directory or any nested subdirectory under `workspace_path`.
    """
    target = _normalize_fs_path(workspace_path)
    if not target:
        return list(submissions)

    target_prefix = target if target.endswith(os.sep) else target + os.sep
    matched: List[Dict[str, Any]] = []
    for item in submissions:
        if not isinstance(item, dict):
            continue
        sub_ws = _normalize_fs_path(str(item.get("workspace_path") or ""))
        if not sub_ws:
            continue
        if sub_ws == target or sub_ws.startswith(target_prefix):
            matched.append(item)
    return matched
