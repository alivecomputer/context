#!/usr/bin/env python3
"""ALIVE Context Graph Generator v4 — Force-Directed with Domain Clustering

Reads _index.json and generates an interactive D3.js force-directed graph
with domain gravity wells, neighborhood highlighting, and semantic zoom.

Design informed by Obsidian graph view research, Cambridge Intelligence
layout patterns, and D3 force best practices.

Usage: python3 .alive/scripts/generate-graph.py [world-root]
"""

import json
import os
import sys
from datetime import datetime, timezone
from collections import defaultdict
import math


def main():
    world_root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    world_root = os.path.abspath(world_root)
    json_file = os.path.join(world_root, '.alive', '_index.json')
    html_file = os.path.join(world_root, '.alive', 'context-graph.html')

    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    stats = data['stats']
    # Exclude archived walnuts from the graph — they shouldn't render as active nodes
    walnuts = [w for w in data['walnuts'] if not w.get('archived')]
    people = data.get('people', [])
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    nodes, links, people_nodes, people_links = build_graph_data(
        walnuts, people, today
    )

    nj = json.dumps(nodes, ensure_ascii=False)
    lj = json.dumps(links, ensure_ascii=False)
    pnj = json.dumps(people_nodes, ensure_ascii=False)
    plj = json.dumps(people_links, ensure_ascii=False)

    html = build_html(stats, nj, lj, pnj, plj)
    with open(html_file, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"Graph: {html_file}")
    print(f"Nodes: {len(nodes)} + {len(people_nodes)} people | "
          f"Edges: {len(links)} + {len(people_links)} people")


def build_graph_data(walnuts, people, today):
    """Build nodes and links for force-directed graph."""
    nodes = []
    node_ids = set()
    link_set = set()
    links = []

    # Compute degree for each walnut (for sizing)
    degree = defaultdict(int)
    for w in walnuts:
        for target in w.get('links', []):
            degree[w['name']] += 1
            degree[target] += 1
        if w.get('parent'):
            degree[w['name']] += 1
            degree[w['parent']] += 1

    for w in walnuts:
        wid = w['name']
        domain = w.get('domain', 'unknown')
        updated = w.get('updated', '')[:10]
        bundles = w.get('capsule_count', 0)
        sessions = w.get('squirrel_sessions', 0)
        archived = w.get('archived', False)

        try:
            days = (datetime.strptime(today, '%Y-%m-%d') -
                    datetime.strptime(updated, '%Y-%m-%d')).days
        except (ValueError, TypeError):
            days = 60

        # Size by degree + activity (wider range for visual hierarchy)
        deg = degree.get(wid, 0)
        size = 4 + math.sqrt(deg) * 4 + min(bundles, 15) * 1.2
        if days <= 3:
            size += 5
        elif days <= 7:
            size += 3
        elif days <= 30:
            size += 1
        size += min(sessions, 5) * 0.5
        size = max(3, min(32, size))

        # Health signal
        rhythm_days = {'daily': 1, 'weekly': 7, 'biweekly': 14, 'monthly': 30}
        r = rhythm_days.get(w.get('rhythm', ''), 14)
        if days <= r:
            health = 'active'
        elif days <= r * 2:
            health = 'quiet'
        else:
            health = 'waiting'

        nodes.append({
            'id': wid,
            'label': wid.replace('-', ' '),
            'domain': domain,
            'type': w.get('type', 'unknown'),
            'goal': w.get('goal', ''),
            'phase': w.get('phase', ''),
            'rhythm': w.get('rhythm', ''),
            'updated': updated,
            'size': round(size, 1),
            'daysSince': days,
            'bundleCount': bundles,
            'sessions': sessions,
            'archived': archived,
            'health': health,
            'tags': w.get('tags', []),
            'peopleList': w.get('people', []),
            'linksList': w.get('links', []),
            'parent': w.get('parent', ''),
            'capsules': w.get('capsules', []),
            # Enriched
            'taskCounts': w.get('task_counts', {}),
            'bundleSummary': w.get('bundle_summary', {}),
            'blockers': w.get('blockers', []),
            'sessionCount': w.get('session_count', 0),
            'lastSession': w.get('last_session', ''),
        })
        node_ids.add(wid)

    # Build links from wikilinks
    for w in walnuts:
        wid = w['name']
        if wid not in node_ids:
            continue
        for target in w.get('links', []):
            if target in node_ids and target != wid:
                key = tuple(sorted([wid, target]))
                if key not in link_set:
                    links.append({
                        'source': wid, 'target': target, 'type': 'link'
                    })
                    link_set.add(key)
        parent = w.get('parent', '')
        if parent and parent in node_ids and parent != wid:
            key = tuple(sorted([wid, parent]))
            if key not in link_set:
                links.append({
                    'source': parent, 'target': wid, 'type': 'parent'
                })
                link_set.add(key)

    # People as bridge nodes (only those connecting 2+ walnuts)
    person_to_walnuts = defaultdict(list)
    for w in walnuts:
        if w.get('archived'):
            continue
        for p in w.get('people', []):
            person_to_walnuts[p].append(w['name'])

    people_nodes = []
    people_links = []
    for person, connected in person_to_walnuts.items():
        if len(connected) >= 2:
            pid = 'p-' + person.lower().replace(' ', '-').replace("'", '')
            people_nodes.append({
                'id': pid,
                'label': person,
                'domain': 'people',
                'type': 'person',
                'size': 4 + min(len(connected), 6),
                'connects': connected,
            })
            for wid in connected:
                if wid in node_ids:
                    people_links.append({
                        'source': pid, 'target': wid,
                        'type': 'person', 'label': person,
                    })

    return nodes, links, people_nodes, people_links


