// Run detail: progress while running, then the full report.
import { api } from '../api.js';
import { h, icon, fmtDate, fmtDuration, fmtNum, shortSha, plural } from '../dom.js';
import {
  card, tile, tiles, banner, dataTable, emptyState, loading, statusBadge, sevBadge, KIND_LABEL, tabs,
  codeViewer, copyButton, downloadButton,
} from '../components.js';

const TRIAGE_LABEL = { code_defect: 'Code defect', test_error: 'Wrong test expectation', environment: 'Sandbox limitation' };

export async function render(root, { params, navigate }) {
  const slot = h('div');
  root.append(slot);
  slot.append(loading());
  let timer = null;
  let stopped = false;

  async function load() {
    let run;
    try {
      run = await api(`/api/runs/${encodeURIComponent(params.id)}`);
    } catch (err) {
      slot.replaceChildren(banner('error', 'Could not load this run', err.message));
      return;
    }
    if (stopped) return;
    slot.replaceChildren(...draw(run, navigate));
    if (run.status === 'queued' || run.status === 'running') timer = setTimeout(load, 2000);
  }
  await load();
  return () => { stopped = true; clearTimeout(timer); };
}

function draw(run, navigate) {
  const out = [];
  out.push(h('div', { class: 'breadcrumb' }, h('a', { href: '#/history', text: 'History' }), ` / ${run.project}`));
  const meta = [KIND_LABEL[run.kind], `started ${fmtDate(run.created_at)}`, `by ${run.user_login}`];
  if (run.duration_ms) meta.push(fmtDuration(run.duration_ms));
  if (run.model) meta.push(run.model);
  const actions = [];
  if (run.kind === 'repo' && run.commit_sha) {
    actions.push(h('a', { class: 'btn', href: `https://github.com/${run.project}/tree/${run.commit_sha}`, target: '_blank', rel: 'noopener' },
      icon('branch'), `${run.ref} @ ${shortSha(run.commit_sha)}`));
  }
  if (run.report) actions.push(downloadButton(`run-${run.id}.json`, () => JSON.stringify(run, null, 2), 'Download JSON', 'application/json'));
  if (run.is_mine && run.status !== 'queued' && run.status !== 'running') {
    actions.push(h('button', {
      type: 'button', class: 'btn btn-danger',
      onclick: async () => {
        if (!window.confirm('Delete this run from history? This cannot be undone.')) return;
        await api(`/api/runs/${run.id}`, { method: 'DELETE' });
        navigate('#/history');
      },
    }, icon('trash'), 'Delete'));
  }
  out.push(h('div', { class: 'page-head' },
    h('div', {}, h('h1', { class: 'mono', text: run.project }), h('p', { text: meta.join(' · ') })),
    h('div', { class: 'actions' }, statusBadge(run.status), ...actions)));

  if (run.status === 'queued' || run.status === 'running') {
    const bar = h('div');
    bar.style.width = `${run.progress || 1}%`;
    out.push(card('In progress', 'This page refreshes automatically. You can leave it; the run continues on the server.',
      h('div', {}, h('div', { class: 'progress', role: 'progressbar', 'aria-valuenow': run.progress, 'aria-valuemin': 0, 'aria-valuemax': 100 }, bar),
        h('p', { class: 'secondary', text: `${run.progress || 0}% — ${run.progress_note || 'Queued'}` }))));
    return out;
  }
  if (run.status === 'failed') {
    out.push(banner('error', 'The run failed', run.error || 'Unknown error.'));
    return out;
  }
  if (!run.report) {
    out.push(banner('warn', 'No report', 'This run finished without a report.'));
    return out;
  }
  out.push(...(run.kind === 'spec' ? specReport(run) : codeReport(run)));
  return out;
}

// ── Code & repository reports ─────────────────────────────────────────────

