// Settings popover: switch rigor and model profile mid-run (run.configure).

import { h } from './dom.js';
import { icon } from './icons.js';
import { popover } from './popover.js';
import { act, choices, errorSlot, payloadFor } from './ui.js';

export function createSettingsMenu(ctx) {
  const trigger = h('button', { class: 'btn btn-ghost btn-icon', type: 'button', 'aria-label': 'Run settings', 'data-tip': 'Rigor and models' }, icon('settings'));
  const menu = popover({ trigger, label: 'Run settings', panelClass: 'settings-popover', onOpen: render });
  const slot = errorSlot();

  function group(title, field, current, describe) {
    const options = choices(ctx.store.schema, 'run.configure', field);
    return h('div', { class: 'settings-group' },
      h('div', { class: 'popover-title', text: title }),
      h('div', { class: 'settings-options', role: 'radiogroup', 'aria-label': title },
        options.map((value) => h('button', {
          class: 'settings-option', type: 'button', role: 'radio', 'aria-checked': String(value === current),
          onclick: (event) => apply(event.currentTarget, { [field]: value }),
        }, h('span', { class: 'settings-option-name', text: describe(value).label }),
        h('span', { class: 'settings-option-desc', text: describe(value).desc })))));
  }

  function render() {
    const { config } = ctx.store.state;
    const detail = ctx.store.schema.rigor_detail;
    menu.panel.replaceChildren(
      group('Rigor', 'rigor', config.rigor, (v) => ({ label: detail.levels[v]?.label || v, desc: detail.levels[v]?.stages.join(' → ') })),
      h('div', { class: 'menu-sep' }),
      group('Model profile', 'profile', config.profile, (v) => ({ label: v[0].toUpperCase() + v.slice(1), desc: detail.profiles[v]?.label })),
      h('p', { class: 'settings-foot', text: 'Changes apply to the next stage that starts.' }),
      slot);
  }

  async function apply(control, change) {
    const { config } = ctx.store.state;
    const values = { rigor: config.rigor, profile: config.profile, ...change };
    await act(control, slot, () => ctx.api.send('run.configure', payloadFor(ctx.store.schema, 'run.configure', values)));
  }

  ctx.store.subscribe(['config'], () => { if (menu.isOpen) render(); });
  return menu;
}
