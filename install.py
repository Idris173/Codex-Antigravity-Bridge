"""Install, inspect, or remove the local Codex Director suite (Python 3.11+)."""

import argparse
import json
import locale
import math
import os
from pathlib import Path
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass

SOURCE = Path(__file__).resolve().parent
BRIDGE_FILES = ("bridge_server.py", "uia_driver.ps1", "uia_daemon.ps1", "BridgeFastUia.cs")
AGY = "chatgpt-bridge"
CODEX = "antigravity_bridge"
CODEX_TABLE = ("mcp_servers", CODEX)
UTF8_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
TOOLS = {
    AGY: {
        "bridge_inspect_app", "bridge_bind_session", "bridge_dispatch_task",
        "bridge_read_latest", "bridge_create_project_session", "bridge_unbind_session",
        "bridge_check_codex_inbox", "bridge_reply_to_codex",
    },
    CODEX: {"agy_submit_for_review", "agy_fetch_director_reply", "agy_bridge_status"},
}
TEXT = {
    "en": {
        "installed": "Installed both skills and MCP registrations.",
        "removed": "Uninstalled the suite and preserved other configuration.",
        "backup": "Backup",
        "warning": "Warning",
        "error": "Error",
        "healthy": "Both MCP roles and the installation passed.",
        "unhealthy": "One or more installation checks failed.",
        "dll_deferred": "DLL precompilation was deferred. An in-use DLL can be refreshed by rerunning the installer after its host stops.",
    },
    "zh": {
        "installed": "已安装双边技能并注册 MCP 服务。",
        "removed": "已卸载本套件并保留其他配置。",
        "backup": "备份",
        "warning": "提示",
        "error": "错误",
        "healthy": "双边 MCP 和安装检查均已通过。",
        "unhealthy": "一项或多项安装检查未通过。",
        "dll_deferred": "已跳过 DLL 预编译。若 DLL 正在使用，可在宿主退出后重新运行安装器更新。",
    },
}


def detect_language(requested="auto"):
    if requested != "auto":
        return requested
    for key in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        if value := os.environ.get(key):
            if value not in ("C", "POSIX", "C.UTF-8"):
                return "zh" if value.lower().startswith(("zh", "chinese")) else "en"
    system_locale = locale.getlocale()[0] or ""
    return "zh" if system_locale.lower().startswith(("zh", "chinese")) else "en"


@dataclass(frozen=True)
class Layout:
    home: Path

    @property
    def bridge(self):
        return self.home / ".gemini/config/sidecars/chatgpt-bridge"

    @property
    def server(self):
        return self.bridge / "bridge_server.py"

    @property
    def gemini(self):
        return self.home / ".gemini/config/mcp_config.json"

    @property
    def codex(self):
        return self.home / ".codex/config.toml"

    @property
    def state(self):
        return self.bridge / ".install-state.json"

    @property
    def skills(self):
        return (
            (SOURCE / "skills/antigravity/codex-director", self.home / ".gemini/config/skills/codex-director"),
            (SOURCE / "skills/codex/director-worker", self.home / ".codex/skills/director-worker"),
        )

    def check_path(self, path):
        resolved = path.resolve()
        if resolved != path or not resolved.is_relative_to(self.home) or resolved == self.home:
            raise ValueError(f"Unexpected path or redirected installation target: {path}")
        return path


def read_bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
    try:
        temporary.write_bytes(data)
        if path.exists():
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_config(path, previous, updated):
    if previous == updated:
        return None
    if read_bytes(path) != previous:
        raise ValueError(f"Configuration changed during the operation: {path}")
    backup = None
    if previous is not None:
        backup = path.with_name(f"{path.name}.{time.time_ns()}.bak")
        with backup.open("xb") as file:
            file.write(previous)
    if updated is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, updated)
    return backup


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def load_json(data):
    result = json.loads(data.decode("utf-8-sig")) if data is not None else {}
    if not isinstance(result, dict):
        raise ValueError("MCP JSON configuration must contain an object.")
    return result


def configuration_map(config, key):
    value = config.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must contain a map of MCP servers.")
    return value


def registry_entry(python, server, role):
    entry = {"command": str(python), "args": [str(server), "--role", role]}
    if role == "codex":
        entry.update(startup_timeout_sec=15, tool_timeout_sec=310, env=UTF8_ENV.copy())
    return entry


