/* ── app.js — AI Test Generator Frontend ─────────────────────────────── */
'use strict';

// ── Tab switching ─────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(target).classList.add('active');
    clearResults();
    clearBanner();
  });
});

// ── Shared state ──────────────────────────────────────────────────────────
let lastResponse = null;   // raw JSON from last successful call

// ── DOM helpers ───────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

function setLoading(btn, on) {
  if (on) { btn.classList.add('loading'); btn.disabled = true; }
  else     { btn.classList.remove('loading'); btn.disabled = false; }
}

function showBanner(type, msg) {
  const el = $('global-banner');
  el.className = `banner ${type} visible`;
  el.textContent = msg;
  el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function clearBanner() {
  const el = $('global-banner');
  el.className = 'banner';
  el.textContent = '';
}

function clearResults() {
  $('results-section').innerHTML = '';
  lastResponse = null;
}

function setInlineError(fieldId, errId, msg) {
  const field = $(fieldId);
  const err   = $(errId);
  if (msg) {
    field.classList.add('error');
    err.textContent = msg;
    err.classList.add('visible');
  } else {
    field.classList.remove('error');
    err.classList.remove('visible');
  }
}

// ── Fetch wrapper with error normalisation ────────────────────────────────
async function apiFetch(path, init) {
  let resp;
  try {
    resp = await fetch(path, init);
  } catch (e) {
    throw new Error(
      `Network error — could not reach the backend at ${window.location.origin}. ` +
      `Is uvicorn running? (${e.message})`
    );
  }
  if (!resp.ok) {
    let detail;
    try {
      const body = await resp.json();
      detail = body.detail || JSON.stringify(body);
    } catch (_) {
      detail = await resp.text();
    }
    const prefix = resp.status === 502
      ? '502 — Upstream failure (GitHub or LLM API): '
      : `${resp.status} — `;
    throw new Error(prefix + detail);
  }
  return resp.json();
}

// ── Download JSON ─────────────────────────────────────────────────────────
function attachDownload(data, filename) {
  const btn = document.createElement('button');
  btn.className = 'btn btn-secondary';
  btn.innerHTML = '⬇ Download full JSON report';
  btn.addEventListener('click', () => {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
  });
  return btn;
}

// ── Coverage bar ──────────────────────────────────────────────────────────
function coverageBar(pct) {
  if (pct === null || pct === undefined) {
    return `<div class="cov-not-measured">Coverage: Not measured</div>`;
  }
  const cls = pct >= 80 ? '' : pct >= 50 ? 'mid' : 'low';
  return `
    <div class="cov-row">
      <div class="cov-label">
        <span>Coverage</span>
        <span class="cov-pct">${pct.toFixed(1)}%</span>
      </div>
      <div class="cov-bar-track">
        <div class="cov-bar-fill ${cls}" style="width:${Math.min(pct,100)}%"></div>
      </div>
    </div>`;
}

// ── Function result block ─────────────────────────────────────────────────
function renderFunction(fn) {
  const isMock = fn.generation_mode === 'mock';
  const badge  = isMock
    ? `<span class="badge badge-mock">mock</span>`
    : (fn.generation_mode === 'llm' ? `<span class="badge badge-llm">llm</span>` : '');

  const passColor = fn.tests_passed > 0 ? 'si-pass' : 'si-val';
  const failColor = fn.tests_failed > 0 ? 'si-fail' : 'si-val';

  const flagsHtml = (fn.flags && fn.flags.length)
    ? `<ul class="flags-list">${fn.flags.map(f => `<li>${esc(f)}</li>`).join('')}</ul>`
    : '';

  const mockWarn = isMock && fn.warning
    ? `<div class="mock-warning">⚠ ${esc(fn.warning)}</div>`
    : '';

  // signature minus the function name prefix if the name is already shown
  const sigDisplay = fn.signature && fn.signature !== fn.name ? fn.signature : '';

  const block = document.createElement('div');
  block.className = 'fn-block';
  block.innerHTML = `
    <div class="fn-header">
      <span class="fn-name">${esc(fn.name)}</span>
      <span class="fn-sig">${esc(sigDisplay)}</span>
      <div class="fn-badges">${badge}</div>
      <span class="fn-chevron">▶</span>
    </div>
    <div class="fn-body">
      ${mockWarn}
      <div class="stats-row">
        <div class="stat-item">
          <span class="si-label">Generated:</span>
          <span class="si-val">${fn.tests_generated}</span>
        </div>
        <div class="stat-item">
          <span class="si-label">Passed:</span>
          <span class="${passColor}">${fn.tests_passed}</span>
        </div>
        <div class="stat-item">
          <span class="si-label">Failed:</span>
          <span class="${failColor}">${fn.tests_failed}</span>
        </div>
      </div>
      ${coverageBar(isMock ? null : fn.coverage_percent)}
      ${flagsHtml}
    </div>`;

  block.querySelector('.fn-header').addEventListener('click', () => block.classList.toggle('open'));
  return block;
}

// ── /analyze — per-function results (no repo wrapper) ────────────────────
function renderAnalyzeResults(data) {
  const section = $('results-section');
  section.innerHTML = '';

  const toolbar = document.createElement('div');
  toolbar.className = 'results-toolbar';
  toolbar.appendChild(attachDownload(data, 'analyze-result.json'));
  section.appendChild(toolbar);

  if (!data.length) {
    section.insertAdjacentHTML('beforeend',
      `<div class="empty-state"><div class="es-icon">🔍</div>
       <div class="es-text">No top-level functions found in this file.</div></div>`);
    return;
  }

  // /analyze returns a flat list of function results — no file grouping
  data.forEach(fn => section.appendChild(renderFunction(fn)));
}

// ── /analyze-repo — metadata + file cards ────────────────────────────────
function renderRepoResults(data) {
  const section = $('results-section');
  section.innerHTML = '';

  // Toolbar
  const toolbar = document.createElement('div');
  toolbar.className = 'results-toolbar';
  toolbar.appendChild(attachDownload(data, 'analyze-repo-result.json'));
  section.appendChild(toolbar);

  // Metadata chips
  const skipped = data.files_skipped_due_to_limit || 0;
  const warnClass = skipped > 0 ? 'warn-chip' : '';
  section.insertAdjacentHTML('beforeend', `
    <div class="repo-meta">
      <div class="meta-chip">
        <span class="mc-label">py files found</span>
        <span class="mc-value">${data.python_files_found}</span>
      </div>
      <div class="meta-chip">
        <span class="mc-label">files selected</span>
        <span class="mc-value">${data.files_selected}</span>
      </div>
      <div class="meta-chip ${warnClass}">
        <span class="mc-label">skipped (limit)</span>
        <span class="mc-value">${skipped}</span>
      </div>
      <div class="meta-chip">
        <span class="mc-label">files analyzed</span>
        <span class="mc-value">${data.files_analyzed}</span>
      </div>
      <div class="meta-chip">
        <span class="mc-label">max_files cap</span>
        <span class="mc-value">${data.max_files}</span>
      </div>
    </div>`);

  if (!data.results || !data.results.length) {
    section.insertAdjacentHTML('beforeend',
      `<div class="empty-state"><div class="es-icon">📭</div>
       <div class="es-text">No Python files were found in this repository.</div></div>`);
    return;
  }

  data.results.forEach(fileEntry => {
    const totalPass = (fileEntry.functions || []).reduce((s, f) => s + (f.tests_passed || 0), 0);
    const totalFail = (fileEntry.functions || []).reduce((s, f) => s + (f.tests_failed || 0), 0);

    const card = document.createElement('div');
    card.className = 'file-card';

    const statsHtml = fileEntry.parse_error
      ? `<span class="stat-pill pill-red">parse error</span>`
      : `<span class="stat-pill pill-green">${totalPass} passed</span>
         ${totalFail ? `<span class="stat-pill pill-red">${totalFail} failed</span>` : ''}`;

    card.innerHTML = `
      <div class="file-card-header">
        <span class="file-path">${esc(fileEntry.file)}</span>
        <div class="file-stats">${statsHtml}</div>
        <span class="chevron">▶</span>
      </div>
      <div class="file-card-body"></div>`;

    card.querySelector('.file-card-header').addEventListener('click', () => card.classList.toggle('open'));

    const body = card.querySelector('.file-card-body');
    if (fileEntry.parse_error) {
      body.innerHTML = `<div class="parse-error">Parse error: ${esc(fileEntry.parse_error)}</div>`;
    } else if (!fileEntry.functions || !fileEntry.functions.length) {
      body.innerHTML = `<p style="font-size:12px;color:var(--text-muted)">No top-level functions found.</p>`;
    } else {
      fileEntry.functions.forEach(fn => body.appendChild(renderFunction(fn)));
    }

    section.appendChild(card);
  });
}

// ── /analyze-spec — operation cards ──────────────────────────────────────
function renderSpecResults(data) {
  const section = $('results-section');
  section.innerHTML = '';

  const toolbar = document.createElement('div');
  toolbar.className = 'results-toolbar';
  toolbar.appendChild(attachDownload(data, 'analyze-spec-result.json'));
  section.appendChild(toolbar);

  // Spec summary chips
  const skipped = data.operations_skipped || 0;
  section.insertAdjacentHTML('beforeend', `
    <div class="spec-meta">
      <div class="meta-chip">
        <span class="mc-label">spec type</span>
        <span class="mc-value" style="font-size:13px">${esc(data.spec_type || '—')}</span>
      </div>
      <div class="meta-chip">
        <span class="mc-label">operations processed</span>
        <span class="mc-value">${data.operations_processed}</span>
      </div>
      ${skipped ? `<div class="meta-chip warn-chip">
        <span class="mc-label">skipped (cap)</span>
        <span class="mc-value">${skipped}</span>
      </div>` : ''}
      ${(data.failed_operations && data.failed_operations.length) ? `<div class="meta-chip warn-chip">
        <span class="mc-label">failed ops</span>
        <span class="mc-value">${data.failed_operations.length}</span>
      </div>` : ''}
    </div>`);

  if (data.warning) {
    section.insertAdjacentHTML('beforeend',
      `<div class="banner warn visible">${esc(data.warning)}</div>`);
  }

  if (!data.results || !data.results.length) {
    section.insertAdjacentHTML('beforeend',
      `<div class="empty-state"><div class="es-icon">📋</div>
       <div class="es-text">No operations were processed.</div></div>`);
    return;
  }

  data.results.forEach(op => {
    const card = document.createElement('div');
    card.className = 'op-card';

    // Method badge
    let methodBadge;
    if (op.gql_operation) {
      const gqlLabel = op.gql_operation.toUpperCase();
      methodBadge = `<span class="op-method method-gql">${gqlLabel}</span>`;
    } else {
      const m = (op.method || 'OTHER').toUpperCase();
      const cls = { GET:'get', POST:'post', PUT:'put', DELETE:'delete', PATCH:'patch' }[m] || 'other';
      methodBadge = `<span class="op-method method-${cls}">${m}</span>`;
    }

    const opLabel = op.path || op.name;
    const passPill = op.tests_passed > 0
      ? `<span class="stat-pill pill-green">${op.tests_passed} passed</span>` : '';
    const failPill = op.tests_failed > 0
      ? `<span class="stat-pill pill-red">${op.tests_failed} failed</span>` : '';

    card.innerHTML = `
      <div class="op-header">
        ${methodBadge}
        <span class="op-path">${esc(opLabel)}</span>
        <div class="file-stats">${passPill}${failPill}</div>
        <span class="op-chevron">▶</span>
      </div>
      <div class="op-body">
        <div class="stats-row">
          <div class="stat-item"><span class="si-label">Generated:</span> <span class="si-val">${op.tests_generated}</span></div>
          <div class="stat-item"><span class="si-label">Passed:</span> <span class="${op.tests_passed > 0 ? 'si-pass' : 'si-val'}">${op.tests_passed}</span></div>
          <div class="stat-item"><span class="si-label">Failed:</span> <span class="${op.tests_failed > 0 ? 'si-fail' : 'si-val'}">${op.tests_failed}</span></div>
        </div>
        ${coverageBar(op.coverage_percent)}
        ${op.summary ? `<p style="font-size:12px;color:var(--text-muted);margin-top:6px">${esc(op.summary)}</p>` : ''}
      </div>`;

    card.querySelector('.op-header').addEventListener('click', () => card.classList.toggle('open'));
    section.appendChild(card);
  });
}

// ── HTML escape ───────────────────────────────────────────────────────────
function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ══════════════════════════════════════════════════════════════════════════
// TAB 1 — Upload .py file  →  POST /analyze
// ══════════════════════════════════════════════════════════════════════════

(function initTab1() {
  const zone     = $('dz-py');
  const input    = $('dz-py-input');
  const nameEl   = $('dz-py-name');
  const analyzeBtn = $('btn-analyze');
  let selectedFile = null;

  function selectFile(f) {
    if (!f) return;
    if (!f.name.endsWith('.py')) {
      showBanner('error', `"${f.name}" is not a .py file. Only Python source files are accepted.`);
      return;
    }
    selectedFile = f;
    nameEl.textContent = f.name;
    nameEl.style.display = 'inline-block';
    clearBanner();
    analyzeBtn.disabled = false;
  }

  zone.addEventListener('click', () => input.click());
  input.addEventListener('change', () => selectFile(input.files[0]));

  zone.addEventListener('dragover',  e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', ()  => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    selectFile(e.dataTransfer.files[0]);
  });

  analyzeBtn.addEventListener('click', async () => {
    if (!selectedFile) return;
    clearBanner();
    clearResults();
    setLoading(analyzeBtn, true);

    const fd = new FormData();
    fd.append('file', selectedFile);

    try {
      const data = await apiFetch('/analyze', { method: 'POST', body: fd });
      lastResponse = data;
      renderAnalyzeResults(data);
    } catch (e) {
      showBanner('error', e.message);
    } finally {
      setLoading(analyzeBtn, false);
    }
  });
})();

