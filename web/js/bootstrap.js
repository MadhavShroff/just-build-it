// Bootstrap: live status, detail and the bootstrap session's log lines.

import { h, reconcile, setText, clockTime, elapsed, shortSha } from './dom.js';
import { icon } from './icons.js';
import { act, errorSlot } from './ui.js';

const STATUS = {
  pending: ['Waiting to start', 'gray'], running: ['Setting up the repository', 'blue'],
  passed: ['Repository ready', 'green'], failed: ['Bootstrap failed', 'red'],
};
const CHECKS = ['Create the repository', 'Install the toolchain', 'Add linters and formatters', 'Write .jbi/commands.json', 'Build and lint in a fresh shell'];

export function mountBootstrap(ctx, root) {
  const ring = h('div', { class: 'boot-ring', 'aria-hidden': 'true' }, h('span'));
  const title = h('h2', { class: 'boot-title' });
  const detail = h('p', { class: 'boot-detail muted' });
  const meta = h('div', { class: 'boot-meta' });
  const checklist = h('ol', { class: 'boot-checks' }, CHECKS.map((text) => h('li', null, h('span', { class: 'boot-check-dot' }), text)));
  const log = h('ol', { class: 'boot-log mono', 'aria-live': 'polite', 'aria-label': 'Bootstrap log' });
  // Shown only after a failure: run bootstrap again, or go back and change the stack or folder.
  const slot = errorSlot();
  const retry = h('button', { class: 'btn btn-primary', type: 'button' }, icon('retry', { size: 14 }), 'Retry bootstrap');
  const back = h('button', { class: 'btn btn-ghost', type: 'button' }, icon('exit', { size: 14 }), 'Back to the stack');
  const recover = h('div', { class: 'boot-recover', hidden: true }, h('div', { class: 'boot-recover-actions' }, back, retry), slot);
  retry.addEventListener('click', () => act(retry, slot, () => ctx.api.send('bootstrap.retry', {})));
  back.addEventListener('click', () => act(back, slot, () => ctx.api.send('stack.back', {})));

  root.append(
    h('div', { class: 'phase-head' }, h('div', null,
      h('div', { class: 'eyebrow', text: 'Step 3 · Bootstrap' }),
      h('h1', { class: 'phase-title', text: 'Setting up the repository' }),
      h('p', { class: 'phase-sub', text: 'One agent creates the repository, the toolchain and the first build. It writes no feature code.' }))),
    h('div', { class: 'boot-grid' },
      h('section', { class: 'card boot-status' }, h('div', { class: 'boot-hero' }, ring, h('div', null, title, detail)), meta, checklist, recover),
      h('section', { class: 'card boot-terminal' },
        h('div', { class: 'terminal-bar' }, h('i'), h('i'), h('i'), h('span', { class: 'terminal-title mono' })),
        log)));

  ctx.store.subscribe(['bootstrap', 'run', 'sessions', 'timeline'], (state) => {
    const status = state.bootstrap.status || 'pending';
    const [label, tone] = STATUS[status] || [status, 'gray'];
    root.dataset.status = status;
    ring.dataset.tone = tone;
    setText(title, label);
    setText(detail, state.bootstrap.detail || 'The agent reports each step here.');
    recover.hidden = status !== 'failed';
    const session = Object.values(state.sessions).find((s) => s.role === 'bootstrap');
    meta.replaceChildren(...[
      chip('folder', state.run.target),
      session ? chip('terminal', session.model) : null,
      session ? chip('clock', elapsed(session.started, session.ended), 'tabular') : null,
      state.bootstrap.commit ? chip('check', shortSha(state.bootstrap.commit), 'mono') : null].filter(Boolean));
    setText(root.querySelector('.terminal-title'), session?.window || 'bootstrap');

    const lines = state.timeline.filter((line) => line.kind === 'bootstrap' || line.kind === 'phase');
    const done = status === 'passed' ? CHECKS.length : Math.min(CHECKS.length - 1, Math.floor(lines.length / 2));
    [...checklist.children].forEach((item, index) => {
      item.classList.toggle('is-done', index < done);
      item.classList.toggle('is-active', index === done && status === 'running');
    });
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    reconcile(log, lines, {
      key: (line) => line.seq,
      create: (line) => h('li', null, h('time', { text: clockTime(line.ts) }), h('span', { text: line.text })),
    });
    if (atBottom) log.scrollTop = log.scrollHeight;
  });
}

const chip = (name, text, extra = '') => (text ? h('span', { class: `chip ${extra}` }, icon(name, { size: 12 }), text) : null);
