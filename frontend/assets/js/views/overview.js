// Overview: totals and per-project status, built only from the user's stored runs.
import { api } from '../api.js';
import { h, relTime, fmtDuration, fmtNum } from '../dom.js';
import {
  pageHead, card, tile, tiles, dataTable, emptyState, loading, statusBadge, KIND_LABEL,
  trendSparkline, resultSummary,
} from '../components.js';

export async function render(root, { navigate }) {
  root.append(pageHead('Overview', 'Latest result for every project you have analyzed, from your own run history.'));
  const slot = h('div');
  root.append(slot);
  slot.append(loading());
  const data = await api('/api/dashboard/overview');
  slot.replaceChildren();

  const t = data.totals;
  if (t.runs === 0) {
    slot.append(card(null, null, emptyState(
      'No analyses yet',
      'Results appear here after your first run. Nothing is shown until you have analyzed something.',
      [
        h('a', { class: 'btn btn-primary', href: '#/code' }, 'Review code'),
        h('a', { class: 'btn', href: '#/repos' }, 'Analyze a repository'),
        h('a', { class: 'btn', href: '#/specs' }, 'Check an API spec'),
      ],
    )));
    return;
  }

  slot.append(tiles(
    tile('Analyses', t.runs, `${fmtNum(t.runs_30d)} in the last 30 days`),
    tile('Projects', t.projects),
    tile('Open findings', t.open_findings, t.ai_incomplete_projects
      ? `latest runs · AI review incomplete for ${t.ai_incomplete_projects} project${t.ai_incomplete_projects === 1 ? '' : 's'}`
      : 'from each project’s latest run'),
    tile('Critical', t.critical, 'in latest runs'),
    tile('Failing tests', t.failing_tests, 'in latest runs'),
    t.running ? tile('In progress', t.running) : null,
  ));

  slot.append(card('Projects', 'Findings trend covers up to the last 12 completed runs of each project.', dataTable([
    { label: 'Project', render: (p) => h('div', {}, h('div', { class: 'mono', text: p.project }), h('div', { class: 'muted', text: KIND_LABEL[p.kind] })) },
    { label: 'Last run', render: (p) => h('span', { class: 'nowrap', text: relTime(p.last_run_at) }) },
    { label: 'Latest result', render: (p) => (p.latest ? resultSummary(p.latest.headline) : h('span', { class: 'muted', text: 'No completed run' })) },
    { label: 'Findings trend', render: (p) => trendSparkline(p.trend) },
    { label: 'Runs', num: true, render: (p) => fmtNum(p.runs) },
  ], data.projects, {
    onRowClick: (p) => navigate(`#/history?project=${encodeURIComponent(p.project)}`),
  }), { flush: true }));

  slot.append(card('Recent runs', null, dataTable([
    { label: 'Started', render: (r) => h('span', { class: 'nowrap', text: relTime(r.created_at) }) },
    { label: 'Project', render: (r) => h('span', { class: 'mono', text: r.project }) },
    { label: 'Type', render: (r) => KIND_LABEL[r.kind] },
    { label: 'Status', render: (r) => statusBadge(r.status) },
    { label: 'Duration', num: true, render: (r) => fmtDuration(r.duration_ms) },
  ], data.recent, { onRowClick: (r) => navigate(`#/runs/${r.id}`) }), { flush: true, actions: [h('a', { class: 'btn btn-sm', href: '#/history' }, 'All runs')] }));
}
