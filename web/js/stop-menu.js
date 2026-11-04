// The stop menu: four stop modes, a confirm step for quit and kill, and the stopping state.

import { h, setText, relativeTime } from './dom.js';
import { icon } from './icons.js';
import { popover } from './popover.js';
import { act, choices, errorSlot, payloadFor } from './ui.js';

const MODES = {
  quit: { label: 'Quit', icon: 'power', desc: 'Stop now. Agents get SIGTERM; unsaved work can be lost.', confirm: true },
  standdown: { label: 'Stand down', icon: 'pause', desc: 'Commit work in progress, pause each task, then exit.' },
  finish: { label: 'Finish tasks then stop', icon: 'flag', desc: 'Start nothing new. Let running tasks merge, then exit.' },
  kill: { label: 'Kill', icon: 'skull', desc: 'SIGKILL the CLI, the tmux server and every child.', confirm: true, danger: true },
};

export function createStopMenu(ctx) {
  const label = h('span', { class: 'stop-label', text: 'Stop' });
  const trigger = h('button', { class: 'btn btn-sm stop-trigger', type: 'button' }, icon('stop', { size: 14 }), label);
  const menu = popover({ trigger, label: 'Stop the run', panelClass: 'stop-popover', onOpen: () => renderList() });
  const slot = errorSlot();

  function renderList() {
    const modes = choices(ctx.store.schema, 'run.stop', 'mode');
    menu.panel.replaceChildren(
      h('div', { class: 'popover-title', text: 'Stop the run' }),
      ...modes.map((mode) => {
        const info = MODES[mode] || { label: mode, icon: 'stop', desc: '' };
        return h('button', {
          class: `menu-item${info.danger ? ' is-danger' : ''}`, type: 'button',
          onclick: () => (info.confirm ? renderConfirm(mode, info) : stop(mode)),
        },
        h('span', { class: 'menu-icon' }, icon(info.icon)),
        h('span', null, h('span', { class: 'menu-label', text: info.label }), h('span', { class: 'menu-desc', text: info.desc })));
      }),
      slot);
  }

  function renderConfirm(mode, info) {
    const go = h('button', { class: 'btn btn-sm btn-danger-solid', type: 'button', onclick: () => stop(mode, go) }, `${info.label} now`);
    menu.panel.replaceChildren(h('div', { class: 'confirm' },
      h('strong', { text: `${info.label} the run?` }),
      h('p', { class: 'confirm-text', text: info.desc }),
      h('div', { class: 'confirm-actions' },
        h('button', { class: 'btn btn-sm', type: 'button', onclick: renderList }, 'Cancel'), go),
      slot));
    go.focus();
  }

  async function stop(mode, control) {
    const reply = await act(control, slot, () => ctx.api.send('run.stop', payloadFor(ctx.store.schema, 'run.stop', { mode })));
    if (reply) menu.hide({ focus: true });
  }

  ctx.store.subscribe(['run'], (state) => {
    const stopping = state.run.stopping;
    trigger.classList.toggle('is-stopping', Boolean(stopping));
    setText(label, stopping ? `Stopping · ${MODES[stopping.mode]?.label || stopping.mode}` : 'Stop');
    trigger.title = stopping ? `Since ${relativeTime(stopping.since)}` : '';
  });

  return menu;
}
