// Dry-run DAG preview UI: renders tasks arranged into waves (columns or rows),
// parallel task groups, the critical path, per-task and per-wave ETAs, and live
// task/wave statuses as the run progresses. No LLM call anywhere in this path.

import { h, setText } from './dom.js';
import { icon } from './icons.js';
import { TASK_TONE, act, errorSlot, pill } from './ui.js';

const STAGE_ETA_SECONDS = {
  implement: 180,
  acceptance: 30,
  verify: 60,
  review: 45,
  security: 45,
  merge: 60,
};
const DEEPSEEK_REVIEW_STAGES = new Set(['review-ds-1', 'review-ds-2', 'review-ds-3']);
const OWNS_FILE_ETA_SECONDS = 15;

const PIPELINES_BY_RIGOR = {
  vibe: [['implement', 'implement'], ['merge', 'merge']],
  medium: [
    ['implement', 'implement'], ['acceptance', 'acceptance'],
    ['review-cascade', 'review'], ['review-ds-1', 'review'], ['review-ds-2', 'review'], ['review-ds-3', 'review'],
    ['merge', 'merge'],
  ],
  high: [
    ['implement', 'implement'], ['acceptance', 'acceptance'], ['verify', 'verify'],
    ['review-cascade', 'review'], ['review-ds-1', 'review'], ['review-ds-2', 'review'], ['review-ds-3', 'review'],
    ['security', 'security'], ['test-audit', 'review'], ['methodology-audit', 'review'], ['dry-run', 'review'],
    ['merge', 'merge'],
  ],
  xhigh: [
    ['implement', 'implement'], ['acceptance', 'acceptance'], ['verify', 'verify'],
    ['review-cascade', 'review'], ['review-ds-1', 'review'], ['review-ds-2', 'review'], ['review-ds-3', 'review'],
    ['security', 'security'], ['test-audit', 'review'], ['methodology-audit', 'review'], ['dry-run', 'review'],
    ['merge', 'merge'],
  ],
};

export function formatEta(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const total = Math.max(0, Math.round(Number(seconds)));
  if (total === 0) return '0s';
  if (total < 60) return `${total}s`;
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (mins < 60) return secs ? `${mins}m ${secs}s` : `${mins}m`;
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  return remMins ? `${hours}h ${remMins}m` : `${hours}h`;
}

function stageWallSeconds(name, role) {
  if (DEEPSEEK_REVIEW_STAGES.has(name)) return 0;
  return STAGE_ETA_SECONDS[role] || 45;
}

export function estimateTaskEta(task, config = {}) {
  const rigor = task.override?.rigor || config.rigor || 'high';
  const stages = PIPELINES_BY_RIGOR[rigor] || PIPELINES_BY_RIGOR.high;
  const ownsBonus = Math.min(120, (task.owns || []).length * OWNS_FILE_ETA_SECONDS);
  const explicit = task.eta_s || task.estimated_seconds;
  const total = typeof explicit === 'number' && explicit > 0
    ? Math.round(explicit)
    : stages.reduce((sum, [name, role]) => sum + stageWallSeconds(name, role), 0) + ownsBonus;

  if (task.status === 'merged' || task.status === 'skipped') return { total, remaining: 0 };
  const recorded = task.stages || {};
  if (!Object.keys(recorded).length) return { total, remaining: total };

  let elapsedEst = 0;
  for (const [name, role] of stages) {
    let wall = stageWallSeconds(name, role);
    if (role === 'implement') wall += ownsBonus;
    if (recorded[name]?.status === 'passed') elapsedEst += wall;
  }
  return { total, remaining: Math.min(total, Math.max(5, total - elapsedEst)) };
}

