// Header: run name, phase stepper, connection dot, settings, accounts, theme and stop menu.

import { h, setText } from './dom.js';
import { icon } from './icons.js';

const PHASE_LABELS = { intake: 'Plan', stack: 'Stack', bootstrap: 'Bootstrap', configure: 'Configure', execute: 'Build', complete: 'Done' };
const CONNECTION_LABELS = { live: 'Live', connecting: 'Connecting', offline: 'Offline', unauthenticated: 'Signed out' };

export function mountHeader(ctx, { onAccounts, onTheme, settingsMenu, stopMenu }) {
  const runName = document.getElementById('run-name');
  const stepper = document.getElementById('stepper');
  const actions = document.getElementById('header-actions');

  const conn = h('span', { class: 'conn', 'data-state': 'connecting', role: 'status' },
    h('span', { class: 'conn-dot', 'aria-hidden': 'true' }), h('span', { class: 'conn-label', text: 'Connecting' }));
  const themeButton = h('button', { class: 'btn btn-ghost btn-icon', type: 'button', 'aria-label': 'Toggle theme', 'data-tip': 'Theme', onclick: onTheme }, icon('moon'));
  const accountsButton = h('button', { class: 'btn btn-ghost btn-icon', type: 'button', 'aria-label': 'Accounts', 'data-tip': 'Accounts', onclick: onAccounts }, icon('key'));
  actions.append(conn, settingsMenu.anchor, accountsButton, themeButton, stopMenu.anchor);

  ctx.store.subscribe(['run'], (state) => {
    setText(runName, state.run.name || 'Untitled run');
    document.title = `${state.run.name || 'Run'} · just-build-it`;
    renderStepper(ctx, stepper, state.run.phase);
    settingsMenu.anchor.hidden = !['configure', 'execute'].includes(state.run.phase);
  });

  ctx.store.subscribe(['$connection'], () => {
    const value = ctx.store.local.connection;
    conn.dataset.state = value;
    setText(conn.querySelector('.conn-label'), CONNECTION_LABELS[value] || value);
  });

  return { setThemeIcon: (dark) => themeButton.replaceChildren(icon(dark ? 'sun' : 'moon')) };
}

function renderStepper(ctx, stepper, phase) {
  const phases = ctx.store.schema.phases;
  const current = phases.indexOf(phase);
  if (!stepper.childElementCount) {
    phases.forEach((name, index) => stepper.append(
      h('span', { class: 'step', 'data-phase': name },
        h('span', { class: 'step-dot', text: String(index + 1) }),
        h('span', { class: 'step-label', text: PHASE_LABELS[name] || name }))));
  }
  [...stepper.children].forEach((step, index) => {
    const done = index < current || phase === 'complete';
    step.classList.toggle('is-done', done);
    step.classList.toggle('is-current', index === current && phase !== 'complete');
    step.toggleAttribute('aria-current', index === current);
    const dot = step.querySelector('.step-dot');
    if (done && !dot.querySelector('.icon')) dot.replaceChildren(icon('check'));
    if (!done && dot.querySelector('.icon')) dot.textContent = String(index + 1);
  });
}
