// Repositories: analyze any repository the signed-in GitHub account can read.
import { api, postJSON } from '../api.js';
import { h, relTime, fmtNum } from '../dom.js';
import { pageHead, card, banner, dataTable, loading, modelLabel } from '../components.js';

export async function render(root, { me, navigate, query }) {
  root.append(pageHead('Repositories',
    `Analyze a GitHub repository at the current commit of a branch. Source files in every language are selected (vendor, build output and generated files are skipped), reviewed by ${modelLabel(me)}, and tested where a sandbox runner exists.`));

  const errorSlot = h('div');
  const repoInput = h('input', { class: 'input', id: 'repo', placeholder: 'owner/name or https://github.com/owner/name', autocomplete: 'off', spellcheck: 'false', value: query.repo || '' });
  const branchInput = h('input', { class: 'input', id: 'branch', placeholder: 'Default branch', autocomplete: 'off', value: query.branch || '' });
  const maxInput = h('input', { class: 'input', id: 'max', type: 'number', min: 1, max: me.limits.repo_files, value: Math.min(20, me.limits.repo_files) });
  const submit = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Analyze repository');

  async function start(repo, branch) {
    errorSlot.replaceChildren();
    submit.disabled = true;
    submit.textContent = 'Starting…';
    try {
      const { id } = await postJSON('/api/runs/repo', {
        repo, branch: branch || null, max_files: parseInt(maxInput.value, 10) || null,
      });
      navigate(`#/runs/${id}`);
    } catch (err) {
      errorSlot.append(banner('error', 'Could not start the analysis', err.message));
      submit.disabled = false;
      submit.textContent = 'Analyze repository';
    }
  }

  const form = h('form', { novalidate: true },
    h('div', { class: 'form-row' },
      h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'repo', text: 'Repository' }), repoInput),
      h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'branch', text: 'Branch' }), branchInput),
      h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'max', text: 'Files to analyze' }), maxInput,
        h('div', { class: 'field-hint', text: `1–${me.limits.repo_files}. Application code is picked before tests.` }))),
    errorSlot,
    h('div', { class: 'actions' }, submit));
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (!repoInput.value.trim()) {
      errorSlot.replaceChildren(banner('error', null, 'Enter a repository.'));
      return;
    }
    start(repoInput.value.trim(), branchInput.value.trim());
  });
  root.append(card('Analyze a repository', null, form));

  // Live list of the user's repositories from GitHub.
  const listSlot = h('div');
  const filter = h('input', { class: 'input input-inline', type: 'search', placeholder: 'Filter repositories', 'aria-label': 'Filter repositories' });
  root.append(card('Your GitHub repositories', 'Owned, collaborator and organization repositories, most recently pushed first.', listSlot,
    { flush: true, actions: [filter] }));
  listSlot.append(loading('Loading repositories from GitHub…'));

  let repos = [];
  try {
    repos = (await api('/api/github/repos')).repos;
  } catch (err) {
    listSlot.replaceChildren(banner('error', 'Could not load repositories', err.message));
    return;
  }

  function draw() {
    const q = filter.value.trim().toLowerCase();
    const rows = repos.filter((r) => !q || r.full_name.toLowerCase().includes(q) || (r.language || '').toLowerCase().includes(q));
    listSlot.replaceChildren(dataTable([
      { label: 'Repository', render: (r) => h('div', {},
        h('a', { href: r.html_url, target: '_blank', rel: 'noopener', class: 'mono', text: r.full_name }),
        r.description ? h('div', { class: 'muted', text: r.description }) : null) },
      { label: 'Visibility', render: (r) => (r.private ? 'Private' : 'Public') + (r.archived ? ' · archived' : '') },
      { label: 'Language', render: (r) => r.language || '—' },
      { label: 'Last push', render: (r) => h('span', { class: 'nowrap', text: relTime(r.pushed_at) }) },
      { label: 'Open issues', num: true, render: (r) => fmtNum(r.open_issues) },
      { label: '', render: (r) => h('button', {
        type: 'button', class: 'btn btn-sm',
        onclick: () => { repoInput.value = r.full_name; branchInput.value = r.default_branch || ''; start(r.full_name, r.default_branch); },
      }, 'Analyze') },
    ], rows.slice(0, 200), { empty: q ? 'No repositories match the filter.' : 'Your account has no repositories.' }));
  }
  filter.addEventListener('input', draw);
  draw();
}
