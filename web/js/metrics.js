// Internal fleet metrics: local SQLite records only. No other service or registry is read.

import { h, shortSha } from './dom.js';
import { icon } from './icons.js';
import { pill } from './ui.js';

function number(value) {
  return value === null || value === undefined ? 'unavailable' : new Intl.NumberFormat().format(value);
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return 'unavailable';
  const total = Math.max(0, Math.round(Number(seconds)));
  if (total < 60) return `${total}s`;
  return total < 3600 ? `${Math.floor(total / 60)}m ${total % 60}s` : `${Math.floor(total / 3600)}h`;
}

function section(title, rows, detail) {
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, icon('list'), title)),
    detail ? h('p', { class: 'metrics-subhead faint', text: detail }) : null,
    rows?.length ? h('div', { class: 'metrics-list' }, ...rows) : h('p', { class: 'empty', text: 'No records.' }));
}

function row(label, state, detail, tone = 'blue') {
  return h('div', { class: 'metrics-row' },
    h('div', { class: 'metrics-row-top' }, h('span', { class: 'mono', text: label }), pill(state, tone)),
    h('div', { class: 'metrics-row-sub faint', text: detail || '' }));
}

function series(rows, key, unit) {
  return h('div', { class: 'metrics-chart-body' }, ...(rows || []).slice(-14).map((entry) =>
    row(entry.date, `${number(entry[key])} ${unit}`, '', 'violet')));
}

function line(id, text) {
  return h('div', { class: 'metrics-row' }, h('div', { class: 'metrics-row-top', id }, text));
}

function unavailable(path) {
  return h('div', { class: 'metrics-unavailable', id: 'metrics-unavailable' },
    pill('unavailable', 'amber'), h('code', { text: path }));
}

export function mountMetrics(ctx, root) {
  const status = h('span', { class: 'metrics-stamp faint tabular' });
  const left = h('div', { class: 'metrics-sections' });
  const center = h('div', { class: 'metrics-sections' });
  const right = h('div', { class: 'metrics-sections' });
  const refresh = h('button', { class: 'btn btn-sm', type: 'button', onclick: () => update() });
  root.append(h('div', { class: 'metrics-head' },
    h('div', { class: 'board-title' },
      h('div', { class: 'eyebrow', text: 'Local fleet store' }),
      h('h1', { class: 'phase-title', text: 'Dispatch & CI Records' }),
      h('p', { id: 'metrics-provenance', class: 'phase-sub', text: 'Recorded leases, costs, messages and verified merges. No external registry.' })),
    h('div', { class: 'metrics-actions' }, status, refresh)), left, center, right);

  function render(data) {
    status.textContent = data.generated_at ? `Updated ${data.generated_at}` : '';
    const store = data.store || {};
    if (!store.available) {
      left.replaceChildren(unavailable(store.path));
      center.replaceChildren();
      right.replaceChildren();
      return;
    }
    const token = data.tokens || {};
    const cache = data.cache;
    left.replaceChildren(
      h('div', { class: 'tokens', id: 'metrics-token-loop' },
        section('Token usage', [
          row('Input', number(token.total), 'Reported provider usage', 'violet'),
          row('Cache read', number(cache?.read), 'Stable-prefix accounting', 'green'),
          row('Cache write', number(cache?.write), 'Reported cache writes', 'green'),
          row('Cache rate', cache?.hit_rate == null ? 'unavailable' : `${Math.round(cache.hit_rate * 100)}%`,
            'read / (read + write + uncached input)', 'green'),
        ], token.path),
        line('metrics-source', 'Local fleet store'),
        line('metrics-token-total', `${number(token.total)} tokens recorded`),
        line('metrics-token-cache_read', `${number(cache?.read)} cache-read tokens`),
        line('metrics-token-hit-rate', `Cache rate ${cache?.hit_rate ?? 'unavailable'}`),
        line('metrics-token-heads', 'Track each dispatch, not one session total')),
      section('Leases', (data.leases || []).map((lease) =>
        row(lease.task_id, lease.released_at ? 'released' : 'live',
          `${(lease.owns || []).join(', ')} · ${lease.worktree}`, lease.released_at ? 'green' : 'amber')),
        'The lease remains live until the release guard succeeds.'),
      section('Message anatomy', (data.messages || []).map((message) =>
        row(message.target, message.delivery_state, `${message.sender}: ${message.text}`, 'teal'))));
    const dispatchRows = (data.dispatches || []).map((dispatch) => {
      const cost = (data.costs || []).find((item) => item.dispatch_id === dispatch.id);
      return row(`${dispatch.task_id} · ${dispatch.attempt}`,
        dispatch.verdict || 'running', `${dispatch.model || 'model unknown'} · ${dispatch.continuation ? 'continuation' : 'fresh'}\n` +
        `Cache ${cost?.cache_read || 0}/${cost?.cache_write || 0}`);
    });
    center.replaceChildren(
      section('Dispatches', dispatchRows, 'A correction appends the same task history.'),
      section('CI submissions', (data.ci_submissions || []).map((submission) =>
        row(`${submission.task_id} · #${submission.pr_number}`, submission.state,
          `${shortSha(submission.head_sha)} · ${submission.green_run || 'No completed run'}`, 'teal'))));
    const ledger = data.task_ledger || {};
    right.replaceChildren(
      section('Verified merges', (data.merges || []).map((merge) =>
        row(`${merge.task_id} · #${merge.pr_number}`, 'merged',
          `${shortSha(merge.head_sha)} → ${shortSha(merge.squash_sha)}`, 'green')),
        `Average duration ${duration(ledger.avg_time_per_task_s)} · average tokens ${number(ledger.avg_tokens_per_task)}`),
      section('Tokens per day', [series(data.over_time?.tokens_per_day, 'tokens', 'tokens')]),
      section('PRs merged per day', [series(data.over_time?.prs_merged_per_day, 'count', 'merged')]),
      section('Tasks completed per day', [series(data.over_time?.tasks_completed_per_day, 'count', 'completed')]),
      section('Token usage',
        [row('Tokens per task, average', number(ledger.avg_tokens_per_task), '', 'violet'),
         row('Time per task, average', duration(ledger.avg_time_per_task_s), '', 'green')]));
  }

  async function update() {
    try {
      const response = await ctx.api.metrics();
      if (response?.metrics) render(response.metrics);
    } catch (error) {
      status.textContent = error.message || 'Metrics request failed.';
    }
  }

  refresh.append(icon('retry', { size: 13 }), document.createTextNode('Refresh'));
  return { refresh: update, render };
}
