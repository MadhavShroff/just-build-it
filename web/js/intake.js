// Intake: the planning chat, the detail selector, the import tab, the rough-PRD preview
// and the "Stop questions and plan" action.

import { h, reconcile, setText, clockTime, trackOverflow } from './dom.js';
import { icon } from './icons.js';
import { act, choices, errorSlot, payloadFor } from './ui.js';

const DETAIL_COPY = {
  user: ['You lead', 'You give the details; the planner records them.'],
  guided: ['Guided', 'The planner asks focused questions.'],
  delegate: ['Delegate', 'The planner decides and records each assumption.'],
};

export function mountIntake(ctx, root) {
  const thread = h('div', { class: 'chat-thread', role: 'log', 'aria-label': 'Planning chat', 'aria-live': 'polite' });
  const typing = h('div', { class: 'chat-typing', hidden: true },
    h('span', { class: 'typing-dots', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')), h('span', { class: 'typing-label' }));
  const scroller = h('div', { class: 'chat-scroll' }, thread, typing);
  const chatEdges = trackOverflow(scroller);   // is-more-top draws the soft top fade

  // composer
  const text = h('textarea', { class: 'composer-input', rows: 1, placeholder: 'Describe what you want to build…', 'aria-label': 'Message' });
  const sendButton = h('button', { class: 'btn btn-primary btn-icon composer-send', type: 'button', 'aria-label': 'Send' }, icon('send'));
  const composerSlot = errorSlot();
  const composer = h('div', { class: 'composer' }, h('div', { class: 'composer-box' }, text, sendButton), composerSlot);

  // import
  const importText = h('textarea', { class: 'textarea mono import-input', rows: 10, placeholder: '- Users can sign up with email\n- Track books as reading / finished\n- Weekly summary email\n…', 'aria-label': 'Task list' });
  const importButton = h('button', { class: 'btn btn-primary', type: 'button' }, icon('upload', { size: 14 }), 'Import list');
  const importSlot = errorSlot();
  const importPane = h('div', { class: 'import-pane', hidden: true },
    h('p', { class: 'muted', text: 'Paste a task list in any format: bullets, a ticket export, a README. The planner turns it into a rough PRD.' }),
    importText, h('div', { class: 'row-end' }, importButton), importSlot);

  const tabs = h('div', { class: 'segmented', role: 'tablist', 'aria-label': 'Input' },
    tab('chat', 'Chat', 'chat', true), tab('import', 'Import', 'list', false));
  function tab(id, label, iconName, selected) {
    return h('button', { class: 'segment', role: 'tab', type: 'button', 'aria-selected': String(selected), 'data-tab': id, onclick: () => selectTab(id) }, icon(iconName, { size: 14 }), label);
  }
  function selectTab(id) {
    for (const button of tabs.children) button.setAttribute('aria-selected', String(button.dataset.tab === id));
    chatPane.hidden = id !== 'chat';
    importPane.hidden = id !== 'import';
    (id === 'chat' ? text : importText).focus();
  }

  const chatPane = h('div', { class: 'chat-pane' }, scroller, composer);
  const conversation = h('section', { class: 'card intake-main' },
    h('div', { class: 'intake-toolbar' }, tabs, h('span', { class: 'faint intake-count tabular' })),
    chatPane, importPane);

  // side panel
  const detailGroup = h('div', { class: 'detail-options', role: 'radiogroup', 'aria-label': 'Detail level' });
  const detailSlot = errorSlot();
  const finishButton = h('button', { class: 'btn btn-primary btn-lg finish-button', type: 'button' }, icon('sparkle', { size: 16 }), 'Stop questions and plan');
  const finishSlot = errorSlot();
  const prdText = h('div', { class: 'prd-text', tabindex: '0', role: 'region', 'aria-label': 'Rough PRD' });
  const prdToggle = h('button', { class: 'btn btn-ghost btn-sm prd-toggle', type: 'button', 'aria-expanded': 'false', hidden: true, text: 'Show the full PRD' });
  const prd = h('div', { class: 'prd-preview', hidden: true }, h('div', { class: 'drawer-section-title', text: 'Rough PRD' }), prdText, prdToggle);
  const prdEdges = trackOverflow(prdText);   // is-more-bottom fades the cut edge while more text sits below
  function syncPrd() {
    const open = prd.classList.contains('is-expanded');
    prdToggle.hidden = !open && prdText.scrollHeight <= prdText.clientHeight + 1;
    prdToggle.setAttribute('aria-expanded', String(open));
    setText(prdToggle, open ? 'Show less' : 'Show the full PRD');
    prdEdges();
  }
  prdToggle.addEventListener('click', () => { prd.classList.toggle('is-expanded'); syncPrd(); });
  new ResizeObserver(syncPrd).observe(prdText);
  const side = h('aside', { class: 'intake-side' },
    h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', { class: 'card-title', text: 'Detail level' })), detailGroup, detailSlot),
    h('section', { class: 'card finish-card' },
      h('h3', { class: 'card-title', text: 'Ready when you are' }),
      h('p', { class: 'muted finish-copy', text: 'The planner writes a PRD, a task graph and an architecture diagram from this conversation.' }),
      finishButton, finishSlot, prd));

  root.append(
    h('div', { class: 'phase-head' }, h('div', null,
      h('div', { class: 'eyebrow', text: 'Step 1 · Plan' }),
      h('h1', { class: 'phase-title', text: 'What should we build?' }),
      h('p', { class: 'phase-sub', text: 'Talk it through with the planner or import a list. Stop whenever the idea is clear enough.' }))),
    h('div', { class: 'intake-grid' }, conversation, side));

  // ---- actions --------------------------------------------------------------------

  const schema = () => ctx.store.schema;
  async function say() {
    const value = text.value.trim();
    if (!value) return;
    const reply = await act(sendButton, composerSlot, () => ctx.api.send('intake.say', payloadFor(schema(), 'intake.say', { text: value })));
    if (reply) { text.value = ''; autosize(); }
  }
  sendButton.addEventListener('click', say);
  text.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); say(); }
  });
  const autosize = () => { text.style.height = 'auto'; text.style.height = `${Math.min(text.scrollHeight, 180)}px`; };
  text.addEventListener('input', autosize);

  importButton.addEventListener('click', async () => {
    const value = importText.value.trim();
    if (!value) { importSlot.show('Paste a list first.'); return; }
    const reply = await act(importButton, importSlot, () => ctx.api.send('intake.import', payloadFor(schema(), 'intake.import', { text: value })));
    if (reply) { importText.value = ''; selectTab('chat'); }
  });
  finishButton.addEventListener('click', () => act(finishButton, finishSlot, () => ctx.api.send('intake.finish', {})));

  // ---- renders --------------------------------------------------------------------

  ctx.store.subscribe(['intake'], (state) => {
    const messages = state.intake.messages.map((message, index) => ({ ...message, index }));
    const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    reconcile(thread, messages, { key: (m) => `${m.index}-${m.ts}`, create: createMessage });
    thread.parentElement.classList.toggle('is-empty', messages.length === 0);
    setText(conversation.querySelector('.intake-count'), messages.length ? `${messages.length} messages` : '');
    if (atBottom || messages.length < 3) requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; chatEdges(); });
    else chatEdges();
    renderDetail(state.intake.detail);
  });

  ctx.store.subscribe(['run'], (state) => {
    const busy = state.run.busy;
    typing.hidden = !busy;
    setText(typing.querySelector('.typing-label'), busy ? `${busy.label}…` : '');
    finishButton.disabled = Boolean(busy);
    sendButton.disabled = Boolean(busy);
    if (busy) requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; });
  });

  ctx.store.subscribe(['plan'], (state) => {
    const rough = state.plan.rough_prd;
    prd.hidden = !rough;
    setText(prdText, rough);
    requestAnimationFrame(syncPrd);
  });

  function renderDetail(current) {
    const levels = choices(schema(), 'intake.detail', 'level');
    reconcile(detailGroup, levels, {
      key: (level) => level, enter: false,
      create: (level) => {
        const [label, desc] = DETAIL_COPY[level] || [level, ''];
        const button = h('button', { class: 'detail-option', type: 'button', role: 'radio' },
          h('span', { class: 'option-radio' }), h('span', null, h('span', { class: 'detail-name', text: label }), h('span', { class: 'detail-desc', text: desc })));
        button.addEventListener('click', () => act(button, detailSlot, () => ctx.api.send('intake.detail', payloadFor(schema(), 'intake.detail', { level }))));
        return button;
      },
      update: (button, level) => button.setAttribute('aria-checked', String(level === current)),
    });
  }
}

function createMessage(message) {
  const mine = message.role === 'user';
  return h('div', { class: `msg ${mine ? 'msg-user' : 'msg-assistant'}` },
    mine ? null : h('span', { class: 'msg-avatar', 'aria-hidden': 'true' }, icon('sparkle', { size: 14 })),
    h('div', { class: 'msg-body' },
      message.imported ? h('span', { class: 'msg-tag', text: 'Imported list' }) : null,
      h('div', { class: 'msg-text', text: message.text }),
      h('time', { class: 'msg-time', datetime: message.ts, text: clockTime(message.ts) })));
}