function codeReport(run) {
  const out = [];
  const s = run.summary || {};
  const files = run.report.files || [];
  const aiErrors = files.filter((f) => f.review.status === 'error');
  const noAI = files.length > 0 && aiErrors.length === files.length;
  const noTests = !s.files_with_tests_executed;
  out.push(tiles(
    tile('Files analyzed', files.length, run.kind === 'repo' ? `${fmtNum(s.source_files)} source files in the repository` : null),
    noAI ? tile('AI findings', '—', 'AI review did not run')
      : tile('AI findings', s.findings || 0, `${fmtNum(s.critical || 0)} critical · ${fmtNum(s.high || 0)} high`
        + (aiErrors.length ? ` · ${plural(aiErrors.length, 'file')} not reviewed` : '')),
    tile('Compiler & linter errors', s.diagnostic_errors || 0, `${fmtNum(s.diagnostic_warnings || 0)} warnings`),
    noTests ? tile('Tests passed', '—', 'no tests were executed') : tile('Tests passed', s.tests_passed || 0),
    noTests ? tile('Tests failed', '—', 'no tests were executed')
      : tile('Tests failed', s.tests_failed || 0, `executed for ${plural(s.files_with_tests_executed, 'file')}`),
  ));
  if (aiErrors.length) {
    out.push(banner('warn', `AI review did not complete for ${plural(aiErrors.length, 'file')}`,
      `${aiErrors[0].review.error} Compiler and linter results are still shown.`));
  }
  if (run.kind === 'repo') {
    const sel = run.report.selection || {};
    const skipped = [];
    if (sel.vendor_or_build) skipped.push(`${fmtNum(sel.vendor_or_build)} vendor/build`);
    if (sel.generated) skipped.push(`${fmtNum(sel.generated)} generated`);
    if (sel.too_large) skipped.push(`${fmtNum(sel.too_large)} over 200 KB`);
    const text = `Analyzed ${fmtNum(files.length)} of ${fmtNum(sel.candidates)} source files (limit ${fmtNum(sel.max_files)}).`
      + (skipped.length ? ` Skipped: ${skipped.join(', ')}.` : '')
      + (run.report.unreadable && run.report.unreadable.length ? ` Unreadable: ${run.report.unreadable.join('; ')}.` : '')
      + (run.report.tree_truncated ? ' GitHub truncated the file tree for this very large repository.' : '');
    out.push(banner('info', null, text));
  }
  if (!files.length) {
    out.push(card(null, null, emptyState('No source files', 'No analyzable source files were found.')));
    return out;
  }

  const detail = h('div');
  const nav = h('div', { class: 'card file-nav' });
  const buttons = files.map((f, i) => {
    const c = f.counts;
    const btn = h('button', { type: 'button', onclick: () => select(i) },
      h('div', { class: 'path', text: f.file }),
      h('div', { class: 'meta' },
        h('span', { class: 'badge', text: f.language }),
        (c.critical + c.high + c.medium + c.low) ? sevBadge(c.critical ? 'critical' : c.high ? 'high' : c.medium ? 'medium' : 'low', c.critical + c.high + c.medium + c.low) : null,
        c.diagnostic_errors ? sevBadge('error', c.diagnostic_errors) : null,
        c.tests_executed ? statusBadge(c.tests_failed ? 'failed' : 'passed', `${c.tests_passed}/${c.tests_passed + c.tests_failed} tests`) : null));
    nav.append(btn);
    return btn;
  });
  function select(i) {
    buttons.forEach((b, j) => b.setAttribute('aria-current', String(i === j)));
    detail.replaceChildren(fileDetail(files[i]));
  }
  // Open the file with the most severe result first.
  const score = (f) => f.counts.critical * 1000 + f.counts.high * 100 + f.counts.tests_failed * 50 + f.counts.diagnostic_errors * 20 + f.counts.medium;
  let first = 0;
  files.forEach((f, i) => { if (score(f) > score(files[first])) first = i; });
  select(first);
  out.push(h('div', { class: 'split' }, nav, detail));
  return out;
}

