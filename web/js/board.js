// The main board (execute and complete): summary, DAG and architecture tabs, sessions,
// CI lanes, telemetry, requests and the live timeline.

import { h, reconcile, setText, beat, elapsed, relativeTime, compactNumber, money, trackOverflow } from './dom.js';
import { icon } from './icons.js';
import { mountDag } from './dag.js';
import { mountArchitecture } from './architecture.js';
import { mountMetrics } from './metrics.js';
import { mountPreview } from './preview.js';
import { TASK_TONE, act, errorSlot, payloadFor, pill, toast } from './ui.js';

const RECENT_ENDED = 2;   // ended sessions that stay visible after the running ones

export function mountBoard(ctx, root, { onOpenTask }) {
  const title = h('h1', { class: 'phase-title' });
  const sub = h('p', { class: 'phase-sub' });
  const progress = h('div', { class: 'progress', role: 'progressbar', 'aria-label': 'Tasks merged', 'aria-valuemin': '0' }, h('span'));
  const counts = h('div', { class: 'status-counts' });
  const telemetry = h('div', { class: 'tiles' });

  const views = h('div', { class: 'segmented', role: 'tablist', 'aria-label': 'Board view' });
  const dagPane = h('div', { class: 'board-pane dag-pane', role: 'tabpanel' });
  const archPane = h('div', { class: 'board-pane arch-pane', role: 'tabpanel', hidden: true });
  const previewPane = h('div', { class: 'board-pane preview-pane', role: 'tabpanel', hidden: true });
  const metricsPane = h('div', { class: 'board-pane metrics-pane', role: 'tabpanel', hidden: true });
  const main = h('section', { class: 'card board-main' }, h('div', { class: 'board-main-head' }, views, counts), dagPane, archPane, previewPane, metricsPane);

  const sessions = h('div', { class: 'sessions', tabindex: '0', role: 'group', 'aria-label': 'Agent sessions' });
  const sessionEdges = trackOverflow(sessions);   // edge fades while more sessions sit off screen
  const tmux = h('code', { class: 'tmux-cmd' });
  const copy = h('button', { class: 'btn btn-sm btn-icon', type: 'button', 'aria-label': 'Copy the tmux attach command', 'data-tip': 'Copy' }, icon('copy', { size: 13 }));
  const lanes = h('div', { class: 'lanes' });
  const requests = h('div', { class: 'requests' });
  const timeline = h('ol', { class: 'timeline', 'aria-live': 'polite', 'aria-label': 'Timeline' });

  root.append(
    h('div', { class: 'board-head' },
      h('div', { class: 'board-title' }, h('div', { class: 'eyebrow board-eyebrow' }), title, sub, h('div', { class: 'progress-row' }, progress, h('span', { class: 'progress-label tabular' }))),
      telemetry),
    h('section', { class: 'card sessions-card' },
      h('div', { class: 'card-head' },
        h('h2', { class: 'card-title' }, icon('terminal'), 'Agent sessions', h('span', { class: 'card-count sessions-count' })),
        h('div', { class: 'tmux' }, tmux, copy)),
      sessions),
    h('div', { class: 'board-grid' },
      main,
      h('div', { class: 'board-side' },
        h('section', { class: 'card requests-card' }, h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, icon('inbox'), 'Requests', h('span', { class: 'card-count requests-count' }))), requests),
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, icon('lane'), 'CI queue', h('span', { class: 'card-count lanes-count' }))), lanes),
        h('section', { class: 'card timeline-card' }, h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, icon('clock'), 'Timeline')), timeline))));

  // ---- view tabs ------------------------------------------------------------------
  const tabs = [['dag', 'Task graph', 'dag'], ['preview', 'DAG preview', 'layers'], ['architecture', 'Architecture', 'graph'], ['metrics', 'Metrics', 'bolt']];
  for (const [id, label, iconName] of tabs) {
    views.append(h('button', { class: 'segment', type: 'button', role: 'tab', 'data-view': id, onclick: () => ctx.store.setLocal({ board: id }) }, icon(iconName, { size: 14 }), label));
  }
  const dag = mountDag(ctx, dagPane, { onOpenTask });
  dag.controls.classList.add('dag-zoom');
  main.firstChild.append(dag.controls);   // the task graph zoom sits in the board head
  const arch = mountArchitecture(ctx, archPane, { onOpenTask });
  const previewView = mountPreview(ctx, previewPane, { onOpenTask });
  const metricsView = mountMetrics(ctx, metricsPane);
  ctx.store.subscribe(['$board'], () => {
    const view = ctx.store.local.board;
    for (const button of views.children) button.setAttribute('aria-selected', String(button.dataset.view === view));
    dagPane.hidden = view !== 'dag';
    previewPane.hidden = view !== 'preview';
    archPane.hidden = view !== 'architecture';
    metricsPane.hidden = view !== 'metrics';
    dag.controls.hidden = view !== 'dag';
    if (view === 'metrics') metricsView.refresh();
    else if (view === 'preview') previewView.refresh();
    else requestAnimationFrame(view === 'architecture' ? arch.fit : dag.fit);
  });

  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(tmux.textContent);
      toast('Copied the attach command');
    } catch {
      const range = document.createRange();
      range.selectNodeContents(tmux);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      toast('Press Ctrl+C to copy');
    }
  });

  // ---- summary ------------------------------------------------------------------
  ctx.store.subscribe(['run', 'plan'], (state) => {
    const tasks = state.plan.order.map((id) => state.plan.tasks[id]).filter(Boolean);
    const merged = tasks.filter((t) => t.status === 'merged').length;
    const complete = state.run.phase === 'complete';
    setText(root.querySelector('.board-eyebrow'), complete ? 'Complete' : 'Building');
    setText(title, complete ? `${state.run.name} is built` : `Building ${state.run.name}`);
    setText(sub, state.run.target || '');
    const pct = tasks.length ? Math.round((merged / tasks.length) * 100) : 0;
    progress.firstChild.style.transform = `scaleX(${pct / 100})`;
    progress.setAttribute('aria-valuemax', String(tasks.length));
    progress.setAttribute('aria-valuenow', String(merged));
    setText(root.querySelector('.progress-label'), `${merged} of ${tasks.length} merged`);
    const byStatus = {};
    for (const task of tasks) byStatus[task.status] = (byStatus[task.status] || 0) + 1;
    const order = ctx.store.schema.task_statuses.filter((s) => byStatus[s]);
    reconcile(counts, order, {
      key: (status) => status, enter: false,
      create: (status) => h('span', { class: `count tone-${TASK_TONE[status] || 'gray'}` }, h('i'), h('span', { class: 'count-n tabular' }), h('span', { class: 'count-label', text: status })),
      update: (el, status) => setText(el.children[1], String(byStatus[status])),
    });
  });

  // ---- telemetry ------------------------------------------------------------------
  const TILES = [
    ['tokens_in', 'Tokens in', 'in', compactNumber],
    ['tokens_out', 'Tokens out', 'out', compactNumber],
    ['cost_usd', 'Cost', 'coins', money],
    ['samples', 'Samples', 'bolt', compactNumber],
  ];
  ctx.store.subscribe(['telemetry'], (state) => {
    reconcile(telemetry, TILES, {
      key: (tile) => tile[0], enter: false,
      create: ([, label, iconName]) => h('div', { class: 'tile' }, h('span', { class: 'tile-label' }, icon(iconName, { size: 13 }), label), h('span', { class: 'tile-value tabular' })),
      update: (el, [key, , , format]) => {
        const value = state.telemetry[key];
        const text = format(value);
        el.classList.toggle('is-unknown', value === null || value === undefined);
        if (el.lastChild.textContent && el.lastChild.textContent !== text) beat(el, 'tick');
        setText(el.lastChild, text);
      },
    });
  });

  // ---- sessions -----------------------------------------------------------------
  ctx.store.subscribe(['sessions', 'run', 'plan'], (state) => {
    setText(tmux, state.run.tmux || '');
    const list = Object.entries(state.sessions).map(([id, session]) => ({ id, ...session }))
      .sort((a, b) => (a.status === 'running' ? 0 : 1) - (b.status === 'running' ? 0 : 1) || String(b.started).localeCompare(String(a.started)))
      .filter((x, index, all) => x.status === 'running' || index < all.filter((y) => y.status === 'running').length + RECENT_ENDED);
    setText(root.querySelector('.sessions-count'), String(list.filter((x) => x.status === 'running').length || ''));
    if (!list.length) { sessions.replaceChildren(h('p', { class: 'empty', text: 'No agent is running.' })); return; }
    if (sessions.querySelector('.empty')) sessions.replaceChildren();
    reconcile(sessions, list, {
      key: (x) => x.id,
      create: (x) => h('button', { class: 'session', type: 'button', onclick: () => x.task && state.plan.tasks[x.task] && onOpenTask(x.task) },
        h('span', { class: 'session-pulse', 'aria-hidden': 'true' }),
        h('span', { class: 'session-main' }, h('span', { class: 'session-task' }), h('span', { class: 'session-meta' })),
        h('span', { class: 'session-time tabular', 'data-started': '' })),
      update: (el, x) => {
        el.dataset.status = x.status;
        setText(el.querySelector('.session-task'), state.plan.tasks[x.task]?.title || x.task || x.id);
        setText(el.querySelector('.session-meta'), [x.role, x.model].filter(Boolean).join(' · '));
        const time = el.querySelector('.session-time');
        time.dataset.started = x.started || '';
        time.dataset.ended = x.ended || '';
        setText(time, elapsed(x.started, x.ended));
      },
    });
    requestAnimationFrame(sessionEdges);
  });
  setInterval(() => {
    for (const time of root.querySelectorAll('.session-time')) setText(time, elapsed(time.dataset.started, time.dataset.ended || null));
    for (const time of root.querySelectorAll('.timeline time')) setText(time, relativeTime(time.dateTime));
  }, 1000);

  // ---- ci queue ----------------------------------------------------------------
  // The CI panel shows the live interface-llm CI service: the server shells
  // `ci-conductor status` (bounded, cached) and the runtime's cooldown lanes.
  // The old state.lanes doc field is gone, so the dead panel is replaced.
  ctx.store.subscribe(['plan', 'run'], (state) => {
    const info = ctx.store.info || {};
    const queue = info.ci_queue || { available: false, text: '' };
    const cooling = Object.entries(info.cooldowns || {}).filter(([, s]) => Number(s) > 0)
      .sort((a, b) => a[0].localeCompare(b[0]));
    const rows = cooling.map(([lane, secs]) => ({ key: lane, name: lane, detail: `cooling ${Number(secs).toFixed(0)}s` }));
    if (queue.available && queue.text) rows.push({ key: '__ci', name: 'service queue', detail: queue.text });
    if (!rows.length) {
      setText(root.querySelector('.lanes-count'), '');
      lanes.replaceChildren(h('p', { class: 'empty', text: 'No cooling lane; the CI service reports no queue.' }));
      return;
    }
    setText(root.querySelector('.lanes-count'), String(cooling.length));
    if (lanes.querySelector('.empty')) lanes.replaceChildren();
    reconcile(lanes, rows, {
      key: (row) => row.key,
      create: () => h('div', { class: 'lane' },
        h('div', { class: 'lane-top' }, h('span', { class: 'lane-name' }), h('span', { class: 'lane-status' })),
        h('div', { class: 'lane-detail faint' })),
      update: (el, row) => {
        el.dataset.status = row.key === '__ci' ? 'info' : 'cooling';
        setText(el.querySelector('.lane-name'), row.name);
        setText(el.querySelector('.lane-status'), row.key === '__ci' ? 'live' : '');
        setText(el.querySelector('.lane-detail'), row.detail);
      },
    });
  });

  // ---- requests ----------------------------------------------------------------
  ctx.store.subscribe(['requests', 'plan'], (state) => {
    const list = Object.entries(state.requests).map(([id, d]) => ({ id, ...d }))
      .sort((a, b) => (a.status === 'open' ? 0 : 1) - (b.status === 'open' ? 0 : 1));
    const open = list.filter((d) => d.status === 'open').length;
    const count = root.querySelector('.requests-count');
    setText(count, open ? `${open} open` : '');
    count.classList.toggle('is-alert', open > 0);
    if (!list.length) { requests.replaceChildren(h('p', { class: 'empty', text: 'Nothing needs your answer.' })); return; }
    if (requests.querySelector('.empty')) requests.replaceChildren();
    reconcile(requests, list, {
      key: (d) => d.id,
      create: (d) => createRequest(ctx, d),
      update: (el, d) => updateRequest(el, d, state),
    });
  });

  // ---- timeline -----------------------------------------------------------------
  ctx.store.subscribe(['timeline'], (state) => {
    const lines = state.timeline.slice(-60).reverse();
    reconcile(timeline, lines, {
      key: (line) => line.seq,
      create: (line) => h('li', { class: `tl tl-${line.kind}` },
        h('span', { class: 'tl-dot', 'aria-hidden': 'true' }),
        h('span', { class: 'tl-text', text: line.text }),
        h('time', { class: 'tl-time tabular', datetime: line.ts, text: relativeTime(line.ts) })),
    });
  });
}

