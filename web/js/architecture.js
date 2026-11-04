// The architecture diagram: a deterministic layered layout (by node layer, then name),
// pan with drag, zoom with the wheel and buttons, fit, search, and selection that
// highlights a node's edges and lists its linked tasks. Same-row edges arc above any node
// between their ends, and every edge label sits on its own path, clear of nodes and labels.

import { h, s, reconcile } from './dom.js';
import { icon, iconPaths } from './icons.js';
import { NODE_TONE, TASK_TONE, act, errorSlot, pill } from './ui.js';
import { createViewport } from './viewport.js';

const NODE_W = 196;
const NODE_H = 64;
const LAYER_GAP = 96;
const NODE_GAP = 112;
const MIN_ZOOM = 0.3;
const MAX_ZOOM = 2.5;
const MOBILE_ZOOM = 0.62;
const LABEL_H = 16;
const NAME_MAX = NODE_W - 60 - 26;   // the status dot sits at the right
const TECH_MAX = NODE_W - 60 - 12;

/** Pure: node id -> {x, y}. Layers are rows top to bottom; each row is sorted by name and centred. */
export function layeredLayout(nodes) {
  const layers = new Map();
  for (const node of nodes) {
    const key = Number.isFinite(node.layer) ? node.layer : 0;
    if (!layers.has(key)) layers.set(key, []);
    layers.get(key).push(node);
  }
  const keys = [...layers.keys()].sort((a, b) => a - b);
  const widest = Math.max(1, ...keys.map((k) => layers.get(k).length));
  const total = widest * NODE_W + (widest - 1) * NODE_GAP;
  const positions = new Map();
  keys.forEach((key, row) => {
    const items = layers.get(key).sort((a, b) => String(a.name).localeCompare(String(b.name)) || String(a.id).localeCompare(String(b.id)));
    const width = items.length * NODE_W + (items.length - 1) * NODE_GAP;
    const offset = (total - width) / 2;
    items.forEach((node, index) => positions.set(node.id, { x: offset + index * (NODE_W + NODE_GAP), y: row * (NODE_H + LAYER_GAP) }));
  });
  return { positions, width: total, height: keys.length * NODE_H + Math.max(0, keys.length - 1) * LAYER_GAP };
}