def owns_entry(entry, server, role):
    if not isinstance(entry, dict):
        return False
    args = entry.get("args")
    if not isinstance(args, list) or not args or not isinstance(args[0], str):
        return False
    trailing = args[1:]
    return Path(args[0]) == server and (
        trailing == ["--role", role] or (not trailing and role == "antigravity")
    )


def merge_registration(servers, name, server, role, entry):
    previous = servers.get(name, {})
    if name in servers and not owns_entry(previous, server, role):
        raise ValueError(f"MCP name {name!r} already points to a different service.")
    merged = {**previous, **entry}
    if "env" in entry:
        merged["env"] = {**configuration_map(previous, "env"), **entry["env"]}
    return merged


def edit_json(original, server, entry, state):
    config = load_json(original)
    servers = configuration_map(config, "mcpServers")
    if entry is not None:
        entry = merge_registration(servers, AGY, server, "antigravity", entry)
        if servers.get(AGY) == entry:
            return original
        servers[AGY] = entry
        config["mcpServers"] = servers
    elif owns_entry(servers.get(AGY), server, "antigravity"):
        del servers[AGY]
        if not servers and not state.get("gemini_map_existed", True):
            del config["mcpServers"]
        if not config and not state.get("gemini_existed", True):
            return None
    else:
        return original
    return json_bytes(config)


def toml_statements(source):
    """Yield complete statements while leaving comments and multiline values intact."""
    start = index = depth = 0
    quote = ""
    while index < len(source):
        char = source[index]
        if quote:
            if char == "\\" and quote[0] == '"':
                index += 2
                continue
            if source.startswith(quote, index):
                index += len(quote)
                if len(quote) == 3:
                    while index < len(source) and source[index] == quote[0]:
                        index += 1
                quote = ""
                continue
        elif char in "\"'":
            quote = char * 3 if source.startswith(char * 3, index) else char
            index += len(quote)
            continue
        elif char == "#":
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline
            continue
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "\n" and depth == 0:
            yield source[start:index + 1]
            start = index + 1
        index += 1
    if start < len(source):
        yield source[start:]


KEY_PREFIX = re.compile(r"""((?:[^"'=]|"(?:[^"\\]|\\.)*"|'[^']*')+?)=""")


def toml_path(probe):
    parts = []
    while probe is not True:
        if isinstance(probe, list):
            probe = probe[0]
        key, probe = next(iter(probe.items()))
        parts.append(key)
    return tuple(parts)


def toml_string(value):
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def toml_value(value):
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_value, value)) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{toml_string(key)} = {toml_value(item)}" for key, item in value.items()) + " }"
    return value.isoformat()  # TOML dates, times, and datetimes.


def rewrite_toml(source, entry, remaining_servers, keep_empty_map):
    newline = "\r\n" if "\r\n" in source else "\n"
    output = []
    table = ()
    for statement in toml_statements(source):
        stripped = statement.lstrip()
        if not stripped or stripped.startswith("#"):
            output.append(statement)
            continue
        if stripped.startswith("["):
            table = toml_path(tomllib.loads(statement + "\n__installer_probe__ = true"))[:-1]
            path = table
        else:
            key = KEY_PREFIX.match(stripped)
            if not key:
                raise ValueError("Could not locate a TOML assignment while merging MCP settings.")
            path = table + toml_path(tomllib.loads(key.group(1) + " = true"))
        if path[:2] == CODEX_TABLE:
            continue
        if path == ("mcp_servers",) and not stripped.startswith("["):
            # An inline parent cannot be extended with a child table.
            for name, server in remaining_servers.items():
                output.append(f"mcp_servers.{toml_string(name)} = {toml_value(server)}{newline}")
            continue
        output.append(statement)
    body = "".join(output)
    if entry is not None:
        block = ["[mcp_servers.antigravity_bridge]"]
        block.extend(f"{key} = {toml_value(value)}" for key, value in entry.items() if key != "env")
        block += ["", "[mcp_servers.antigravity_bridge.env]"]
        block.extend(f"{key} = {toml_value(value)}" for key, value in entry["env"].items())
    elif keep_empty_map and "mcp_servers" not in tomllib.loads(body):
        block = ["[mcp_servers]"]
    else:
        return body
    if body and not body.endswith("\n"):
        body += newline
    if body and not body.endswith(newline * 2):
        body += newline
    return body + newline.join(block) + newline