function fileDetail(f) {
  const review = f.review;
  const exec = f.tests.execution;
  const flags = new Map();
  const flag = (line, text) => { if (!line) return; flags.set(line, [...(flags.get(line) || []), text]); };
  for (const fd of review.findings) for (let l = fd.line; l && l <= (fd.end_line || fd.line); l++) flag(l, `${fd.severity}: ${fd.title}`);
  for (const d of f.static.diagnostics) if (d.severity !== 'info') flag(d.line, `${d.tool}: ${d.message}`);

  const viewer = codeViewer(f.source, flags);
  let tabView;
  const goToLine = (n) => {
    tabView.select('source');
    requestAnimationFrame(() => viewer.scrollToLine(n));
  };
  const lineButton = (a, b) => (a ? h('button', { type: 'button', class: 'line-link', onclick: () => goToLine(a), text: b && b !== a ? `lines ${a}–${b}` : `line ${a}` }) : null);

  const findingsPane = () => {
    if (!review.findings.length) {
      return emptyState(review.status === 'ok' ? 'No defects reported' : 'AI review did not run',
        review.status === 'ok' ? 'The model reported no issues in this file. Check the Tests tab for executed results.' : review.error);
    }
    return h('div', {}, review.findings.map((fd) => h('div', { class: 'finding' },
      h('div', { class: 'finding-head' }, sevBadge(fd.severity), h('span', { class: 'badge', text: fd.category }),
        fd.confirmed_by_test ? h('span', { class: 'badge status-failed', title: 'A generated test fails because of this defect' }, icon('x'), `Confirmed by failing test ${fd.confirmed_by_test}`) : null,
        lineButton(fd.line, fd.end_line)),
      h('div', { class: 'finding-title', text: fd.title }),
      fd.explanation ? h('p', { text: fd.explanation }) : null,
      fd.suggestion ? h('p', {}, h('span', { class: 'label-inline', text: 'Fix: ' }), fd.suggestion) : null,
      fd.fixed_code ? h('pre', { class: 'block', text: fd.fixed_code }) : null)));
  };

  const testsPane = () => {
    const parts = [];
    const head = [statusBadge(exec.status)];
    if (exec.runner) head.push(h('span', { class: 'badge', text: exec.runner }));
    if (exec.executed) head.push(h('span', { class: 'secondary', text: `${exec.passed} passed · ${exec.failed} failed · ${exec.errors} errors · ${exec.skipped} skipped` }));
    if (exec.coverage_percent !== null && exec.coverage_percent !== undefined) head.push(h('span', { class: 'secondary', text: `line coverage ${exec.coverage_percent}%` }));
    parts.push(h('div', { class: 'card-body' }, h('div', { class: 'actions' }, head), exec.reason ? h('p', { class: 'secondary', text: exec.reason }) : null,
      f.tests.triage_error ? h('p', { class: 'muted', text: `Failure triage unavailable: ${f.tests.triage_error}` }) : null));
    if (exec.tests.length) {
      parts.push(dataTable([
        { label: 'Test', render: (t) => h('span', { class: 'mono', text: t.name }) },
        { label: 'Result', render: (t) => statusBadge(t.status) },
        { label: 'Diagnosis', render: (t) => (t.triage ? h('div', {}, h('strong', { text: TRIAGE_LABEL[t.triage.verdict] }),
          t.triage.line ? h('span', {}, ' at ', lineButton(t.triage.line)) : null,
          h('div', { class: 'secondary', text: t.triage.explanation })) : '') },
        { label: 'Output', render: (t) => (t.message ? h('details', { class: 'disclosure' }, h('summary', {}, 'Show'), h('pre', { class: 'block wrap', text: t.message })) : '') },
      ], exec.tests));
    }
    if (f.tests.code) {
      const name = f.tests.file && !f.tests.file.startsWith('(') ? f.tests.file : `tests-${f.file.split('/').pop()}.txt`;
      parts.push(h('div', { class: 'card-body' },
        h('div', { class: 'actions' }, h('h3', { text: `Generated tests (${f.tests.framework})` }), copyButton(() => f.tests.code), downloadButton(name, () => f.tests.code)),
        h('pre', { class: 'block', text: f.tests.code })));
    }
    if (exec.output) {
      parts.push(h('div', { class: 'card-body' }, h('details', { class: 'disclosure' }, h('summary', {}, 'Runner output'), h('pre', { class: 'block wrap', text: exec.output }))));
    }
    return h('div', {}, parts);
  };

  const diagnosticsPane = () => h('div', {},
    dataTable([
      { label: 'Tool', render: (t) => h('span', { class: 'mono', text: t.tool }) },
      { label: 'Status', render: (t) => statusBadge(t.status) },
      { label: 'Detail', render: (t) => t.detail || '' },
    ], f.static.tools),
    f.static.diagnostics.length ? dataTable([
      { label: 'Line', render: (d) => lineButton(d.line) || '—' },
      { label: 'Severity', render: (d) => sevBadge(d.severity) },
      { label: 'Tool', render: (d) => h('span', { class: 'mono', text: d.code ? `${d.tool} ${d.code}` : d.tool }) },
      { label: 'Message', render: (d) => d.message },
    ], f.static.diagnostics) : emptyState('No diagnostics', 'The compiler and linter reported nothing for this file.'));

  tabView = tabs([
    { id: 'findings', label: 'Findings', count: review.findings.length, render: findingsPane },
    { id: 'tests', label: 'Tests', count: exec.tests.length, render: testsPane },
    { id: 'diagnostics', label: 'Compiler & linter', count: f.static.diagnostics.length, render: diagnosticsPane },
    { id: 'source', label: 'Source', render: () => viewer },
  ]);

  return h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('div', {}, h('h2', { class: 'mono', text: f.file }),
        h('p', { text: [f.language, `${fmtNum(f.lines)} lines`, f.truncated ? 'review truncated to the first 2,500 lines' : null].filter(Boolean).join(' · ') }))),
    review.status === 'error' ? h('div', { class: 'card-body' }, banner('warn', 'AI review unavailable for this file', review.error)) : null,
    review.summary ? h('div', { class: 'card-body' }, h('p', { class: 'secondary', text: review.summary })) : null,
    tabView);
}

