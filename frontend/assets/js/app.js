// Application shell: session check, navigation, theme, hash router.
import { api } from './api.js';
import { h, icon, clear } from './dom.js';
import { banner } from './components.js';
import * as overview from './views/overview.js';
import * as code from './views/code.js';
import * as repos from './views/repos.js';
import * as specs from './views/specs.js';
import * as history from './views/history.js';
import * as teams from './views/teams.js';
import * as run from './views/run.js';

const NAV_ICONS = { overview: 'overview', code: 'code', repos: 'repo', specs: 'spec', history: 'history', teams: 'team' };

// ── Theme (system / light / dark), remembered per browser ────────────────
const THEMES = ['system', 'light', 'dark'];
function readTheme() {
  try { return localStorage.getItem('aitg-theme') || 'system'; } catch (_) { return 'system'; }
}
function applyTheme(theme) {
  if (theme === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', theme);
  const btn = document.getElementById('theme-toggle');
  btn.replaceChildren(icon('theme'), h('span', { class: 'sr-only', text: `Theme: ${theme}` }));
  btn.title = `Theme: ${theme} (click to change)`;
}
function initTheme() {
  let theme = readTheme();
  applyTheme(theme);
  document.getElementById('theme-toggle').addEventListener('click', () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    try { localStorage.setItem('aitg-theme', theme); } catch (_) { /* storage unavailable */ }
    applyTheme(theme);
  });
}

// ── Router ────────────────────────────────────────────────────────────────
const ROUTES = [
  { re: /^\/?$/, nav: 'overview', view: overview },
  { re: /^\/code$/, nav: 'code', view: code },
  { re: /^\/repos$/, nav: 'repos', view: repos },
  { re: /^\/specs$/, nav: 'specs', view: specs },
  { re: /^\/history$/, nav: 'history', view: history },
  { re: /^\/teams$/, nav: 'teams', view: teams },
  { re: /^\/teams\/([^/]+)$/, nav: 'teams', view: teams, param: 'org' },
  { re: /^\/runs\/([\w-]+)$/, nav: 'history', view: run, param: 'id' },
];

let me = null;
let cleanup = null;

export function navigate(hash) {
  if (window.location.hash === hash) route();
  else window.location.hash = hash;
}

async function route() {
  const raw = window.location.hash.replace(/^#/, '') || '/';
  const [path, queryString] = raw.split('?');
  const query = Object.fromEntries(new URLSearchParams(queryString || ''));
  const match = ROUTES.map((r) => ({ r, m: path.match(r.re) })).find((x) => x.m);
  const view = document.getElementById('view');
  if (typeof cleanup === 'function') cleanup();
  cleanup = null;
  clear(view);
  for (const a of document.querySelectorAll('#sidenav a')) {
    if (match && a.dataset.route === match.r.nav) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  if (!match) {
    view.append(banner('warn', 'Page not found', 'That address does not exist in this app.'));
    return;
  }
  const params = match.r.param ? { [match.r.param]: decodeURIComponent(match.m[1]) } : {};
  try {
    cleanup = await match.r.view.render(view, { me, params, query, navigate });
  } catch (err) {
    view.append(banner('error', 'Something went wrong', err.message || String(err)));
  }
  view.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

async function start() {
  initTheme();
  for (const a of document.querySelectorAll('#sidenav a')) a.prepend(icon(NAV_ICONS[a.dataset.route]));
  document.getElementById('signout').prepend(icon('signout'));
  try {
    me = await api('/api/me');
  } catch (err) {
    document.getElementById('view').append(banner('error', 'Could not load your session', err.message));
    return;
  }
  document.getElementById('model-tag').textContent = me.model;
  document.getElementById('user').append(
    me.avatar_url ? h('img', { class: 'avatar', src: me.avatar_url, alt: '' }) : null,
    h('span', { class: 'login', text: me.login }));
  window.addEventListener('hashchange', route);
  route();
}

start();