// ══════════════════════════════════════════════════════════════════════════
// TAB 2 — GitHub repo  →  POST /analyze-repo
// ══════════════════════════════════════════════════════════════════════════

(function initTab2() {
  const urlInput    = $('repo-url');
  const urlErr      = $('repo-url-err');
  const maxInput    = $('repo-max-files');
  const repoBtn     = $('btn-analyze-repo');

  urlInput.addEventListener('input', () => setInlineError('repo-url', 'repo-url-err', ''));

  repoBtn.addEventListener('click', async () => {
    const url = urlInput.value.trim();
    if (!url) {
      setInlineError('repo-url', 'repo-url-err', 'Please enter a GitHub repository URL.');
      return;
    }
    if (!/^https?:\/\/github\.com\//i.test(url)) {
      setInlineError('repo-url', 'repo-url-err', 'URL must start with https://github.com/');
      return;
    }
    setInlineError('repo-url', 'repo-url-err', '');
    clearBanner();
    clearResults();
    setLoading(repoBtn, true);

    const payload = {
      github_url: url,
      max_files:  parseInt(maxInput.value, 10) || 20,
    };

    try {
      const data = await apiFetch('/analyze-repo', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(payload),
      });
      lastResponse = data;
      renderRepoResults(data);
    } catch (e) {
      // Surface inline URL error when backend says 400 with "Not a recognised"
      if (e.message.includes('Not a recognised') || e.message.includes('400')) {
        setInlineError('repo-url', 'repo-url-err', e.message.replace(/^400\s*—\s*/i, ''));
      } else {
        showBanner('error', e.message);
      }
    } finally {
      setLoading(repoBtn, false);
    }
  });
})();