// ── Spec reports ──────────────────────────────────────────────────────────

const KIND_NAME = { openapi: 'OpenAPI', graphql: 'GraphQL SDL', json: 'JSON', yaml: 'YAML' };
const docLabel = (r) => (r.kind === 'openapi' && String(r.spec_version || '').startsWith('2') ? 'Swagger' : KIND_NAME[r.kind] || r.kind);

function specReport(run) {
  const r = run.report;
  const s = run.summary || {};
  const out = [];
  const live = r.live;
  out.push(tiles(
    tile('Document', `${docLabel(r)}${r.spec_version ? ` ${r.spec_version}` : ''}`, r.title),
    tile('Errors', s.issues_error || 0, `${fmtNum(s.issues_warning || 0)} warnings · ${fmtNum(s.issues_info || 0)} info`),
    r.operations.length ? tile('Operations', r.operations.length, r.operations_skipped ? `${fmtNum(r.operations_skipped)} over the cap` : null) : null,
    live ? tile('Live checks', `${fmtNum(s.live_passed)} / ${fmtNum(s.live_passed + s.live_failed)}`, `passed against ${live.base_url}`) : null,
    s.tests_generated ? tile('Generated tests', `${fmtNum(s.tests_passed)} passed`, `${fmtNum(s.tests_failed)} failed`) : null,
  ));
  if (r.ai && r.ai.error) out.push(banner('warn', 'Test generation did not complete', r.ai.error));
  if (r.schema_validation) {
    const v = r.schema_validation;
    out.push(banner(v.valid ? 'info' : 'error', v.valid ? 'Valid against the JSON Schema' : 'Does not match the JSON Schema',
      v.valid ? `${v.title || 'Schema'} (${v.draft || 'JSON Schema'})` : `${fmtNum(v.errors || 0)} validation errors — see Issues.`));
  }

  const viewer = codeViewer(r.source || '', new Map(r.issues.filter((i) => i.line && i.severity !== 'info').map((i) => [i.line, [`${i.rule}: ${i.message}`]])));
  let tabView;
  const goToLine = (n) => { tabView.select('source'); requestAnimationFrame(() => viewer.scrollToLine(n)); };

  const defs = [{
    id: 'issues', label: 'Issues', count: r.issues.length,
    render: () => dataTable([
      { label: 'Severity', render: (i) => sevBadge(i.severity) },
      { label: 'Line', render: (i) => (i.line ? h('button', { type: 'button', class: 'line-link', onclick: () => goToLine(i.line), text: `${i.line}${i.column ? `:${i.column}` : ''}` }) : '—') },
      { label: 'Rule', render: (i) => h('span', { class: 'mono', text: i.rule }) },
      { label: 'Message', render: (i) => h('div', {}, i.message, i.pointer ? h('div', { class: 'muted mono', text: i.pointer }) : null) },
    ], r.issues, { empty: 'No issues found.' }),
  }];
  if (live) {
    defs.push({ id: 'live', label: 'Live checks', count: (live.operations || live.checks || []).length, render: () => liveView(live) });
  }
  if (r.operations.length) {
    defs.push({
      id: 'operations', label: 'Operations', count: r.operations.length,
      render: () => dataTable([
        { label: 'Operation', render: (o) => h('span', { class: 'mono', text: o.type === 'rest' ? `${o.method} ${o.path}` : `${o.gql_operation} ${o.name}` }) },
        { label: 'Parameters', render: (o) => (o.params.length ? o.params.map((p) => `${p.name}${p.required ? '*' : ''}: ${p.type}`).join(', ') : '—') },
        { label: 'Responses', render: (o) => (o.responses.length ? o.responses.join(', ') : (o.return_type || '—')) },
        { label: 'Summary', render: (o) => o.summary || '' },
      ], r.operations),
    });
  }
  if (r.tests.length) {
    defs.push({ id: 'tests', label: 'Generated tests', count: r.tests.length, render: () => specTests(r) });
  }
  defs.push({ id: 'source', label: 'Source', render: () => viewer });
  tabView = tabs(defs);
  out.push(h('section', { class: 'card' }, tabView));
  return out;
}