def comparable_toml(value):
    if isinstance(value, dict):
        return {key: comparable_toml(item) for key, item in value.items()}
    if isinstance(value, list):
        return [comparable_toml(item) for item in value]
    if isinstance(value, float) and math.isnan(value):
        return (float, "nan")
    return value


def edit_toml(original, server, entry, state):
    source = original.decode("utf-8") if original is not None else ""
    expected = tomllib.loads(source)
    servers = configuration_map(expected, "mcp_servers")
    if entry is not None:
        entry = merge_registration(servers, CODEX, server, "codex", entry)
        canonical = re.search(r"(?m)^\[mcp_servers\.antigravity_bridge\]\s*$", source)
        canonical_env = re.search(r"(?m)^\[mcp_servers\.antigravity_bridge\.env\]\s*$", source)
        if servers.get(CODEX) == entry and canonical and canonical_env:
            return original
    elif not owns_entry(servers.get(CODEX), server, "codex"):
        return original
    remaining = {key: value for key, value in servers.items() if key != CODEX}
    keep_map = bool(remaining) or state.get("codex_map_existed", True)
    if entry is not None:
        expected["mcp_servers"] = {**remaining, CODEX: entry}
    elif keep_map:
        expected["mcp_servers"] = remaining
    else:
        expected.pop("mcp_servers")
    merged = rewrite_toml(source, entry, remaining, keep_map)
    parsed = tomllib.loads(merged)
    if entry is None and not keep_map and parsed.get("mcp_servers") == {}:
        # A user-added explicit parent table is part of their configuration.
        expected["mcp_servers"] = {}
    if comparable_toml(parsed) != comparable_toml(expected):
        raise ValueError("TOML merge changed unrelated configuration.")
    if entry is None and not parsed and not merged.strip() and not state.get("codex_existed", True):
        return None
    return merged.encode("utf-8")

def plans(layout, python=None):
    original_json = read_bytes(layout.check_path(layout.gemini))
    original_toml = read_bytes(layout.check_path(layout.codex))
    state_bytes = read_bytes(layout.check_path(layout.state))
    state = load_json(state_bytes)
    if python is not None and state_bytes is None:
        state = {
            "gemini_existed": original_json is not None,
            "gemini_map_existed": "mcpServers" in load_json(original_json),
            "codex_existed": original_toml is not None,
            "codex_map_existed": "mcp_servers" in tomllib.loads((original_toml or b"").decode("utf-8")),
        }
    entry_json = registry_entry(python, layout.server, "antigravity") if python else None
    entry_toml = registry_entry(python, layout.server, "codex") if python else None
    return state, [
        (layout.gemini, original_json, edit_json(original_json, layout.server, entry_json, state)),
        (layout.codex, original_toml, edit_toml(original_toml, layout.server, entry_toml, state)),
    ]


def copy_file(layout, source, target):
    layout.check_path(target)
    data = source.read_bytes()
    if read_bytes(target) == data:
        return False
    atomic_write(target, data)
    return True


def copy_assets(layout):
    for source, destination in layout.skills:
        layout.check_path(destination)
        for file in sorted(source.rglob("*")):
            if file.is_file():
                copy_file(layout, file, destination / file.relative_to(source))
    changed = False
    for name in BRIDGE_FILES:
        copied = copy_file(layout, SOURCE / "bridge" / name, layout.bridge / name)
        if name == "BridgeFastUia.cs":
            changed = copied
    return changed


