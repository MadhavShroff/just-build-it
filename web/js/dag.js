// The task DAG: wave columns, SVG dependency edges, status colours and a running shimmer.
// Cards are HTML (keyed, so status changes animate in place); edges are one SVG layer.
// Titles wrap, so every render measures the real card heights first and then places the
// rows and edges from those measurements. Zoom comes from the shared viewport module.

import { h, s, reconcile, setText, trackOverflow } from './dom.js';
import { TASK_TONE } from './ui.js';
import { createViewport } from './viewport.js';

const CARD_W = 196;
const CARD_MIN_H = 82;
const COL_GAP = 52;
const ROW_GAP = 14;
const HEAD_H = 34;

export function mountDag(ctx, root, { onOpenTask }) {
  const heads = h('div', { class: 'dag-heads' });
  const edges = s('svg', { class: 'dag-edges', 'aria-hidden': 'true' });
  const cards = h('div', { class: 'dag-cards', role: 'group', 'aria-label': 'Tasks' });
  const canvas = h('div', { class: 'dag-canvas' }, heads, edges, cards);
  const stage = h('div', { class: 'dag-stage' }, canvas);
  const scroller = h('div', { class: 'dag-scroll', tabindex: '0', 'aria-label': 'Task graph. Scroll to pan. Use the zoom buttons, the plus and minus keys, or Ctrl and the wheel to zoom.' }, stage);
  const fade = trackOverflow(scroller, root);

  let extent = { width: 0, height: 0 };
  const zoom = createViewport({
    element: scroller, mode: 'scroll', observe: null,
    pad: 20, minZoom: 0.4, maxZoom: 2, maxFit: 1, mobileZoom: 0.8,
    size: () => extent,
    apply: (view) => {
      stage.style.width = `${view.sw}px`;
      stage.style.height = `${view.sh}px`;
      canvas.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.k})`;
      fade();
    },
  });
  root.classList.add('fade-x');
  root.append(scroller);

  const heights = new Map();   // task id -> last measured natural card height
  let last = null;

  function render(state) {
    last = state;
    const plan = state.plan;
    const columns = plan.waves.map((wave) => wave.filter((id) => plan.tasks[id]));
    const where = new Map();
    columns.forEach((column, c) => column.forEach((id, r) => where.set(id, { c, r })));
    const ordered = plan.order.filter((id) => where.has(id)).map((id) => plan.tasks[id]);

    reconcile(cards, ordered, {
      key: (task) => task.id,
      create: (task) => createCard(task, onOpenTask),
      update: (card, task) => updateCard(card, task, state),
    });

    // measure after the text is in place: clear the row height, then read every card once
    const live = ordered.map((task) => [task.id, cards.querySelector(`[data-key="${CSS.escape(task.id)}"]`)]);
    const visible = scroller.offsetParent !== null;
    if (visible) {
      for (const [, card] of live) card.style.height = '';
      for (const [id, card] of live) heights.set(id, Math.max(CARD_MIN_H, Math.ceil(card.offsetHeight)));
    }
    const rows = Math.max(1, ...columns.map((column) => column.length));
    const rowH = Array.from({ length: rows }, (_, r) => Math.max(CARD_MIN_H,
      ...columns.map((column) => (column[r] ? heights.get(column[r]) || CARD_MIN_H : 0))));
    const rowY = [];
    rowH.reduce((y, height, r) => { rowY[r] = y; return y + height + ROW_GAP; }, HEAD_H);
    const positions = new Map();
    for (const [id, { c, r }] of where) positions.set(id, { x: c * (CARD_W + COL_GAP), y: rowY[r], h: rowH[r] });
    for (const [id, card] of live) {
      const p = positions.get(id);
      card.style.height = `${p.h}px`;
      card.style.transform = `translate(${p.x}px, ${p.y}px)`;
    }

    const width = Math.max(0, columns.length * (CARD_W + COL_GAP) - COL_GAP);
    const height = rowY[rows - 1] + rowH[rows - 1];
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    edges.setAttribute('viewBox', `0 0 ${Math.max(width, 1)} ${height}`);
    edges.setAttribute('width', Math.max(width, 1));
    edges.setAttribute('height', height);

    reconcile(heads, columns.map((column, index) => ({ index, column })), {
      key: (wave) => wave.index, enter: false,
      create: () => h('div', { class: 'dag-head' }, h('span', { class: 'dag-head-name' }), h('span', { class: 'dag-head-count tabular' })),
      update: (head, { index, column }) => {
        head.style.transform = `translateX(${index * (CARD_W + COL_GAP)}px)`;
        head.style.width = `${CARD_W}px`;
        const merged = column.filter((id) => plan.tasks[id].status === 'merged').length;
        setText(head.firstChild, `Wave ${index + 1}`);
        setText(head.lastChild, `${merged}/${column.length}`);
        head.classList.toggle('is-done', merged === column.length);
      },
    });

    // edges from the measured card positions: right middle of the dependency to left middle of the task
    const paths = [];
    for (const task of ordered) {
      const to = positions.get(task.id);
      for (const dep of task.deps) {
        const from = positions.get(dep);
        if (!from) continue;
        const x1 = from.x + CARD_W, y1 = from.y + from.h / 2, x2 = to.x, y2 = to.y + to.h / 2;
        const bend = Math.max(24, (x2 - x1) / 2);
        const status = plan.tasks[dep].status === 'merged' ? (['running', 'verifying', 'merging'].includes(task.status) ? 'active' : 'done') : 'waiting';
        paths.push(s('path', { d: `M${x1} ${y1} C${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`, class: `dag-edge is-${status}`, 'data-from': dep, 'data-to': task.id }));
      }
    }
    edges.replaceChildren(...paths);

    extent = { width, height };
    if (visible) zoom.refresh();
  }

  ctx.store.subscribe(['plan', 'sessions'], render);
  // the pane was hidden or fonts changed the text metrics: measure again
  new ResizeObserver(() => { if (last) render(last); }).observe(scroller);
  document.fonts?.ready.then(() => { if (last) render(last); });
  return { fit: zoom.fit, controls: zoom.controls };
}

function createCard(task, onOpenTask) {
  const card = h('button', { class: 'task-card', type: 'button', onclick: () => onOpenTask(task.id) },
    h('span', { class: 'task-shimmer', 'aria-hidden': 'true' }),
    h('span', { class: 'task-top' }, h('span', { class: 'task-status-dot', 'aria-hidden': 'true' }), h('span', { class: 'task-id mono' }), h('span', { class: 'task-status' })),
    h('span', { class: 'task-title' }),
    h('span', { class: 'task-foot' }, h('span', { class: 'task-stages' }), h('span', { class: 'task-model mono' })));
  card.style.width = `${CARD_W}px`;
  return card;
}

function updateCard(card, task, state) {
  const tone = TASK_TONE[task.status] || 'gray';
  if (card.dataset.status && card.dataset.status !== task.status) {
    card.classList.remove('is-changed');
    void card.offsetWidth;
    card.classList.add('is-changed');
  }
  card.dataset.status = task.status;
  card.dataset.tone = tone;
  const [, top, title, foot] = card.children;
  setText(top.children[1], task.id);
  setText(top.children[2], task.status);
  setText(title, task.title);
  card.setAttribute('aria-label', `${task.title}, ${task.status}`);
  const stages = Object.entries(task.stages || {});
  const bar = foot.children[0];
  if (bar.childElementCount !== stages.length) bar.replaceChildren(...stages.map(() => h('i')));
  stages.forEach(([name, stage], i) => {
    bar.children[i].dataset.status = stage.status;
    bar.children[i].title = `${name}: ${stage.status}`;
  });
  const session = Object.values(state.sessions).find((item) => item.task === task.id && item.status === 'running');
  const note = session ? session.model
    : task.status === 'merged' ? (task.commit ? String(task.commit).slice(0, 7) : 'merged')
    : task.blocked_reason ? task.blocked_reason
    : task.attempts ? `attempt ${task.attempts}` : '';
  setText(foot.children[1], note);
}
