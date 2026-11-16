// Deterministic metrics view: reads /api/v1/metrics via ctx.api.metrics() with no LLM call.
// If any source is missing or unreadable, renders "unavailable" and names the source path.

import { h, setText, compactNumber, shortSha } from './dom.js';
import { icon } from './icons.js';
import { pill } from './ui.js';

function formatNumber(value) {
  if (value === null || value === undefined) return 'unavailable';
  return new Intl.NumberFormat().format(value);
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return 'unavailable';
  const total = Math.max(0, Math.round(Number(seconds)));
  if (total < 60) return `${total}s`;
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (mins < 60) return `${mins}m ${secs}s`;
  const hours = Math.floor(mins / 60);
  return `${hours}h ${mins % 60}m`;
}

function unavailableBlock(path, error) {
  return h('div', { class: 'metrics-unavailable' },
    pill('unavailable', 'amber', { dot: true }),
    h('code', { class: 'metrics-path mono', text: String(path || 'unknown path') }),
    error ? h('span', { class: 'metrics-error faint', text: String(error) }) : null);
}

function renderSeries(rows, valueKey, unitLabel) {
  if (!rows || !rows.length) {
    return h('p', { class: 'empty', text: `No ${unitLabel} recorded in the window.` });
  }
  const recent = rows.slice(-14);
  const maxVal = Math.max(1, ...recent.map((r) => Number(r[valueKey] || 0)));
  return h('div', { class: 'metrics-series' },
    ...recent.slice().reverse().map((row) => {
      const value = Number(row[valueKey] || 0);
      const pct = Math.max(2, Math.round((value / maxVal) * 100));
      return h('div', { class: 'metrics-bar-row' },
        h('span', { class: 'metrics-bar-date mono tabular', text: row.date }),
        h('div', { class: 'metrics-bar-track' },
          h('span', { class: 'metrics-bar-fill', style: { width: `${pct}%` } })),
        h('span', { class: 'metrics-bar-value tabular', text: formatNumber(value) }));
    }));
}

