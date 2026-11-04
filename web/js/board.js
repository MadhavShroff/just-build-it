// The main board (execute and complete): summary, DAG and architecture tabs, sessions,
// CI lanes, telemetry, decisions and the live timeline.

import { h, reconcile, setText, beat, elapsed, relativeTime, compactNumber, money, shortSha, trackOverflow } from './dom.js';
import { icon } from './icons.js';
import { mountDag } from './dag.js';
import { mountArchitecture } from './architecture.js';
import { LANE_TONE, TASK_TONE, act, errorSlot, payloadFor, pill, toast } from './ui.js';

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
  const main = h('section', { class: 'card board-main' }, h('div', { class: 'board-main-head' }, views, counts), dagPane, archPane);

  const sessions = h('div', { class: 'sessions', tabindex: '0', role: 'group', 'aria-label': 'Agent sessions' });
  const sessionEdges = trackOverflow(sessions);   // edge fades while more sessions sit off screen
  const tmux = h('code', { class: 'tmux-cmd' });
  const copy = h('button', { class: 'btn btn-sm btn-icon', type: 'button', 'aria-label': 'Copy the tmux attach command', 'data-tip': 'Copy' }, icon('copy', { size: 13 }));
  const lanes = h('div', { class: 'lanes' });
  const decisions = h('div', { class: 'decisions' });
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
        h('section', { class: 'card decisions-card' }, h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, icon('inbox'), 'Decisions', h('span', { class: 'card-count decisions-count' }))), decisions),
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, icon('lane'), 'CI lanes', h('span', { class: 'card-count lanes-count' }))), lanes),
        h('section', { class: 'card timeline-card' }, h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, icon('clock'), 'Timeline')), timeline))));

  // ---- view tabs ------------------------------------------------------------------
  const tabs = [['dag', 'Task graph', 'dag'], ['architecture', 'Architecture', 'graph']];
  for (const [id, label, iconName] of tabs) {
    views.append(h('button', { class: 'segment', type: 'button', role: 'tab', 'data-view': id, onclick: () => ctx.store.setLocal({ board: id }) }, icon(iconName, { size: 14 }), label));
  }
  const dag = mountDag(ctx, dagPane, { onOpenTask });
  dag.controls.classList.add('dag-zoom');
  main.firstChild.append(dag.controls);   // the task graph zoom sits in the board head
  const arch = mountArchitecture(ctx, archPane, { onOpenTask });
  ctx.store.subscribe(['$board'], () => {
    const view = ctx.store.local.board;
    for (const button of views.children) button.setAttribute('aria-selected', String(button.dataset.view === view));
    dagPane.hidden = view !== 'dag';
    archPane.hidden = view !== 'architecture';
    dag.controls.hidden = view !== 'dag';
    requestAnimationFrame(view === 'architecture' ? arch.fit : dag.fit);
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

  // ---- lanes --------------------------------------------------------------------
  ctx.store.subscribe(['lanes', 'plan'], (state) => {
    const list = Object.entries(state.lanes).map(([id, lane]) => ({ id, ...lane })).sort((a, b) => a.id.localeCompare(b.id, undefined, { numeric: true }));
    setText(root.querySelector('.lanes-count'), list.length ? String(list.length) : '');
    if (!list.length) { lanes.replaceChildren(h('p', { class: 'empty', text: 'No lane is configured.' })); return; }
    if (lanes.querySelector('.empty')) lanes.replaceChildren();
    reconcile(lanes, list, {
      key: (lane) => lane.id,
      create: () => h('div', { class: 'lane' },
        h('div', { class: 'lane-top' }, h('span', { class: 'lane-name' }), h('span', { class: 'lane-task' }), h('span', { class: 'lane-status' })),
        h('div', { class: 'lane-shas mono' }), h('div', { class: 'lane-checks' }), h('div', { class: 'lane-detail faint' })),
      update: (el, lane) => {
        el.dataset.status = lane.status;
        setText(el.querySelector('.lane-name'), lane.id.replace(/^lane-/, 'Lane '));
        setText(el.querySelector('.lane-task'), lane.task ? (state.plan.tasks[lane.task]?.title || lane.task) : 'Idle');
        const status = el.querySelector('.lane-status');
        if (status.dataset.value !== lane.status) {
          status.dataset.value = lane.status;
          status.replaceChildren(pill(lane.status, LANE_TONE[lane.status]));
        }
        setText(el.querySelector('.lane-shas'), lane.base || lane.head ? `${shortSha(lane.base)} ← ${shortSha(lane.head)}` : '');
        const checks = lane.checks || [];
        reconcile(el.querySelector('.lane-checks'), checks.map((c) => (typeof c === 'string' ? { name: c, status: 'pending' } : c)), {
          key: (c) => c.name, enter: false,
          create: () => h('span', { class: 'check' }, h('i'), h('span')),
          update: (chip, c) => { chip.dataset.status = c.status || 'pending'; setText(chip.lastChild, c.name); },
        });
        setText(el.querySelector('.lane-detail'), lane.detail || '');
      },
    });
  });

  // ---- decisions ----------------------------------------------------------------
  ctx.store.subscribe(['decisions', 'plan'], (state) => {
    const list = Object.entries(state.decisions).map(([id, d]) => ({ id, ...d }))
      .sort((a, b) => (a.status === 'open' ? 0 : 1) - (b.status === 'open' ? 0 : 1));
    const open = list.filter((d) => d.status === 'open').length;
    const count = root.querySelector('.decisions-count');
    setText(count, open ? `${open} open` : '');
    count.classList.toggle('is-alert', open > 0);
    if (!list.length) { decisions.replaceChildren(h('p', { class: 'empty', text: 'Nothing needs your answer.' })); return; }
    if (decisions.querySelector('.empty')) decisions.replaceChildren();
    reconcile(decisions, list, {
      key: (d) => d.id,
      create: (d) => createDecision(ctx, d),
      update: (el, d) => updateDecision(el, d, state),
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

function createDecision(ctx, decision) {
  const slot = errorSlot();
  const field = h('input', { class: 'input', placeholder: 'Or write an answer', 'aria-label': `Answer: ${decision.title}` });
  const submit = h('button', { class: 'btn btn-sm btn-primary', type: 'button' }, 'Answer');
  const answer = (control, value) => act(control, slot, () => ctx.api.send('decision.answer',
    payloadFor(ctx.store.schema, 'decision.answer', { decision: decision.id, answer: value })));
  submit.addEventListener('click', () => (field.value.trim() ? answer(submit, field.value) : slot.show('Write an answer or pick an option.')));
  field.addEventListener('keydown', (event) => { if (event.key === 'Enter') submit.click(); });
  const el = h('article', { class: 'decision' },
    h('div', { class: 'decision-top' }, h('span', { class: 'decision-badge' }), h('span', { class: 'decision-task faint' })),
    h('h3', { class: 'decision-title' }), h('p', { class: 'decision-body muted' }),
    h('div', { class: 'decision-options' }),
    h('div', { class: 'decision-free input-group' }, field, submit),
    h('p', { class: 'decision-answer' }), slot);
  el.answer = answer;
  return el;
}

function updateDecision(el, decision, state) {
  const open = decision.status === 'open';
  el.dataset.status = decision.status;
  el.querySelector('.decision-badge').replaceChildren(pill(open ? 'Needs you' : 'Answered', open ? 'amber' : 'green'));
  setText(el.querySelector('.decision-task'), decision.task ? (state.plan.tasks[decision.task]?.title || decision.task) : '');
  setText(el.querySelector('.decision-title'), decision.title);
  setText(el.querySelector('.decision-body'), decision.body);
  const options = el.querySelector('.decision-options');
  options.hidden = !open;
  el.querySelector('.decision-free').hidden = !open;
  if (open && options.childElementCount !== (decision.options || []).length) {
    options.replaceChildren(...(decision.options || []).map((option) => {
      const button = h('button', { class: 'btn btn-sm decision-option', type: 'button', text: option });
      button.addEventListener('click', () => el.answer(button, option));
      return button;
    }));
  }
  const answer = el.querySelector('.decision-answer');
  answer.hidden = open;
  answer.replaceChildren(open ? '' : h('span', null, icon('check', { size: 12 }), ` ${decision.answer || ''}`));
}
