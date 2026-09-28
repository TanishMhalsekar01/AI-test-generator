// Code review: upload files or paste code in any language.
import { postForm } from '../api.js';
import { h } from '../dom.js';
import { pageHead, card, banner, modelLabel } from '../components.js';

const RUNNER_NAMES = { python: 'Python', javascript: 'JavaScript', go: 'Go', ruby: 'Ruby', rust: 'Rust' };

export async function render(root, { me, navigate }) {
  const runnable = Object.entries(me.runners).filter(([, ok]) => ok).map(([k]) => RUNNER_NAMES[k]);
  root.append(pageHead('Code review',
    `Each file is checked with the real compiler or linter for its language where one is installed, reviewed by ${modelLabel(me)} for defects, and given a generated test suite. Tests are executed in a sandbox for ${runnable.join(', ')}; for other languages they are provided to run locally.`));

  if (!me.ai_configured) {
    root.append(banner('warn', 'AI review is not configured on this server',
      'GEMINI_API_KEY is not set. Compiler and linter checks still run; AI findings and generated tests will be skipped.'));
  }

  const errorSlot = h('div');
  let mode = 'upload';
  let files = [];

  const fileInput = h('input', { type: 'file', multiple: true, class: 'hidden' });
  const fileList = h('ul', { class: 'file-list' });
  const drop = h('div', { class: 'dropzone', tabindex: 0, role: 'button', 'aria-label': 'Choose source files' },
    h('div', {}, h('strong', { text: 'Choose files' }), ' or drop them here'),
    h('div', { class: 'field-hint', text: `Up to ${me.limits.upload_files} files, ${Math.round(me.limits.file_bytes / 1000)} KB each. Any language.` }));

  function renderFiles() {
    fileList.replaceChildren(...files.map((f, i) => h('li', {},
      h('span', { class: 'mono', text: f.name }),
      h('button', { type: 'button', class: 'btn btn-sm btn-ghost', onclick: () => { files.splice(i, 1); renderFiles(); } }, 'Remove'))));
  }
  function addFiles(list) {
    for (const f of list) if (!files.some((x) => x.name === f.name)) files.push(f);
    files = files.slice(0, me.limits.upload_files);
    renderFiles();
  }
  drop.addEventListener('click', () => fileInput.click());
  drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } });
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('drag'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('drag'); addFiles(e.dataTransfer.files); });
  fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });

  const filename = h('input', { class: 'input', id: 'paste-name', placeholder: 'e.g. invoice.ts, main.go, utils.py', autocomplete: 'off' });
  const codeArea = h('textarea', { class: 'textarea', id: 'paste-code', spellcheck: 'false', placeholder: 'Paste source code here' });
  const uploadPane = h('div', {}, drop, fileInput, fileList);
  const pastePane = h('div', { class: 'hidden' },
    h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'paste-name', text: 'File name' }), filename,
      h('div', { class: 'field-hint', text: 'The extension decides the language, compiler and test framework.' })),
    h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'paste-code', text: 'Code' }), codeArea));

  const segUpload = h('button', { type: 'button', 'aria-pressed': 'true' }, 'Upload files');
  const segPaste = h('button', { type: 'button', 'aria-pressed': 'false' }, 'Paste code');
  function setMode(m) {
    mode = m;
    segUpload.setAttribute('aria-pressed', String(m === 'upload'));
    segPaste.setAttribute('aria-pressed', String(m === 'paste'));
    uploadPane.classList.toggle('hidden', m !== 'upload');
    pastePane.classList.toggle('hidden', m !== 'paste');
  }
  segUpload.addEventListener('click', () => setMode('upload'));
  segPaste.addEventListener('click', () => setMode('paste'));

  const project = h('input', { class: 'input', id: 'project', placeholder: 'Defaults to the first file name', autocomplete: 'off' });
  const runTests = h('input', { type: 'checkbox', checked: true });
  const submit = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Start review');

  const form = h('form', { novalidate: true },
    h('div', { class: 'segmented', role: 'group', 'aria-label': 'Input method' }, segUpload, segPaste),
    uploadPane, pastePane,
    h('div', { class: 'form-row' },
      h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'project', text: 'Project name (optional)' }), project,
        h('div', { class: 'field-hint', text: 'Runs with the same project name are grouped in History.' }))),
    h('label', { class: 'check' }, runTests, h('span', {}, 'Execute the generated tests ',
      h('span', { class: 'muted', text: '(recommended — failing tests are how defects are confirmed)' }))),
    errorSlot,
    h('div', { class: 'actions' }, submit));

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errorSlot.replaceChildren();
    const fd = new FormData();
    if (mode === 'upload') {
      if (!files.length) { errorSlot.append(banner('error', null, 'Choose at least one file.')); return; }
      for (const f of files) fd.append('files', f, f.name);
    } else {
      if (!codeArea.value.trim()) { errorSlot.append(banner('error', null, 'Paste some code first.')); return; }
      fd.append('code', codeArea.value);
      fd.append('filename', filename.value.trim() || 'snippet.txt');
    }
    fd.append('project', project.value.trim());
    fd.append('run_tests', runTests.checked ? 'true' : 'false');
    submit.disabled = true;
    submit.textContent = 'Starting…';
    try {
      const { id } = await postForm('/api/runs/code', fd);
      navigate(`#/runs/${id}`);
    } catch (err) {
      errorSlot.append(banner('error', 'Could not start the review', err.message));
      submit.disabled = false;
      submit.textContent = 'Start review';
    }
  });

  root.append(card('Source', null, form));
}
