// The task drawer: goal, acceptance, owned paths, the stage pipeline, attempts, model,
// commit, receipt, overrides (task.override) and retry / pause / resume.

import { h, setText, shortSha } from './dom.js';
import { icon } from './icons.js';
import { STAGE_TONE, TASK_TONE, act, choices, closeDrawer, errorSlot, isDrawerOpen, payloadFor, pill, showDrawer } from './ui.js';

export function mountTaskDrawer(ctx) {
  const drawer = document.getElementById('task-drawer');

  function open(id) {
    ctx.store.setLocal({ selectedTask: id });
    showDrawer(drawer, { onClose: () => ctx.store.setLocal({ selectedTask: null }) });
  }

  // The override form keeps its own DOM while the same task stays open, so a live
  // update never discards what the user is typing.
  let shown = { id: null, form: null, fingerprint: '' };

  function render(state) {
    const id = ctx.store.local.selectedTask;
    const task = id && state.plan.tasks[id];
    if (!task) { if (isDrawerOpen(drawer)) closeDrawer(); return; }
    const session = Object.values(state.sessions).find((s) => s.task === id && s.status === 'running');
    const fingerprint = JSON.stringify([task, session, state.run.phase, state.config]);
    if (shown.id === id && shown.fingerprint === fingerprint) return;
    if (shown.id !== id) shown = { id, form: overrides(ctx, state, task), fingerprint: '' };
    shown.fingerprint = fingerprint;
    const focusedName = document.activeElement?.dataset?.focusKey;
    const live = body(ctx, state, task);
    live.append(shown.form);
    drawer.replaceChildren(head(task), live);
    if (focusedName) drawer.querySelector(`[data-focus-key="${focusedName}"]`)?.focus();
  }

  ctx.store.subscribe(['plan', 'sessions', '$selectedTask', 'run'], (state) => { if (ctx.store.local.selectedTask) render(state); });
  return { open };
}

function head(task) {
  return h('header', { class: 'drawer-head' },
    h('div', { class: 'grow' },
      h('div', { class: 'task-drawer-meta' }, h('span', { class: 'mono faint', text: task.id }), pill(task.status, TASK_TONE[task.status]), h('span', { class: 'faint', text: `Wave ${task.wave}` })),
      h('h2', { class: 'drawer-title', id: 'task-title', text: task.title })),
    h('button', { class: 'btn btn-ghost btn-icon', type: 'button', 'aria-label': 'Close', onclick: closeDrawer }, icon('close')));
}

function body(ctx, state, task) {
  const schema = ctx.store.schema;
  const session = Object.values(state.sessions).find((s) => s.task === task.id && s.status === 'running');
  const stages = Object.entries(task.stages || {});
  const facts = [
    ['Attempts', String(task.attempts ?? 0)],
    ['Model', session?.model || task.model || '—'],
    ['Stage', task.stage || '—'],
    ['Commit', shortSha(task.commit)],
    ['Branch', task.branch || '—'],
  ];

  const controlSlot = errorSlot();
  const control = (type, label, iconName, enabled) => {
    const button = h('button', { class: 'btn btn-sm', type: 'button', disabled: !enabled || state.run.phase !== 'execute', 'data-focus-key': type }, icon(iconName, { size: 13 }), label);
    button.addEventListener('click', () => act(button, controlSlot, () => ctx.api.send(type, payloadFor(schema, type, { task: task.id }))));
    return button;
  };

  return h('div', { class: 'drawer-body' },
    h('section', null, h('div', { class: 'drawer-section-title', text: 'Goal' }), h('p', { class: 'task-goal', text: task.goal })),
    h('dl', { class: 'facts' }, facts.map(([k, v]) => h('div', null, h('dt', { text: k }), h('dd', { class: 'mono', text: v })))),
    h('section', null, h('div', { class: 'drawer-section-title', text: 'Stage pipeline' }),
      stages.length ? h('ol', { class: 'pipeline' }, stages.map(([name, stage]) => h('li', { class: 'pipe', 'data-status': stage.status },
        h('span', { class: 'pipe-dot' }, stage.status === 'passed' ? icon('check', { size: 11 }) : stage.status === 'failed' ? icon('x', { size: 11 }) : null),
        h('span', { class: 'pipe-main' }, h('span', { class: 'pipe-name', text: name }), stage.detail ? h('span', { class: 'pipe-detail', text: stage.detail }) : null),
        h('span', { class: 'pipe-side' }, stage.model ? h('span', { class: 'mono faint', text: stage.model }) : null, pill(stage.status, STAGE_TONE[stage.status], { dot: false })))))
        : h('p', { class: 'empty', text: 'Stages appear when the task starts.' })),
    h('section', null, h('div', { class: 'drawer-section-title', text: 'Acceptance' }), h('pre', { class: 'code-block', text: task.acceptance })),
    task.owns?.length ? h('section', null, h('div', { class: 'drawer-section-title', text: 'Owns' }), h('div', { class: 'owns' }, task.owns.map((p) => h('code', { class: 'chip mono', text: p })))) : null,
    task.deps?.length ? h('section', null, h('div', { class: 'drawer-section-title', text: 'Depends on' }), h('div', { class: 'owns' }, task.deps.map((d) => h('span', { class: 'chip' }, h('span', { class: `dot tone-${TASK_TONE[state.plan.tasks[d]?.status] || 'gray'}` }), state.plan.tasks[d]?.title || d)))) : null,
    task.receipt ? h('section', null, h('div', { class: 'drawer-section-title', text: 'Receipt' }), h('pre', { class: 'code-block receipt', text: typeof task.receipt === 'string' ? task.receipt : JSON.stringify(task.receipt, null, 2) })) : null,
    h('section', null, h('div', { class: 'drawer-section-title', text: 'Control' }),
      h('div', { class: 'row-wrap' },
        control('task.retry', 'Retry', 'retry', ['failed', 'paused', 'decision'].includes(task.status)),
        control('task.pause', 'Pause', 'pause', ['running', 'verifying', 'queued', 'ready', 'blocked'].includes(task.status)),
        control('task.resume', 'Resume', 'play', task.status === 'paused')),
      controlSlot));
}

function overrides(ctx, state, task) {
  const schema = ctx.store.schema;
  const current = task.override || {};
  const slot = errorSlot();
  const select = (field, label) => h('label', { class: 'field' }, h('span', { class: 'field-label', text: label }),
    h('select', { class: 'select', 'data-field': field, 'data-focus-key': `override-${field}` },
      h('option', { value: '', text: `Run default (${state.config[field]})` }),
      choices(schema, 'task.override', field).map((value) => h('option', { value, text: value, selected: current[field] === value }))));
  const model = h('input', { class: 'input mono', 'data-field': 'model', value: current.model || '', placeholder: 'Profile default', 'data-focus-key': 'override-model' });
  const save = h('button', { class: 'btn btn-sm btn-primary', type: 'button' }, 'Apply override');
  const form = h('section', { class: 'override' },
    h('div', { class: 'drawer-section-title', text: 'Override for this task' }),
    h('div', { class: 'override-grid' }, select('rigor', 'Rigor'), select('profile', 'Profile'),
      h('label', { class: 'field' }, h('span', { class: 'field-label', text: 'Model' }), model)),
    h('div', { class: 'row-end' }, save), slot);
  save.addEventListener('click', () => {
    const values = { task: task.id };
    for (const input of form.querySelectorAll('[data-field]')) values[input.dataset.field] = input.value;
    return act(save, slot, () => ctx.api.send('task.override', payloadFor(schema, 'task.override', values)));
  });
  return form;
}
