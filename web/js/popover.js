// A small accessible popover: a trigger button and a panel that closes on Escape or outside click.

import { h } from './dom.js';

const open = new Set();

export function popover({ trigger, panelClass = '', label, onOpen }) {
  const panel = h('div', { class: `popover ${panelClass}`, role: 'dialog', 'aria-label': label, 'data-open': 'false', hidden: true });
  const anchor = h('div', { class: 'popover-anchor' }, trigger, panel);
  trigger.setAttribute('aria-haspopup', 'dialog');
  trigger.setAttribute('aria-expanded', 'false');

  const api = {
    anchor, panel, trigger,
    get isOpen() { return panel.dataset.open === 'true'; },
    show() {
      for (const other of open) if (other !== api) other.hide();
      panel.hidden = false;
      onOpen?.(api);
      requestAnimationFrame(() => {
        panel.dataset.open = 'true';
        panel.querySelector('button, input, select, textarea')?.focus();
      });
      trigger.setAttribute('aria-expanded', 'true');
      open.add(api);
    },
    hide({ focus = false } = {}) {
      if (!api.isOpen) return;
      panel.dataset.open = 'false';
      trigger.setAttribute('aria-expanded', 'false');
      open.delete(api);
      setTimeout(() => { if (!api.isOpen) panel.hidden = true; }, 170);
      if (focus) trigger.focus();
    },
    toggle() { api.isOpen ? api.hide() : api.show(); },
  };
  trigger.addEventListener('click', (event) => { event.stopPropagation(); api.toggle(); });
  panel.addEventListener('click', (event) => event.stopPropagation());
  return api;
}

document.addEventListener('click', () => { for (const item of [...open]) item.hide(); });
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  for (const item of [...open]) item.hide({ focus: true });
});
