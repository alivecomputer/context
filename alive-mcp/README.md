# alive-mcp

Read-only MCP server exposing the ALIVE Context System. v0.1 scaffold. Full README lands in T16 (`fn-10-60k.16`).

## Dev env

The monorepo's system Python is 3.14. alive-mcp pins to `>=3.10,<3.14` — contributors install a pinned interpreter:

```bash
cd claude-code/alive-mcp
uv python install 3.12
uv venv --python 3.12

uv run alive-mcp --version        # prints 0.1.0
uvx --from . alive-mcp --version  # same

# Tests run from a bare checkout -- tests/__init__.py adds src/ to sys.path
# so you don't need the package installed or PYTHONPATH set.
python3 -m unittest discover tests
# Or, if you've already set up the uv venv:
uv run python -m unittest discover tests
```

Full design: `.flow/specs/fn-10-60k.md` at the walnut root.