function checkRow(c) {
  const state = c.passed === true ? ['passed', 'check'] : c.passed === false ? ['failed', 'x'] : ['skipped', 'minus'];
  return h('div', { class: 'finding-head' }, statusBadge(state[0], c.passed === null ? 'Note' : null), h('strong', { text: c.name }), h('span', { class: 'secondary', text: c.detail || '' }));
}

function liveView(live) {
  if (live.error) return h('div', { class: 'card-body' }, banner('error', 'Live checks could not run', live.error));
  if (live.operations) {
    return h('div', {}, live.operations.map((o) => h('div', { class: 'finding' },
      h('div', { class: 'finding-head' }, statusBadge(o.status), h('strong', { class: 'mono', text: o.operation }),
        o.status_code ? h('span', { class: 'badge', text: `HTTP ${o.status_code}` }) : null,
        o.duration_ms !== undefined ? h('span', { class: 'muted', text: fmtDuration(o.duration_ms) }) : null),
      o.reason ? h('p', { text: o.reason }) : null,
      o.url ? h('div', { class: 'muted mono', text: o.url }) : null,
      (o.checks || []).map(checkRow))));
  }
  return dataTable([
    { label: 'Result', render: (c) => statusBadge(c.passed === true ? 'passed' : c.passed === false ? 'failed' : 'skipped', c.passed === null ? 'Note' : null) },
    { label: 'Type / field', render: (c) => h('span', { class: 'mono', text: c.name }) },
    { label: 'Detail', render: (c) => c.detail },
  ], live.checks, { empty: 'No comparable types found.' });
}

function specTests(r) {
  return h('div', {},
    r.tests_note ? h('div', { class: 'card-body' }, h('p', { class: 'secondary', text: r.tests_note })) : null,
    r.tests.map((t) => {
      const ex = t.execution || {};
      const name = `test_${t.operation.replace(/\W+/g, '_').replace(/^_|_$/g, '').toLowerCase()}.py`;
      return h('div', { class: 'finding' },
        h('div', { class: 'finding-head' }, statusBadge(ex.status || 'not_executed'), h('strong', { class: 'mono', text: t.operation }),
          ex.executed ? h('span', { class: 'secondary', text: `${ex.passed} passed · ${ex.failed + ex.errors} failed` }) : null),
        t.error ? h('p', { text: t.error }) : null,
        ex.reason ? h('p', { text: ex.reason }) : null,
        (ex.tests || []).filter((x) => x.status !== 'passed').map((x) => h('details', { class: 'disclosure' },
          h('summary', {}, `${x.name} — ${x.status}`), h('pre', { class: 'block wrap', text: x.message || '' }))),
        t.code ? h('details', { class: 'disclosure' }, h('summary', {}, 'Test code'),
          h('div', { class: 'actions' }, copyButton(() => t.code), downloadButton(name, () => t.code)),
          h('pre', { class: 'block', text: t.code })) : null);
    }));
}
