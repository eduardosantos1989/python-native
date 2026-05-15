# Python Native

Semantic Python tools for Codex, backed by `pyright-langserver`, `pyright`, and Python.

This plugin exposes a local MCP server that gives Codex token-efficient Python code intelligence without relying on text search alone. It starts `pyright-langserver --stdio` per project root for semantic LSP queries and shells out to Pyright and Python for validation.

## Features

- Pyright LSP hover, definitions, references, document symbols, workspace symbols, diagnostics, and code actions.
- `pyright --outputjson` type diagnostics.
- `python -m py_compile` syntax checks.
- Lightweight `--selftest` fixture for smoke testing the MCP server.

## Requirements

- Python 3.11 or newer.
- `pyright` on `PATH`.
- `pyright-langserver` on `PATH`.

Install Pyright with npm if needed:

```powershell
npm install -g pyright
```

## Installation

Place this directory at:

```text
%USERPROFILE%\.codex\plugins\python-native
```

The plugin metadata is in `.codex-plugin/plugin.json`, and the MCP server command is in `.mcp.json`.

## Smoke Test

Run from the plugin root:

```powershell
python scripts\python_native_mcp.py --selftest
```

Expected result: JSON output with `"ok": true`.

## Tooling Notes

- LSP positions use 1-based `line` and 1-based `character` inputs.
- Call `python_lsp_diagnostics` before `python_lsp_code_actions` when asking for quick fixes on a problematic range.
- `python_compile_check` validates syntax only; use `python_pyright_check` for type diagnostics.

## License

MIT.