export function mountMetrics(ctx, root) {
  const statusNote = h('span', { class: 'metrics-stamp faint tabular' });
  const refreshBtn = h('button', {
    class: 'btn btn-sm',
    type: 'button',
    onclick: () => refresh(),
  }, icon('retry', { size: 13 }), 'Refresh');

  const averagesGrid = h('div', { class: 'metrics-tiles' });
  const busyWindowsHost = h('div', { class: 'metrics-list' });
  const prsInCiHost = h('div', { class: 'metrics-list' });
  const trainHost = h('div', { class: 'metrics-list' });
  const boardHost = h('div', { class: 'metrics-list' });
  const tokensDayHost = h('div', { class: 'metrics-chart-body' });
  const prsDayHost = h('div', { class: 'metrics-chart-body' });
  const tasksDayHost = h('div', { class: 'metrics-chart-body' });

  root.replaceChildren(
    h('div', { class: 'metrics-head' },
      h('div', { class: 'board-title' },
        h('div', { class: 'eyebrow', text: 'Direct telemetry · No LLM' }),
        h('h1', { class: 'phase-title', text: 'Dispatcher & CI Metrics' }),
        h('p', { class: 'phase-sub', text: 'Live window pool, CI train, board rows, token usage, and merged PRs.' })),
      h('div', { class: 'metrics-actions' }, statusNote, refreshBtn)),
    averagesGrid,
    h('div', { class: 'metrics-grid-now' },
      h('section', { class: 'card' },
        h('div', { class: 'card-head' },
          h('h2', { class: 'card-title' }, icon('terminal'), 'Right now: Window pool')),
        busyWindowsHost),
      h('section', { class: 'card' },
        h('div', { class: 'card-head' },
          h('h2', { class: 'card-title' }, icon('lane'), 'Right now: PRs in CI')),
        prsInCiHost),
      h('section', { class: 'card' },
        h('div', { class: 'card-head' },
          h('h2', { class: 'card-title' }, icon('bolt'), 'CI train, lanes & held greens')),
        trainHost),
      h('section', { class: 'card' },
        h('div', { class: 'card-head' },
          h('h2', { class: 'card-title' }, icon('list'), 'Board rows (open & done)')),
        boardHost)),
    h('div', { class: 'metrics-grid-time' },
      h('section', { class: 'card' },
        h('div', { class: 'card-head' },
          h('h2', { class: 'card-title' }, icon('coins'), 'Over time: Tokens per day')),
        tokensDayHost),
      h('section', { class: 'card' },
        h('div', { class: 'card-head' },
          h('h2', { class: 'card-title' }, icon('dag'), 'Over time: PRs merged per day')),
        prsDayHost),
      h('section', { class: 'card' },
        h('div', { class: 'card-head' },
          h('h2', { class: 'card-title' }, icon('check'), 'Over time: Tasks completed per day')),
        tasksDayHost)));

  function render(data) {
    if (!data) return;
    setText(statusNote, data.generated_at ? `Updated ${data.generated_at}` : '');
    const { tokens, task_ledger: ledger, prs_merged: prs, ci_train: train, board, window_pool: pool } = data;

    // 1. Averages & Totals tiles
    averagesGrid.replaceChildren(
      renderSummaryTile(
        'Tokens used, total',
        tokens,
        tokens?.available ? compactNumber(tokens.total) : null,
        tokens?.available ? `${formatNumber(tokens.total)} tokens` : null,
      ),
      renderSummaryTile(
        'Tokens per task, average',
        ledger,
        ledger?.available && ledger.avg_tokens_per_task !== null ? compactNumber(ledger.avg_tokens_per_task) : null,
        ledger?.available
          ? (ledger.avg_tokens_per_task !== null ? `${formatNumber(ledger.avg_tokens_per_task)} avg (${ledger.completed_count} tasks)` : 'No token samples in ledger')
          : null,
      ),
      renderSummaryTile(
        'Time per task, average',
        ledger,
        ledger?.available && ledger.avg_time_per_task_s !== null ? formatDuration(ledger.avg_time_per_task_s) : null,
        ledger?.available
          ? (ledger.avg_time_per_task_s !== null ? `${ledger.avg_time_per_task_s}s avg (${ledger.completed_count} tasks)` : 'No duration samples in ledger')
          : null,
      ),
      renderSummaryTile(
        'Window pool (busy / free)',
        pool,
        pool?.available ? `${pool.busy_count} busy · ${pool.free_count} free` : null,
        pool?.available ? pool.path : null,
      ),
      renderSummaryTile(
        'Board rows (open / done)',
        board,
        board?.available ? `${formatNumber(board.open_rows)} open · ${formatNumber(board.done_rows)} done` : null,
        board?.available ? board.path : null,
      ),
      renderSummaryTile(
        'PRs merged (14d)',
        prs,
        prs?.available ? formatNumber(prs.total) : null,
        prs?.available ? prs.path : null,
      ),
    );

    // 2. Right now: Window pool
    if (!pool?.available) {
      busyWindowsHost.replaceChildren(unavailableBlock(pool?.path, pool?.error));
    } else {
      const windows = pool.windows || [];
      if (!windows.length) {
        busyWindowsHost.replaceChildren(h('p', { class: 'empty', text: 'No windows registered in pool.' }));
      } else {
        busyWindowsHost.replaceChildren(
          h('div', { class: 'metrics-subhead faint mono', text: `${pool.busy_count} busy, ${pool.free_count} free — ${pool.path}` }),
          ...windows.map((w) => h('div', { class: 'metrics-row' },
            h('div', { class: 'metrics-row-top' },
              h('span', { class: 'mono', text: w.window }),
              pill(w.status, w.status === 'busy' ? 'blue' : 'green'),
              w.task ? h('span', { class: 'metrics-task-label', text: w.title ? `${w.task} · ${w.title}` : w.task }) : h('span', { class: 'faint', text: 'Idle' }),
              w.pr_number ? pill(`PR #${w.pr_number}`, 'violet', { dot: false }) : null),
            w.head_sha ? h('div', { class: 'metrics-row-sub mono faint', text: `head ${shortSha(w.head_sha)} · attempt ${w.attempts || 1}` }) : null)),
        );
      }
    }

    // 3. Right now: PRs in CI
    if (!pool?.available && !train?.available) {
      prsInCiHost.replaceChildren(unavailableBlock(`${pool?.path || ''}, ${train?.path || ''}`, 'window pool and CI train history are unavailable'));
    } else {
      const activePrs = data.right_now?.prs_in_ci || [];
      if (!activePrs.length) {
        prsInCiHost.replaceChildren(h('p', { class: 'empty', text: 'No pull requests in CI right now.' }));
      } else {
        prsInCiHost.replaceChildren(
          ...activePrs.map((item) => h('div', { class: 'metrics-row' },
            h('div', { class: 'metrics-row-top' },
              h('span', { class: 'mono', text: `PR #${item.pr_number}` }),
              pill(item.status || 'in CI', 'violet'),
              item.task ? h('span', { text: `task ${item.task}` }) : null,
              item.window ? h('span', { class: 'mono faint', text: item.window }) : null,
              item.lane ? h('span', { class: 'mono faint', text: `lane ${item.lane}` }) : null),
            item.head_sha ? h('div', { class: 'metrics-row-sub mono faint', text: `head ${shortSha(item.head_sha)}` }) : null)),
        );
      }
    }

    // 4. CI train, lanes, held greens
    if (!train?.available) {
      trainHost.replaceChildren(unavailableBlock(train?.path, train?.error));
    } else {
      const lanes = train.lanes || [];
      const held = train.held_greens || [];
      trainHost.replaceChildren(
        h('div', { class: 'metrics-row' },
          h('div', { class: 'metrics-row-top' },
            h('span', { text: 'Train state' }),
            pill(train.train_state || 'idle', train.train_state === 'red' ? 'red' : 'teal'),
            train.train_id ? h('code', { class: 'mono faint', text: train.train_id }) : null)),
        h('div', { class: 'metrics-subhead faint', text: `Lanes (${lanes.length})` }),
        ...(lanes.length
          ? lanes.map((l) => h('div', { class: 'metrics-row' },
              h('div', { class: 'metrics-row-top' },
                h('span', { class: 'mono', text: `Lane ${l.lane}` }),
                pill(l.status || 'idle', l.status === 'red' ? 'red' : l.status === 'green' ? 'green' : 'blue'),
                l.prs?.length ? h('span', { class: 'mono', text: l.prs.map((p) => `#${p}`).join(', ') }) : null),
              l.failed_gates?.length ? h('div', { class: 'metrics-row-sub faint', text: `failed gates: ${l.failed_gates.join(', ')}` }) : null))
          : [h('p', { class: 'empty', text: 'No lane records.' })]),
        h('div', { class: 'metrics-subhead faint', text: `Held greens (${held.length})` }),
        ...(held.length
          ? held.map((hg) => h('div', { class: 'metrics-row' },
              h('div', { class: 'metrics-row-top' },
                h('code', { class: 'mono', text: hg.train_id || hg.input_key || 'train' }),
                pill(hg.state || 'held', 'amber')),
              hg.other_lanes?.length ? h('div', { class: 'metrics-row-sub faint', text: `waiting on: ${hg.other_lanes.join(', ')}` }) : null))
          : [h('p', { class: 'empty', text: 'No held green trains.' })]),
      );
    }

    // 5. Board rows open and done
    if (!board?.available) {
      boardHost.replaceChildren(unavailableBlock(board?.path, board?.error));
    } else {
      const states = Object.entries(board.by_state || {});
      boardHost.replaceChildren(
        h('div', { class: 'metrics-row' },
          h('div', { class: 'metrics-row-top' },
            pill(`${formatNumber(board.open_rows)} open`, 'amber'),
            pill(`${formatNumber(board.done_rows)} done`, 'green'))),
        h('div', { class: 'metrics-subhead faint mono', text: board.path }),
        ...states.map(([stateName, count]) => h('div', { class: 'metrics-row' },
          h('div', { class: 'metrics-row-top' },
            h('span', { class: 'mono', text: stateName }),
            h('span', { class: 'tabular', text: formatNumber(count) })))),
      );
    }

    // 6. Over time: Tokens per day (plus sub-source availability check)
    if (!tokens?.available) {
      tokensDayHost.replaceChildren(unavailableBlock(tokens?.path, tokens?.error));
    } else {
      const subSources = [];
      for (const [label, src] of [['glm-proxy', tokens.sources?.glm_proxy], ['claude transcripts', tokens.sources?.claude_projects]]) {
        if (src && !src.available) {
          subSources.push(h('div', { class: 'metrics-sub-unavail' },
            h('span', { class: 'faint', text: `${label}: ` }),
            unavailableBlock(src.path, src.error)));
        } else if (src?.available) {
          subSources.push(h('div', { class: 'metrics-subhead faint mono', text: `${label}: ${formatNumber(src.total)} (${src.path})` }));
        }
      }
      tokensDayHost.replaceChildren(...subSources, renderSeries(tokens.by_day, 'tokens', 'tokens'));
    }

    // 7. Over time: PRs merged per day
    if (!prs?.available) {
      prsDayHost.replaceChildren(unavailableBlock(prs?.path, prs?.error));
    } else {
      prsDayHost.replaceChildren(
        h('div', { class: 'metrics-subhead faint mono', text: `${prs.path} (origin/develop)` }),
        renderSeries(prs.by_day, 'count', 'merged PRs'),
      );
    }

    // 8. Over time: Tasks completed per day
    if (!ledger?.available) {
      tasksDayHost.replaceChildren(unavailableBlock(ledger?.path, ledger?.error));
    } else {
      tasksDayHost.replaceChildren(
        h('div', { class: 'metrics-subhead faint mono', text: ledger.path }),
        renderSeries(ledger.tasks_per_day, 'count', 'completed tasks'),
      );
    }
  }

  function renderSummaryTile(label, source, primaryText, secondaryText) {
    if (!source || !source.available) {
      return h('div', { class: 'tile is-unknown' },
        h('span', { class: 'tile-label', text: label }),
        unavailableBlock(source?.path, source?.error));
    }
    return h('div', { class: 'tile' },
      h('span', { class: 'tile-label', text: label }),
      h('span', { class: 'tile-value tabular', text: primaryText ?? 'unavailable' }),
      secondaryText ? h('span', { class: 'metrics-tile-sub faint mono', text: secondaryText }) : null);
  }

  async function refresh() {
    refreshBtn.classList.add('is-busy');
    try {
      const response = await ctx.api.metrics();
      if (response?.metrics) render(response.metrics);
    } catch (error) {
      setText(statusNote, error.message || 'Metrics request failed.');
    } finally {
      refreshBtn.classList.remove('is-busy');
    }
  }

  return { refresh, render };
}
