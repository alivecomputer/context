# ALIVE v4 ULID prefix table (alive-demo)

ULIDs identify every kind of object the demo skill scaffolds. Prefix is a 3-letter
ASCII tag joined to the 26-char Crockford-base32 ULID body with an underscore, e.g.
`wld_01j5hk7yvgkw0zfg9k3vh4p9xv`. Bodies are emitted lowercase per ALIVE convention
(`from ulid import ULID; str(ULID()).lower()`).

The table below is the v4-aligned namespace. v3.2 (this release) only generates
`wld_` IDs at runtime; the remaining 8 prefixes are documented here so the demo
output is forward-compatible with the v4 atoms pipeline (planned for the next
major release). Each row names the prefix, the kind of object it identifies, and
the v3.2 generator status.

| Prefix | Object | v3.2 status |
| ------ | ------ | ----------- |
| `wld_` | World — the demo-generated personal-context world (root of the tree). | **generated** by Stage 5; persisted into `<world>/.alive/key.md` and `demo-state.json`. |
| `wal_` | Walnut — a single venture / person / experiment within the world. | reserved (v4); v3.2 walnuts are addressed by directory slug, not ULID. |
| `prs_` | Person — a person walnut atom. | reserved (v4). |
| `bnd_` | Bundle — a unit of focused work inside a walnut. | reserved (v4). |
| `ses_` | Session — a single squirrel session. | reserved (v4); v3.2 sessions are addressed by their existing 8-hex `squirrel:<id>`. |
| `tsk_` | Task — a queued / completed task. | reserved (v4); v3.2 tasks remain `t001`-style sequence ids per `tasks.py`. |
| `ent_` | Entity — a generic context-graph atom (catch-all for v4 atoms). | reserved (v4). |
| `ist_` | Insight — an evergreen insight emitted into `_kernel/insights.md`. | reserved (v4). |
| `atm_` | Atom — a v4 atom-pipeline node (raw context unit). | reserved (v4). |

## Slug-shape contract

For every prefix, the part after the underscore is the lowercase Crockford-base32
encoding of a 128-bit ULID (26 chars, alphabet `0-9a-hjkmnp-tv-z`). Slugs that
appear in user-facing labels (walnut directory names, bundle directory names) are
derived from free-text descriptions via `lib.derive_label` and follow the
filesystem-safe slug rule from `_spike.md`:

```
^[a-z0-9]+(-[a-z0-9]+)*$
```

(lowercase ASCII alphanumerics with single-hyphen separators only — no leading or
trailing hyphens, no double hyphens, no dots, no slashes, no unicode). The slug
rule is enforced at Stage 0 emission and re-validated by `validate.py` before
Stage 2 dispatch (fn-2-2zz.10).

## References

- `plugins/alive/skills/demo/lib.py` — `new_world_ulid()`, `derive_label()` helpers.
- `plugins/alive/_vendor/ulid/` — vendored python-ulid 3.1.0 (MIT, mdomke).
- `_spike.md` § "Slug sanitization (path-safety guard)" — slug regex contract.
- `.flow/specs/fn-2-2zz.md` § "Acceptance" — v4-aligned prefix-table requirement.