function waveWallSeconds(taskIds, weights, maxSessions) {
  if (!taskIds.length) return 0;
  const vals = taskIds.map((id) => Math.max(0, weights[id] || 0)).sort((a, b) => b - a);
  const slots = Math.max(1, Number(maxSessions || 3));
  const loads = new Array(Math.min(slots, vals.length)).fill(0);
  for (const d of vals) {
    let minIdx = 0;
    for (let i = 1; i < loads.length; i += 1) {
      if (loads[i] < loads[minIdx]) minIdx = i;
    }
    loads[minIdx] += d;
  }
  return Math.max(0, ...loads);
}

export function computePreviewFromState(state) {
  const plan = state?.plan || {};
  const config = state?.config || {};
  const tasks = plan.tasks || {};
  const order = (plan.order || Object.keys(tasks)).filter((id) => tasks[id]);
  const rawWaves = (plan.waves && plan.waves.length) ? plan.waves : (order.length ? [order] : []);
  const maxSessions = Number(config.max_sessions || 3);

  const totalByTask = {};
  const remByTask = {};
  for (const tid of order) {
    const { total, remaining } = estimateTaskEta(tasks[tid], config);
    totalByTask[tid] = total;
    remByTask[tid] = remaining;
  }

  const topo = [];
  const seen = new Set();
  for (const wave of rawWaves) {
    for (const tid of wave) {
      if (tasks[tid] && !seen.has(tid)) { topo.push(tid); seen.add(tid); }
    }
  }
  for (const tid of order) {
    if (!seen.has(tid)) { topo.push(tid); seen.add(tid); }
  }

  function solveLongestPath(weights) {
    if (!topo.length) return { path: [], totalSeconds: 0, earliestStart: {}, earliestFinish: {}, slack: {} };
    const earliestStart = {};
    const earliestFinish = {};
    const pred = {};
    for (const tid of topo) {
      const deps = (tasks[tid].deps || []).filter((d) => earliestFinish[d] !== undefined);
      if (!deps.length) {
        earliestStart[tid] = 0;
        pred[tid] = null;
      } else {
        let best = deps[0];
        for (const d of deps.slice(1)) {
          if (earliestFinish[d] > earliestFinish[best]) best = d;
        }
        earliestStart[tid] = earliestFinish[best];
        pred[tid] = best;
      }
      earliestFinish[tid] = earliestStart[tid] + (weights[tid] || 0);
    }
    let endTid = topo[0];
    for (const tid of topo.slice(1)) {
      if (earliestFinish[tid] > earliestFinish[endTid]) endTid = tid;
    }
    const totalSeconds = earliestFinish[endTid] || 0;
    const path = [];
    let cur = endTid;
    while (cur) { path.push(cur); cur = pred[cur]; }
    path.reverse();
    return { path, totalSeconds, earliestStart, earliestFinish };
  }

  const cpTotal = solveLongestPath(totalByTask);
  const cpRem = solveLongestPath(remByTask);
  const cpSet = new Set(cpTotal.path);

  let cumulativeEta = 0;
  let cumulativeRem = 0;
  let wavesCompleted = 0;

  const waves = rawWaves.map((waveIds, idx) => {
    const number = idx + 1;
    const validIds = waveIds.filter((id) => tasks[id]);
    const mergedCount = validIds.filter((id) => tasks[id].status === 'merged').length;
    const activeCount = validIds.filter((id) => ['running', 'verifying', 'queued', 'merging'].includes(tasks[id].status)).length;
    let status = 'blocked';
    if (validIds.length && mergedCount === validIds.length) {
      status = 'merged';
      wavesCompleted += 1;
    } else if (activeCount > 0 || (mergedCount > 0 && mergedCount < validIds.length)) {
      status = 'running';
    } else if (validIds.some((id) => tasks[id].status === 'ready')) {
      status = 'ready';
    }

    const waveEta = waveWallSeconds(validIds, totalByTask, maxSessions);
    const waveRem = waveWallSeconds(validIds, remByTask, maxSessions);
    cumulativeEta += waveEta;
    cumulativeRem += waveRem;

    const waveTasks = validIds.map((tid) => {
      const t = tasks[tid];
      const peers = validIds.filter((other) => other !== tid);
      const myOwns = new Set(t.owns || []);
      const conflicts = peers.filter((other) => (tasks[other].owns || []).some((p) => myOwns.has(p)));
      const parallelWith = peers.filter((other) => !conflicts.includes(other));
      return {
        id: tid,
        title: t.title || tid,
        goal: t.goal || '',
        status: t.status || 'blocked',
        stage: t.stage || null,
        stages: t.stages || {},
        deps: t.deps || [],
        owns: t.owns || [],
        wave: number,
        attempts: t.attempts || 0,
        dry_run: Boolean(t.dry_run),
        eta_s: totalByTask[tid],
        remaining_eta_s: remByTask[tid],
        critical: cpSet.has(tid),
        parallel: parallelWith.length > 0,
        parallel_with: parallelWith,
        owns_conflict_with: conflicts,
      };
    });

    return {
      wave: number,
      status,
      task_ids: validIds,
      tasks: waveTasks,
      total_count: validIds.length,
      merged_count: mergedCount,
      parallel: validIds.length > 1,
      parallel_count: validIds.length,
      eta_s: waveEta,
      remaining_eta_s: waveRem,
      cumulative_eta_s: cumulativeEta,
      cumulative_remaining_eta_s: cumulativeRem,
    };
  });

  const mergedTasks = order.filter((id) => tasks[id].status === 'merged').length;
  const runningTasks = order.filter((id) => ['running', 'verifying', 'queued', 'merging'].includes(tasks[id].status)).length;

  return {
    dry_run: Boolean(config.dry_run),
    rigor: config.rigor || 'high',
    max_sessions: maxSessions,
    total_tasks: order.length,
    merged_tasks: mergedTasks,
    running_tasks: runningTasks,
    waves_total: waves.length,
    waves_completed: wavesCompleted,
    critical_path: cpTotal.path,
    critical_path_eta_s: cpTotal.totalSeconds,
    remaining_eta_s: cpRem.totalSeconds,
    waves,
  };
}