export function mountArchitecture(ctx, root, { onOpenTask }) {
  const world = s('g', { class: 'arch-world' });
  const edgeLayer = s('g', { class: 'arch-edges' });
  const labelLayer = s('g', { class: 'arch-labels' });
  const nodeLayer = s('g', { class: 'arch-nodes' });
  world.append(edgeLayer, labelLayer, nodeLayer);
  const defs = s('defs', null,
    s('marker', { id: 'arch-arrow', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' },
      s('path', { d: 'M0 1.5L9 5L0 8.5z', class: 'arch-arrow' })),
    s('pattern', { id: 'arch-grid', width: 22, height: 22, patternUnits: 'userSpaceOnUse' }, s('circle', { cx: 1, cy: 1, r: 1, class: 'arch-dot' })));
  const svg = s('svg', { class: 'arch-svg', role: 'img', 'aria-label': 'Architecture diagram' },
    defs, s('rect', { class: 'arch-bg', width: '100%', height: '100%', fill: 'url(#arch-grid)' }), world);

  const search = h('input', { class: 'input arch-search', type: 'search', placeholder: 'Search nodes', 'aria-label': 'Search architecture nodes', spellcheck: 'false' });
  const refreshSlot = errorSlot();
  const refresh = h('button', { class: 'btn btn-sm', type: 'button' }, icon('retry', { size: 13 }), 'Sync');
  const toolbar = h('div', { class: 'arch-toolbar' },
    h('div', { class: 'arch-search-wrap' }, icon('search', { size: 14 }), search),
    h('span', { class: 'grow' }),
    refresh);
  const legend = h('div', { class: 'arch-legend' }, Object.entries(NODE_TONE).map(([name, tone]) => pill(name, tone)));
  const detail = h('aside', { class: 'arch-detail', hidden: true });
  const viewport = h('div', { class: 'arch-viewport', tabindex: '0', 'aria-label': 'Architecture diagram. Drag to pan, scroll to zoom.' }, svg, legend, detail);
  root.append(toolbar, refreshSlot, viewport);

  let graph = { nodes: [], edges: [], positions: new Map(), width: 0, height: 0, box: { x: 0, y: 0, width: 0, height: 0 } };

  // ---- view transform (shared with the task graph) ----------------------------------

  const zoom = createViewport({
    element: svg, mode: 'pan', keys: viewport, observe: viewport,
    minZoom: MIN_ZOOM, maxZoom: MAX_ZOOM, mobileZoom: MOBILE_ZOOM, pad: 40, reserve: 16,   // keep the legend clear
    size: () => graph.box,
    apply: (view) => world.setAttribute('transform', `translate(${view.x} ${view.y}) scale(${view.k})`),
    onTap: (event) => {
      const node = event.target.closest?.('.arch-node');
      select(node ? node.dataset.key : null);
    },
  });
  toolbar.insertBefore(zoom.controls, refresh);
  const fit = zoom.fit;

  // ---- selection and search -----------------------------------------------------

  function select(id) {
    ctx.store.setLocal({ selectedNode: id });
  }
  search.addEventListener('input', () => highlight());
  search.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    const match = matches()[0];
    if (match) { select(match.id); centreOn(match.id); }
  });
  const matches = () => {
    const query = search.value.trim().toLowerCase();
    return query ? graph.nodes.filter((n) => `${n.name} ${n.tech} ${n.kind} ${n.id}`.toLowerCase().includes(query)) : [];
  };
  function centreOn(id) {
    const p = graph.positions.get(id);
    if (p) zoom.centreOn(p.x + NODE_W / 2, p.y + NODE_H / 2);
  }

  function highlight() {
    const selected = ctx.store.local.selectedNode;
    const hits = new Set(matches().map((n) => n.id));
    const linked = new Set();
    for (const edge of graph.edges) {
      const on = selected && (edge.from === selected || edge.to === selected);
      if (on) { linked.add(edge.from); linked.add(edge.to); }
    }
    for (const el of nodeLayer.children) {
      const id = el.dataset.key;
      el.classList.toggle('is-selected', id === selected);
      el.classList.toggle('is-dim', Boolean((selected && !linked.has(id) && id !== selected) || (hits.size && !hits.has(id))));
      el.classList.toggle('is-hit', hits.has(id));
    }
    for (const el of [...edgeLayer.children, ...labelLayer.children]) {
      const on = selected && (el.dataset.from === selected || el.dataset.to === selected);
      el.classList.toggle('is-on', Boolean(on));
      el.classList.toggle('is-dim', Boolean(selected && !on));
    }
    renderDetail(selected);
  }

  function renderDetail(id) {
    const node = graph.nodes.find((n) => n.id === id);
    detail.hidden = !node;
    if (!node) return;
    const tasks = ctx.store.state.plan.tasks;
    const inbound = graph.edges.filter((e) => e.to === id);
    const outbound = graph.edges.filter((e) => e.from === id);
    const name = (nid) => graph.nodes.find((n) => n.id === nid)?.name || nid;
    detail.replaceChildren(...[
      h('div', { class: 'arch-detail-head' },
        h('span', { class: `arch-detail-icon tone-${NODE_TONE[node.status] || 'gray'}` }, icon(node.kind, { size: 16 })),
        h('div', { class: 'grow' }, h('div', { class: 'arch-detail-name', text: node.name }), h('div', { class: 'faint', text: `${node.kind} · ${node.tech || '—'}` })),
        h('button', { class: 'btn btn-ghost btn-sm btn-icon', type: 'button', 'aria-label': 'Clear selection', onclick: () => select(null) }, icon('close', { size: 14 }))),
      pill(node.status, NODE_TONE[node.status]),
      node.notes ? h('p', { class: 'arch-notes', text: node.notes }) : null,
      edgeList('Receives from', inbound, (e) => name(e.from)),
      edgeList('Sends to', outbound, (e) => name(e.to)),
      h('div', { class: 'drawer-section-title', text: `Linked tasks · ${node.tasks?.length || 0}` }),
      h('div', { class: 'arch-tasks' }, (node.tasks || []).map((tid) => {
        const task = tasks[tid];
        return h('button', { class: 'arch-task', type: 'button', onclick: () => onOpenTask(tid) },
          h('span', { class: `dot tone-${TASK_TONE[task?.status] || 'gray'}` }), h('span', { class: 'grow', text: task?.title || tid }), h('span', { class: 'faint mono', text: task?.status || '' }));
      }))].filter(Boolean));
  }

  const edgeList = (title, list, label) => (list.length ? h('div', null,
    h('div', { class: 'drawer-section-title', text: title }),
    h('ul', { class: 'arch-edge-list' }, list.map((e) => h('li', null, h('span', { text: label(e) }), h('span', { class: 'faint mono', text: [e.label, e.protocol].filter(Boolean).join(' · ') }))))) : null);

  // ---- render -------------------------------------------------------------------

  function render(state) {
    const nodes = Object.values(state.architecture.nodes || {});
    const edges = Object.values(state.architecture.edges || {}).filter((e) => state.architecture.nodes[e.from] && state.architecture.nodes[e.to]);
    const shape = nodes.map((n) => `${n.id}:${n.layer}:${n.name}`).sort().join('|');
    const relayout = shape !== graph.shape;
    const { positions, width, height } = layeredLayout(nodes);

    reconcile(nodeLayer, nodes, {
      key: (n) => n.id,
      create: createNode,
      update: (el, node) => updateNode(el, node, positions.get(node.id)),
    });
    const boxes = [...positions.values()].map((p) => ({ x: p.x, y: p.y, w: NODE_W, h: NODE_H }));
    const geometry = edges.map((edge) => ({ edge, pts: route(positions.get(edge.from), positions.get(edge.to), boxes) }));
    for (const g of geometry) g.d = `M${g.pts[0].x} ${g.pts[0].y} C${g.pts[1].x} ${g.pts[1].y}, ${g.pts[2].x} ${g.pts[2].y}, ${g.pts[3].x} ${g.pts[3].y}`;
    reconcile(edgeLayer, geometry, {
      key: (g) => g.edge.id, enter: false,
      create: () => s('path', { class: 'arch-edge', 'marker-end': 'url(#arch-arrow)' }),
      update: (el, g) => {
        el.setAttribute('d', g.d);
        el.dataset.from = g.edge.from;
        el.dataset.to = g.edge.to;
        el.setAttribute('class', `arch-edge status-${g.edge.status || 'planned'}`);
      },
    });
    const labelled = geometry.filter((g) => g.edge.label);
    const font = `500 10px ${getComputedStyle(document.body).fontFamily}`;
    for (const g of labelled) g.w = Math.ceil(textWidth(g.edge.label, font)) + 14;
    placeLabels(labelled, boxes);
    graph = { nodes, edges, positions, width, height, shape, box: contentBox(boxes, geometry, labelled) };
    reconcile(labelLayer, labelled, {
      key: (g) => g.edge.id, enter: false,
      create: () => s('g', { class: 'arch-label' }, s('rect', { rx: 5 }), s('text', null)),
      update: (el, g) => {
        el.dataset.from = g.edge.from;
        el.dataset.to = g.edge.to;
        const text = el.lastChild;
        text.textContent = g.edge.label;
        text.setAttribute('x', g.mx);
        text.setAttribute('y', g.my + 3.5);
        const w = g.w;
        const rect = el.firstChild;
        rect.setAttribute('x', g.mx - w / 2);
        rect.setAttribute('y', g.my - LABEL_H / 2);
        rect.setAttribute('width', w);
        rect.setAttribute('height', LABEL_H);
      },
    });
    if (relayout) requestAnimationFrame(fit);
    else zoom.refresh();
    highlight();
  }

  refresh.addEventListener('click', () => act(refresh, refreshSlot, () => ctx.api.send('architecture.refresh', {})));
  ctx.store.subscribe(['architecture', 'plan'], render);
  ctx.store.subscribe(['$selectedNode'], highlight);
  return { fit };
}