def build_html(stats, nj, lj, pnj, plj):
    # NOTE: the f-string uses {{ }} for literal JS braces
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ALIVE Context Graph</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}

:root {{
  --bg: #FAF8F5;
  --surface: #FFFFFF;
  --text: #1A1715;
  --text-dim: #6B6560;
  --text-muted: #9A9490;
  --border: #E8E2DB;
  --life: #3B82F6;
  --ventures: #F97316;
  --experiments: #10B981;
  --archive: #9CA3AF;
  --people: #8B5CF6;
}}

[data-theme="dark"] {{
  --bg: #0F1117;
  --surface: #1A1D27;
  --text: #E8E2DB;
  --text-dim: #8A8078;
  --text-muted: #5A5550;
  --border: #2A2D37;
  --life: #60A5FA;
  --ventures: #FB923C;
  --experiments: #34D399;
  --archive: #6B7280;
  --people: #A78BFA;
}}

body {{
  font-family: 'Inter', -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text);
  overflow: hidden;
  height: 100vh;
}}

#header {{
  position: fixed; top:0; left:0; right:0; z-index:100;
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 20px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  gap: 12px;
}}

#header h1 {{ font-size: 13px; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; white-space: nowrap; }}
#header .stats {{ font-size: 11px; color: var(--text-dim); white-space: nowrap; }}

#controls {{
  display: flex; gap: 6px; align-items: center; flex-wrap: wrap; justify-content: flex-end;
}}

#controls button {{
  background: var(--surface); border: 1px solid var(--border); color: var(--text-dim);
  padding: 3px 9px; border-radius: 4px; font-size: 11px; font-family: inherit;
  cursor: pointer; transition: all 0.15s; white-space: nowrap;
}}
#controls button:hover {{ color: var(--text); border-color: var(--text-muted); }}
#controls button.active {{ background: var(--text); color: var(--bg); border-color: var(--text); }}

#search-box {{
  position: fixed; top: 46px; right: 20px; z-index: 100; display: none;
}}
#search-box input {{
  background: var(--surface); border: 1px solid var(--border); color: var(--text);
  padding: 5px 10px; border-radius: 4px; font-size: 12px; font-family: inherit;
  width: 220px; outline: none;
}}
#search-box input:focus {{ border-color: var(--ventures); }}

#tooltip {{
  position: fixed; z-index: 200;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 6px; padding: 10px 14px; font-size: 12px;
  max-width: 300px; pointer-events: none; opacity: 0;
  transition: opacity 0.12s; box-shadow: 0 4px 16px rgba(0,0,0,0.12);
}}
.tt-name {{ font-weight: 600; font-size: 13px; margin-bottom: 3px; }}
.tt-badge {{
  display: inline-block; font-size: 9px; text-transform: uppercase;
  letter-spacing: 0.5px; padding: 1px 5px; border-radius: 3px; margin-bottom: 5px;
}}
.tt-goal {{ color: var(--text-dim); margin-bottom: 3px; line-height: 1.4; font-size: 11px; }}
.tt-meta {{ color: var(--text-muted); font-size: 10px; }}

.link {{ fill: none; stroke-opacity: 0.25; }}
.link-parent {{ stroke-dasharray: 4 3; }}
.link-person {{ stroke-dasharray: 2 2; }}

.node-circle {{ cursor: pointer; transition: opacity 0.2s; }}
.node-label {{ fill: var(--text-dim); font-size: 10px; text-anchor: middle; }}

.dimmed {{ opacity: 0.06 !important; }}
.highlighted {{ opacity: 1 !important; }}