export function mountPreview(ctx, root, { onOpenTask } = {}) {
  let layout = 'columns';
  let lastPreview = null;
  let lastRunName = 'DAG Preview';

  const eyebrow = h('div', { class: 'eyebrow', text: 'Dry-run execution plan · Live DAG preview' });
  const title = h('h1', { class: 'phase-title', text: 'Execution DAG Preview' });
  const sub = h('p', { class: 'phase-sub', text: 'Waves, parallel task groups, critical path, and live ETAs.' });
  const slot = errorSlot();

  const colBtn = h('button', {
    class: 'segment', type: 'button', 'aria-pressed': 'true',
    onclick: () => { layout = 'columns'; updateLayoutToggle(); if (lastPreview) render(lastPreview, lastRunName); },
  }, icon('dag', { size: 14 }), 'Columns');
  const rowBtn = h('button', {
    class: 'segment', type: 'button', 'aria-pressed': 'false',
    onclick: () => { layout = 'rows'; updateLayoutToggle(); if (lastPreview) render(lastPreview, lastRunName); },
  }, icon('list', { size: 14 }), 'Rows');
  const layoutToggle = h('div', { class: 'segmented', role: 'group', 'aria-label': 'Wave layout' }, colBtn, rowBtn);

  const startBtn = h('button', {
    class: 'btn btn-sm btn-primary', type: 'button', hidden: true,
    onclick: () => act(startBtn, slot, () => ctx.api.send('run.start', {})),
  }, icon('play', { size: 13 }), 'Start rehearsal');

  const summaryTiles = h('div', { class: 'preview-tiles' });
  const criticalStrip = h('section', { class: 'card preview-cp-card' });
  const wavesContainer = h('div', { class: 'preview-waves is-columns' });

  root.replaceChildren(
    h('div', { class: 'preview-head' },
      h('div', { class: 'board-title' }, eyebrow, title, sub),
      h('div', { class: 'preview-actions' }, layoutToggle, startBtn)),
    slot,
    summaryTiles,
    criticalStrip,
    wavesContainer);

  function updateLayoutToggle() {
    colBtn.setAttribute('aria-pressed', String(layout === 'columns'));
    rowBtn.setAttribute('aria-pressed', String(layout === 'rows'));
    wavesContainer.classList.toggle('is-columns', layout === 'columns');
    wavesContainer.classList.toggle('is-rows', layout === 'rows');
  }

  function render(preview, runName = 'DAG Preview') {
    if (!preview) return;
    lastPreview = preview;
    lastRunName = runName;

    setText(eyebrow, preview.dry_run ? 'Dry-run rehearsal · Live DAG preview' : 'Execution plan · Live DAG preview');
    setText(title, `${runName} — Execution DAG`);
    setText(sub, `${preview.total_tasks} tasks across ${preview.waves_total} waves · max ${preview.max_sessions} parallel sessions · rigor ${preview.rigor}`);

    const canStart = preview.total_tasks > 0 && preview.merged_tasks === 0 && preview.running_tasks === 0;
    startBtn.hidden = !canStart;

    summaryTiles.replaceChildren(
      h('div', { class: 'tile' },
        h('span', { class: 'tile-label' }, icon('layers', { size: 13 }), 'Waves complete'),
        h('span', { class: 'tile-value tabular', text: `${preview.waves_completed} / ${preview.waves_total}` })),
      h('div', { class: 'tile' },
        h('span', { class: 'tile-label' }, icon('check', { size: 13 }), 'Tasks merged'),
        h('span', { class: 'tile-value tabular', text: `${preview.merged_tasks} / ${preview.total_tasks}` })),
      h('div', { class: 'tile' },
        h('span', { class: 'tile-label' }, icon('bolt', { size: 13 }), 'Critical path ETA'),
        h('span', { class: 'tile-value tabular', text: formatEta(preview.critical_path_eta_s) })),
      h('div', { class: 'tile' },
        h('span', { class: 'tile-label' }, icon('clock', { size: 13 }), 'Remaining ETA'),
        h('span', { class: 'tile-value tabular', text: formatEta(preview.remaining_eta_s) })),
    );

    const taskMap = new Map();
    for (const w of preview.waves || []) {
      for (const t of w.tasks || []) taskMap.set(t.id, t);
    }

    const cpIds = preview.critical_path || [];
    criticalStrip.replaceChildren(
      h('div', { class: 'card-head' },
        h('h2', { class: 'card-title' }, icon('bolt'), 'Critical path',
          h('span', { class: 'card-count tabular', text: `${cpIds.length} tasks · ${formatEta(preview.critical_path_eta_s)} total · ${formatEta(preview.remaining_eta_s)} remaining` }))),
      cpIds.length
        ? h('div', { class: 'preview-cp-chain' },
            ...cpIds.flatMap((tid, index) => {
              const t = taskMap.get(tid) || { id: tid, title: tid, status: 'blocked', eta_s: 0, remaining_eta_s: 0 };
              const node = h('button', {
                class: 'preview-cp-node', type: 'button',
                'data-status': t.status,
                onclick: () => onOpenTask && onOpenTask(tid),
              },
                h('span', { class: 'mono', text: tid }),
                pill(t.status, TASK_TONE[t.status] || 'gray'),
                h('span', { class: 'faint tabular', text: t.status === 'merged' ? 'done' : `ETA ${formatEta(t.remaining_eta_s)}` }));
              return index === 0 ? [node] : [h('span', { class: 'preview-cp-arrow faint', text: '→' }), node];
            }))
        : h('p', { class: 'empty', text: 'No tasks in the DAG.' }),
    );

    if (!(preview.waves || []).length) {
      wavesContainer.replaceChildren(h('p', { class: 'empty', text: 'Load a DAG file to preview waves and ETAs.' }));
      return;
    }

    wavesContainer.replaceChildren(
      ...(preview.waves || []).map((wave) => renderWave(wave, onOpenTask)),
    );
  }

  function renderWave(wave, openTask) {
    const waveTone = wave.status === 'merged' ? 'green' : wave.status === 'running' ? 'blue' : wave.status === 'ready' ? 'teal' : 'gray';
    const parallelLabel = wave.parallel ? `${wave.parallel_count} parallel tasks` : '1 task';
    const etaLabel = wave.status === 'merged'
      ? `Completed (${formatEta(wave.eta_s)})`
      : `Wave ETA ${formatEta(wave.eta_s)} · rem ${formatEta(wave.remaining_eta_s)}`;

    return h('section', { class: 'card preview-wave', 'data-status': wave.status },
      h('div', { class: 'preview-wave-head' },
        h('div', { class: 'preview-wave-title' },
          h('span', { class: 'preview-wave-num', text: `Wave ${wave.wave}` }),
          pill(`${wave.merged_count}/${wave.total_count} ${wave.status}`, waveTone),
          pill(parallelLabel, wave.parallel ? 'violet' : 'gray', { dot: false })),
        h('div', { class: 'preview-wave-eta mono tabular', text: etaLabel })),
      h('div', { class: 'preview-wave-tasks' },
        ...(wave.tasks || []).map((t) => renderTaskCard(t, openTask))));
  }

  function renderTaskCard(t, openTask) {
    const tone = TASK_TONE[t.status] || 'gray';
    const stageEntries = Object.entries(t.stages || {});
    const etaText = t.status === 'merged'
      ? `done (est ${formatEta(t.eta_s)})`
      : `ETA ${formatEta(t.eta_s)} · rem ${formatEta(t.remaining_eta_s)}`;

    return h('article', {
      class: `preview-task${t.critical ? ' is-critical' : ''}`,
      'data-status': t.status,
      'data-tone': tone,
      onclick: () => openTask && openTask(t.id),
    },
      h('div', { class: 'preview-task-top' },
        h('span', { class: 'mono preview-task-id', text: t.id }),
        t.critical ? pill('Critical path', 'amber', { dot: false }) : null,
        t.dry_run ? pill('dry-run', 'teal', { dot: false }) : null,
        pill(t.status, tone)),
      h('h3', { class: 'preview-task-title', text: t.title }),
      h('div', { class: 'preview-task-meta mono faint' },
        h('span', { class: 'tabular', text: etaText }),
        t.deps?.length ? h('span', { text: `deps: ${t.deps.join(', ')}` }) : h('span', { text: 'no deps' }),
        t.parallel_with?.length ? h('span', { text: `parallel with: ${t.parallel_with.join(', ')}` }) : null,
        t.owns_conflict_with?.length ? h('span', { text: `owns lock with: ${t.owns_conflict_with.join(', ')}` }) : null),
      stageEntries.length
        ? h('div', { class: 'task-stages' },
            ...stageEntries.map(([sname, srec]) => h('i', { 'data-status': srec.status, title: `${sname}: ${srec.status}` })))
        : null);
  }

  ctx.store.subscribe(['plan', 'config', 'sessions', 'run'], (state) => {
    if (!state?.plan) return;
    render(computePreviewFromState(state), state.run?.name || 'DAG Preview');
  });

  async function refresh() {
    if (ctx.store.state?.plan?.order?.length) {
      render(computePreviewFromState(ctx.store.state), ctx.store.state.run?.name || 'DAG Preview');
      return;
    }
    try {
      const response = await ctx.api.preview();
      if (response?.preview) {
        render(response.preview, response.run?.name || 'DAG Preview');
      }
    } catch {
      // ignore offline preview fetch error when SSE store will populate
    }
  }

  return { refresh, render };
}