function createNode(node) {
  const g = s('g', { class: 'arch-node', tabindex: '0', role: 'button' },
    s('rect', { class: 'arch-node-box', width: NODE_W, height: NODE_H, rx: 14 }),
    s('rect', { class: 'arch-node-glow', width: NODE_W, height: NODE_H, rx: 14 }),
    s('rect', { class: 'arch-node-icon-bg', x: 12, y: 14, width: 36, height: 36, rx: 10 }),
    s('g', { class: 'arch-node-icon', transform: 'translate(20 22) scale(0.83)' }),
    s('text', { class: 'arch-node-name', x: 60, y: 29 }),
    s('text', { class: 'arch-node-tech', x: 60, y: 46 }),
    s('circle', { class: 'arch-node-status', cx: NODE_W - 16, cy: 16, r: 4 }));
  g.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      g.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }));
      g.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }));
    }
  });
  return g;
}

function updateNode(el, node, position) {
  el.setAttribute('transform', `translate(${position.x} ${position.y})`);
  el.dataset.status = node.status || 'planned';
  el.dataset.kind = node.kind;
  el.setAttribute('aria-label', `${node.name}, ${node.kind}, ${node.status}`);
  const iconGroup = el.querySelector('.arch-node-icon');
  if (iconGroup.dataset.kind !== node.kind) {
    iconGroup.dataset.kind = node.kind;
    iconGroup.replaceChildren(...iconPaths(node.kind).map((d) => s('path', { d })));
  }
  const family = getComputedStyle(document.body);
  fitText(el.querySelector('.arch-node-name'), node.name, NAME_MAX, `600 13px ${family.fontFamily}`);
  fitText(el.querySelector('.arch-node-tech'), node.tech || node.kind, TECH_MAX, `11px ${family.getPropertyValue('--font-mono')}`);
}