.health-active {{ stroke: #22C55E !important; }}
.health-quiet {{ stroke: #F59E0B !important; }}
.health-waiting {{ stroke: #EF4444 !important; }}

.glow {{
  filter: drop-shadow(0 0 4px var(--ventures));
}}

.domain-label {{
  fill: var(--text-muted);
  font-size: 13px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 1.5px;
  opacity: 0.35;
  pointer-events: none;
}}

#legend {{
  position: fixed; bottom: 16px; left: 16px; z-index: 100;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 6px; padding: 10px 14px; font-size: 11px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}}
#legend .item {{ display: flex; align-items: center; gap: 6px; margin: 3px 0; }}
#legend .dot {{ width: 10px; height: 10px; border-radius: 50%; }}

#detail-panel {{
  position: fixed; top: 44px; right: 0; bottom: 0; width: 320px;
  z-index: 150; background: var(--surface); border-left: 1px solid var(--border);
  overflow-y: auto; padding: 20px; font-size: 12px;
  transform: translateX(100%); transition: transform 0.25s ease;
  box-shadow: -4px 0 16px rgba(0,0,0,0.08);
}}
#detail-panel.open {{ transform: translateX(0); }}
#detail-panel .dp-close {{
  position: absolute; top: 12px; right: 12px; cursor: pointer;
  background: none; border: 1px solid var(--border); border-radius: 4px;
  color: var(--text-dim); font-size: 16px; width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  font-family: inherit;
}}
#detail-panel .dp-close:hover {{ color: var(--text); border-color: var(--text-muted); }}
#detail-panel .dp-name {{ font-size: 18px; font-weight: 700; margin-bottom: 4px; padding-right: 36px; }}
#detail-panel .dp-badges {{ display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }}
#detail-panel .dp-badge {{
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px;
  padding: 2px 8px; border-radius: 4px; font-weight: 500;
}}
#detail-panel .dp-section {{ margin-bottom: 14px; }}
#detail-panel .dp-section-title {{
  font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
  color: var(--text-muted); margin-bottom: 4px; font-weight: 600;
}}
#detail-panel .dp-goal {{ color: var(--text-dim); line-height: 1.5; margin-bottom: 12px; }}
#detail-panel .dp-item {{ padding: 3px 0; color: var(--text-dim); }}
#detail-panel .dp-item.clickable {{ cursor: pointer; color: var(--text); }}
#detail-panel .dp-item.clickable:hover {{ text-decoration: underline; }}
#detail-panel .dp-bundle {{
  display: flex; justify-content: space-between; align-items: center;
  padding: 4px 0; border-bottom: 1px solid var(--border);
}}
#detail-panel .dp-bundle-status {{
  font-size: 9px; text-transform: uppercase; padding: 1px 5px; border-radius: 3px;
}}
#detail-panel .dp-meta {{ color: var(--text-muted); font-size: 11px; margin-top: 12px; }}
</style>
</head>
<body>

<div id="header">
  <h1>ALIVE</h1>
  <div class="stats">{stats['walnuts']} walnuts &middot; {stats.get('capsules',0)} bundles &middot; {stats['people']} people</div>
  <div id="controls">
    <button id="btn-people">People</button>
    <button id="btn-labels">Labels</button>
    <button id="btn-archive">Archive</button>
    <button id="btn-search">/ Search</button>
    <button id="btn-theme">Dark</button>
  </div>
</div>

<div id="search-box"><input type="text" placeholder="Search..." id="search-input"></div>
<div id="tooltip"></div>

<div id="legend">
  <div class="item"><div class="dot" style="background:var(--life)"></div> Life</div>
  <div class="item"><div class="dot" style="background:var(--ventures)"></div> Ventures</div>
  <div class="item"><div class="dot" style="background:var(--experiments)"></div> Experiments</div>
  <div class="item"><div class="dot" style="background:var(--people)"></div> People</div>
  <div class="item"><div class="dot" style="background:var(--archive)"></div> Archive</div>
</div>

<div id="detail-panel">
  <button class="dp-close" onclick="closePanel()">&times;</button>
  <div id="dp-content"></div>
</div>

<svg id="graph"></svg>

<script>
const rawNodes = {nj};
const rawLinks = {lj};
const rawPeopleNodes = {pnj};
const rawPeopleLinks = {plj};

let showPeople = false;
let showLabels = true;
let showArchive = false;
let isDark = false;
let pinnedNode = null;

const domainColorVar = {{
  life: '--life', ventures: '--ventures', experiments: '--experiments',
  archive: '--archive', people: '--people',
}};

function dc(domain) {{
  const v = domainColorVar[domain] || '--ventures';
  return getComputedStyle(document.documentElement).getPropertyValue(v).trim();
}}