function createRequest(ctx, request) {
  const slot = errorSlot();
  const field = h('input', { class: 'input', placeholder: 'Or write an answer', 'aria-label': `Answer: ${request.title}` });
  const submit = h('button', { class: 'btn btn-sm btn-primary', type: 'button' }, 'Answer');
  const answer = (control, value) => act(control, slot, () => ctx.api.send('request.answer',
    payloadFor(ctx.store.schema, 'request.answer', { request: request.id, answer: value })));
  submit.addEventListener('click', () => (field.value.trim() ? answer(submit, field.value) : slot.show('Write an answer or pick an option.')));
  field.addEventListener('keydown', (event) => { if (event.key === 'Enter') submit.click(); });
  const el = h('article', { class: 'request' },
    h('div', { class: 'request-top' }, h('span', { class: 'request-badge' }), h('span', { class: 'request-task faint' })),
    h('h3', { class: 'request-title' }), h('p', { class: 'request-body muted' }),
    h('div', { class: 'request-options' }),
    h('div', { class: 'request-free input-group' }, field, submit),
    h('p', { class: 'request-answer' }), slot);
  el.answer = answer;
  return el;
}

function updateRequest(el, request, state) {
  const open = request.status === 'open';
  el.dataset.status = request.status;
  el.querySelector('.request-badge').replaceChildren(pill(open ? 'Needs you' : 'Answered', open ? 'amber' : 'green'));
  setText(el.querySelector('.request-task'), request.task ? (state.plan.tasks[request.task]?.title || request.task) : '');
  setText(el.querySelector('.request-title'), request.title);
  setText(el.querySelector('.request-body'), request.body);
  const options = el.querySelector('.request-options');
  options.hidden = !open;
  el.querySelector('.request-free').hidden = !open;
  if (open && options.childElementCount !== (request.options || []).length) {
    options.replaceChildren(...(request.options || []).map((option) => {
      const button = h('button', { class: 'btn btn-sm request-option', type: 'button', text: option });
      button.addEventListener('click', () => el.answer(button, option));
      return button;
    }));
  }
  const answer = el.querySelector('.request-answer');
  answer.hidden = open;
  answer.replaceChildren(open ? '' : h('span', null, icon('check', { size: 12 }), ` ${request.answer || ''}`));
}
