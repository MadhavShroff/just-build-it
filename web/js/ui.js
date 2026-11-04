// Shared view helpers: command actions with inline errors, payloads from the schema,
// drawers with focus handling, toasts and status vocabularies.

import { h, leave } from './dom.js';

/** Build a payload that holds only the fields the schema lists for `type`. */
export function payloadFor(schema, type, values) {
  const spec = schema?.commands?.[type]?.payload || {};
  const payload = {};
  for (const [name, field] of Object.entries(spec)) {
    const value = values[name];
    if (value === undefined || value === null || (value === '' && !field.required)) continue;
    payload[name] = typeof value === 'string' ? value.trim() : value;
  }
  return payload;
}

/** The allowed values of one enum field, read from the schema. */
export const choices = (schema, type, field) => schema?.commands?.[type]?.payload?.[field]?.enum || [];

/** An inline error slot that sits next to the control that caused the error. */
export function errorSlot() {
  const slot = h('div', { class: 'error-slot', 'aria-live': 'polite' });
  slot.show = (message) => {
    slot.replaceChildren(message ? h('p', { class: 'error-inline', role: 'alert', text: message }) : '');
  };
  slot.clear = () => slot.show('');
  return slot;
}

/**
 * Run a command from a control. The control shows a busy state; a server error appears
 * in `slot` as error.message. Resolves with the reply or null.
 */
export async function act(control, slot, task) {
  slot?.clear();
  control?.classList.add('is-busy');
  control?.setAttribute('aria-busy', 'true');
  try {
    return await task();
  } catch (error) {
    if (slot) slot.show(error.message || 'The command failed.');
    else toast(error.message || 'The command failed.', { error: true });
    return null;
  } finally {
    control?.classList.remove('is-busy');
    control?.removeAttribute('aria-busy');
  }
}

export function toast(message, { error = false, ms = 3200 } = {}) {
  const host = document.getElementById('toasts');
  const item = h('div', { class: `toast${error ? ' is-error' : ''}`, text: message });
  host.append(item);
  setTimeout(() => leave(item), ms);
}

// ---- drawers -------------------------------------------------------------------

let openDrawer = null;

export function showDrawer(drawer, { onClose } = {}) {
  if (openDrawer && openDrawer.drawer !== drawer) closeDrawer();
  const scrim = document.getElementById('scrim');
  const opener = document.activeElement;
  drawer.hidden = false;
  drawer.classList.remove('is-closing');
  scrim.hidden = false;
  scrim.onclick = () => closeDrawer();
  openDrawer = { drawer, opener, onClose };
  requestAnimationFrame(() => (drawer.querySelector('[data-autofocus]') || drawer.querySelector('button, input, textarea'))?.focus());
}

export function closeDrawer() {
  if (!openDrawer) return;
  const { drawer, opener, onClose } = openDrawer;
  openDrawer = null;
  document.getElementById('scrim').hidden = true;
  drawer.classList.add('is-closing');
  setTimeout(() => { drawer.hidden = true; drawer.classList.remove('is-closing'); }, 200);
  onClose?.();
  opener?.focus?.();
}

export const isDrawerOpen = (drawer) => openDrawer?.drawer === drawer;

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && openDrawer) {
    event.preventDefault();
    closeDrawer();
  }
  if (event.key === 'Tab' && openDrawer) trapFocus(event, openDrawer.drawer);
});

function trapFocus(event, container) {
  const focusable = [...container.querySelectorAll('button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])')]
    .filter((el) => !el.disabled && el.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

// ---- status vocabularies -------------------------------------------------------

export const TASK_TONE = {
  blocked: 'gray', ready: 'teal', running: 'blue', verifying: 'violet', queued: 'amber',
  merging: 'violet', merged: 'green', failed: 'red', paused: 'amber', decision: 'amber', skipped: 'gray',
};
export const NODE_TONE = { planned: 'gray', building: 'blue', built: 'green', failed: 'red' };
export const ACCOUNT_TONE = { ok: 'green', failed: 'red', missing: 'amber', unknown: 'gray' };
export const LANE_TONE = { idle: 'gray', testing: 'blue', merging: 'violet', failed: 'red' };
export const STAGE_TONE = { pending: 'gray', running: 'blue', passed: 'green', failed: 'red' };
export const COST_TONE = { free: 'green', low: 'amber', paid: 'red' };

export const pill = (text, tone = 'gray', { dot = true } = {}) =>
  h('span', { class: `pill pill-${tone}${dot ? ' pill-dot' : ''}`, text });