/** Show the whole text; condense the glyphs only when it is wider than the room (never cut it). */
function fitText(el, value, max, font) {
  const text = String(value ?? '');
  el.textContent = text;
  if (textWidth(text, font) > max) {
    el.setAttribute('textLength', max);
    el.setAttribute('lengthAdjust', 'spacingAndGlyphs');
  } else {
    el.removeAttribute('textLength');
    el.removeAttribute('lengthAdjust');
  }
}

let measure = null;
function textWidth(text, font) {
  measure ||= document.createElement('canvas').getContext('2d');
  measure.font = font;
  return measure.measureText(text).width;
}

const lerp = (p, q, t) => ({ x: p.x + (q.x - p.x) * t, y: p.y + (q.y - p.y) * t });
const line = (p, q) => [p, lerp(p, q, 1 / 3), lerp(p, q, 2 / 3), q];

/** Pure: the cubic control points of one edge. A same-row edge arcs above any node between its ends. */
export function route(a, b, boxes) {
  if (Math.abs(a.y - b.y) < 1) {
    const right = a.x < b.x;
    const lo = Math.min(a.x, b.x), hi = Math.max(a.x, b.x);
    const between = boxes.some((box) => Math.abs(box.y - a.y) < 1 && box.x > lo + 1 && box.x < hi - 1);
    if (!between) {
      const y = a.y + NODE_H / 2;
      return line({ x: right ? a.x + NODE_W : a.x, y }, { x: right ? b.x : b.x + NODE_W, y });
    }
    const ax = a.x + NODE_W * (right ? 0.72 : 0.28), bx = b.x + NODE_W * (right ? 0.28 : 0.72);
    const lift = Math.min(LAYER_GAP * 0.62, 36 + (hi - lo) * 0.04) / 0.75;   // the peak sits `lift * 0.75` above the row
    return [{ x: ax, y: a.y }, { x: ax, y: a.y - lift }, { x: bx, y: b.y - lift }, { x: bx, y: b.y }];
  }
  const down = b.y > a.y;
  const ax = a.x + NODE_W / 2, bx = b.x + NODE_W / 2;
  const y1 = down ? a.y + NODE_H : a.y, y2 = down ? b.y : b.y + NODE_H;
  const mid = (y1 + y2) / 2;
  return [{ x: ax, y: y1 }, { x: ax, y: mid }, { x: bx, y: mid }, { x: bx, y: y2 }];
}

