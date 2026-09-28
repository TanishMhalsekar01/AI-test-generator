// History: every stored run, filterable by scope (mine / team), type and project.
import { api } from '../api.js';
import { h, fmtDate, fmtDuration, shortSha } from '../dom.js';
import {
  pageHead, card, dataTable, loading, banner, statusBadge, KIND_LABEL, resultSummary, barChart,
} from '../components.js';

const PAGE = 50;

export async function render(root, { navigate, query }) {
  root.append(pageHead('History', 'All stored runs. Team scope shows runs by anyone on repositories owned by a GitHub organization you belong to.'));

  const scope = h('select', { class: 'select input-inline', id: 'h-scope', 'aria-label': 'Scope' },
    h('option', { value: 'mine' }, 'My runs'), h('option', { value: 'all' }, 'Mine and my teams'));
  const kind = h('select', { class: 'select input-inline', id: 'h-kind', 'aria-label': 'Type' },
    h('option', { value: '' }, 'All types'), ...Object.entries(KIND_LABEL).map(([v, l]) => h('option', { value: v }, l)));
  const project = h('input', { class: 'input input-inline', id: 'h-project', placeholder: 'Project (exact name)', list: 'h-projects', value: query.project || '' });
  const datalist = h('datalist', { id: 'h-projects' });
  const apply = h('button', { type: 'button', class: 'btn' }, 'Apply');
  const clearBtn = h('button', { type: 'button', class: 'btn btn-ghost' }, 'Clear');
  // A team scope from the URL must exist as an option before the org list loads.
  if (query.scope && !['mine', 'all'].includes(query.scope)) {
    scope.append(h('option', { value: query.scope }, `Team: ${query.scope.replace(/^team:/, '')}`));
  }
  scope.value = query.scope || 'mine';
  kind.value = query.kind || '';

  root.append(h('div', { class: 'actions filters', role: 'search' }, scope, kind, project, datalist, apply, clearBtn));

  // Team scopes are added once organizations load (non-blocking).
  api('/api/github/orgs').then(({ orgs }) => {
    for (const o of orgs) {
      if (!scope.querySelector(`option[value="team:${CSS.escape(o.login)}"]`)) {
        scope.append(h('option', { value: `team:${o.login}` }, `Team: ${o.login}`));
      }
    }
    if (query.scope) scope.value = query.scope;
  }).catch(() => {});

  const chartSlot = h('div');
  const tableSlot = h('div');
  root.append(chartSlot, tableSlot);

  function go() {
    const q = new URLSearchParams();
    if (scope.value !== 'mine') q.set('scope', scope.value);
    if (kind.value) q.set('kind', kind.value);
    if (project.value.trim()) q.set('project', project.value.trim());
    navigate(`#/history${q.toString() ? `?${q}` : ''}`);
  }
  apply.addEventListener('click', go);
  project.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
  clearBtn.addEventListener('click', () => navigate('#/history'));

  const params = new URLSearchParams({ scope: scope.value, limit: String(PAGE) });
  if (kind.value) params.set('kind', kind.value);
  if (query.project) params.set('project', query.project);

  let offset = 0;
  const runs = [];
  tableSlot.append(loading());

  async function loadMore() {
    params.set('offset', String(offset));
    const data = await api(`/api/runs?${params}`);
    runs.push(...data.runs);
    offset += data.runs.length;
    return data.runs.length;
  }

  let count;
  try {
    count = await loadMore();
  } catch (err) {
    tableSlot.replaceChildren(banner('error', 'Could not load runs', err.message));
    return;
  }

  function draw(hasMore) {
    datalist.replaceChildren(...[...new Set(runs.map((r) => r.project))].map((p) => h('option', { value: p })));
    const table = dataTable([
      { label: 'Started', render: (r) => h('span', { class: 'nowrap', text: fmtDate(r.created_at) }) },
      { label: 'Project', render: (r) => h('div', {}, h('span', { class: 'mono', text: r.project }),
        r.commit_sha ? h('div', { class: 'muted mono', text: `${r.ref || ''} @ ${shortSha(r.commit_sha)}` }) : null) },
      { label: 'Type', render: (r) => KIND_LABEL[r.kind] },
      { label: 'By', render: (r) => r.user_login },
      { label: 'Status', render: (r) => statusBadge(r.status) },
      { label: 'Result', render: (r) => (r.status === 'completed' ? resultSummary(r.headline) : h('span', { class: 'muted', text: r.error ? 'See run' : '—' })) },
      { label: 'Duration', num: true, render: (r) => fmtDuration(r.duration_ms) },
    ], runs, { onRowClick: (r) => navigate(`#/runs/${r.id}`), empty: 'No runs match these filters.' });
    const more = hasMore ? h('div', { class: 'card-body' }, h('button', {
      type: 'button', class: 'btn',
      onclick: async (e) => { e.target.disabled = true; const n = await loadMore(); draw(n === PAGE); },
    }, 'Load more')) : null;
    tableSlot.replaceChildren(card(null, null, h('div', {}, table, more), { flush: true }));

    // Per-project trend: only meaningful when one project is selected.
    chartSlot.replaceChildren();
    if (query.project) {
      const done = runs.filter((r) => r.status === 'completed').slice(0, 30).reverse();
      if (done.length >= 2) {
        chartSlot.append(card(`Findings per run — ${query.project}`,
          'Findings plus compiler errors (code) or lint errors, warnings and failed live checks (specs), oldest to newest. Select a bar to open the run.',
          barChart(done.map((r) => ({
            label: fmtDate(r.created_at), short: fmtDate(r.created_at).split(',')[0], value: r.headline.findings,
            detail: `${r.headline.tests_passed} tests passed, ${r.headline.tests_failed} failed`, id: r.id,
          })), { height: 180, valueLabel: 'findings', title: `Findings per run for ${query.project}`, onBarClick: (p) => navigate(`#/runs/${p.id}`) })));
      }
    }
  }
  draw(count === PAGE);
}
