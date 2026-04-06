#!/usr/bin/env python3
"""ALIVE Context Graph Generator v3

Reads _index.json and generates an interactive D3.js force-directed graph.
Run after generate-index.py to create .alive/context-graph.html.

Features:
- ALIVE branded (cream light mode default, forest green dark mode)
- Custom fonts (Array, Khand, Inter)
- Light/dark theme toggle
- Expandable capsule nodes (click walnut to show/hide capsules)
- Click-to-pin details panel
- Edge labels on hover
- Domain region labels in cluster mode
- Search with / hotkey
- People connectors toggle

Usage: python3 .alive/scripts/generate-graph.py [world-root]
"""

import json
import os
import sys
from datetime import datetime, timezone
from collections import defaultdict


def main():
    world_root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    world_root = os.path.abspath(world_root)
    json_file = os.path.join(world_root, '.alive', '_index.json')
    html_file = os.path.join(world_root, '.alive', 'context-graph.html')

    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        print(f"Error reading {json_file}: {e}", file=sys.stderr)
        sys.exit(1)

    stats = data['stats']
    walnuts = data['walnuts']
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    nodes = []
    node_ids = set()

    for w in walnuts:
        wid = w['name']
        domain = w.get('domain', 'unknown')
        phase = w.get('phase', 'unknown')
        updated = w.get('updated', '2026-01-01')[:10]
        capsule_count = w.get('capsule_count', 0)
        capsule_details = w.get('capsules', [])
        sessions = w.get('squirrel_sessions', 0)
        archived = w.get('archived', False)

        try:
            days_since = (datetime.strptime(today, '%Y-%m-%d') -
                         datetime.strptime(updated, '%Y-%m-%d')).days
        except (ValueError, TypeError, KeyError):
            days_since = 30

        if capsule_count >= 15: size = 20
        elif capsule_count >= 5: size = 15
        elif capsule_count >= 1: size = 11
        elif days_since <= 2: size = 12
        elif days_since <= 7: size = 9
        elif days_since <= 14: size = 7
        else: size = 5

        nodes.append({
            'id': wid, 'label': wid.replace('-', ' '),
            'domain': domain, 'type': w.get('type', 'unknown'),
            'goal': w.get('goal', ''), 'phase': phase,
            'rhythm': w.get('rhythm', ''), 'updated': updated,
            'next': w.get('next', ''), 'size': size,
            'daysSince': days_since, 'capsuleCount': capsule_count,
            'capsuleDetails': capsule_details,
            'sessions': sessions, 'archived': archived,
            'isPerson': False, 'tags': w.get('tags', []),
            'people': w.get('people', []),
            'activeCapsule': w.get('active_capsule', ''),
        })
        node_ids.add(wid)

    links = []
    link_set = set()
    for w in walnuts:
        wid = w['name']
        if wid not in node_ids: continue
        for target in w.get('links', []):
            if target in node_ids and target != wid:
                key = tuple(sorted([wid, target]))
                if key not in link_set:
                    links.append({'source': wid, 'target': target, 'type': 'link'})
                    link_set.add(key)
        parent = w.get('parent', '')
        if parent and parent in node_ids and parent != wid:
            key = tuple(sorted([wid, parent]))
            if key not in link_set:
                links.append({'source': parent, 'target': wid, 'type': 'parent'})
                link_set.add(key)

    person_to_walnuts = defaultdict(list)
    for w in walnuts:
        if w.get('archived'): continue
        for p in w.get('people', []):
            person_to_walnuts[p].append(w['name'])

    people_nodes = []
    people_links = []
    for person, connected in person_to_walnuts.items():
        if len(connected) >= 2:
            pid = 'p-' + person.lower().replace(' ', '-').replace("'", '')
            people_nodes.append({
                'id': pid, 'label': person, 'domain': 'people',
                'type': 'person', 'goal': '', 'phase': '',
                'size': 5 + min(len(connected), 6),
                'isPerson': True, 'connects': connected,
            })
            for wid in connected:
                if wid in node_ids:
                    people_links.append({
                        'source': pid, 'target': wid,
                        'type': 'person', 'label': person,
                    })

    # Central world root node — reads name from .alive/key.md at runtime
    wr_name = 'world root'
    wr_id = 'world-root'
    try:
        import re
        key_path = os.path.join(world_root, '.alive', 'key.md')
        if os.path.exists(key_path):
            with open(key_path, encoding='utf-8') as f:
                content = f.read()
            m = re.search(r'^name:\s*(.+)$', content, re.MULTILINE)
            if m:
                wr_name = m.group(1).strip()
                wr_id = wr_name.lower().replace(' ', '-')
    except Exception:
        pass

    world_root_node = {
        'id': wr_id, 'label': wr_name, 'domain': 'life',
        'type': 'world', 'goal': 'Build the life, ventures, and systems that matter',
        'phase': 'world', 'size': 24, 'special': True, 'isPerson': False,
    }
    world_root_links = []
    # Connect to top-level walnuts from key.md links field
    try:
        key_path = os.path.join(world_root, '.alive', 'key.md')
        if os.path.exists(key_path):
            with open(key_path, encoding='utf-8') as f:
                content = f.read()
            # Parse wikilinks from links: field
            links_match = re.search(r'^links:\s*(.+)$', content, re.MULTILINE)
            if links_match:
                wikilinks = re.findall(r'\[\[([^\]]+)\]\]', links_match.group(1))
                for wl in wikilinks:
                    if wl in node_ids:
                        world_root_links.append({'source': wr_id, 'target': wl, 'type': 'link'})
    except Exception:
        pass
    # Fallback: connect to all top-level domain folders if no links found
    if not world_root_links:
        for t in node_ids:
            if t.startswith('0') and '_' in t:
                world_root_links.append({'source': wr_id, 'target': t, 'type': 'link'})

    inputs_node = {
        'id': 'inputs', 'label': f"{stats['inputs']} inputs",
        'domain': 'inputs', 'type': 'buffer',
        'goal': f"{stats['inputs']} unrouted items in 03_Inbox/",
        'phase': 'buffer', 'size': 14, 'special': True, 'isPerson': False,
    }

    all_nodes = nodes + [world_root_node, inputs_node]
    all_links = links + world_root_links

    nj = json.dumps(all_nodes, ensure_ascii=False)
    lj = json.dumps(all_links, ensure_ascii=False)
    pnj = json.dumps(people_nodes, ensure_ascii=False)
    plj = json.dumps(people_links, ensure_ascii=False)

    html = build_html(stats, nj, lj, pnj, plj)
    with open(html_file, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"Graph: {html_file}")
    print(f"Nodes: {len(all_nodes)} + {len(people_nodes)} people | "
          f"Edges: {len(all_links)} + {len(people_links)} people")