export function bezier(p, t) {
  const u = 1 - t;
  const a = u * u * u, b = 3 * u * u * t, c = 3 * u * t * t, d = t * t * t;
  return { x: a * p[0].x + b * p[1].x + c * p[2].x + d * p[3].x, y: a * p[0].y + b * p[1].y + c * p[2].y + d * p[3].y };
}
function normal(p, t) {
  const u = 1 - t;
  const dx = 3 * u * u * (p[1].x - p[0].x) + 6 * u * t * (p[2].x - p[1].x) + 3 * t * t * (p[3].x - p[2].x);
  const dy = 3 * u * u * (p[1].y - p[0].y) + 6 * u * t * (p[2].y - p[1].y) + 3 * t * t * (p[3].y - p[2].y);
  const len = Math.hypot(dx, dy) || 1;
  return { x: -dy / len, y: dx / len };
}
const overlap = (r, q, pad) => Math.max(0, Math.min(r.x + r.w, q.x + q.w) + pad - Math.max(r.x, q.x))
  * Math.max(0, Math.min(r.y + r.h, q.y + q.h) + pad - Math.max(r.y, q.y));

const STEPS = [0.5, 0.42, 0.58, 0.34, 0.66, 0.27, 0.73, 0.2, 0.8, 0.14, 0.86];
const OFFSETS = [0, 11, -11, 22, -22, 33, -33];

/**
 * Pure: give each label a centre (mx, my) on its own path. Try points along the path first,
 * then perpendicular offsets; take the first spot that touches no node box and no placed
 * label, or the spot with the least overlap. Shorter edges choose first: they have less room.
 */
export function placeLabels(items, boxes) {
  const placed = [];
  const length = (g) => Math.hypot(g.pts[3].x - g.pts[0].x, g.pts[3].y - g.pts[0].y);
  for (const item of [...items].sort((p, q) => length(p) - length(q))) {
    let best = null;
    for (const offset of OFFSETS) {
      for (const t of STEPS) {
        const p = bezier(item.pts, t), n = normal(item.pts, t);
        const cx = p.x + n.x * offset, cy = p.y + n.y * offset;
        const rect = { x: cx - item.w / 2, y: cy - LABEL_H / 2, w: item.w, h: LABEL_H };
        const cost = boxes.reduce((sum, box) => sum + overlap(rect, box, 6), 0) * 4
          + placed.reduce((sum, other) => sum + overlap(rect, other, 4), 0) + Math.abs(offset) * 0.01;
        if (!best || cost < best.cost) best = { cx, cy, rect, cost };
        if (cost < 1) break;
      }
      if (best.cost < 1) break;
    }
    item.mx = best.cx;
    item.my = best.cy;
    item.rect = best.rect;
    placed.push(best.rect);
  }
  return items;
}

/** The bounds of nodes, arcs and labels, so fit shows everything. */
function contentBox(boxes, geometry, labels) {
  const xs = [], ys = [];
  for (const b of boxes) { xs.push(b.x, b.x + b.w); ys.push(b.y, b.y + b.h); }
  for (const g of geometry) for (let t = 0; t <= 1; t += 0.25) { const p = bezier(g.pts, t); xs.push(p.x); ys.push(p.y); }
  for (const l of labels) { xs.push(l.rect.x, l.rect.x + l.rect.w); ys.push(l.rect.y, l.rect.y + l.rect.h); }
  if (!xs.length) return { x: 0, y: 0, width: 0, height: 0 };
  const x = Math.min(...xs), y = Math.min(...ys);
  return { x, y, width: Math.max(...xs) - x, height: Math.max(...ys) - y };
}
