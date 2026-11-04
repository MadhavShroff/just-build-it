// Configure: four rigor cards with stage chips, four model profile cards with the
// role -> tier -> model mapping for the enabled accounts, and Start build.

import { h, reconcile, setText } from './dom.js';
import { icon } from './icons.js';
import { act, choices, errorSlot, payloadFor } from './ui.js';
import { resolveModel } from './models.js';

const ROLE_LABELS = { implement: 'Implement', verify: 'Verify', review: 'Review', security: 'Security', plan: 'Plan' };

export function mountConfigure(ctx, root) {
  const rigorGrid = h('div', { class: 'option-grid', role: 'radiogroup', 'aria-label': 'Rigor level' });
  const profileGrid = h('div', { class: 'option-grid', role: 'radiogroup', 'aria-label': 'Model profile' });
  const provider = h('span', { class: 'chip' });
  const configSlot = errorSlot();
  const start = h('button', { class: 'btn btn-primary btn-lg', type: 'button' }, icon('play', { size: 14 }), 'Start build');
  const startSlot = errorSlot();
  const summary = h('p', { class: 'muted start-summary' });

  root.append(
    h('div', { class: 'phase-head' }, h('div', null,
      h('div', { class: 'eyebrow', text: 'Step 4 · Configure' }),
      h('h1', { class: 'phase-title', text: 'How careful, and with which models?' }),
      h('p', { class: 'phase-sub', text: 'Rigor decides the stages each task passes. The profile decides the model for each role. Both can change mid-run.' }))),
    h('div', { class: 'configure-wrap' },
      h('div', { class: 'section-head' }, h('h2', { class: 'section-title', text: 'Rigor' })), rigorGrid,
      h('div', { class: 'section-head' }, h('h2', { class: 'section-title', text: 'Model profile' }), provider), profileGrid,
      configSlot,
      h('div', { class: 'start-bar card' }, summary, h('span', { class: 'grow' }), h('div', null, start, startSlot))));

  const schema = () => ctx.store.schema;
  const configure = (control, change) => {
    const { config } = ctx.store.state;
    return act(control, configSlot, () => ctx.api.send('run.configure',
      payloadFor(schema(), 'run.configure', { rigor: config.rigor, profile: config.profile, ...change })));
  };
  start.addEventListener('click', () => act(start, startSlot, async () => {
    const { config } = ctx.store.state;
    await ctx.api.send('run.configure', payloadFor(schema(), 'run.configure', config));
    return ctx.api.send('run.start', {});
  }));

  ctx.store.subscribe(['config', 'accounts', 'plan'], (state) => {
    const detail = schema().rigor_detail;
    reconcile(rigorGrid, choices(schema(), 'run.configure', 'rigor'), {
      key: (level) => level, enter: false,
      create: (level) => {
        const info = detail.levels[level];
        const card = h('button', { class: 'option', type: 'button', role: 'radio' },
          h('span', { class: 'option-radio' }),
          h('div', { class: 'option-name', text: info.label }),
          h('p', { class: 'option-desc', text: info.summary }),
          h('div', { class: 'stage-chips' }, info.stages.map((stage, i) => h('span', { class: 'stage-step' },
            i ? h('span', { class: 'stage-arrow', 'aria-hidden': 'true' }, '→') : null, h('span', { class: 'stage-chip', text: stage })))),
          h('div', { class: 'option-foot faint', text: info.lane_checks.length ? `Lane checks: ${info.lane_checks.join(', ')}` : 'No lane checks' }));
        card.addEventListener('click', () => configure(card, { rigor: level }));
        return card;
      },
      update: (card, level) => card.setAttribute('aria-checked', String(level === state.config.rigor)),
    });

    reconcile(profileGrid, choices(schema(), 'run.configure', 'profile'), {
      key: (profile) => profile, enter: false,
      create: (profile) => {
        const card = h('button', { class: 'option', type: 'button', role: 'radio' },
          h('span', { class: 'option-radio' }),
          h('div', { class: 'option-name', text: profile[0].toUpperCase() + profile.slice(1) }),
          h('p', { class: 'option-desc', text: detail.profiles[profile].label }),
          h('dl', { class: 'role-map' }));
        card.addEventListener('click', () => configure(card, { profile }));
        return card;
      },
      update: (card, profile) => {
        card.setAttribute('aria-checked', String(profile === state.config.profile));
        const tiers = detail.profiles[profile].tiers;
        card.querySelector('.role-map').replaceChildren(...Object.entries(tiers).map(([role, tier]) => {
          const choice = resolveModel(schema(), state, tier);
          return h('div', { class: 'role-row' }, h('dt', { text: ROLE_LABELS[role] || role }),
            h('dd', null, h('span', { class: 'tier', text: tier }), h('span', { class: 'role-model mono', text: choice.model })));
        }));
      },
    });

    const first = resolveModel(schema(), state, 'L');
    setText(provider, `Provider: ${first.label}`);
    const tasks = state.plan.order.length;
    const level = detail.levels[state.config.rigor];
    setText(summary, `${tasks} tasks · ${level.label} rigor (${level.stages.length} stages each) · ${state.config.profile} models · up to ${state.config.max_sessions} agents`);
  });
}
