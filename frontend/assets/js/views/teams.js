// Teams: GitHub organizations the user belongs to, and a dashboard per organization
// combining live GitHub data with runs stored for that organization's repositories.
import { api } from '../api.js';
import { h, relTime, fmtNum, shortSha } from '../dom.js';
import {
  pageHead, card, tile, tiles, dataTable, loading, banner, emptyState, statusBadge, KIND_LABEL,
  resultSummary, trendSparkline,
} from '../components.js';

export async function render(root, ctx) {
  if (ctx.params.org) return renderOrg(root, ctx);
  root.append(pageHead('Teams', 'Organizations your GitHub account belongs to. Team dashboards include runs by every member on the organization’s repositories.'));
  const slot = h('div');
  root.append(slot);
  slot.append(loading('Loading organizations from GitHub…'));
  let orgs;
  try {
    orgs = (await api('/api/github/orgs')).orgs;
  } catch (err) {
    slot.replaceChildren(banner('error', 'Could not load organizations', err.message));
    return;
  }
  if (!orgs.length) {
    slot.replaceChildren(card(null, null, emptyState('No organizations',
      'Your GitHub account is not a member of any organization this app can see. If an organization restricts OAuth apps, an owner has to approve this app under Settings → Third-party access on GitHub.')));
    return;
  }
  slot.replaceChildren(card(null, null, dataTable([
    { label: 'Organization', render: (o) => h('div', { class: 'user' },
      o.avatar_url ? h('img', { class: 'avatar', src: o.avatar_url, alt: '' }) : null,
      h('div', {}, h('div', { class: 'mono', text: o.login }), o.description ? h('div', { class: 'muted', text: o.description }) : null)) },
    { label: '', render: (o) => h('a', { class: 'btn btn-sm', href: `#/teams/${encodeURIComponent(o.login)}` }, 'Open dashboard') },
  ], orgs, { onRowClick: (o) => ctx.navigate(`#/teams/${encodeURIComponent(o.login)}`) }), { flush: true }));
}

async function renderOrg(root, { params, navigate }) {
  const org = params.org;
  root.append(h('div', { class: 'breadcrumb' }, h('a', { href: '#/teams', text: 'Teams' }), ` / ${org}`));
  const slot = h('div');
  root.append(slot);
  slot.append(loading(`Loading ${org} from GitHub…`));
  let d;
  try {
    d = await api(`/api/dashboard/team/${encodeURIComponent(org)}`);
  } catch (err) {
    slot.replaceChildren(banner('error', `Could not load ${org}`, err.message));
    return;
  }
  slot.replaceChildren();
  slot.append(pageHead(d.org.name || d.org.login, d.org.description || null,
    h('a', { class: 'btn', href: d.org.html_url, target: '_blank', rel: 'noopener' }, 'View on GitHub')));
  for (const e of d.errors) slot.append(banner('warn', 'Partial data from GitHub', e));

  const t = d.totals;
  slot.append(tiles(
    tile('Repositories', t.repositories),
    tile('Members', t.members),
    tile('Repositories analyzed', t.repositories_analysed, t.repositories ? `of ${fmtNum(t.repositories)}` : null),
    tile('Analyses', t.runs_30d, 'last 30 days'),
    tile('Open findings', t.open_findings, 'latest run per repository'),
    tile('Failing tests', t.failing_tests, 'latest run per repository'),
  ));

  slot.append(card('Repositories', 'Latest analysis per repository by any member. Unanalyzed repositories are listed after analyzed ones.', dataTable([
    { label: 'Repository', render: (r) => h('div', {},
      h('a', { href: r.html_url, target: '_blank', rel: 'noopener', class: 'mono', text: r.name }),
      h('div', { class: 'muted', text: [r.private ? 'Private' : 'Public', r.language].filter(Boolean).join(' · ') })) },
    { label: 'Last push', render: (r) => h('span', { class: 'nowrap', text: relTime(r.pushed_at) }) },
    { label: 'Latest analysis', render: (r) => (r.latest
      ? h('div', {}, h('a', { href: `#/runs/${r.latest.id}`, text: relTime(r.latest.created_at) }),
        h('div', { class: 'muted', text: `${r.latest.user_login} · ${r.latest.ref || ''} @ ${shortSha(r.latest.commit_sha)}` }))
      : h('span', { class: 'muted', text: 'Not analyzed' })) },
    { label: 'Result', render: (r) => (r.latest ? resultSummary(r.latest.headline) : '') },
    { label: 'Trend', render: (r) => trendSparkline(r.trend) },
    { label: '', render: (r) => h('button', {
      type: 'button', class: 'btn btn-sm',
      onclick: () => navigate(`#/repos?repo=${encodeURIComponent(r.full_name)}&branch=${encodeURIComponent(r.default_branch || '')}`),
    }, 'Analyze') },
  ], d.repositories, { empty: 'This organization has no repositories visible to you.' }), { flush: true }));

  slot.append(card('Contributors', 'Members who have run analyses on this organization’s repositories.', dataTable([
    { label: 'Member', render: (m) => h('span', { class: 'user' }, m.avatar_url ? h('img', { class: 'avatar avatar-sm', src: m.avatar_url, alt: '' }) : null, m.login) },
    { label: 'Analyses', num: true, render: (m) => fmtNum(m.runs) },
    { label: 'Last 30 days', num: true, render: (m) => fmtNum(m.runs_30d) },
    { label: 'Last run', render: (m) => relTime(m.last_run_at) },
  ], d.contributors, { empty: 'No member has analyzed a repository of this organization yet.' }), { flush: true }));

  slot.append(card('Activity', null, dataTable([
    { label: 'Started', render: (r) => h('span', { class: 'nowrap', text: relTime(r.created_at) }) },
    { label: 'Repository', render: (r) => h('span', { class: 'mono', text: r.project }) },
    { label: 'Type', render: (r) => KIND_LABEL[r.kind] },
    { label: 'By', render: (r) => r.user_login },
    { label: 'Status', render: (r) => statusBadge(r.status) },
  ], d.activity, { onRowClick: (r) => navigate(`#/runs/${r.id}`), empty: 'No activity yet.' }), { flush: true }));
}
