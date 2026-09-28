// Small DOM helpers. All dynamic text goes through textContent / text nodes —
// never innerHTML — because file names, code and model output are untrusted.

const SVG_NS = 'http://www.w3.org/2000/svg';

function appendChildren(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function applyAttrs(el, attrs) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.setAttribute('class', value);
    else if (key === 'text') el.textContent = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, String(value));
  }
}

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  applyAttrs(el, attrs);
  appendChildren(el, children);
  return el;
}

export function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  applyAttrs(el, attrs);
  appendChildren(el, children);
  return el;
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

// Static icon geometry (constants only — never user data).
const ICONS = {
  overview: '<rect x="3.5" y="3.5" width="7" height="7" rx="1"/><rect x="13.5" y="3.5" width="7" height="7" rx="1"/><rect x="3.5" y="13.5" width="7" height="7" rx="1"/><rect x="13.5" y="13.5" width="7" height="7" rx="1"/>',
  code: '<path d="m8.5 7-5 5 5 5M15.5 7l5 5-5 5"/>',
  repo: '<path d="M5 4.5A1.5 1.5 0 0 1 6.5 3H19v15H6.5A1.5 1.5 0 0 0 5 19.5v-15Z"/><path d="M5 19.5A1.5 1.5 0 0 0 6.5 21H19v-3"/>',
  spec: '<path d="M8 4H7a2 2 0 0 0-2 2v3.5a1.5 1.5 0 0 1-1.5 1.5v2A1.5 1.5 0 0 1 5 14.5V18a2 2 0 0 0 2 2h1M16 4h1a2 2 0 0 1 2 2v3.5a1.5 1.5 0 0 0 1.5 1.5v2a1.5 1.5 0 0 0-1.5 1.5V18a2 2 0 0 1-2 2h-1"/>',
  history: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  team: '<circle cx="9" cy="8.5" r="3.5"/><path d="M2.5 20c.6-3.5 3.3-5.5 6.5-5.5s5.9 2 6.5 5.5"/><path d="M16 5.2a3.5 3.5 0 0 1 0 6.6M18 14.8c1.9.7 3.2 2.5 3.5 5.2"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  x: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  alert: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.5"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  minus: '<path d="M6 12h12"/>',
  download: '<path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="1.5"/><path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>',
  trash: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13"/>',
  external: '<path d="M14 4.5h5.5V10M19.5 4.5 11 13M18 14v5.5H4.5V6H10"/>',
  theme: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17a8.5 8.5 0 0 0 0-17Z" fill="currentColor" stroke="none"/>',
  signout: '<path d="M14.5 4.5h4a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-4M10 16.5 14.5 12 10 7.5M14.5 12H4.5"/>',
  branch: '<circle cx="7" cy="5.5" r="2"/><circle cx="7" cy="18.5" r="2"/><circle cx="17" cy="8.5" r="2"/><path d="M7 7.5v9M17 10.5c0 4-4 3.5-8.5 6.5"/>',
};

export function icon(name, extraClass = '') {
  const el = document.createElementNS(SVG_NS, 'svg');
  el.setAttribute('viewBox', '0 0 24 24');
  el.setAttribute('fill', 'none');
  el.setAttribute('stroke', 'currentColor');
  el.setAttribute('stroke-width', '1.8');
  el.setAttribute('stroke-linecap', 'round');
  el.setAttribute('stroke-linejoin', 'round');
  el.setAttribute('aria-hidden', 'true');
  if (extraClass) el.setAttribute('class', extraClass);
  el.innerHTML = ICONS[name] || ''; // constant markup from the table above
  return el;
}

// ── Formatting ────────────────────────────────────────────────────────────

const numberFmt = new Intl.NumberFormat();
export const fmtNum = (n) => (n === null || n === undefined ? '—' : numberFmt.format(n));

export function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function relTime(iso) {
  if (!iso) return '—';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 45) return 'just now';
  const units = [[60, 'minute'], [3600, 'hour'], [86400, 'day'], [604800, 'week'], [2629800, 'month'], [31557600, 'year']];
  let chosen = units[0];
  for (const u of units) if (diff >= u[0]) chosen = u;
  const value = Math.floor(diff / chosen[0]);
  return `${value} ${chosen[1]}${value === 1 ? '' : 's'} ago`;
}

export function fmtDuration(ms) {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms} ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(1)} s`;
  const min = Math.floor(sec / 60);
  return `${min} min ${Math.round(sec % 60)} s`;
}

export const shortSha = (sha) => (sha ? sha.slice(0, 7) : '');

export function plural(n, word, pluralWord) {
  return `${fmtNum(n)} ${n === 1 ? word : (pluralWord || `${word}s`)}`;
}