def build_html(stats, nj, lj, pnj, plj):
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ALIVE Context Graph</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<link rel="preconnect" href="https://api.fontshare.com">
<link href="https://api.fontshare.com/v2/css?f[]=array@400,600,700&f[]=khand@400,500,600,700&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}

/* ─── THEME VARIABLES ─── */
:root {{
  --bg: #FAF8F5;
  --surface: #FFFFFF;
  --surface-2: #F0EBE5;
  --border: #E8E2DB;
  --border-light: #EDE8E2;
  --text: #1A1715;
  --text-dim: #6B6560;
  --text-muted: #8A8078;
  --primary: #F97316;
  --primary-dim: #F9731618;
  --primary-glow: #F9731640;
  --copper: #B87333;
  --copper-dim: #B8733320;
  --green: #22C55E;
  --green-dark: #16A34A;
  --cream: #FAF8F5;
  --cream-dark: #F0EBE5;
  --shadow: rgba(26, 23, 21, 0.06);
  --shadow-heavy: rgba(26, 23, 21, 0.12);

  /* Domain colors — light mode */
  --c-life: #3B7DD8;
  --c-ventures: #E86A10;
  --c-experiments: #16A34A;
  --c-archive: #8A8078;
  --c-people: #8B45A6;
  --c-inputs: #C44E3F;

  /* Capsule status */
  --cap-draft: #8A8078;
  --cap-prototype: #E09020;
  --cap-published: #3B7DD8;
  --cap-done: #16A34A;

  /* Graph */
  --link-color: #1A171512;
  --link-parent: #B8733330;
  --link-person: #8B45A620;
  --link-capsule: #8A807830;
  --node-label: #1A171590;
  --node-label-dim: #1A171540;
  --glow-filter: drop-shadow(0 0 4px rgba(249, 115, 22, 0.4));
}}

html.dark {{
  --bg: #0A1F0D;
  --surface: #14532D;
  --surface-2: #1A3A1F;
  --border: rgba(250, 248, 245, 0.1);
  --border-light: rgba(250, 248, 245, 0.06);
  --text: #FAF8F5;
  --text-dim: #D4CFC8;
  --text-muted: #8A8078;
  --primary: #F97316;
  --primary-dim: #F9731625;
  --primary-glow: #F9731650;
  --copper: #D4A574;
  --copper-dim: #B8733330;
  --shadow: rgba(0, 0, 0, 0.3);
  --shadow-heavy: rgba(0, 0, 0, 0.5);

  --c-life: #5B9DE9;
  --c-ventures: #F97316;
  --c-experiments: #4ADE80;
  --c-archive: #8A8078;
  --c-people: #B67FD0;
  --c-inputs: #E74C3C;

  --cap-draft: #8A8078;
  --cap-prototype: #F0AD4E;
  --cap-published: #5B9DE9;
  --cap-done: #4ADE80;

  --link-color: #FAF8F510;
  --link-parent: #D4A57430;
  --link-person: #B67FD020;
  --link-capsule: #8A807830;
  --node-label: #FAF8F580;
  --node-label-dim: #FAF8F530;
  --glow-filter: drop-shadow(0 0 6px rgba(249, 115, 22, 0.5));
}}

body {{
  background: var(--bg);
  color: var(--text);
  font-family: 'Inter', -apple-system, sans-serif;
  overflow: hidden;
  height: 100vh; width: 100vw;
  transition: background 0.3s, color 0.3s;
  -webkit-font-smoothing: antialiased;
}}
svg {{ display: block; }}

/* ─── HEADER ─── */
.header {{
  position: fixed; top:0; left:0; right:0; height: 52px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 24px; z-index: 100;
  transition: background 0.3s, border-color 0.3s;
}}
.header-left {{ display: flex; align-items: center; gap: 20px; }}
.header-title {{
  font-family: 'Array', 'Inter', sans-serif;
  font-weight: 600; font-size: 16px; letter-spacing: 0.02em;
  color: var(--primary);
}}
.header-stats {{
  color: var(--text-muted); font-size: 11px;
  font-family: 'Khand', monospace; font-weight: 500;
  display: flex; gap: 16px; letter-spacing: 0.02em;
}}
.header-stats span {{ display: flex; align-items: center; gap: 4px; }}
.stat-hl {{ color: var(--text-dim); }}
.header-right {{ display: flex; align-items: center; gap: 12px; }}

/* Theme toggle */
.theme-toggle {{
  background: var(--surface-2); border: 1px solid var(--border);
  color: var(--text-muted); width: 36px; height: 36px;
  border-radius: 8px; cursor: pointer; display: flex;
  align-items: center; justify-content: center;
  font-size: 16px; transition: all 0.2s;
}}
.theme-toggle:hover {{ border-color: var(--primary); color: var(--primary); }}