// ─── Domain gravity targets (spread around center) ───
const domainAngle = {{ life: -90, ventures: 30, experiments: 150, archive: 210, people: -30 }};
function domainTarget(domain, w, h) {{
  const angle = (domainAngle[domain] || 0) * Math.PI / 180;
  const r = Math.min(w, h) * 0.22;
  return {{ x: Math.cos(angle) * r, y: Math.sin(angle) * r }};
}}

// ─── Setup ───
const width = window.innerWidth;
const height = window.innerHeight - 44;

const svg = d3.select('#graph')
  .attr('width', width).attr('height', height);

const zoomG = svg.append('g');
const domainLabelG = zoomG.append('g').attr('class', 'domain-labels');
const linkG = zoomG.append('g').attr('class', 'links');
const nodeG = zoomG.append('g').attr('class', 'nodes');

const zoom = d3.zoom()
  .scaleExtent([0.2, 5])
  .on('zoom', (e) => zoomG.attr('transform', e.transform));
svg.call(zoom);
svg.call(zoom.transform, d3.zoomIdentity.translate(width/2, height/2 + 22));

// ─── Build simulation ───
let simulation, nodeEls, linkEls, labelEls;

function buildGraph() {{
  // Filter nodes
  let nodes = rawNodes.filter(n => showArchive || !n.archived);
  let nodeIds = new Set(nodes.map(n => n.id));
  let links = rawLinks.filter(l =>
    nodeIds.has(typeof l.source === 'object' ? l.source.id : l.source) &&
    nodeIds.has(typeof l.target === 'object' ? l.target.id : l.target)
  );

  if (showPeople) {{
    nodes = [...nodes, ...rawPeopleNodes];
    rawPeopleNodes.forEach(n => nodeIds.add(n.id));
    links = [...links, ...rawPeopleLinks.filter(l =>
      nodeIds.has(typeof l.source === 'object' ? l.source.id : l.source) &&
      nodeIds.has(typeof l.target === 'object' ? l.target.id : l.target)
    )];
  }}

  // Adjacency map for neighbor highlighting
  const neighbors = new Map();
  nodes.forEach(n => neighbors.set(n.id, new Set()));
  links.forEach(l => {{
    const s = typeof l.source === 'object' ? l.source.id : l.source;
    const t = typeof l.target === 'object' ? l.target.id : l.target;
    if (neighbors.has(s)) neighbors.get(s).add(t);
    if (neighbors.has(t)) neighbors.get(t).add(s);
  }});

  // Clear
  linkG.selectAll('*').remove();
  nodeG.selectAll('*').remove();
  domainLabelG.selectAll('*').remove();

  // Domain region labels
  const domainNames = {{ life: 'Life', ventures: 'Ventures', experiments: 'Experiments', people: 'People', archive: 'Archive' }};
  Object.entries(domainAngle).forEach(([domain, angle]) => {{
    if (!showArchive && domain === 'archive') return;
    const t = domainTarget(domain, width, height);
    domainLabelG.append('text')
      .attr('class', 'domain-label')
      .attr('x', t.x).attr('y', t.y - Math.min(width, height) * 0.16)
      .attr('text-anchor', 'middle')
      .attr('fill', dc(domain))
      .text(domainNames[domain] || domain);
  }});

  // Links
  linkEls = linkG.selectAll('line')
    .data(links)
    .join('line')
    .attr('class', d => `link ${{d.type === 'parent' ? 'link-parent' : d.type === 'person' ? 'link-person' : ''}}`)
    .attr('stroke', d => {{
      if (d.type === 'person') return dc('people');
      return isDark ? '#555' : '#ccc';
    }})
    .attr('stroke-width', d => d.type === 'parent' ? 1.5 : 1);

  // Node groups
  const ng = nodeG.selectAll('g')
    .data(nodes, d => d.id)
    .join('g')
    .attr('class', 'node');

  // Circles with health indicators
  ng.append('circle')
    .attr('class', d => {{
      let cls = 'node-circle';
      if (d.health && !d.archived) cls += ` health-${{d.health}}`;
      if (d.daysSince !== undefined && d.daysSince <= 3) cls += ' glow';
      return cls;
    }})
    .attr('r', d => d.size)
    .attr('fill', d => {{
      const c = dc(d.domain);
      if (d.archived) return c + '30';
      if (d.type === 'person') return c + '50';
      // Opacity by recency
      if (d.daysSince <= 7) return c + 'DD';
      if (d.daysSince <= 30) return c + '99';
      return c + '55';
    }})
    .attr('stroke', d => {{
      if (d.health === 'active' && !d.archived) return '#22C55E';
      if (d.health === 'quiet' && !d.archived) return '#F59E0B';
      if (d.health === 'waiting' && !d.archived) return '#EF4444';
      return dc(d.domain);
    }})
    .attr('stroke-width', d => {{
      if (d.archived) return 0.5;
      if (d.health === 'active') return 2.5;
      if (d.health === 'quiet') return 2;
      if (d.health === 'waiting') return 2;
      return 1;
    }})
    .attr('stroke-opacity', d => d.archived ? 0.2 : 0.8);

  // Bundle count badge for nodes with bundles
  ng.filter(d => d.bundleCount > 0 && !d.archived)
    .append('text')
    .attr('text-anchor', 'middle')
    .attr('dy', '0.35em')
    .attr('font-size', d => Math.max(7, Math.min(10, d.size * 0.6)) + 'px')
    .attr('font-weight', '700')
    .attr('fill', '#fff')
    .text(d => d.bundleCount);

  // Urgent task indicator (red dot top-right)
  ng.filter(d => d.taskCounts && d.taskCounts.urgent > 0)
    .append('circle')
    .attr('cx', d => d.size * 0.6)
    .attr('cy', d => -d.size * 0.6)
    .attr('r', 4)
    .attr('fill', '#EF4444')
    .attr('stroke', isDark ? '#0F1117' : '#FAF8F5')
    .attr('stroke-width', 1.5);

  // Blocker indicator (amber triangle)
  ng.filter(d => d.blockers && d.blockers.length > 0)
    .append('polygon')
    .attr('points', d => {{
      const x = -d.size * 0.6;
      const y = -d.size * 0.6;
      return `${{x}},${{y-5}} ${{x-4}},${{y+3}} ${{x+4}},${{y+3}}`;
    }})
    .attr('fill', '#F59E0B');

  // Labels
  labelEls = ng.append('text')
    .attr('class', 'node-label')
    .attr('dy', d => d.size + 12)
    .attr('font-size', d => {{
      if (d.size >= 14) return '11px';
      if (d.size >= 8) return '9px';
      return '8px';
    }})
    .attr('font-weight', d => d.size >= 12 ? '500' : '400')
    .attr('opacity', d => {{
      if (!showLabels) return 0;
      if (d.size >= 10) return 1;
      return 0;
    }})
    .text(d => {{
      const name = d.label;
      if (d.size < 8 && name.length > 12) return name.slice(0,10) + '..';
      return name;
    }});

  nodeEls = ng;

  // ─── Hover: neighborhood highlight ───
  const tooltip = d3.select('#tooltip');

  ng.on('mouseenter', function(event, d) {{
    if (pinnedNode) return;
    highlightNeighbors(d, neighbors);
    showTooltip(event, d);
  }})
  .on('mousemove', (event) => {{
    tooltip.style('left', (event.clientX + 14) + 'px')
           .style('top', (event.clientY - 8) + 'px');
  }})
  .on('mouseleave', function() {{
    if (pinnedNode) return;
    clearHighlight();
    tooltip.style('opacity', 0);
  }})
  .on('click', function(event, d) {{
    event.stopPropagation();
    if (pinnedNode === d.id) {{
      pinnedNode = null;
      clearHighlight();
      tooltip.style('opacity', 0);
      closePanel();
    }} else {{
      pinnedNode = d.id;
      highlightNeighbors(d, neighbors);
      tooltip.style('opacity', 0);
      openDetailPanel(d, neighbors);
    }}
  }});

  svg.on('click', () => {{
    pinnedNode = null;
    clearHighlight();
    tooltip.style('opacity', 0);
    closePanel();
  }});

  // ─── Drag ───
  ng.call(d3.drag()
    .on('start', (event, d) => {{
      if (!event.active) simulation.alphaTarget(0.15).restart();
      d.fx = d.x; d.fy = d.y;
    }})
    .on('drag', (event, d) => {{
      d.fx = event.x; d.fy = event.y;
    }})
    .on('end', (event, d) => {{
      if (!event.active) simulation.alphaTarget(0);
      d.fx = null; d.fy = null;
    }})
  );

  // ─── Simulation ───
  simulation = d3.forceSimulation(nodes)
    .force('charge', d3.forceManyBody()
      .strength(d => d.archived ? -30 : d.type === 'person' ? -40 : -120))
    .force('link', d3.forceLink(links).id(d => d.id)
      .distance(d => d.type === 'parent' ? 35 : d.type === 'person' ? 50 : 55)
      .strength(d => d.type === 'parent' ? 0.7 : 0.2))
    .force('collide', d3.forceCollide()
      .radius(d => d.size + 3).strength(0.8))
    .force('x', d3.forceX()
      .x(d => domainTarget(d.domain, width, height).x)
      .strength(d => d.parent ? 0.02 : 0.06))
    .force('y', d3.forceY()
      .y(d => domainTarget(d.domain, width, height).y)
      .strength(d => d.parent ? 0.02 : 0.06))
    .alphaDecay(0.02)
    .on('tick', () => {{
      linkEls
        .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
      nodeEls.attr('transform', d => `translate(${{d.x}},${{d.y}})`);
    }});

  function highlightNeighbors(d, nbrs) {{
    const connected = nbrs.get(d.id) || new Set();
    connected.add(d.id);

    nodeEls.select('circle')
      .classed('dimmed', n => !connected.has(n.id))
      .classed('highlighted', n => connected.has(n.id));

    labelEls
      .classed('dimmed', n => !connected.has(n.id))
      .classed('highlighted', n => connected.has(n.id))
      .attr('opacity', n => {{
        if (connected.has(n.id)) return 1;
        return 0.06;
      }});

    linkEls
      .classed('dimmed', l => {{
        const s = typeof l.source === 'object' ? l.source.id : l.source;
        const t = typeof l.target === 'object' ? l.target.id : l.target;
        return !(connected.has(s) && connected.has(t));
      }})
      .classed('highlighted', l => {{
        const s = typeof l.source === 'object' ? l.source.id : l.source;
        const t = typeof l.target === 'object' ? l.target.id : l.target;
        return connected.has(s) && connected.has(t);
      }})
      .attr('stroke-opacity', l => {{
        const s = typeof l.source === 'object' ? l.source.id : l.source;
        const t = typeof l.target === 'object' ? l.target.id : l.target;
        return (connected.has(s) && connected.has(t)) ? 0.7 : 0.03;
      }});
  }}

  function clearHighlight() {{
    nodeEls.select('circle').classed('dimmed', false).classed('highlighted', false);
    labelEls.classed('dimmed', false).classed('highlighted', false)
      .attr('opacity', d => {{
        if (!showLabels) return 0;
        if (d.size >= 10) return 1;
        return 0;
      }});
    linkEls.classed('dimmed', false).classed('highlighted', false)
      .attr('stroke-opacity', 0.25);
  }}

  function showTooltip(event, d) {{
    let html = `<div class="tt-name">${{d.label}}</div>`;
    html += `<span class="tt-badge" style="background:${{dc(d.domain)}}22;color:${{dc(d.domain)}}">${{d.type || d.domain}}</span>`;
    if (d.goal) html += `<div class="tt-goal">${{d.goal}}</div>`;

    const meta = [];
    if (d.phase && d.phase !== 'unknown') meta.push(d.phase);
    if (d.updated) meta.push(`${{d.updated}}`);
    if (d.bundleCount) meta.push(`${{d.bundleCount}} bundles`);
    if (d.sessions) meta.push(`${{d.sessions}} sessions`);
    if (d.peopleList && d.peopleList.length) meta.push(d.peopleList.join(', '));
    if (meta.length) html += `<div class="tt-meta">${{meta.join(' &middot; ')}}</div>`;

    tooltip.html(html).style('opacity', 1);
  }}
}}