// ══════════════════════════════════════════════════════════════════════════
// TAB 3 — API spec  →  POST /analyze-spec
// ══════════════════════════════════════════════════════════════════════════

(function initTab3() {
  const zone      = $('dz-spec');
  const input     = $('dz-spec-input');
  const nameEl    = $('dz-spec-name');
  const baseInput = $('spec-base-url');
  const specBtn   = $('btn-analyze-spec');
  let selectedFile = null;

  function selectFile(f) {
    if (!f) return;
    selectedFile = f;
    nameEl.textContent = f.name;
    nameEl.style.display = 'inline-block';
    clearBanner();
    specBtn.disabled = false;
  }

  zone.addEventListener('click', () => input.click());
  input.addEventListener('change', () => selectFile(input.files[0]));

  zone.addEventListener('dragover',  e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', ()  => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    selectFile(e.dataTransfer.files[0]);
  });

  specBtn.addEventListener('click', async () => {
    if (!selectedFile) return;
    clearBanner();
    clearResults();
    setLoading(specBtn, true);

    const fd = new FormData();
    fd.append('file', selectedFile);
    const baseUrl = baseInput.value.trim();
    if (baseUrl) fd.append('base_url', baseUrl);

    try {
      const data = await apiFetch('/analyze-spec', { method: 'POST', body: fd });
      lastResponse = data;
      renderSpecResults(data);
    } catch (e) {
      showBanner('error', e.message);
    } finally {
      setLoading(specBtn, false);
    }
  });
})();