/* ─── SEARCH ─── */
.search-box {{ position: fixed; top: 64px; left: 50%; transform: translateX(-50%); z-index: 100; }}
.search-box input {{
  background: var(--surface); border: 1px solid var(--border);
  color: var(--text); font-family: 'Khand', monospace; font-weight: 500;
  font-size: 13px; padding: 8px 20px; border-radius: 24px;
  width: 260px; outline: none; transition: all 0.2s;
  box-shadow: 0 2px 8px var(--shadow);
  letter-spacing: 0.01em;
}}
.search-box input:focus {{
  border-color: var(--primary);
  box-shadow: 0 2px 16px var(--primary-glow);
  width: 340px;
}}
.search-box input::placeholder {{ color: var(--text-muted); }}

/* ─── LEGEND ─── */
.legend {{
  position: fixed; bottom: 24px; left: 24px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px; z-index: 100;
  font-size: 11px; min-width: 150px;
  box-shadow: 0 4px 16px var(--shadow);
  transition: background 0.3s, border-color 0.3s;
}}
.legend-title {{
  font-family: 'Khand', sans-serif; font-weight: 600;
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em;
  color: var(--text-muted); margin-bottom: 8px;
}}
.legend-item {{
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 5px; cursor: default; user-select: none;
  font-family: 'Inter', sans-serif; font-size: 11px;
  color: var(--text-dim);
}}
.legend-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
.legend-count {{
  color: var(--text-muted); font-family: 'Khand', monospace;
  margin-left: auto; font-size: 11px; font-weight: 500;
}}

/* ─── TOOLTIP ─── */
.tooltip {{
  position: fixed; background: var(--surface);
  border: 1px solid var(--border); border-radius: 12px;
  padding: 14px 18px; max-width: 340px; z-index: 200;
  pointer-events: none; opacity: 0; transition: opacity 0.12s;
  font-size: 12px; box-shadow: 0 8px 32px var(--shadow-heavy);
}}
.tooltip.visible {{ opacity: 1; }}
.tooltip h3 {{
  font-family: 'Khand', sans-serif; font-weight: 600;
  font-size: 16px; margin-bottom: 4px; color: var(--text);
}}
.badge {{
  display: inline-block; font-family: 'Khand', monospace;
  font-size: 10px; font-weight: 500; padding: 2px 8px;
  border-radius: 4px; text-transform: uppercase;
  letter-spacing: 0.06em; margin-right: 4px; margin-bottom: 6px;
}}
.tooltip .goal {{ color: var(--text-dim); margin-bottom: 6px; line-height: 1.4; font-size: 11px; }}
.tooltip .meta {{
  font-family: 'Khand', monospace; font-size: 11px; font-weight: 500;
  color: var(--text-muted); display: flex; flex-wrap: wrap; gap: 5px;
}}
.tooltip .meta span {{
  background: var(--surface-2); padding: 2px 8px;
  border-radius: 4px; border: 1px solid var(--border);
}}
.tooltip .next-action {{
  margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border);
  color: var(--text-dim); font-size: 11px; line-height: 1.4;
}}
.tooltip .next-action strong {{ color: var(--primary); font-weight: 500; }}

/* ─── EDGE LABEL ─── */
.edge-label {{
  position: fixed; background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 4px;
  padding: 3px 8px; font-size: 9px; font-family: 'Khand', monospace;
  font-weight: 500; color: var(--text-muted); pointer-events: none;
  opacity: 0; transition: opacity 0.1s; z-index: 150;
}}
.edge-label.visible {{ opacity: 1; }}