// ─── Detail Panel ───
function openDetailPanel(d, nbrs) {{
  const panel = document.getElementById('detail-panel');
  const content = document.getElementById('dp-content');
  const data = d;

  const healthColors = {{ active: '#22C55E', quiet: '#F59E0B', waiting: '#EF4444' }};
  const healthLabels = {{ active: 'Active', quiet: 'Quiet', waiting: 'Stale' }};
  const statusColors = {{ draft: '#9CA3AF', prototype: '#F59E0B', published: '#3B82F6', done: '#22C55E' }};

  let html = `<div class="dp-name">${{data.label}}</div>`;
  html += `<div class="dp-badges">`;
  html += `<span class="dp-badge" style="background:${{dc(data.domain)}}22;color:${{dc(data.domain)}}">${{data.domain}}</span>`;
  if (data.type && data.type !== data.domain) {{
    html += `<span class="dp-badge" style="background:var(--border);color:var(--text-dim)">${{data.type}}</span>`;
  }}
  if (data.health) {{
    const hc = healthColors[data.health] || '#999';
    html += `<span class="dp-badge" style="background:${{hc}}22;color:${{hc}}">${{healthLabels[data.health] || data.health}}</span>`;
  }}
  if (data.phase && data.phase !== 'unknown') {{
    html += `<span class="dp-badge" style="background:var(--border);color:var(--text-dim)">${{data.phase}}</span>`;
  }}
  html += `</div>`;

  if (data.goal) {{
    html += `<div class="dp-goal">${{data.goal}}</div>`;
  }}

  // Blockers
  if (data.blockers && data.blockers.length > 0) {{
    html += `<div class="dp-section" style="background:#EF444415;border-radius:4px;padding:8px 10px;margin-bottom:12px">`;
    html += `<div class="dp-section-title" style="color:#EF4444">Blockers (${{data.blockers.length}})</div>`;
    data.blockers.forEach(b => {{
      html += `<div class="dp-item" style="color:#EF4444">${{b}}</div>`;
    }});
    html += `</div>`;
  }}

  // Tasks summary
  const tc = data.taskCounts || {{}};
  if (tc.urgent || tc.active || tc.todo || tc.blocked) {{
    html += `<div class="dp-section"><div class="dp-section-title">Tasks</div>`;
    html += `<div style="display:flex;gap:8px;flex-wrap:wrap">`;
    if (tc.urgent) html += `<span class="dp-badge" style="background:#EF444422;color:#EF4444">${{tc.urgent}} urgent</span>`;
    if (tc.active) html += `<span class="dp-badge" style="background:#3B82F622;color:#3B82F6">${{tc.active}} active</span>`;
    if (tc.todo) html += `<span class="dp-badge" style="background:var(--border);color:var(--text-dim)">${{tc.todo}} todo</span>`;
    if (tc.blocked) html += `<span class="dp-badge" style="background:#F59E0B22;color:#F59E0B">${{tc.blocked}} blocked</span>`;
    html += `</div></div>`;
  }}

  // Bundles
  if (data.capsules && data.capsules.length > 0) {{
    html += `<div class="dp-section"><div class="dp-section-title">Bundles (${{data.capsules.length}})</div>`;
    data.capsules.forEach(b => {{
      const sc = statusColors[b.status] || '#999';
      html += `<div class="dp-bundle"><span>${{b.name.replace(/-/g, ' ')}}</span>`;
      html += `<span class="dp-bundle-status" style="background:${{sc}}22;color:${{sc}}">${{b.status || 'draft'}}</span></div>`;
    }});
    html += `</div>`;
  }}

  // People
  if (data.peopleList && data.peopleList.length > 0) {{
    html += `<div class="dp-section"><div class="dp-section-title">People (${{data.peopleList.length}})</div>`;
    data.peopleList.forEach(p => {{
      html += `<div class="dp-item">${{p}}</div>`;
    }});
    html += `</div>`;
  }}

  // Connected walnuts (clickable)
  const connected = nbrs.get(data.id) || new Set();
  if (connected.size > 0) {{
    html += `<div class="dp-section"><div class="dp-section-title">Connected (${{connected.size}})</div>`;
    connected.forEach(cid => {{
      html += `<div class="dp-item clickable" onclick="focusNode('${{cid}}')">${{cid.replace(/-/g, ' ')}}</div>`;
    }});
    html += `</div>`;
  }}

  // Tags
  if (data.tags && data.tags.length > 0) {{
    html += `<div class="dp-section"><div class="dp-section-title">Tags</div>`;
    html += `<div style="display:flex;flex-wrap:wrap;gap:4px">`;
    data.tags.forEach(t => {{
      html += `<span class="dp-badge" style="background:var(--border);color:var(--text-dim)">${{t}}</span>`;
    }});
    html += `</div></div>`;
  }}

  // Meta
  const meta = [];
  if (data.updated) meta.push(`Updated: ${{data.updated}}`);
  if (data.rhythm) meta.push(`Rhythm: ${{data.rhythm}}`);
  if (data.sessions) meta.push(`Sessions: ${{data.sessions}}`);
  if (data.bundleCount) meta.push(`Bundles: ${{data.bundleCount}}`);
  if (data.daysSince !== undefined) meta.push(`${{data.daysSince}}d ago`);
  if (meta.length) html += `<div class="dp-meta">${{meta.join(' &middot; ')}}</div>`;

  content.innerHTML = html;
  panel.classList.add('open');
}}

