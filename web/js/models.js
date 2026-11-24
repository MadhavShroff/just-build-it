// Model selection for display. Mirrors jbi/models.py: the first enabled provider whose
// last probe did not fail, in preference order; the simulator is the fallback.

const PREFERENCE = ['agy', 'codex', 'claude', 'openai-compatible', 'simulator'];

export function usableProviders(schema, state) {
  const providers = schema.models.providers;
  const order = [...PREFERENCE.filter((p) => p in providers), ...Object.keys(providers).filter((p) => !PREFERENCE.includes(p))];
  const names = order.filter((p) => state.accounts[p]?.enabled && !['failed', 'missing'].includes(state.accounts[p]?.status));
  return names.length ? names : ['simulator'];
}

export function resolveModel(schema, state, tier, served = null) {
  // The served routing is the truth: the preview endpoint resolves each task's
  // stage through jbi/models.select, so display never re-derives a choice the
  // run would not make (audit M4). The local mirror below is the fallback.
  if (served && served.provider && served.model) {
    const info = schema.models.providers[served.provider] || { label: served.provider, models: {} };
    return { provider: served.provider, label: info.label, model: served.model, tier: served.tier || tier };
  }
  const provider = usableProviders(schema, state)[0];
  const info = schema.models.providers[provider] || { label: provider, models: {} };
  return { provider, label: info.label, model: info.models[tier] || '—', tier };
}
