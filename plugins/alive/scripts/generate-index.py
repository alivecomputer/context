#!/usr/bin/env python3
"""Walnut World Index Generator v3

Walks the tree, reads all key.md + companion.md frontmatter, dumps to _index.yaml + _index.json.
Runs: post-save hook, on-demand via alive:my-context-graph, or manually.

v3 changes:
- Reads _kernel/now.json (v3 flat) first, falls back to _kernel/_generated/now.json (v2), then now.md (v1)
- Extracts per-walnut task_counts from enriched now.json (urgent/active/todo/blocked)
- .walnut/ references migrated to .alive/

v2 fixes:
- Correctly identifies walnut names when key.md is inside _core/
- Deduplicates walnut entries (prefers _core/ version)
- Skips template walnuts ({{placeholders}})
- Extracts links (wikilinks), tags, and people names
- Outputs JSON alongside YAML for graph consumption

Usage: python3 .alive/scripts/generate-index.py [world-root]
"""

import os
import sys
import re
import json
from datetime import datetime, timezone


def extract_frontmatter(filepath):
    """Extract YAML frontmatter from a markdown file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except (IOError, UnicodeDecodeError):
        return {}

    match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not match:
        return {}

    fm = {}
    lines = match.group(1).split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        kv = re.match(r'^(\w[\w-]*)\s*:\s*(.*)', line)
        if kv:
            key = kv.group(1)
            val = kv.group(2).strip()

            # Check for multi-line list (next lines start with "  - ")
            if val == '' or val == '[]':
                items = []
                j = i + 1
                while j < len(lines) and re.match(r'^\s+-\s', lines[j]):
                    item_match = re.match(r'^\s+-\s+(.*)', lines[j])
                    if item_match:
                        items.append(item_match.group(1).strip())
                    j += 1
                if items:
                    fm[key] = items
                    i = j
                    continue
                else:
                    fm[key] = val
            else:
                # Remove quotes
                if (val.startswith('"') and val.endswith('"')) or \
                   (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                fm[key] = val
        i += 1
    return fm


def parse_inline_list(val):
    """Parse [a, b, c] into a list."""
    if not val:
        return []
    val = val.strip()
    if val.startswith('[') and val.endswith(']'):
        val = val[1:-1]
    return [x.strip().strip('"').strip("'") for x in val.split(',') if x.strip()]


def extract_wikilinks(val):
    """Extract [[name]] references from a string or list."""
    if isinstance(val, list):
        result = []
        for item in val:
            result.extend(re.findall(r'\[\[([^\]]+)\]\]', str(item)))
        return result
    return re.findall(r'\[\[([^\]]+)\]\]', str(val))


def parse_people_names(filepath):
    """Extract people names from multi-line people: block in frontmatter."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except (IOError, UnicodeDecodeError):
        return []

    match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not match:
        return []

    names = []
    in_people = False
    for line in match.group(1).split('\n'):
        if re.match(r'^people\s*:', line):
            in_people = True
            continue
        if in_people:
            if re.match(r'^\w', line):  # New top-level key
                break
            name_match = re.match(r'^\s+-?\s*name\s*:\s*(.+)', line)
            if name_match:
                name = name_match.group(1).strip().strip('"').strip("'")
                names.append(name)
    return names


def detect_domain(rel_path):
    """Determine Walnut domain from relative path."""
    parts = rel_path.split(os.sep)
    if not parts:
        return "unknown"
    first = parts[0]
    domain_map = {
        "01_Archive": "archive",
        "02_Life": "life",
        "03_Inbox": "inputs",
        "04_Ventures": "ventures",
        "05_Experiments": "experiments",
    }
    domain = domain_map.get(first, "unknown")
    if domain == "life" and len(parts) > 1 and parts[1] == "people":
        return "people"
    return domain


def yaml_escape(s):
    """Escape a string for YAML output."""
    if not s:
        return '""'
    s = str(s)
    if any(c in s for c in ':{}[]&*#?|->!%@`,"\'\\'):
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if s.startswith((' ', '-')) or s.endswith(' '):
        return '"' + s + '"'
    return s


def yaml_list(items):
    """Format a list as YAML inline."""
    if not items:
        return '[]'
    return '[' + ', '.join(yaml_escape(i) for i in items) + ']'