function closePanel() {{
  document.getElementById('detail-panel').classList.remove('open');
}}

function focusNode(nodeId) {{
  // Find and highlight the target node in the graph
  const targetNode = nodeEls.data().find(d => d.id === nodeId);
  if (targetNode) {{
    pinnedNode = nodeId;
    // Pan to the node
    const transform = d3.zoomTransform(svg.node());
    const x = targetNode.x * transform.k + transform.x;
    const y = targetNode.y * transform.k + transform.y;
    svg.transition().duration(500)
      .call(zoom.translateTo, targetNode.x, targetNode.y);

    // Build neighbors map and highlight
    const neighbors = new Map();
    const allNodes = nodeEls.data();
    allNodes.forEach(n => neighbors.set(n.id, new Set()));
    const allLinks = linkEls.data();
    allLinks.forEach(l => {{
      const s = typeof l.source === 'object' ? l.source.id : l.source;
      const t = typeof l.target === 'object' ? l.target.id : l.target;
      if (neighbors.has(s)) neighbors.get(s).add(t);
      if (neighbors.has(t)) neighbors.get(t).add(s);
    }});

    // Trigger highlight + panel
    const connected = neighbors.get(nodeId) || new Set();
    connected.add(nodeId);
    nodeEls.select('circle')
      .classed('dimmed', n => !connected.has(n.id))
      .classed('highlighted', n => connected.has(n.id));
    labelEls
      .attr('opacity', n => connected.has(n.id) ? 1 : 0.06);
    linkEls
      .attr('stroke-opacity', l => {{
        const s = typeof l.source === 'object' ? l.source.id : l.source;
        const t = typeof l.target === 'object' ? l.target.id : l.target;
        return (connected.has(s) && connected.has(t)) ? 0.7 : 0.03;
      }});

    openDetailPanel(targetNode, neighbors);
  }}
}}

