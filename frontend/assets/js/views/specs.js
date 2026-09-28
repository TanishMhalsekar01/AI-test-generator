// API & specs: OpenAPI / Swagger, GraphQL SDL, JSON / YAML with JSON Schema.
import { postForm } from '../api.js';
import { h } from '../dom.js';
import { pageHead, card, banner } from '../components.js';

export async function render(root, { me, navigate }) {
  root.append(pageHead('API & specs',
    'Lint OpenAPI 3.x and Swagger 2.0 documents, validate GraphQL SDL, and validate JSON or YAML data against a JSON Schema. With a base URL, the live server is checked against its documented contract and the generated pytest suite is run against it.'));

  const errorSlot = h('div');
  let mode = 'file';
  const specFile = h('input', { type: 'file', class: 'input', id: 'spec-file', accept: '.json,.yaml,.yml,.graphql,.gql,.graphqls,.sdl' });
  const specText = h('textarea', { class: 'textarea', id: 'spec-text', spellcheck: 'false', placeholder: 'Paste an OpenAPI document, GraphQL SDL or JSON' });
  const specUrl = h('input', { class: 'input', id: 'spec-url', type: 'url', placeholder: 'https://api.example.com/openapi.json', autocomplete: 'off' });
  const panes = {
    file: h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'spec-file', text: 'Document' }), specFile,
      h('div', { class: 'field-hint', text: 'JSON, YAML, or GraphQL SDL (.graphql, .gql). Up to 5 MB.' })),
    text: h('div', { class: 'field hidden' }, h('label', { class: 'field-label', for: 'spec-text', text: 'Document' }), specText),
    url: h('div', { class: 'field hidden' }, h('label', { class: 'field-label', for: 'spec-url', text: 'Document URL' }), specUrl),
  };
  const segs = Object.fromEntries([['file', 'Upload'], ['text', 'Paste'], ['url', 'From URL']].map(([id, label]) => [id,
    h('button', { type: 'button', 'aria-pressed': String(id === 'file'), onclick: () => setMode(id) }, label)]));
  function setMode(m) {
    mode = m;
    for (const [id, btn] of Object.entries(segs)) btn.setAttribute('aria-pressed', String(id === m));
    for (const [id, pane] of Object.entries(panes)) pane.classList.toggle('hidden', id !== m);
  }

  const schemaFile = h('input', { type: 'file', class: 'input', id: 'schema-file', accept: '.json,.yaml,.yml' });
  const baseUrl = h('input', { class: 'input', id: 'base-url', type: 'url', placeholder: 'https://staging.example.com', autocomplete: 'off' });
  const mutations = h('input', { type: 'checkbox' });
  const generate = h('input', { type: 'checkbox', checked: true });
  const project = h('input', { class: 'input', id: 'spec-project', placeholder: 'Defaults to the document title', autocomplete: 'off' });
  const submit = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Run checks');

  const form = h('form', { novalidate: true },
    h('div', { class: 'segmented', role: 'group', 'aria-label': 'Document source' }, Object.values(segs)),
    Object.values(panes),
    h('div', { class: 'form-row' },
      h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'base-url', text: 'Live server base URL (optional)' }), baseUrl,
        h('div', { class: 'field-hint', text: 'Enables contract checks against a running API. For GraphQL, give the endpoint URL.' })),
      h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'schema-file', text: 'JSON Schema (optional)' }), schemaFile,
        h('div', { class: 'field-hint', text: 'For plain JSON/YAML data: validates the document against this schema.' })),
      h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'spec-project', text: 'Project name (optional)' }), project)),
    h('label', { class: 'check' }, mutations, h('span', {}, 'Send write requests (POST, PUT, PATCH, DELETE) during live checks ',
      h('span', { class: 'muted', text: '— only against a test environment' }))),
    h('label', { class: 'check' }, generate, h('span', {}, `Generate a pytest suite per operation with ${me.model}`)),
    errorSlot,
    h('div', { class: 'actions' }, submit));

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errorSlot.replaceChildren();
    const fd = new FormData();
    if (mode === 'file') {
      if (!specFile.files.length) { errorSlot.append(banner('error', null, 'Choose a document.')); return; }
      fd.append('file', specFile.files[0]);
    } else if (mode === 'text') {
      if (!specText.value.trim()) { errorSlot.append(banner('error', null, 'Paste a document.')); return; }
      fd.append('spec_text', specText.value);
    } else {
      if (!specUrl.value.trim()) { errorSlot.append(banner('error', null, 'Enter the document URL.')); return; }
      fd.append('spec_url', specUrl.value.trim());
    }
    if (schemaFile.files.length) fd.append('schema_file', schemaFile.files[0]);
    fd.append('base_url', baseUrl.value.trim());
    fd.append('allow_mutations', mutations.checked ? 'true' : 'false');
    fd.append('generate_tests', generate.checked ? 'true' : 'false');
    fd.append('project', project.value.trim());
    submit.disabled = true;
    submit.textContent = 'Starting…';
    try {
      const { id } = await postForm('/api/runs/spec', fd);
      navigate(`#/runs/${id}`);
    } catch (err) {
      errorSlot.append(banner('error', 'Could not start the checks', err.message));
      submit.disabled = false;
      submit.textContent = 'Run checks';
    }
  });

  root.append(card('Document', null, form));
}