def main():
    world_root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    world_root = os.path.abspath(world_root)
    alive_dir = os.path.join(world_root, '.alive')
    index_file = os.path.join(alive_dir, '_index.yaml')
    json_file = os.path.join(alive_dir, '_index.json')
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Dict to dedup — keyed by walnut rel_path
    walnut_entries = {}
    people_entries = {}
    total_capsules = 0

    for root, dirs, files in os.walk(world_root):
        # Skip hidden dirs, node_modules, etc.
        dirs[:] = [d for d in dirs if not d.startswith('.')
                   and d not in ('node_modules', 'Icon\r', '__pycache__',
                                 'dist', 'build', '.next', 'target')]

        if 'key.md' not in files:
            continue

        keyfile = os.path.join(root, 'key.md')

        # Determine walnut directory — if key.md is inside _core/ or _kernel/, walnut is parent
        walnut_dir = root
        basename = os.path.basename(root)
        in_core = basename in ('_core', '_kernel')
        if in_core:
            walnut_dir = os.path.dirname(root)

        walnut_name = os.path.basename(walnut_dir)
        rel_path = os.path.relpath(walnut_dir, world_root)

        # Skip world root
        if rel_path == '.':
            continue

        # Dedup: if already seen and this ISN'T the _core version, skip
        # (_core version overwrites flat version since os.walk visits subdirs after parent)
        if rel_path in walnut_entries and not in_core:
            continue
        if rel_path in people_entries and not in_core:
            continue

        fm = extract_frontmatter(keyfile)

        # Skip template walnuts
        if any('{{' in str(v) for v in fm.values()):
            continue

        domain = detect_domain(rel_path)
        wtype = fm.get('type', 'unknown')
        goal = fm.get('goal', '')
        rhythm = fm.get('rhythm', '')
        created = fm.get('created', '')

        # Extract parent
        parent_raw = fm.get('parent', '')
        parent_links = extract_wikilinks(parent_raw)
        parent = parent_links[0] if parent_links else ''

        # Extract links (wikilinks)
        links_raw = fm.get('links', '')
        if isinstance(links_raw, list):
            links = extract_wikilinks(links_raw)
        else:
            links = extract_wikilinks(links_raw)

        # Extract tags
        tags_raw = fm.get('tags', '')
        if isinstance(tags_raw, list):
            tags = tags_raw
        else:
            tags = parse_inline_list(tags_raw)

        # Extract people names
        people_names = parse_people_names(keyfile)

        # Read now.json (v3 flat first, v2 generated, then v1 now.md fallback)
        phase = ''
        updated = ''
        next_action = ''
        active_capsule = ''
        task_counts = {}
        now_json_loaded = False
        for candidate in [os.path.join(walnut_dir, '_kernel', 'now.json'),
                          os.path.join(walnut_dir, '_kernel', '_generated', 'now.json')]:
            if os.path.isfile(candidate):
                try:
                    with open(candidate, 'r', encoding='utf-8') as nf:
                        now_data = json.load(nf)
                    phase = now_data.get('phase', '')
                    updated = now_data.get('updated', '')
                    nxt = now_data.get('next')
                    if isinstance(nxt, dict):
                        next_action = nxt.get('action', '')
                    elif isinstance(nxt, str):
                        next_action = nxt
                    active_capsule = now_data.get('capsule', now_data.get('outcome', ''))

                    # Extract task counts from v3 enriched now.json
                    tc = {'urgent': 0, 'active': 0, 'todo': 0, 'blocked': 0}
                    # Unscoped tasks
                    unscoped = now_data.get('unscoped_tasks', {})
                    uc = unscoped.get('counts', {})
                    for k in tc:
                        tc[k] += uc.get(k, 0)
                    # Bundle tasks (active + recent tiers)
                    bundles = now_data.get('bundles', {})
                    for tier_name in ('active', 'recent'):
                        tier = bundles.get(tier_name, {})
                        for bname, bdata in tier.items():
                            bc = bdata.get('counts', {})
                            for k in tc:
                                tc[k] += bc.get(k, 0)
                    # Only include if there are any non-zero counts
                    if any(v > 0 for v in tc.values()):
                        task_counts = tc

                    now_json_loaded = True
                except (IOError, json.JSONDecodeError, UnicodeDecodeError):
                    pass
                if now_json_loaded:
                    break

        # v1 fallback: now.md
        if not now_json_loaded:
            for candidate in [os.path.join(walnut_dir, '_core', 'now.md'),
                              os.path.join(walnut_dir, 'now.md')]:
                if os.path.isfile(candidate):
                    nfm = extract_frontmatter(candidate)
                    phase = nfm.get('phase', '')
                    updated = nfm.get('updated', '')
                    next_action = nfm.get('next', '')
                    active_capsule = nfm.get('capsule', nfm.get('outcome', ''))
                    break

        # Count capsules
        capsule_entries = []
        capsule_count = 0
        for cap_dir in [os.path.join(walnut_dir, '_core', '_capsules'),
                        os.path.join(walnut_dir, '_capsules')]:
            if os.path.isdir(cap_dir):
                for item in sorted(os.listdir(cap_dir)):
                    cap_path = os.path.join(cap_dir, item)
                    if os.path.isdir(cap_path):
                        capsule_count += 1
                        comp = os.path.join(cap_path, 'companion.md')
                        if os.path.isfile(comp):
                            cfm = extract_frontmatter(comp)
                            capsule_entries.append({
                                'name': item,
                                'goal': cfm.get('goal', ''),
                                'status': cfm.get('status', 'draft'),
                                'updated': cfm.get('updated', ''),
                            })
                break  # Use first capsules dir found

        # Count squirrel sessions
        squirrel_count = 0
        for sq_dir in [os.path.join(walnut_dir, '_core', '_squirrels'),
                       os.path.join(walnut_dir, '_squirrels')]:
            if os.path.isdir(sq_dir):
                squirrel_count = len([f for f in os.listdir(sq_dir)
                                      if f.endswith('.yaml')])
                break

        is_archived = rel_path.startswith('01_Archive')
        total_capsules += capsule_count

        entry = {
            'name': walnut_name,
            'path': rel_path,
            'type': wtype,
            'goal': goal,
            'phase': phase,
            'rhythm': rhythm,
            'updated': updated,
            'created': created,
            'domain': domain,
            'archived': is_archived,
            'capsule_count': capsule_count,
            'squirrel_sessions': squirrel_count,
            'active_capsule': active_capsule,
            'next': next_action,
            'capsules': capsule_entries,
            'links': links,
            'tags': tags,
            'people': people_names,
            'parent': parent,
            'task_counts': task_counts,
        }

        target = people_entries if (wtype == 'person' or domain == 'people') else walnut_entries
        target[rel_path] = entry

    # Convert to sorted lists
    walnuts = list(walnut_entries.values())
    people = list(people_entries.values())

    # World-level squirrels
    world_sq_dir = os.path.join(world_root, '.alive', '_squirrels')
    world_sq_count = 0
    if os.path.isdir(world_sq_dir):
        world_sq_count = len([f for f in os.listdir(world_sq_dir)
                              if f.endswith('.yaml')])

    # ─── Recent sessions + unsigned stash count ───
    recent_sessions = []
    unsigned_with_stash = 0
    if os.path.isdir(world_sq_dir):
        sq_files = [f for f in os.listdir(world_sq_dir) if f.endswith('.yaml')]
        # Sort by modification time, newest first
        sq_files.sort(
            key=lambda f: os.path.getmtime(os.path.join(world_sq_dir, f)),
            reverse=True
        )

        def extract_sq_field(content, field):
            """Extract a field value from squirrel YAML via regex."""
            m = re.search(r'^' + re.escape(field) + r'\s*:\s*(.*)', content, re.MULTILINE)
            if not m:
                return ''
            val = m.group(1).strip()
            # Remove surrounding quotes
            if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or
                                   (val[0] == "'" and val[-1] == "'")):
                val = val[1:-1]
            return val

        for sq_file in sq_files:
            sq_path = os.path.join(world_sq_dir, sq_file)
            try:
                with open(sq_path, 'r', encoding='utf-8') as sf:
                    sq_content = sf.read()
            except (IOError, UnicodeDecodeError):
                continue

            saves_str = extract_sq_field(sq_content, 'saves')
            saves_val = 0
            try:
                saves_val = int(saves_str)
            except (ValueError, TypeError):
                pass

            # Check for unsigned with non-empty stash
            has_empty_stash = bool(re.search(r'^stash\s*:\s*\[\s*\]\s*$', sq_content, re.MULTILINE))
            has_stash_key = bool(re.search(r'^stash\s*:', sq_content, re.MULTILINE))
            if saves_val == 0 and has_stash_key and not has_empty_stash:
                # Check stash actually has items (next line starts with "  - ")
                stash_m = re.search(r'^stash\s*:.*\n(\s+-\s)', sq_content, re.MULTILINE)
                if stash_m:
                    unsigned_with_stash += 1

            # Collect recent sessions (top 10 by mtime)
            if len(recent_sessions) < 10:
                session_id = extract_sq_field(sq_content, 'session_id')
                walnut = extract_sq_field(sq_content, 'walnut')
                started = extract_sq_field(sq_content, 'started')
                recovery = extract_sq_field(sq_content, 'recovery_state')
                bundle = extract_sq_field(sq_content, 'bundle')
                tags_raw = extract_sq_field(sq_content, 'tags')

                # Extract date from started (YYYY-MM-DD)
                date = ''
                if started:
                    date_m = re.match(r'(\d{4}-\d{2}-\d{2})', started)
                    if date_m:
                        date = date_m.group(1)

                # Parse inline tags [a, b, c]
                tags_list = parse_inline_list(tags_raw)

                entry = {
                    'squirrel': session_id[:8] if session_id else sq_file[:8],
                    'walnut': walnut if walnut and walnut != 'null' else '',
                    'date': date,
                    'saves': saves_val,
                    'summary': recovery,
                }
                if bundle:
                    entry['bundle'] = bundle
                if tags_list:
                    entry['tags'] = tags_list

                recent_sessions.append(entry)

    # Inputs count
    inputs_dir = os.path.join(world_root, '03_Inbox')
    input_count = 0
    if os.path.isdir(inputs_dir):
        input_count = len([f for f in os.listdir(inputs_dir)
                           if not f.startswith('.') and f != 'Icon\r'])

    # Sort by domain then name
    domain_order = {'life': 0, 'ventures': 1, 'experiments': 2, 'archive': 3,
                    'unknown': 4}
    walnuts.sort(key=lambda w: (domain_order.get(w['domain'], 4), w['name']))
    people.sort(key=lambda p: p['name'])

    # ─── Write YAML index ───
    lines = [
        '# Walnut World Index — GENERATED, DO NOT HAND-EDIT',
        '# Regenerated by .alive/scripts/generate-index.py',
        f'generated: "{timestamp}"',
        f'walnut_count: {len(walnuts)}',
        f'people_count: {len(people)}',
        f'capsule_count: {total_capsules}',
        f'world_squirrel_sessions: {world_sq_count}',
        f'unrouted_inputs: {input_count}',
        '',
        'walnuts:',
    ]

    current_domain = None
    for w in walnuts:
        if w['domain'] != current_domain:
            current_domain = w['domain']
            lines.append(f'  # ─── {current_domain.upper()} ───')

        lines.append(f'  {w["name"]}:')
        lines.append(f'    path: {yaml_escape(w["path"])}')
        lines.append(f'    type: {w["type"]}')
        if w['goal']:
            lines.append(f'    goal: {yaml_escape(w["goal"])}')
        lines.append(f'    phase: {w["phase"] or "unknown"}')
        if w['rhythm']:
            lines.append(f'    rhythm: {w["rhythm"]}')
        lines.append(f'    updated: {yaml_escape(w["updated"] or "unknown")}')
        if w['created']:
            lines.append(f'    created: {w["created"]}')
        lines.append(f'    domain: {w["domain"]}')
        if w['archived']:
            lines.append(f'    archived: true')
        if w['links']:
            lines.append(f'    links: {yaml_list(w["links"])}')
        if w['tags']:
            lines.append(f'    tags: {yaml_list(w["tags"])}')
        if w['people']:
            lines.append(f'    people: {yaml_list(w["people"])}')
        if w['parent']:
            lines.append(f'    parent: {yaml_escape(w["parent"])}')
        if w['capsule_count'] > 0:
            lines.append(f'    capsule_count: {w["capsule_count"]}')
        if w['squirrel_sessions'] > 0:
            lines.append(f'    squirrel_sessions: {w["squirrel_sessions"]}')
        if w['active_capsule']:
            lines.append(f'    active_capsule: {w["active_capsule"]}')
        if w['next']:
            lines.append(f'    next: {yaml_escape(w["next"])}')
        if w.get('task_counts'):
            tc = w['task_counts']
            lines.append(f'    task_counts:')
            lines.append(f'      urgent: {tc.get("urgent", 0)}')
            lines.append(f'      active: {tc.get("active", 0)}')
            lines.append(f'      todo: {tc.get("todo", 0)}')
            lines.append(f'      blocked: {tc.get("blocked", 0)}')
        if w['capsules']:
            lines.append(f'    capsules:')
            for c in w['capsules']:
                lines.append(f'      {c["name"]}:')
                if c['goal']:
                    lines.append(f'        goal: {yaml_escape(c["goal"])}')
                lines.append(f'        status: {c["status"]}')
                if c['updated']:
                    lines.append(f'        updated: {yaml_escape(c["updated"])}')
        lines.append('')

    lines.append('people:')
    lines.append(f'  # {len(people)} people walnuts')
    for p in people:
        lines.append(f'  {p["name"]}:')
        lines.append(f'    path: {yaml_escape(p["path"])}')
        lines.append(f'    updated: {yaml_escape(p["updated"] or "unknown")}')

    lines.append('')
    lines.append('recent_sessions:')
    if recent_sessions:
        for rs in recent_sessions:
            lines.append(f'  - squirrel: {yaml_escape(rs["squirrel"])}')
            if rs.get('walnut'):
                lines.append(f'    walnut: {yaml_escape(rs["walnut"])}')
            if rs.get('date'):
                lines.append(f'    date: {yaml_escape(rs["date"])}')
            if rs.get('bundle'):
                lines.append(f'    bundle: {yaml_escape(rs["bundle"])}')
            lines.append(f'    saves: {rs["saves"]}')
            if rs.get('summary'):
                lines.append(f'    summary: {yaml_escape(rs["summary"])}')
            if rs.get('tags'):
                lines.append(f'    tags: {yaml_list(rs["tags"])}')
    else:
        lines.append('  # no recent sessions found')

    lines.append('')
    lines.append(f'unsigned_with_stash: {unsigned_with_stash}')

    output = '\n'.join(lines) + '\n'

    os.makedirs(alive_dir, exist_ok=True)
    with open(index_file, 'w', encoding='utf-8') as f:
        f.write(output)

    # ─── Write JSON for graph consumption ───
    # Strip empty values for cleaner JSON
    def clean(entry, keep_zero=None):
        keep_zero = keep_zero or set()
        return {k: v for k, v in entry.items()
                if v or v == 0 and k in keep_zero
                or (v != '' and v != [] and v is not None and v is not False)}

    def clean_walnut(entry):
        return {k: v for k, v in entry.items()
                if v and v != [] and v != 0 and v is not False}

    def clean_session(entry):
        """Clean session entry but preserve saves: 0 since it's meaningful."""
        return {k: v for k, v in entry.items()
                if k == 'saves' or (v and v != [] and v is not False)}

    json_data = {
        'generated': timestamp,
        'stats': {
            'walnuts': len(walnuts),
            'people': len(people),
            'capsules': total_capsules,
            'sessions': world_sq_count,
            'inputs': input_count,
            'unsigned_with_stash': unsigned_with_stash,
        },
        'walnuts': [clean_walnut(w) for w in walnuts],
        'people': [clean_walnut(p) for p in people],
        'recent_sessions': [clean_session(rs) for rs in recent_sessions],
    }
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, indent=2, default=str)

    print(f"Index: {index_file}")
    print(f"JSON:  {json_file}")
    print(f"Walnuts: {len(walnuts)} | People: {len(people)} | "
          f"Capsules: {total_capsules} | Inputs: {input_count} | "
          f"Sessions: {world_sq_count}")


if __name__ == '__main__':
    main()