/* ─── DETAILS PANEL ─── */
.details {{
  position: fixed; top: 52px; right: 0; bottom: 0; width: 360px;
  background: var(--surface); border-left: 1px solid var(--border);
  z-index: 90; overflow-y: auto;
  transform: translateX(100%); transition: transform 0.25s ease, background 0.3s;
  padding: 28px 24px;
}}
.details.open {{ transform: translateX(0); }}
.details-close {{
  position: absolute; top: 16px; right: 16px;
  background: var(--surface-2); border: 1px solid var(--border);
  color: var(--text-muted); cursor: pointer; font-size: 14px;
  width: 28px; height: 28px; border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  transition: all 0.2s;
}}
.details-close:hover {{ border-color: var(--primary); color: var(--primary); }}
.details h2 {{
  font-family: 'Khand', sans-serif; font-weight: 600;
  font-size: 20px; margin-bottom: 4px; padding-right: 40px;
  color: var(--text);
}}
.details .badges {{ margin-bottom: 14px; }}
.details .goal-text {{
  color: var(--text-dim); font-size: 13px; line-height: 1.5; margin-bottom: 18px;
}}
.details section {{ margin-bottom: 22px; }}
.details section h4 {{
  font-family: 'Khand', sans-serif; font-weight: 600;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em;
  color: var(--text-muted); margin-bottom: 8px;
}}
.details .next-box {{
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 14px;
  font-size: 12px; color: var(--text-dim); line-height: 1.5;
}}
.details .next-box strong {{ color: var(--primary); }}
.capsule-list {{ list-style: none; }}
.capsule-list li {{
  display: flex; justify-content: space-between; align-items: center;
  padding: 7px 0; border-bottom: 1px solid var(--border); font-size: 12px;
}}
.capsule-list li:last-child {{ border-bottom: none; }}
.capsule-list .cap-name {{ color: var(--text); }}
.capsule-list .cap-name.active {{ color: var(--primary); font-weight: 600; }}
.cap-status {{
  font-family: 'Khand', monospace; font-size: 10px; font-weight: 500;
  padding: 2px 8px; border-radius: 4px; text-transform: uppercase;
  letter-spacing: 0.05em;
}}
.cap-status.draft {{ background: var(--surface-2); color: var(--cap-draft); }}
.cap-status.prototype {{ background: #E0902018; color: var(--cap-prototype); }}
.cap-status.published {{ background: #3B7DD818; color: var(--cap-published); }}
.cap-status.done {{ background: #16A34A18; color: var(--cap-done); }}
.people-list {{ list-style: none; }}
.people-list li {{ font-size: 12px; color: var(--text-dim); padding: 3px 0; }}
.tags-list {{ display: flex; flex-wrap: wrap; gap: 4px; }}
.tags-list span {{
  font-family: 'Khand', monospace; font-size: 11px; font-weight: 500;
  color: var(--text-muted); background: var(--surface-2);
  border: 1px solid var(--border); padding: 2px 8px; border-radius: 4px;
}}
.meta-row {{
  display: flex; justify-content: space-between; font-size: 11px;
  color: var(--text-muted); padding: 3px 0;
}}
.meta-row .value {{ color: var(--text-dim); font-family: 'Khand', monospace; font-weight: 500; }}

/* ─── CONTROLS ─── */
.controls {{
  position: fixed; top: 64px; right: 24px;
  display: flex; flex-direction: column; gap: 6px; z-index: 100;
  transition: right 0.25s;
}}
.controls.shifted {{ right: 384px; }}
.controls button {{
  background: var(--surface); border: 1px solid var(--border);
  color: var(--text-dim); font-family: 'Khand', monospace;
  font-size: 12px; font-weight: 500; padding: 6px 14px;
  border-radius: 8px; cursor: pointer; transition: all 0.2s;
  text-align: left; white-space: nowrap;
  box-shadow: 0 2px 6px var(--shadow);
}}
.controls button:hover {{ border-color: var(--primary); color: var(--primary); }}
.controls button.active {{
  background: var(--primary-dim); border-color: var(--primary); color: var(--primary);
}}

/* Domain labels */
.domain-label {{
  font-family: 'Array', 'Inter', sans-serif;
  font-size: 52px; font-weight: 700;
  pointer-events: none; text-anchor: middle;
  dominant-baseline: central; text-transform: uppercase;
  letter-spacing: 0.12em;
}}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <span class="header-title">ALIVE Context Graph</span>
    <div class="header-stats">
      <span><span class="stat-hl">{stats['walnuts']}</span> walnuts</span>
      <span><span class="stat-hl">{stats['people']}</span> people</span>
      <span><span class="stat-hl">{stats['capsules']}</span> capsules</span>
      <span><span class="stat-hl">{stats['inputs']}</span> inputs</span>
      <span><span class="stat-hl">{stats['sessions']}</span> sessions</span>
    </div>
  </div>
  <div class="header-right">
    <button class="theme-toggle" id="themeToggle" onclick="toggleTheme()" title="Toggle theme">
      <span id="themeIcon">\u263C</span>
    </button>
  </div>
</div>

<div class="search-box">
  <input type="text" id="searchInput" placeholder="search walnuts... ( / )" />
</div>
<div class="legend" id="legend"></div>
<div class="tooltip" id="tooltip">
  <h3 id="tipName"></h3>
  <div id="tipBadges"></div>
  <div class="goal" id="tipGoal"></div>
  <div class="meta" id="tipMeta"></div>
  <div class="next-action" id="tipNext"></div>
</div>
<div class="edge-label" id="edgeLabel"></div>

<div class="details" id="details">
  <button class="details-close" onclick="closeDetails()">&times;</button>
  <div id="detailsContent"></div>
</div>

<div class="controls" id="controls">
  <button onclick="resetView()">reset view</button>
  <button onclick="togglePeople()" id="btnPeople">show people</button>
  <button onclick="toggleLabels()" id="btnLabels" class="active">labels on</button>
  <button onclick="toggleArchive()" id="btnArchive">show archive</button>
  <button onclick="clusterByDomain()" id="btnCluster">cluster</button>
</div>

<svg id="graph"></svg>

<script>
// ─── DATA ───
const walnutNodes = {nj};
const walnutLinks = {lj};
const peopleNodesData = {pnj};
const peopleLinksData = {plj};

// ─── THEME ───
let isDark = false;

// Domain colors per theme
const themes = {{
  light: {{
    life: "#3B7DD8", ventures: "#E86A10", experiments: "#16A34A",
    archive: "#8A8078", people: "#8B45A6", inputs: "#C44E3F",
    capDraft: "#8A8078", capPrototype: "#E09020", capPublished: "#3B7DD8", capDone: "#16A34A",
    linkColor: "#1A171510", linkParent: "#B8733330", linkPerson: "#8B45A618", linkCapsule: "#8A807825",
    nodeLabel: "#1A171585", nodeLabelDim: "#1A171535", nodeLabelPerson: "#8B45A650",
    nodeLabelSpecial: "#E86A10", nodeLabelCapsule: "#8A807880",
    domainLabelFill: "#1A171506",
    benFill: "#F97316", benStroke: "#E86A10",
  }},
  dark: {{
    life: "#5B9DE9", ventures: "#F97316", experiments: "#4ADE80",
    archive: "#8A8078", people: "#B67FD0", inputs: "#E74C3C",
    capDraft: "#8A8078", capPrototype: "#F0AD4E", capPublished: "#5B9DE9", capDone: "#4ADE80",
    linkColor: "#FAF8F50C", linkParent: "#D4A57430", linkPerson: "#B67FD018", linkCapsule: "#8A807825",
    nodeLabel: "#FAF8F580", nodeLabelDim: "#FAF8F530", nodeLabelPerson: "#B67FD060",
    nodeLabelSpecial: "#D4A574", nodeLabelCapsule: "#8A807890",
    domainLabelFill: "#FAF8F506",
    benFill: "#F97316", benStroke: "#D4A574",
  }},
}};

function T() {{ return themes[isDark ? 'dark' : 'light']; }}

function toggleTheme() {{
  isDark = !isDark;
  document.documentElement.classList.toggle('dark', isDark);
  document.getElementById('themeIcon').textContent = isDark ? '\u263E' : '\u263C';
  localStorage.setItem('walnut-graph-theme', isDark ? 'dark' : 'light');
  render();
}}

// Restore saved theme
if (localStorage.getItem('walnut-graph-theme') === 'dark') {{
  isDark = true;
  document.documentElement.classList.add('dark');
  document.getElementById('themeIcon').textContent = '\u263E';
}}

// ─── STATE ───
const W = window.innerWidth, H = window.innerHeight - 52;
let showPeople = false, showLabels = true, showArchive = false, clustered = false;
let expandedWalnuts = new Set();
let detailsOpen = false;
let simulation;

// ─── BUILD GRAPH ───
function buildGraph() {{
  let nodes = walnutNodes.filter(n => showArchive || !n.archived).map(n => ({{...n}}));
  let links = walnutLinks.map(l => ({{source:l.source, target:l.target, type:l.type, label:l.label||l.type}}));
  const nodeIds = new Set(nodes.map(n => n.id));
  links = links.filter(l => nodeIds.has(l.source) && nodeIds.has(l.target));

  // Capsule expansion
  expandedWalnuts.forEach(wid => {{
    const parent = nodes.find(n => n.id === wid);
    if (!parent || !parent.capsuleDetails) return;
    parent.capsuleDetails.forEach(cap => {{
      const cid = `cap:${{wid}}:${{cap.name}}`;
      const st = cap.status || 'draft';
      nodes.push({{
        id: cid, label: cap.name.replace(/-/g, ' '),
        domain: parent.domain, type: 'capsule', goal: cap.goal || '',
        phase: st, size: parent.activeCapsule === cap.name ? 7 : 5,
        isCapsule: true, capsuleStatus: st,
        isActiveCapsule: parent.activeCapsule === cap.name,
        parentWalnut: wid, isPerson: false,
      }});
      links.push({{ source: wid, target: cid, type: 'capsule' }});
    }});
  }});

  if (showPeople) {{
    nodes = nodes.concat(peopleNodesData.map(n => ({{...n}})));
    const ids = new Set(nodes.map(n => n.id));
    links = links.concat(peopleLinksData.filter(l => ids.has(l.target)).map(l => ({{...l}})));
  }}
  return {{ nodes, links }};
}}

// ─── SVG ───
const svg = d3.select("#graph").attr("width", W).attr("height", H).style("margin-top", "52px");
const g = svg.append("g");
const zoom = d3.zoom().scaleExtent([0.1, 6]).on("zoom", e => g.attr("transform", e.transform));
svg.call(zoom);

// ─── RENDER ───
function render() {{
  g.selectAll("*").remove();
  svg.selectAll("defs").remove();
  const t = T();
  const {{ nodes, links }} = buildGraph();

  simulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id(d=>d.id)
      .distance(d => d.type==="capsule"?28 : d.type==="person"?50 : d.type==="parent"?40 : 90)
      .strength(d => d.type==="capsule"?0.9 : d.type==="person"?0.2 : d.type==="parent"?0.7 : 0.4))
    .force("charge", d3.forceManyBody()
      .strength(d => d.isCapsule?-25 : d.special?-500 : d.isPerson?-60 : d.capsuleCount>10?-350 : -180))
    .force("center", d3.forceCenter(W/2, H/2))
    .force("collision", d3.forceCollide().radius(d => d.size + (d.isCapsule?3:10)))
    .force("x", d3.forceX(W/2).strength(0.02))
    .force("y", d3.forceY(H/2).strength(0.02));

  if (clustered) {{
    const dx = {{life:W*0.15, ventures:W*0.5, experiments:W*0.85, inputs:W*0.5, archive:W*0.5}};
    const dy = {{life:H*0.3, ventures:H*0.5, experiments:H*0.3, inputs:H*0.85, archive:H*0.85}};
    simulation.force("x", d3.forceX(d => dx[d.domain]||W/2).strength(0.12));
    simulation.force("y", d3.forceY(d => dy[d.domain]||H/2).strength(0.12));
    [["LIFE","life",W*0.15,H*0.3],["VENTURES","ventures",W*0.5,H*0.5],["EXPERIMENTS","experiments",W*0.85,H*0.3]].forEach(([label,key,x,y]) => {{
      g.append("text").attr("class","domain-label")
        .attr("x",x).attr("y",y).text(label)
        .attr("fill", t.domainLabelFill);
    }});
  }}

  // Glow filter
  const defs = svg.append("defs");
  const gf = defs.append("filter").attr("id","glow").attr("x","-50%").attr("y","-50%").attr("width","200%").attr("height","200%");
  gf.append("feGaussianBlur").attr("stdDeviation","4").attr("result","b");
  gf.append("feMerge").selectAll("feMergeNode").data(["b","SourceGraphic"]).enter().append("feMergeNode").attr("in",d=>d);

  // Links
  const linkEl = g.append("g").selectAll("line").data(links).enter().append("line")
    .attr("stroke", d => {{
      if (d.type==="capsule") return t.linkCapsule;
      if (d.type==="person") return t.linkPerson;
      if (d.type==="parent") return t.linkParent;
      return t.linkColor;
    }})
    .attr("stroke-width", d => d.type==="parent"?1.5 : d.type==="person"?0.5 : 0.8)
    .attr("stroke-dasharray", d => d.type==="person"?"2,4" : d.type==="parent"?"4,3" : "none")
    .style("cursor", "pointer")
    .on("mouseover", showEdgeLabel).on("mouseout", hideEdgeLabel);

  // Capsule glow rings
  g.append("g").selectAll("circle").data(nodes.filter(n=>n.capsuleCount>5&&!n.isCapsule))
    .enter().append("circle").attr("class","glow-ring")
    .attr("r", d=>d.size+6).attr("fill","none")
    .attr("stroke", d=>t[d.domain]||t.archive)
    .attr("stroke-width",1).attr("stroke-opacity",0.2);

  // Nodes
  const nodeEl = g.append("g").selectAll("circle").data(nodes).enter().append("circle")
    .attr("r", d=>d.size)
    .attr("fill", d => {{
      if (d.isCapsule) {{ const cs = {{draft:t.capDraft, prototype:t.capPrototype, published:t.capPublished, done:t.capDone}}; return cs[d.capsuleStatus]||t.capDraft; }}
      if (d.special && d.special) return t.benFill;
      if (d.isPerson) return t.people;
      return t[d.domain] || t.archive;
    }})
    .attr("fill-opacity", d => {{
      if (d.isCapsule) return d.isActiveCapsule ? 0.9 : 0.55;
      if (d.special) return 1;
      if (d.archived||d.phase==="dead") return 0.15;
      if (d.daysSince>21) return 0.35;
      if (d.daysSince>14) return 0.5;
      if (d.daysSince>7) return 0.65;
      return 0.9;
    }})
    .attr("stroke", d => {{
      if (d.isCapsule && d.isActiveCapsule) return t[`cap${{d.capsuleStatus?.charAt(0).toUpperCase()}}${{d.capsuleStatus?.slice(1)}}`] || "#fff";
      if (d.special && d.special) return t.benStroke;
      if (!d.isCapsule && d.daysSince<=1) return t.benFill;
      if (!d.isCapsule && d.capsuleCount>10) return t[d.domain]||t.archive;
      return "transparent";
    }})
    .attr("stroke-width", d => {{
      if (d.isCapsule&&d.isActiveCapsule) return 1.5;
      if (d.special) return 2.5;
      if (d.daysSince<=1) return 2;
      if (d.capsuleCount>10) return 1.5;
      return 0;
    }})
    .attr("filter", d => (!d.isCapsule&&d.daysSince<=1) ? "url(#glow)" : "none")
    .style("cursor","pointer")
    .call(d3.drag().on("start",ds).on("drag",dd).on("end",de))
    .on("mouseover", showTooltip).on("mouseout", hideTooltip)
    .on("click", handleClick);

  // Labels
  const labelEl = g.append("g").selectAll("text").data(nodes).enter().append("text")
    .text(d => d.label)
    .attr("font-size", d => d.isCapsule?8 : d.special?14 : d.isPerson?9 : d.capsuleCount>10?11 : d.size>=12?10 : 9)
    .attr("fill", d => {{
      if (d.isCapsule) return t.nodeLabelCapsule;
      if (d.special) return t.nodeLabelSpecial;
      if (d.isPerson) return t.nodeLabelPerson;
      if (d.archived) return t.nodeLabelDim;
      return t.nodeLabel;
    }})
    .attr("font-family", d => d.special ? "'Array', 'Inter', sans-serif" : "'Inter', sans-serif")
    .attr("font-weight", d => (d.special||d.capsuleCount>10) ? 600 : 400)
    .attr("text-anchor","middle")
    .attr("dy", d => d.size + (d.isCapsule?10:14))
    .style("pointer-events","none")
    .style("display", showLabels ? "block" : "none");

  simulation.on("tick", () => {{
    linkEl.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y).attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
    nodeEl.attr("cx",d=>d.x).attr("cy",d=>d.y);
    labelEl.attr("x",d=>d.x).attr("y",d=>d.y);
    g.selectAll(".glow-ring").attr("cx",d=>d.x).attr("cy",d=>d.y);
  }});

  window._nodeEl=nodeEl; window._linkEl=linkEl; window._labelEl=labelEl;
  window._nodes=nodes; window._links=links;
}}

// ─── DRAG ───
function ds(e,d) {{ if(!e.active) simulation.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; }}
function dd(e,d) {{ d.fx=e.x; d.fy=e.y; }}
function de(e,d) {{ if(!e.active) simulation.alphaTarget(0); d.fx=null; d.fy=null; }}

// ─── TOOLTIP ───
const tip = document.getElementById("tooltip");
function showTooltip(event, d) {{
  const t = T();
  document.getElementById("tipName").textContent = d.label;
  const badges = document.getElementById("tipBadges");
  badges.innerHTML = "";
  if (d.domain && !d.isCapsule) {{
    const c = t[d.domain]||t.archive;
    badges.innerHTML += `<span class="badge" style="background:${{c}}18;color:${{c}}">${{d.domain}}</span>`;
  }}
  if (d.phase) {{
    const phaseColors = {{building:"#16A34A",active:"#3B7DD8",launching:"#F97316",legacy:"#8A8078","pre-launch":"#E09020",onboarding:"#5BC0DE",starting:"#8A8078",dead:"#8A8078",marinating:"#8B45A6",planning:"#5BC0DE",exploring:"#5BC0DE",placeholder:"#8A8078",ready:"#16A34A",review:"#E09020","retainer-pending":"#E09020",buffer:"#C44E3F",world:"#F97316",research:"#5BC0DE",complete:"#16A34A",waiting:"#E09020",unknown:"#8A8078"}};
    let pc = phaseColors[d.phase]||"#8A8078";
    if (d.isCapsule) pc = {{draft:t.capDraft,prototype:t.capPrototype,published:t.capPublished,done:t.capDone}}[d.phase]||pc;
    badges.innerHTML += `<span class="badge" style="background:${{pc}}18;color:${{pc}}">${{d.phase}}</span>`;
  }}
  document.getElementById("tipGoal").textContent = d.goal || "";
  const meta = document.getElementById("tipMeta");
  meta.innerHTML = "";
  if (d.rhythm) meta.innerHTML += `<span>${{d.rhythm}}</span>`;
  if (d.updated) meta.innerHTML += `<span>${{d.updated}}</span>`;
  if (d.capsuleCount) meta.innerHTML += `<span>${{d.capsuleCount}} capsules</span>`;
  if (d.sessions) meta.innerHTML += `<span>${{d.sessions}} sessions</span>`;
  if (d.connects) meta.innerHTML += `<span>connects ${{d.connects.length}}</span>`;
  const nx = document.getElementById("tipNext");
  nx.innerHTML = d.next ? `<strong>next:</strong> ${{d.next}}` : "";
  nx.style.display = d.next ? "block" : "none";
  tip.classList.add("visible");
  tip.style.left = Math.min(event.pageX+16, W-360) + "px";
  tip.style.top = Math.min(event.pageY-10, H-200) + "px";
  highlightConnected(d);
}}
function hideTooltip() {{ tip.classList.remove("visible"); resetHighlight(); }}

function highlightConnected(d) {{
  const c = new Set([d.id]);
  window._links.forEach(l => {{
    const s=l.source.id||l.source, t=l.target.id||l.target;
    if(s===d.id) c.add(t); if(t===d.id) c.add(s);
  }});
  window._nodeEl.attr("fill-opacity", n => c.has(n.id) ? (n.special?1:0.95) : 0.06);
  window._labelEl.attr("fill-opacity", n => c.has(n.id) ? 1 : 0.04);
  window._linkEl.attr("stroke-opacity", l => {{
    const s=l.source.id||l.source, t=l.target.id||l.target;
    return (c.has(s)&&c.has(t)) ? 0.7 : 0.02;
  }});
}}
function resetHighlight() {{
  window._nodeEl.attr("fill-opacity", d => {{
    if(d.isCapsule) return d.isActiveCapsule?0.9:0.55;
    if(d.special) return 1;
    if(d.archived||d.phase==="dead") return 0.15;
    if(d.daysSince>21) return 0.35; if(d.daysSince>14) return 0.5;
    if(d.daysSince>7) return 0.65; return 0.9;
  }});
  window._labelEl.attr("fill-opacity",1);
  window._linkEl.attr("stroke-opacity",1);
}}

// ─── EDGE LABEL ───
const edgeLbl = document.getElementById("edgeLabel");
function showEdgeLabel(event, d) {{
  const labels = {{link:"linked", parent:"parent \u2192 child", person:d.label||"person", capsule:"capsule"}};
  edgeLbl.textContent = labels[d.type]||d.type;
  edgeLbl.classList.add("visible");
  edgeLbl.style.left = event.pageX+"px";
  edgeLbl.style.top = (event.pageY-24)+"px";
}}
function hideEdgeLabel() {{ edgeLbl.classList.remove("visible"); }}

// ─── CLICK ───
function handleClick(event, d) {{
  event.stopPropagation();
  if (!d.isCapsule && !d.isPerson && !d.special && d.capsuleCount > 0) {{
    if (expandedWalnuts.has(d.id)) expandedWalnuts.delete(d.id);
    else expandedWalnuts.add(d.id);
    render();
  }}
  showDetails(d);
}}

function showDetails(d) {{
  const t = T();
  const panel = document.getElementById("details");
  let html = `<h2>${{d.label}}</h2><div class="badges">`;
  if (d.domain && !d.isCapsule) {{
    const c=t[d.domain]||t.archive;
    html+=`<span class="badge" style="background:${{c}}18;color:${{c}}">${{d.domain}}</span>`;
  }}
  if (d.phase) {{
    const pc = d.isCapsule ? ({{draft:t.capDraft,prototype:t.capPrototype,published:t.capPublished,done:t.capDone}}[d.phase]||"#8A8078") : "#8A8078";
    html+=`<span class="badge" style="background:${{pc}}18;color:${{pc}}">${{d.phase}}</span>`;
  }}
  html += `</div>`;
  if (d.goal) html+=`<div class="goal-text">${{d.goal}}</div>`;
  html+=`<section>`;
  if (d.rhythm) html+=`<div class="meta-row"><span>rhythm</span><span class="value">${{d.rhythm}}</span></div>`;
  if (d.updated) html+=`<div class="meta-row"><span>updated</span><span class="value">${{d.updated}}</span></div>`;
  if (d.daysSince!==undefined&&!d.isCapsule) html+=`<div class="meta-row"><span>days ago</span><span class="value">${{d.daysSince}}d</span></div>`;
  if (d.sessions) html+=`<div class="meta-row"><span>sessions</span><span class="value">${{d.sessions}}</span></div>`;
  html+=`</section>`;
  if (d.next) html+=`<section><h4>Next Action</h4><div class="next-box"><strong>next:</strong> ${{d.next}}</div></section>`;
  if (d.capsuleDetails && d.capsuleDetails.length) {{
    const expanded = expandedWalnuts.has(d.id);
    html+=`<section><h4>Capsules (${{d.capsuleDetails.length}})${{expanded?' \u2014 expanded':' \u2014 click to expand'}}</h4><ul class="capsule-list">`;
    d.capsuleDetails.forEach(c => {{
      const active = d.activeCapsule===c.name;
      html+=`<li><span class="cap-name${{active?' active':''}}">${{c.name.replace(/-/g,' ')}}</span><span class="cap-status ${{c.status||'draft'}}">${{c.status||'draft'}}</span></li>`;
    }});
    html+=`</ul></section>`;
  }}
  if (d.people && d.people.length) {{
    html+=`<section><h4>People</h4><ul class="people-list">`;
    d.people.forEach(p => html+=`<li>${{p}}</li>`);
    html+=`</ul></section>`;
  }}
  if (d.connects) {{
    html+=`<section><h4>Connects</h4><ul class="people-list">`;
    d.connects.forEach(w => html+=`<li>${{w.replace(/-/g,' ')}}</li>`);
    html+=`</ul></section>`;
  }}
  if (d.tags && d.tags.length) {{
    html+=`<section><h4>Tags</h4><div class="tags-list">`;
    d.tags.forEach(t => html+=`<span>${{t}}</span>`);
    html+=`</div></section>`;
  }}
  document.getElementById("detailsContent").innerHTML = html;
  panel.classList.add("open");
  document.getElementById("controls").classList.add("shifted");
  detailsOpen = true;
}}

function closeDetails() {{
  document.getElementById("details").classList.remove("open");
  document.getElementById("controls").classList.remove("shifted");
  detailsOpen = false;
}}
svg.on("click", () => {{ if(detailsOpen) closeDetails(); }});

// ─── CONTROLS ───
function resetView() {{ svg.transition().duration(500).call(zoom.transform, d3.zoomIdentity); }}
function togglePeople() {{
  showPeople=!showPeople;
  const b=document.getElementById("btnPeople");
  b.classList.toggle("active",showPeople); b.textContent=showPeople?"hide people":"show people";
  render();
}}
function toggleLabels() {{
  showLabels=!showLabels;
  const b=document.getElementById("btnLabels");
  b.classList.toggle("active",showLabels); b.textContent=showLabels?"labels on":"labels off";
  if(window._labelEl) window._labelEl.style("display",showLabels?"block":"none");
}}
function toggleArchive() {{
  showArchive=!showArchive;
  const b=document.getElementById("btnArchive");
  b.classList.toggle("active",showArchive); b.textContent=showArchive?"hide archive":"show archive";
  render();
}}
function clusterByDomain() {{
  clustered=!clustered;
  const b=document.getElementById("btnCluster");
  b.classList.toggle("active",clustered); b.textContent=clustered?"organic":"cluster";
  render();
}}

// ─── SEARCH ───
const searchInput = document.getElementById("searchInput");
searchInput.addEventListener("input", e => {{
  const q=e.target.value.toLowerCase().trim();
  if(!q) {{ resetHighlight(); return; }}
  const hits=new Set();
  walnutNodes.forEach(n => {{
    if(n.id.includes(q)||n.label.includes(q)||(n.goal&&n.goal.toLowerCase().includes(q))
      ||(n.tags&&n.tags.some(t=>t.includes(q)))||(n.people&&n.people.some(p=>p.toLowerCase().includes(q)))) hits.add(n.id);
  }});
  window._nodeEl.attr("fill-opacity", n => hits.has(n.id)?0.95:0.04);
  window._labelEl.attr("fill-opacity", n => hits.has(n.id)?1:0.03);
  window._linkEl.attr("stroke-opacity", 0.02);
}});
searchInput.addEventListener("keydown", e => {{ if(e.key==="Escape") {{ searchInput.value=""; resetHighlight(); searchInput.blur(); }} }});
document.addEventListener("keydown", e => {{
  if(e.key==="/"&&document.activeElement!==searchInput) {{ e.preventDefault(); searchInput.focus(); }}
  if(e.key==="Escape"&&detailsOpen) closeDetails();
}});

// ─── LEGEND ───
function buildLegend() {{
  const t = T();
  const counts = {{}};
  walnutNodes.forEach(n => {{ if(!n.archived) counts[n.domain]=(counts[n.domain]||0)+1; }});
  let html = '<div class="legend-title">domains</div>';
  [["life","Life"],["ventures","Ventures"],["experiments","Experiments"],["inputs","Inputs"],["people","People"]].forEach(([k,l]) => {{
    html+=`<div class="legend-item"><div class="legend-dot" style="background:${{t[k]||t.archive}}"></div>${{l}}<span class="legend-count">${{counts[k]||0}}</span></div>`;
  }});
  html+='<div style="margin:10px 0;border-top:1px solid var(--border)"></div><div class="legend-title">capsules</div>';
  [["draft","Draft",t.capDraft],["prototype","Prototype",t.capPrototype],["done","Done",t.capDone]].forEach(([k,l,c]) => {{
    html+=`<div class="legend-item"><div class="legend-dot" style="background:${{c}}"></div>${{l}}</div>`;
  }});
  html+='<div style="margin:10px 0;border-top:1px solid var(--border)"></div>';
  html+='<div style="font-size:9px;color:var(--text-muted)">/ search \u00b7 click expand \u00b7 esc close</div>';
  document.getElementById("legend").innerHTML = html;
}}

// ─── INIT ───
buildLegend();
render();

window.addEventListener("resize", () => {{
  const w=window.innerWidth, h=window.innerHeight-52;
  svg.attr("width",w).attr("height",h);
  if(simulation) {{ simulation.force("center",d3.forceCenter(w/2,h/2)); simulation.alpha(0.3).restart(); }}
}});
</script>
</body>
</html>'''


if __name__ == '__main__':
    main()
