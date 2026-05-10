---
name: python-native
description: Use when working in Python repositories where semantic accuracy matters. Prefer pyright-langserver LSP tools for hover, definitions, references, symbols, diagnostics, and code actions, and pyright/Python tools for type checking and syntax compilation.
---

# Python Native

Use this skill for non-trivial Python code reading, editing, refactoring, debugging, and review.

## When To Use

- Use for Python repositories, `pyright-langserver` lookups, and compiler/type-checker validation.
- Prefer this over text search when symbol identity, inferred types, or references matter.

## What It Provides

- `pyright-langserver` hover, definition, references, document symbols, workspace symbols, diagnostics, and code actions.
- `pyright` JSON type diagnostics.
- `python -m py_compile` syntax validation.

## Workflow

1. Resolve the Python project root. Prefer the nearest directory with `pyproject.toml`, `pyrightconfig.json`, `setup.py`, `setup.cfg`, `requirements.txt`, or `.git`.
2. Call `python_environment` once to confirm `python`, `pyright`, and `pyright-langserver` availability.
3. Use pyright-langserver before guessing semantic details:
   - `python_lsp_hover` for inferred types and docs.
   - `python_lsp_definition` before following a symbol by text search.
   - `python_lsp_references` for semantic references.
   - `python_lsp_document_symbols` and `python_lsp_workspace_symbols` for compact navigation.
   - `python_lsp_diagnostics` before `python_lsp_code_actions` when asking for quick fixes on a problematic range.
   - `python_lsp_code_actions` when imports or fixes may be available.
4. Use verification tools before finalizing Python edits:
   - `python_pyright_check` for type diagnostics using `pyright --outputjson`.
   - `python_compile_check` for syntax validation using `python -m py_compile`.

## Output Discipline

- Keep `max_items` and `max_chars` low until more detail is needed.
- Treat LSP and pyright output as evidence, not as a replacement for reading surrounding code.
- Do not rewrite files or run formatters unless the user explicitly asks for that.
- Use `wait_ms` around 1500 for cold `python_lsp_diagnostics` calls; subsequent calls can usually use lower waits.

## Position Convention

The LSP tools accept 1-based `line` and 1-based `character` values. The MCP server converts them to zero-based LSP positions internally and clamps invalid values to zero.
