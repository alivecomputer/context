# MCPB bundle — developer notes

This directory holds the `.mcpb` bundle source for Claude Desktop
one-click install. The canonical build entrypoint is
`../scripts/build-mcpb.sh`; the manifest here is what gets copied into
the bundle root.

## Why `server.mcp_config` is present even though we use `server.type: "uv"`

The MCPB v0.4 spec (see `github.com/anthropics/mcpb/blob/main/MANIFEST.md`)
documents `mcp_config` as optional for `type: "uv"`, noting that the
host manages execution. **In practice the v0.4 JSON schema shipped
with the `@anthropic-ai/mcpb` CLI marks `mcp_config` as REQUIRED** —
see `mcpb-manifest-v0.4.schema.json`:

```json
"server": {
  ...
  "required": ["type", "entry_point", "mcp_config"]
}
```

So `mcpb validate` rejects any `uv` bundle that omits it. We therefore
declare a minimal `mcp_config` that mirrors the canonical example from
`anthropics/mcpb/examples/hello-world-uv/manifest.json`:

```json
"mcp_config": {
  "command": "uv",
  "args": ["run", "--directory", "${__dirname}", "src/alive_mcp/__main__.py"]
}
```

`${__dirname}` is MCPB's standard placeholder for the bundle's
unpacked root directory; Claude Desktop expands it at runtime. This
gives `uv` the directory containing `pyproject.toml`, which it uses to
sync the environment and run our entry point.

**When upstream ships a spec-conformant schema (`mcp_config` genuinely
optional for `uv`), this block can drop down to just `type` +
`entry_point`.** Track at
`github.com/anthropics/mcpb/issues` — watch for the v0.5 manifest.

## Why the manifest lives here and not at the package root

`mcpb pack` packs every file in the directory it is pointed at (minus
the `.mcpbignore` exclusions). If we left `manifest.json` at
`alive-mcp/manifest.json` and packed from there, a careless run would
hoover up `.venv/`, `node_modules/`, `tests/`, and every other
development artifact.

Placing the manifest in a dedicated `mcpb/` subdir means
`scripts/build-mcpb.sh` can stage the exact file set we want into a
clean dir under `dist/.mcpb-staging/` and pack from THAT. The staging
dir contains only: `manifest.json`, `pyproject.toml`, `uv.lock`,
`README.md`, `LICENSE`, and `src/alive_mcp/`. 29 files, ~220KB packed.

## Editing the manifest

`version` in `manifest.json` MUST match `version` in
`../pyproject.toml`. The build script enforces this via an awk
cross-check before packing; if they drift, the build fails with a
clear "version drift detected" message. Bump both together.

The awk parser in `scripts/build-mcpb.sh` looks for `version` at
two-space indent — standard JSON formatting. If you reformat the file
with different indentation, update the awk regex too.
