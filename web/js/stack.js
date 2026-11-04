// Stack: one proposal card with layer rows, the target and app name, and
// Proceed (stack.accept), Make changes (stack.revise) and Back to questions (stack.back).

import { h, reconcile, setText, slug, beat } from './dom.js';
import { icon } from './icons.js';
import { COST_TONE, act, errorSlot, payloadFor } from './ui.js';

export function mountStack(ctx, root) {
  const summary = h('span', { class: 'stack-summary' });
  const layers = h('div', { class: 'stack-layers', role: 'list' });
  const name = h('input', { class: 'input', id: 'stack-name', autocomplete: 'off', spellcheck: 'false' });
  const target = h('input', { class: 'input mono', id: 'stack-target', autocomplete: 'off', spellcheck: 'false' });
  const proceed = h('button', { class: 'btn btn-primary btn-lg', type: 'button' }, 'Proceed', icon('arrowRight', { size: 15 }));
  const change = h('button', { class: 'btn btn-lg', type: 'button' }, icon('edit', { size: 14 }), 'Make changes');
  const back = h('button', { class: 'btn btn-ghost', type: 'button' }, icon('arrowLeft', { size: 14 }), 'Back to questions');
  const acceptSlot = errorSlot();
  const backSlot = errorSlot();
  const request = h('textarea', { class: 'textarea', rows: 3, placeholder: 'For example: use Postgres instead of SQLite, and deploy to Render.', 'aria-label': 'Requested changes' });
  const revise = h('button', { class: 'btn btn-primary', type: 'button' }, 'Revise proposal');
  const reviseSlot = errorSlot();
  const busy = h('div', { class: 'stack-busy', hidden: true }, h('span', { class: 'typing-dots', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')), h('span'));
  const changes = h('div', { class: 'stack-changes', hidden: true },
    h('label', { class: 'field-label', text: 'What should change?' }), request,
    h('div', { class: 'row-end' }, h('button', { class: 'btn btn-ghost', type: 'button', onclick: () => { changes.hidden = true; } }, 'Cancel'), revise), reviseSlot);

  const card = h('section', { class: 'card stack-card' },
    h('div', { class: 'stack-head' },
      h('span', { class: 'stack-icon', 'aria-hidden': 'true' }, icon('layers', { size: 18 })),
      h('div', null, h('div', { class: 'stack-kicker', text: 'Proposed tech stack' }), summary)),
    layers, busy,
    h('div', { class: 'stack-fields' },
      h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'stack-name', text: 'App name' }), name),
      h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'stack-target', text: 'Target folder' }), target,
        h('span', { class: 'field-hint', text: 'A new project needs an empty or absent folder.' }))),
    h('div', { class: 'stack-actions' }, back, h('span', { class: 'grow' }), change, proceed),
    acceptSlot, backSlot, changes);

  root.append(
    h('div', { class: 'phase-head' }, h('div', null,
      h('div', { class: 'eyebrow', text: 'Step 2 · Stack' }),
      h('h1', { class: 'phase-title', text: 'One stack, cheapest first' }),
      h('p', { class: 'phase-sub', text: 'Each layer uses the cheapest option that works. Proceed, or ask for changes.' }))),
    h('div', { class: 'stack-wrap' }, card));

  const schema = () => ctx.store.schema;
  proceed.addEventListener('click', () => act(proceed, acceptSlot, () =>
    ctx.api.send('stack.accept', payloadFor(schema(), 'stack.accept', { target: target.value, name: name.value }))));
  change.addEventListener('click', () => { changes.hidden = false; request.focus(); });
  revise.addEventListener('click', async () => {
    if (!request.value.trim()) { reviseSlot.show('Describe the change first.'); return; }
    const reply = await act(revise, reviseSlot, () => ctx.api.send('stack.revise', payloadFor(schema(), 'stack.revise', { request: request.value })));
    if (reply) { request.value = ''; changes.hidden = true; }
  });
  back.addEventListener('click', () => act(back, backSlot, () => ctx.api.send('stack.back', {})));

  let touched = false;
  for (const input of [name, target]) input.addEventListener('input', () => { touched = true; });

  ctx.store.subscribe(['stack'], (state) => {
    setText(summary, state.stack.summary || 'Waiting for the planner');
    reconcile(layers, state.stack.layers, {
      key: (layer) => layer.layer,
      create: (layer) => h('div', { class: 'stack-layer', role: 'listitem' },
        h('span', { class: 'stack-layer-name' }), h('div', { class: 'stack-layer-main' }, h('span', { class: 'stack-choice' }), h('span', { class: 'stack-reason' })), h('span', { class: 'stack-cost' })),
      update: (row, layer) => {
        setText(row.children[0], layer.layer);
        const changed = row.dataset.choice && row.dataset.choice !== layer.choice;
        row.dataset.choice = layer.choice;
        setText(row.children[1].children[0], layer.choice);
        setText(row.children[1].children[1], layer.reason);
        row.children[2].replaceChildren(h('span', { class: `pill pill-${COST_TONE[layer.cost] || 'gray'}`, text: layer.cost }));
        if (changed) beat(row);
      },
    });
  });

  ctx.store.subscribe(['run'], (state) => {
    if (!touched) {
      name.value = state.run.name || '';
      target.value = state.run.target || `~/${slug(state.run.name)}`;
    }
    const working = state.run.busy;
    busy.hidden = !working;
    setText(busy.lastChild, working ? `${working.label}…` : '');
    proceed.disabled = Boolean(working);
  });
}
