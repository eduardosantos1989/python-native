#!/usr/bin/env python3
"""Dependency-free MCP server for Python semantic tools."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


SERVER_NAME = "python-native"
SERVER_VERSION = "0.1.0"
LSP_COMMAND = "pyright-langserver"
LANGUAGE_ID = "python"
ROOT_MARKERS = ("pyrightconfig.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", ".git")
DEFAULT_TIMEOUT = 30.0


def as_json_text(value: Any, max_chars: int = 12000) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    suffix = "\n... truncated ..."
    cutoff = max(0, max_chars - len(suffix))
    newline = text.rfind("\n", 0, cutoff)
    if newline > 0:
        cutoff = newline
    return text[:cutoff] + suffix


def content(value: Any, max_chars: int = 12000) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": as_json_text(value, max_chars)}]}


def run_command(args: list[str], cwd: Path, timeout: float = 120.0) -> dict[str, Any]:
    started = time.time()
    try:
        resolved = shutil.which(args[0])
        command_args = [resolved or args[0], *args[1:]]
        proc = subprocess.run(command_args, cwd=str(cwd), text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "duration_ms": int((time.time() - started) * 1000), "stdout": proc.stdout, "stderr": proc.stderr}
    except FileNotFoundError:
        return {"ok": False, "error": f"Command not found: {args[0]}"}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": f"Timed out after {timeout} seconds", "stdout": exc.stdout or "", "stderr": exc.stderr or ""}


def find_root(root_path: str | None = None, file_path: str | None = None) -> Path:
    if root_path:
        start = Path(root_path).expanduser()
    elif file_path:
        start = Path(file_path).expanduser().parent
    else:
        start = Path.cwd()
    if start.is_file():
        start = start.parent
    start = start.resolve()
    for parent in [start, *start.parents]:
        if any((parent / marker).exists() for marker in ROOT_MARKERS):
            return parent
    return start


def path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


def uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    return url2pathname(unquote(parsed.path))


def to_zero_based(value: Any) -> int:
    try:
        return max(0, int(value) - 1)
    except Exception:
        return 0


def compact_location(location: Any) -> Any:
    if isinstance(location, list):
        return [compact_location(item) for item in location]
    if not isinstance(location, dict):
        return location
    target = location.get("targetUri") or location.get("uri")
    rng = location.get("targetSelectionRange") or location.get("range") or {}
    start = rng.get("start", {})
    end = rng.get("end", {})
    return {
        "file": uri_to_path(target) if target else None,
        "start": {"line": start.get("line", 0) + 1, "character": start.get("character", 0) + 1},
        "end": {"line": end.get("line", 0) + 1, "character": end.get("character", 0) + 1},
    }


def compact_markup(value: Any) -> Any:
    if isinstance(value, dict) and "contents" in value:
        return compact_markup(value["contents"])
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    if isinstance(value, list):
        return "\n\n".join(str(compact_markup(item)) for item in value)
    return value


def range_overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_start = left.get("start", {})
    left_end = left.get("end", left_start)
    right_start = right.get("start", {})
    right_end = right.get("end", right_start)
    left_start_pos = (int(left_start.get("line", 0)), int(left_start.get("character", 0)))
    left_end_pos = (int(left_end.get("line", 0)), int(left_end.get("character", 0)))
    right_start_pos = (int(right_start.get("line", 0)), int(right_start.get("character", 0)))
    right_end_pos = (int(right_end.get("line", 0)), int(right_end.get("character", 0)))
    return left_start_pos <= right_end_pos and right_start_pos <= left_end_pos


def filter_diagnostics(diagnostics: list[dict[str, Any]], rng: dict[str, Any]) -> list[dict[str, Any]]:
    return [diag for diag in diagnostics if range_overlaps(diag.get("range", {}), rng)]


def command_status(command: str, root: Path, version_args: list[str] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    found = shutil.which(command)
    result: dict[str, Any] = {"present": found is not None, "path": found}
    if found and version_args:
        result["version"] = run_command([command, *version_args], root, timeout=timeout)
    return result


def request_until_ready(client: "LspClient", method: str, params: dict[str, Any], is_empty: Callable[[Any], bool], attempts: int = 8, delay: float = 0.4) -> Any:
    result = None
    for attempt in range(attempts):
        result = client.request(method, params)
        if not is_empty(result):
            return result
        if attempt + 1 < attempts:
            time.sleep(delay)
    return result


class LspClient:
    def __init__(self, root: Path):
        command = shutil.which(LSP_COMMAND)
        if not command:
            raise RuntimeError(f"{LSP_COMMAND} not found on PATH")
        self.root = root
        self.proc = subprocess.Popen([command, "--stdio"], cwd=str(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.next_id = 1
        self.responses: dict[int, Any] = {}
        self.diagnostics: dict[str, list[dict[str, Any]]] = {}
        self.diagnostic_events: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.open_versions: dict[str, int] = {}
        self.open_stats: dict[str, tuple[int, int]] = {}
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()
        self._initialize()

    def _stderr_loop(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line and os.environ.get("PYTHON_NATIVE_LSP_STDERR") == "1":
                print(f"[pyright:{self.root}] {line}", file=sys.stderr, flush=True)

    def _read_headers(self) -> dict[str, str] | None:
        assert self.proc.stdout is not None
        headers: dict[str, str] = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return None
            text = line.decode("ascii", errors="replace").strip()
            if text == "":
                return headers
            if ":" in text:
                key, value = text.split(":", 1)
                headers[key.lower()] = value.strip()

    def _read_exact(self, length: int) -> bytes | None:
        assert self.proc.stdout is not None
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.proc.stdout.read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        while True:
            headers = self._read_headers()
            if headers is None:
                return
            length = int(headers.get("content-length", "0"))
            if length <= 0:
                continue
            body = self._read_exact(length)
            if body is None:
                return
            try:
                msg = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            with self.cv:
                if "id" in msg:
                    self.responses[msg["id"]] = msg
                    self.cv.notify_all()
                elif msg.get("method") == "textDocument/publishDiagnostics":
                    params = msg.get("params", {})
                    uri = params.get("uri", "")
                    self.diagnostics[uri] = params.get("diagnostics", [])
                    event = self.diagnostic_events.get(uri)
                    if event:
                        event.set()

    def _send(self, msg: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        raw = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with self.lock:
            self.proc.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
            self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
        with self.cv:
            msg_id = self.next_id
            self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        with self.cv:
            while msg_id not in self.responses:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"LSP request timed out: {method}")
                self.cv.wait(remaining)
            response = self.responses.pop(msg_id)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _initialize(self) -> None:
        self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": path_to_uri(self.root),
                "workspaceFolders": [{"uri": path_to_uri(self.root), "name": self.root.name}],
                "capabilities": {
                    "textDocument": {
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "definition": {"linkSupport": True},
                        "references": {},
                        "codeAction": {"isPreferredSupport": True},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "publishDiagnostics": {"relatedInformation": True},
                    },
                    "workspace": {"symbol": {}},
                },
            },
            timeout=60.0,
        )
        self.notify("initialized", {})

    def open_file(self, file_path: str) -> str:
        path = Path(file_path).expanduser().resolve()
        uri = path_to_uri(path)
        stat = path.stat()
        current_stat = (stat.st_mtime_ns, stat.st_size)
        version = self.open_versions.get(uri, 0)
        if version > 0 and self.open_stats.get(uri) == current_stat:
            return uri
        text = path.read_text(encoding="utf-8", errors="replace")
        version += 1
        self.open_versions[uri] = version
        self.open_stats[uri] = current_stat
        self.diagnostic_events[uri] = threading.Event()
        if version == 1:
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": LANGUAGE_ID, "version": version, "text": text}})
        else:
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]})
        return uri

    def shutdown(self) -> None:
        try:
            self.request("shutdown", {}, timeout=5.0)
            self.notify("exit", {})
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2.0)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class PythonNativeServer:
    def __init__(self) -> None:
        self.clients: dict[str, LspClient] = {}
        self.tools = self._tool_specs()

    def get_lsp(self, root_path: str | None, file_path: str | None = None) -> LspClient:
        root = find_root(root_path, file_path)
        key = str(root)
        client = self.clients.get(key)
        if client and client.proc.poll() is None:
            return client
        client = LspClient(root)
        self.clients[key] = client
        return client

    def _tool_specs(self) -> list[dict[str, Any]]:
        def obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
            return {"type": "object", "properties": props, "required": required or []}
        path_prop = {"type": "string", "description": "Python project root or any directory inside it."}
        file_prop = {"type": "string", "description": "Absolute Python source path."}
        line_prop = {"type": "integer", "minimum": 1}
        max_items = {"type": "integer", "minimum": 1, "maximum": 200, "default": 40}
        max_chars = {"type": "integer", "minimum": 1000, "maximum": 100000, "default": 12000}
        pos = {"root_path": path_prop, "file_path": file_prop, "line": line_prop, "character": line_prop, "max_chars": max_chars}
        return [
            {"name": "python_environment", "description": "Report python, pyright, and pyright-langserver availability plus project root details.", "inputSchema": obj({"root_path": path_prop, "max_chars": max_chars})},
            {"name": "python_lsp_hover", "description": "Ask pyright-langserver for hover info at a source position.", "inputSchema": obj(pos, ["file_path", "line", "character"])},
            {"name": "python_lsp_definition", "description": "Ask pyright-langserver for semantic definition locations.", "inputSchema": obj({**pos, "max_items": max_items}, ["file_path", "line", "character"])},
            {"name": "python_lsp_references", "description": "Ask pyright-langserver for semantic references.", "inputSchema": obj({**pos, "include_declaration": {"type": "boolean", "default": False}, "max_items": max_items}, ["file_path", "line", "character"])},
            {"name": "python_lsp_document_symbols", "description": "Ask pyright-langserver for document symbols in one Python file.", "inputSchema": obj({"root_path": path_prop, "file_path": file_prop, "max_items": max_items, "max_chars": max_chars}, ["file_path"])},
            {"name": "python_lsp_workspace_symbols", "description": "Ask pyright-langserver for workspace symbols matching a query.", "inputSchema": obj({"root_path": path_prop, "query": {"type": "string"}, "max_items": max_items, "max_chars": max_chars}, ["query"])},
            {"name": "python_lsp_code_actions", "description": "Ask pyright-langserver for available code actions for a range.", "inputSchema": obj({"root_path": path_prop, "file_path": file_prop, "start_line": line_prop, "start_character": line_prop, "end_line": line_prop, "end_character": line_prop, "max_items": max_items, "max_chars": max_chars}, ["file_path", "start_line", "start_character", "end_line", "end_character"])},
            {"name": "python_lsp_diagnostics", "description": "Open Python files and return pyright diagnostics seen shortly after opening.", "inputSchema": obj({"root_path": path_prop, "file_paths": {"type": "array", "items": file_prop}, "wait_ms": {"type": "integer", "default": 1500}, "max_items": max_items, "max_chars": max_chars})},
            {"name": "python_pyright_check", "description": "Run pyright --outputjson and return compact diagnostics.", "inputSchema": obj({"root_path": path_prop, "paths": {"type": "array", "items": {"type": "string"}}, "python_version": {"type": "string"}, "venv_path": {"type": "string"}, "extra_args": {"type": "array", "items": {"type": "string"}}, "max_items": max_items, "max_chars": max_chars})},
            {"name": "python_compile_check", "description": "Run python -m py_compile on one or more Python files.", "inputSchema": obj({"root_path": path_prop, "file_paths": {"type": "array", "items": file_prop}, "max_chars": max_chars}, ["file_paths"])},
        ]

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        max_chars = int(args.get("max_chars", 12000))
        try:
            return content(getattr(self, f"tool_{name}")(args), max_chars)
        except Exception as exc:
            return content({"ok": False, "tool": name, "error": str(exc)}, max_chars)

    def tool_python_environment(self, args: dict[str, Any]) -> dict[str, Any]:
        root = find_root(args.get("root_path"))
        versions = {
            "python": command_status("python", root, ["--version"]),
            "pyright": command_status("pyright", root, ["--version"]),
            "pyright_langserver": command_status("pyright-langserver", root),
        }
        markers = {marker: (root / marker).exists() for marker in ROOT_MARKERS}
        return {"ok": True, "root": str(root), "markers": markers, "versions": versions}

    def _position_params(self, args: dict[str, Any]) -> tuple[LspClient, str, dict[str, Any]]:
        client = self.get_lsp(args.get("root_path"), args["file_path"])
        uri = client.open_file(args["file_path"])
        return client, uri, {"textDocument": {"uri": uri}, "position": {"line": to_zero_based(args["line"]), "character": to_zero_based(args["character"])}}

    def tool_python_lsp_hover(self, args: dict[str, Any]) -> dict[str, Any]:
        client, _, params = self._position_params(args)
        result = request_until_ready(client, "textDocument/hover", params, lambda value: value is None)
        return {"ok": True, "hover": compact_markup(result)}

    def tool_python_lsp_definition(self, args: dict[str, Any]) -> dict[str, Any]:
        client, _, params = self._position_params(args)
        result = request_until_ready(client, "textDocument/definition", params, lambda value: not value)
        locations = compact_location(result) or []
        return {"ok": True, "locations": locations[: int(args.get("max_items", 40))] if isinstance(locations, list) else locations}

    def tool_python_lsp_references(self, args: dict[str, Any]) -> dict[str, Any]:
        client, _, params = self._position_params(args)
        params["context"] = {"includeDeclaration": bool(args.get("include_declaration", False))}
        result = request_until_ready(client, "textDocument/references", params, lambda value: not value)
        locations = compact_location(result) or []
        return {"ok": True, "locations": locations[: int(args.get("max_items", 40))] if isinstance(locations, list) else locations, "count": len(result or [])}

    def tool_python_lsp_document_symbols(self, args: dict[str, Any]) -> dict[str, Any]:
        client = self.get_lsp(args.get("root_path"), args["file_path"])
        uri = client.open_file(args["file_path"])
        result = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        return {"ok": True, "symbols": (result or [])[: int(args.get("max_items", 40))]}

    def tool_python_lsp_workspace_symbols(self, args: dict[str, Any]) -> dict[str, Any]:
        client = self.get_lsp(args.get("root_path"))
        result = client.request("workspace/symbol", {"query": args["query"]}, timeout=60.0)
        return {"ok": True, "symbols": (result or [])[: int(args.get("max_items", 40))], "count": len(result or [])}

    def tool_python_lsp_code_actions(self, args: dict[str, Any]) -> dict[str, Any]:
        client = self.get_lsp(args.get("root_path"), args["file_path"])
        uri = client.open_file(args["file_path"])
        rng = {"start": {"line": to_zero_based(args["start_line"]), "character": to_zero_based(args["start_character"])}, "end": {"line": to_zero_based(args["end_line"]), "character": to_zero_based(args["end_character"])}}
        diagnostics = filter_diagnostics(client.diagnostics.get(uri, []), rng)
        result = client.request("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": rng, "context": {"diagnostics": diagnostics}})
        return {"ok": True, "actions": [{"title": item.get("title"), "kind": item.get("kind"), "isPreferred": item.get("isPreferred")} for item in (result or [])[: int(args.get("max_items", 40))]], "count": len(result or [])}

    def tool_python_lsp_diagnostics(self, args: dict[str, Any]) -> dict[str, Any]:
        file_paths = args.get("file_paths") or []
        client = self.get_lsp(args.get("root_path"), file_paths[0] if file_paths else None)
        uris = [client.open_file(path) for path in file_paths]
        deadline = time.time() + (int(args.get("wait_ms", 1500)) / 1000)
        for uri in uris:
            event = client.diagnostic_events.get(uri)
            if not event or event.is_set():
                continue
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            event.wait(remaining)
        out = []
        for uri in uris:
            for diag in client.diagnostics.get(uri, []):
                start = diag.get("range", {}).get("start", {})
                out.append({"file": uri_to_path(uri), "line": start.get("line", 0) + 1, "character": start.get("character", 0) + 1, "severity": diag.get("severity"), "code": diag.get("code"), "source": diag.get("source"), "message": diag.get("message")})
        return {"ok": True, "diagnostics": out[: int(args.get("max_items", 40))], "count": len(out)}

    def tool_python_pyright_check(self, args: dict[str, Any]) -> dict[str, Any]:
        root = find_root(args.get("root_path"))
        cmd = ["pyright", "--outputjson"]
        if args.get("python_version"):
            cmd.extend(["--pythonversion", args["python_version"]])
        if args.get("venv_path"):
            cmd.extend(["--venvpath", args["venv_path"]])
        cmd.extend(args.get("extra_args") or [])
        cmd.extend(args.get("paths") or [str(root)])
        raw = run_command(cmd, root, timeout=300.0)
        parsed = None
        if raw.get("stdout"):
            try:
                parsed = json.loads(raw["stdout"])
            except json.JSONDecodeError:
                parsed = None
        diagnostics = (parsed or {}).get("generalDiagnostics", []) if isinstance(parsed, dict) else []
        return {"ok": raw.get("ok", False), "root": str(root), "command": cmd, "returncode": raw.get("returncode"), "summary": (parsed or {}).get("summary") if isinstance(parsed, dict) else None, "diagnostics": diagnostics[: int(args.get("max_items", 40))], "diagnostic_count": len(diagnostics), "stdout": "" if parsed else raw.get("stdout", ""), "stderr": raw.get("stderr", ""), "error": raw.get("error")}

    def tool_python_compile_check(self, args: dict[str, Any]) -> dict[str, Any]:
        root = find_root(args.get("root_path"), (args.get("file_paths") or [None])[0])
        results = []
        for file_path in args.get("file_paths", []):
            path = str(Path(file_path).resolve())
            raw = run_command(["python", "-m", "py_compile", path], root, timeout=60.0)
            results.append({"file": path, "ok": raw.get("ok", False), "returncode": raw.get("returncode"), "stdout": raw.get("stdout", ""), "stderr": raw.get("stderr", ""), "error": raw.get("error")})
        return {"ok": all(item["ok"] for item in results), "root": str(root), "results": results}


def mcp_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(server: PythonNativeServer, msg: dict[str, Any]) -> dict[str, Any] | None:
    msg_id = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": server.tools}}
    if method == "tools/call":
        params = msg.get("params") or {}
        return {"jsonrpc": "2.0", "id": msg_id, "result": server.call(params.get("name"), params.get("arguments") or {})}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    return None if msg_id is None else mcp_error(msg_id, -32601, f"Unknown method: {method}")


def run_selftest() -> int:
    server = PythonNativeServer()
    checks = []
    try:
        init = handle(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        checks.append({"name": "initialize", "ok": bool(init and init.get("result", {}).get("serverInfo", {}).get("name") == SERVER_NAME)})
        tools = handle(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = [tool.get("name") for tool in tools.get("result", {}).get("tools", [])] if tools else []
        checks.append({"name": "tools/list", "ok": "python_environment" in tool_names and "python_lsp_hover" in tool_names, "tool_count": len(tool_names)})
        root = Path(__file__).resolve().parents[1]
        env = server.tool_python_environment({"root_path": str(root)})
        checks.append({"name": "python_environment", "ok": bool(env.get("ok")), "versions": env.get("versions")})
        fixture = Path(__file__).resolve().parent / "tests" / "fixture.py"
        compile_result = server.tool_python_compile_check({"root_path": str(fixture.parent), "file_paths": [str(fixture)]})
        checks.append({"name": "python_compile_check", "ok": bool(compile_result.get("ok"))})
        if shutil.which(LSP_COMMAND) and fixture.exists():
            hover = server.tool_python_lsp_hover({"root_path": str(fixture.parent), "file_path": str(fixture), "line": 7, "character": 13, "max_chars": 4000})
            checks.append({"name": "python_lsp_hover", "ok": bool(hover.get("ok")), "hover": hover.get("hover")})
        else:
            checks.append({"name": "python_lsp_hover", "ok": True, "skipped": f"{LSP_COMMAND} unavailable or fixture missing"})
    finally:
        for client in server.clients.values():
            client.shutdown()
    ok = all(check.get("ok") for check in checks)
    print(json.dumps({"ok": ok, "checks": checks}, indent=2, ensure_ascii=False))
    return 0 if ok else 1


def main() -> int:
    server = PythonNativeServer()
    # The Codex plugin transport sends one JSON-RPC message per stdin line.
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = handle(server, json.loads(line))
        except Exception as exc:
            response = mcp_error(None, -32603, str(exc))
        if response is not None:
            print(json.dumps(response, separators=(",", ":"), ensure_ascii=False), flush=True)
    for client in server.clients.values():
        client.shutdown()
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(run_selftest())
    raise SystemExit(main())