COMPILE_DLL = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase, System.Windows.Forms
Add-Type -Path $env:CODEX_DIRECTOR_CS_SOURCE -ReferencedAssemblies 'UIAutomationClient','UIAutomationTypes','WindowsBase','System.Core' -OutputAssembly $env:CODEX_DIRECTOR_DLL_PATH
"""
CHECK_DLL = """
$ErrorActionPreference = 'Stop'
$assembly = [Reflection.Assembly]::ReflectionOnlyLoadFrom($env:CODEX_DIRECTOR_DLL_PATH)
[void]$assembly.GetType('BridgeFastUia', $true)
"""


def run_powershell(script, env):
    executable = shutil.which("powershell.exe")
    if executable is None:
        raise ValueError("Windows PowerShell (powershell.exe) is required for the UI Automation DLL.")
    return subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, **env}, capture_output=True, timeout=45, check=True,
    )


def compile_dll(layout, changed):
    dll = layout.check_path(layout.bridge / "BridgeFastUia.dll")
    source = layout.bridge / "BridgeFastUia.cs"
    if not changed and dll.is_file() and dll.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        try:
            run_powershell(CHECK_DLL, {"CODEX_DIRECTOR_DLL_PATH": str(dll)})
            return True
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    try:
        with tempfile.TemporaryDirectory(dir=layout.bridge, prefix=".compile-") as directory:
            built = Path(directory) / dll.name
            run_powershell(COMPILE_DLL, {
                "CODEX_DIRECTOR_CS_SOURCE": str(source), "CODEX_DIRECTOR_DLL_PATH": str(built),
                "TEMP": directory, "TMP": directory,
            })
            try:
                built.replace(dll)
            except PermissionError:
                retired = dll.with_name(f"BridgeFastUia.{time.time_ns()}.old.dll")
                dll.replace(retired)
                try:
                    built.replace(dll)
                except Exception:
                    os.replace(retired, dll)
                    raise
        for stale in layout.bridge.glob("BridgeFastUia.*.old.dll"):
            try:
                stale.unlink()
            except OSError:
                pass
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return True


def confirm_python(python):
    completed = subprocess.run(
        [str(python), "-B", "-I", "-c", "import sys; raise SystemExit(sys.version_info < (3, 11))"],
        capture_output=True, timeout=10,
    )
    if completed.returncode:
        raise ValueError(f"Python 3.11 or later is required: {python}")


def install(layout, python):
    state, edits = plans(layout, python)
    confirm_python(python)
    changed = copy_assets(layout)
    dll_ok = compile_dll(layout, changed)
    if not layout.state.exists():
        atomic_write(layout.state, json_bytes(state))
    backups = [backup for path, old, new in edits if (backup := write_config(path, old, new))]
    return backups, dll_ok


def uninstall(layout):
    _, edits = plans(layout)
    owns_installation = layout.state.is_file() or any(old != new for _, old, new in edits)
    directories = [layout.check_path(path) for path in [layout.bridge, *(dest for _, dest in layout.skills)]]
    backups = [backup for path, old, new in edits if (backup := write_config(path, old, new))]
    if owns_installation:
        for directory in directories:
            if directory.is_dir():
                shutil.rmtree(directory)
    return backups


def exchange_mcp(entry, server_name, method, params=None):
    """Use a real stdio connection and wait for initialize before the tool request."""
    messages = queue.Queue()
    environment = {**os.environ, **UTF8_ENV, **entry.get("env", {})}
    command = [entry["command"], "-B", *entry["args"]]
    with subprocess.Popen(
        command, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, encoding="utf-8",
    ) as child:
        def read_messages():
            try:
                for line in child.stdout:
                    messages.put(line)
            finally:
                messages.put(None)

        threading.Thread(target=read_messages, daemon=True).start()

        def send(method_name, parameters=None, request_id=None):
            request = {"jsonrpc": "2.0", "method": method_name}
            if request_id is not None:
                request["id"] = request_id
            if parameters is not None:
                request["params"] = parameters
            child.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            child.stdin.flush()

        def receive(request_id):
            try:
                line = messages.get(timeout=10)
            except queue.Empty as error:
                raise ValueError(f"MCP request {request_id} timed out.") from error
            if line is None:
                raise ValueError("MCP server closed stdout.")
            reply = json.loads(line)
            if not isinstance(reply, dict) or reply.get("id") != request_id or "result" not in reply:
                raise ValueError(f"MCP request {request_id} did not return a result.")
            return reply["result"]

        try:
            protocol = "2025-06-18"
            send("initialize", {
                "protocolVersion": protocol, "capabilities": {},
                "clientInfo": {"name": "codex-director-doctor", "version": "1"},
            }, 1)
            info = receive(1)
            if info.get("serverInfo", {}).get("name") != server_name or info.get("protocolVersion") != protocol:
                raise ValueError(f"MCP initialization did not identify {server_name}.")
            send("notifications/initialized")
            send(method, params, 2)
            return receive(2)
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def doctor(layout):
    checks = {}

    def result(name, ok, **details):
        checks[name] = {"ok": bool(ok), **details}

    for source, destination in layout.skills:
        expected_files = [file for file in source.rglob("*") if file.is_file()]
        good = bool(expected_files) and all(
            read_bytes(layout.check_path(destination / file.relative_to(source))) == file.read_bytes()
            for file in expected_files
        )
        result(destination.name, good, path=str(destination))

    script_paths = [layout.check_path(layout.bridge / file) for file in BRIDGE_FILES]
    bridge_ok = all(read_bytes(path) == (SOURCE / "bridge" / path.name).read_bytes() for path in script_paths)
    result("bridge_files", bridge_ok, path=str(layout.bridge))
    dll = layout.check_path(layout.bridge / "BridgeFastUia.dll")
    try:
        run_powershell(CHECK_DLL, {"CODEX_DIRECTOR_DLL_PATH": str(dll)})
        fresh = dll.stat().st_mtime_ns >= (layout.bridge / "BridgeFastUia.cs").stat().st_mtime_ns
        result("uia_dll", fresh, path=str(dll))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result("uia_dll", False, path=str(dll), error=type(error).__name__)

    for config_path, loader, table, name, role in (
        (layout.gemini, load_json, "mcpServers", AGY, "antigravity"),
        (layout.codex, lambda data: tomllib.loads(data.decode("utf-8")), "mcp_servers", CODEX, "codex"),
    ):
        entry = None
        try:
            data = layout.check_path(config_path).read_bytes()
            config = loader(data)
            entry = configuration_map(config, table).get(name)
            valid = (
                owns_entry(entry, layout.server, role)
                and isinstance(entry.get("command"), str)
                and Path(entry["command"]).is_absolute()
                and Path(entry["command"]).is_file()
            )
            if valid and role == "codex":
                valid = isinstance(entry.get("env"), dict) and all(entry["env"].get(key) == value for key, value in UTF8_ENV.items())
            result(f"{role}_config", valid, path=str(config_path))
        except (OSError, ValueError) as error:
            result(f"{role}_config", False, path=str(config_path), error=type(error).__name__)
            valid = False
        if not valid:
            result(f"{role}_mcp", False, error="configuration_unavailable")
            continue
        if not bridge_ok:
            result(f"{role}_mcp", False, error="bridge_files_unverified")
            continue
        try:
            reply = exchange_mcp(entry, name, "tools/list")
            found = sorted(tool["name"] for tool in reply.get("tools", []))
            result(f"{role}_mcp", set(found) == TOOLS[name], tools=found)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            result(f"{role}_mcp", False, error=str(error))
    return {"ok": all(check["ok"] for check in checks.values()), "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install, diagnose, or uninstall the Codex Director suite.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--doctor", action="store_true", help="Report both MCP handshakes as JSON.")
    modes.add_argument("--uninstall", action="store_true", help="Remove the suite and its local bridge state.")
    parser.add_argument("--home-dir", type=Path, default=Path.home(), help="Override the installation home.")
    parser.add_argument("--python-exe", type=Path, default=Path(sys.executable), help="Interpreter for both MCP registrations.")
    parser.add_argument("--lang", choices=("auto", "zh", "en"), default="auto", help="CLI language.")
    args = parser.parse_args(argv)
    language = detect_language(args.lang)
    text = TEXT[language]
    try:
        layout = Layout(args.home_dir.expanduser().resolve())
        if args.doctor:
            report = doctor(layout)
            report["language"] = language
            report["message"] = text["healthy" if report["ok"] else "unhealthy"]
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
        if os.name != "nt" and not args.uninstall:
            raise ValueError("The current bridge requires Windows.")
        if args.uninstall:
            backups = uninstall(layout)
            print(text["removed"])
        else:
            backups, dll_ok = install(layout, args.python_exe.expanduser().resolve())
            print(text["installed"])
            if not dll_ok:
                print(f'{text["warning"]}: {text["dll_deferred"]}', file=sys.stderr)
        for backup in backups:
            print(f'{text["backup"]}: {backup}')
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if args.doctor:
            print(json.dumps({"ok": False, "language": language, "message": text["unhealthy"], "error": str(error)}, ensure_ascii=False))
        else:
            print(f'{text["error"]}: {error}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
