// Accounts drawer: one card per provider from schema.models.providers. Enable, key, probe.

import { h, reconcile, relativeTime, setText, beat } from './dom.js';
import { icon } from './icons.js';
import { ACCOUNT_TONE, act, closeDrawer, errorSlot, payloadFor, showDrawer } from './ui.js';

const LOGO = { claude: ['C', '#d97757'], codex: ['O', '#10a37f'], agy: ['G', '#4285f4'], 'openai-compatible': ['λ', '#6b7280'], simulator: ['S', '#8b5cf6'] };

export function mountAccounts(ctx) {
  const drawer = document.getElementById('accounts-drawer');
  const list = h('div', { class: 'account-list', style: { display: 'grid', gap: '12px' } });
  drawer.append(
    h('header', { class: 'drawer-head' },
      h('div', null,
        h('h2', { class: 'drawer-title', id: 'accounts-title', text: 'Accounts' }),
        h('p', { class: 'drawer-sub', text: 'Enable the providers this run may use. A probe proves that a provider answers.' })),
      h('button', { class: 'btn btn-ghost btn-icon', type: 'button', 'aria-label': 'Close', onclick: closeDrawer }, icon('close'))),
    h('div', { class: 'drawer-body' }, list,
      h('p', { class: 'field-hint', text: 'Keys go to the system keychain. The dashboard never shows a key again.' })));

  function render(state) {
    const providers = ctx.store.schema.models.providers;
    const items = Object.entries(providers).map(([id, info]) => ({ id, info, account: state.accounts[id] || {} }));
    reconcile(list, items, { key: (item) => item.id, create: createCard, update: updateCard });
  }

  function createCard({ id, info }) {
    const [letter, colour] = LOGO[id] || [info.label[0], '#6b7280'];
    const toggle = h('button', { class: 'switch', type: 'button', role: 'switch', 'aria-label': `Enable ${info.label}` });
    const status = h('span', { class: 'account-status' });
    const detail = h('div', { class: 'account-detail' }, h('span', { class: 'account-detail-text' }), h('span', { class: 'account-checked faint tabular' }));
    const key = h('input', { class: 'input', type: 'password', autocomplete: 'off', spellcheck: 'false', placeholder: 'Optional API key', 'aria-label': `${info.label} API key` });
    const save = h('button', { class: 'btn btn-sm', type: 'button' }, 'Save key');
    const probe = h('button', { class: 'btn btn-sm', type: 'button' }, icon('bolt', { size: 13 }), 'Probe');
    const note = h('p', { class: 'account-note' });
    const slot = errorSlot();
    const card = h('article', { class: 'account' },
      h('div', { class: 'account-head' },
        h('span', { class: 'account-logo', style: { background: colour }, text: letter, 'aria-hidden': 'true' }),
        h('div', { class: 'account-grow' }, h('div', { class: 'account-name', text: info.label }),
          h('div', { class: 'account-meta mono', text: info.cli ? `${info.cli} · ${info.auth}` : info.auth })),
        status, toggle),
      detail,
      h('div', { class: 'account-actions' }, key, save, probe),
      note, slot);

    const send = (control, values) => act(control, slot, () => ctx.api.send('account.set', payloadFor(ctx.store.schema, 'account.set', { provider: id, ...values })));
    toggle.addEventListener('click', () => send(toggle, { enabled: toggle.getAttribute('aria-checked') !== 'true' }));
    save.addEventListener('click', async () => {
      if (!key.value.trim()) { slot.show('Paste a key first.'); return; }
      const reply = await send(save, { enabled: true, api_key: key.value });
      if (reply) key.value = '';
    });
    key.addEventListener('keydown', (event) => { if (event.key === 'Enter') save.click(); });
    probe.addEventListener('click', () => act(probe, slot, () => ctx.api.send('account.probe', payloadFor(ctx.store.schema, 'account.probe', { provider: id }))));
    card.parts = { toggle, status, detail, note };
    return card;
  }

  function updateCard(card, { account }) {
    const { toggle, status, detail, note } = card.parts;
    toggle.setAttribute('aria-checked', String(Boolean(account.enabled)));
    const value = account.status || 'unknown';
    if (status.dataset.value !== value) {
      if (status.dataset.value) beat(card);
      status.dataset.value = value;
      status.replaceChildren(h('span', { class: `pill pill-dot pill-${ACCOUNT_TONE[value] || 'gray'}`, text: value }));
    }
    setText(detail.firstChild, account.detail || (account.enabled ? 'Not probed yet.' : 'Disabled for this run.'));
    setText(detail.lastChild, account.checked ? `checked ${relativeTime(account.checked)}` : '');
    note.replaceChildren(account.key ? h('span', null, icon('key', { size: 12 }), ` ${account.key}`) : '');
  }

  ctx.store.subscribe(['accounts', '$schema'], render);
  return { open: () => showDrawer(drawer) };
}