// ─── Search (highlight without hiding) ───
const searchInput = document.getElementById('search-input');
searchInput.addEventListener('input', () => {{
  const term = searchInput.value.toLowerCase();
  if (!term) {{
    nodeEls.select('circle').classed('dimmed', false);
    labelEls.classed('dimmed', false).attr('opacity', d => {{
      if (!showLabels) return 0;
      return d.size >= 10 ? 1 : 0;
    }});
    linkEls.attr('stroke-opacity', 0.25);
    return;
  }}
  nodeEls.select('circle').classed('dimmed', d => {{
    const name = (d.label || '').toLowerCase();
    const goal = (d.goal || '').toLowerCase();
    const tags = (d.tags || []).join(' ').toLowerCase();
    return !(name.includes(term) || goal.includes(term) || tags.includes(term));
  }});
  labelEls.attr('opacity', d => {{
    const name = (d.label || '').toLowerCase();
    const goal = (d.goal || '').toLowerCase();
    return (name.includes(term) || goal.includes(term)) ? 1 : 0.06;
  }});
}});

// ─── Controls ───
document.getElementById('btn-people').addEventListener('click', function() {{
  showPeople = !showPeople;
  this.classList.toggle('active', showPeople);
  buildGraph();
}});

document.getElementById('btn-labels').addEventListener('click', function() {{
  showLabels = !showLabels;
  this.classList.toggle('active', !showLabels);
  this.textContent = showLabels ? 'Labels' : 'No Labels';
  if (labelEls) labelEls.attr('opacity', d => {{
    if (!showLabels) return 0;
    return d.size >= 10 ? 1 : 0;
  }});
}});

document.getElementById('btn-archive').addEventListener('click', function() {{
  showArchive = !showArchive;
  this.classList.toggle('active', showArchive);
  buildGraph();
}});

const searchBox = document.getElementById('search-box');
document.getElementById('btn-search').addEventListener('click', () => {{
  searchBox.style.display = searchBox.style.display === 'none' ? 'block' : 'none';
  if (searchBox.style.display === 'block') searchInput.focus();
}});

document.getElementById('btn-theme').addEventListener('click', function() {{
  isDark = !isDark;
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : '');
  this.textContent = isDark ? 'Light' : 'Dark';
  buildGraph();
}});

document.addEventListener('keydown', (e) => {{
  if (e.key === '/') {{ e.preventDefault(); searchBox.style.display = 'block'; searchInput.focus(); }}
  if (e.key === 'Escape') {{
    searchBox.style.display = 'none';
    searchInput.value = '';
    pinnedNode = null;
    if (nodeEls) {{
      nodeEls.select('circle').classed('dimmed', false);
      labelEls.classed('dimmed', false).attr('opacity', d => {{
        if (!showLabels) return 0;
        return d.size >= 10 ? 1 : 0;
      }});
      linkEls.attr('stroke-opacity', 0.25);
    }}
    d3.select('#tooltip').style('opacity', 0);
  }}
}});

window.addEventListener('resize', () => {{
  const w = window.innerWidth, h = window.innerHeight - 44;
  svg.attr('width', w).attr('height', h);
}});

// ─── Semantic zoom: show/hide labels based on zoom level ───
svg.call(zoom.on('zoom', (e) => {{
  zoomG.attr('transform', e.transform);
  const k = e.transform.k;
  if (labelEls) {{
    labelEls.attr('opacity', d => {{
      if (!showLabels) return 0;
      if (d.size * k >= 8) return 1;
      if (d.size * k >= 5) return 0.5;
      return 0;
    }});
  }}
}}));

// ─── Init ───
buildGraph();
</script>
</body>
</html>'''


if __name__ == '__main__':
    main()
