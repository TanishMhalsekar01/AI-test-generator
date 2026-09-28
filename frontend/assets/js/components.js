// Reusable UI pieces: tiles, badges, tables, tabs, charts, code viewer.
import { h, s, icon, fmtNum, fmtDate } from './dom.js';

export function pageHead(title, description, ...actions) {
  return h('div', { class: 'page-head' },
    h('div', {}, h('h1', { text: title }), description ? h('p', { text: description }) : null),
    actions.length ? h('div', { class: 'actions' }, actions) : null);
}

export function card(title, description, body, { actions = [], flush = false } = {}) {
  return h('section', { class: 'card' },
    title ? h('div', { class: 'card-head' },
      h('div', {}, h('h2', { text: title }), description ? h('p', { text: description }) : null),
      actions.length ? h('div', { class: 'actions' }, actions) : null) : null,
    h('div', { class: flush ? 'card-body flush' : 'card-body' }, body));
}

export function tile(label, value, sub) {
  return h('div', { class: 'tile' },
    h('div', { class: 'label', text: label }),
    h('div', { class: 'value', text: typeof value === 'number' ? fmtNum(value) : value }),
    sub ? h('div', { class: 'sub', text: sub }) : null);
}

export const tiles = (...items) => h('div', { class: 'tiles' }, items);

const SEVERITY_LABEL = { critical: 'Critical', high: 'High', medium: 'Medium', low: 'Low', error: 'Error', warning: 'Warning', info: 'Info' };
const SEVERITY_MARK = { critical: 'critical', high: 'high', medium: 'medium', low: 'low', error: 'critical', warning: 'medium', info: 'info' };

export function sevBadge(severity, count) {
  const label = SEVERITY_LABEL[severity] || severity;
  return h('span', { class: 'badge', title: label },
    h('span', { class: `mark mark-${SEVERITY_MARK[severity] || 'info'}` }),
    count !== undefined ? `${fmtNum(count)} ${label.toLowerCase()}` : label);
}

const STATUS = {
  completed: ['check', 'Completed'], passed: ['check', 'Passed'], failed: ['x', 'Failed'], error: ['alert', 'Error'],
  running: ['clock', 'Running'], queued: ['clock', 'Queued'], skipped: ['minus', 'Skipped'],
  not_executed: ['minus', 'Not executed'], ran: ['check', 'Ran'], unavailable: ['minus', 'Unavailable'],
  timeout: ['alert', 'Timed out'],
};

export function statusBadge(status, labelOverride) {
  const [ic, label] = STATUS[status] || ['minus', status];
  return h('span', { class: `badge status-${status}` }, icon(ic), labelOverride || label);
}

export const KIND_LABEL = { code: 'Code review', repo: 'Repository', spec: 'API & specs' };

export function banner(type, title, text, extra) {
  const ic = type === 'error' ? 'alert' : type === 'warn' ? 'alert' : 'clock';
  return h('div', { class: `banner ${type}`, role: type === 'error' ? 'alert' : 'status' },
    icon(ic), h('div', {}, title ? h('strong', { text: title }) : null, text ? h('span', { text }) : null, extra || null));
}

export function emptyState(title, text, actions = []) {
  return h('div', { class: 'empty' }, h('h3', { text: title }), text ? h('p', { text }) : null,
    actions.length ? h('div', { class: 'actions' }, actions) : null);
}

export function loading(text = 'Loading…') {
  return h('div', { class: 'empty muted', role: 'status', text });
}

/**
 * columns: [{ label, render(row) -> Node|string, num?: bool, className? }]
 */
export function dataTable(columns, rows, { onRowClick, empty = 'Nothing to show.', rowClass } = {}) {
  if (!rows.length) return emptyState(empty);
  const thead = h('thead', {}, h('tr', {}, columns.map((c) => h('th', { class: c.num ? 'num' : c.className || null, text: c.label }))));
  const tbody = h('tbody', {}, rows.map((row) => {
    const tr = h('tr', { class: [onRowClick ? 'clickable' : '', rowClass ? rowClass(row) : ''].join(' ').trim() || null },
      columns.map((c) => {
        const value = c.render(row);
        return h('td', { class: c.num ? 'num' : c.className || null }, value);
      }));
    if (onRowClick) {
      tr.tabIndex = 0;
      tr.addEventListener('click', (e) => { if (!e.target.closest('a,button')) onRowClick(row); });
      tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') onRowClick(row); });
    }
    return tr;
  }));
  return h('div', { class: 'table-wrap' }, h('table', { class: 'data' }, thead, tbody));
}

export function tabs(defs, initial) {
  const wrap = h('div');
  const bar = h('div', { class: 'tabs', role: 'tablist' });
  const panel = h('div', { role: 'tabpanel' });
  let current = initial || defs[0].id;
  function select(id) {
    current = id;
    for (const btn of bar.children) btn.setAttribute('aria-selected', String(btn.dataset.id === id));
    panel.replaceChildren(defs.find((d) => d.id === id).render());
  }
  for (const d of defs) {
    bar.append(h('button', { type: 'button', role: 'tab', dataset: { id: d.id }, onclick: () => select(d.id) },
      d.label, d.count !== undefined ? h('span', { class: 'count', text: fmtNum(d.count) }) : null));
  }
  wrap.append(bar, panel);
  select(current);
  wrap.select = select;
  return wrap;
}

export function copyButton(getText, label = 'Copy') {
  const btn = h('button', { type: 'button', class: 'btn btn-sm' }, icon('copy'), label);
  btn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(getText());
      btn.lastChild.textContent = 'Copied';
      setTimeout(() => { btn.lastChild.textContent = label; }, 1500);
    } catch (_) {
      btn.lastChild.textContent = 'Copy failed';
    }
  });
  return btn;
}

export function downloadButton(filename, getText, label = 'Download', mime = 'text/plain') {
  return h('button', {
    type: 'button', class: 'btn btn-sm',
    onclick: () => {
      const url = URL.createObjectURL(new Blob([getText()], { type: mime }));
      const a = h('a', { href: url, download: filename });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
  }, icon('download'), label);
}

/** Source listing with line numbers; `flags` maps line -> [titles]. */
export function codeViewer(source, flags = new Map()) {
  const rows = (source || '').split('\n').map((line, i) => {
    const n = i + 1;
    const titles = flags.get(n);
    return h('tr', { id: `L${n}`, class: titles ? 'flag' : null, title: titles ? titles.join('\n') : null },
      h('td', { class: 'ln', text: String(n) }), h('td', { text: line || ' ' }));
  });
  const box = h('div', { class: 'code' }, h('table', {}, h('tbody', {}, rows)));
  box.scrollToLine = (n) => {
    const row = box.querySelector(`#L${n}`);
    if (!row) return;
    box.querySelectorAll('tr.target').forEach((r) => r.classList.remove('target'));
    row.classList.add('target');
    box.scrollTop = row.offsetTop - box.clientHeight / 3;
    row.scrollIntoView({ block: 'nearest' });
  };
  return box;
}

// ── Bar chart (single series; thin bars, 4px rounded data-end, hairline grid) ──

function roundedTopBar(x, y, w, hgt, r) {
  const rr = Math.min(r, w / 2, hgt);
  if (hgt <= 0) return '';
  return `M${x},${y + hgt}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + hgt}Z`;
}

/**
 * points: [{ label, value, detail? , href? }]
 * Sparkline mode draws no axes; native <title> + aria provides the values.
 */
export function barChart(points, { height = 160, sparkline = false, title = '', valueLabel = 'value', onBarClick } = {}) {
  const n = Math.max(points.length, 1);
  const barW = sparkline ? 6 : Math.min(24, Math.max(8, Math.floor(560 / n) - 6));
  const gap = sparkline ? 2 : Math.max(6, barW / 2);
  const padLeft = sparkline ? 0 : 36;
  const padBottom = sparkline ? 0 : 22;
  const width = padLeft + n * (barW + gap);
  const max = Math.max(1, ...points.map((p) => p.value));
  const niceMax = sparkline ? max : Math.ceil(max / Math.pow(10, Math.floor(Math.log10(max)))) * Math.pow(10, Math.floor(Math.log10(max)));
  const plotH = height - padBottom;
  const wrap = h('div', { class: 'chart' });
  const svg = s('svg', { width, height, role: 'img', 'aria-label': title || `${valueLabel} per run` });

  if (!sparkline) {
    for (const t of [0, niceMax / 2, niceMax]) {
      const y = plotH - (t / niceMax) * plotH;
      svg.append(s('line', { class: t === 0 ? 'baseline' : 'grid', x1: padLeft, x2: width, y1: y + 0.5, y2: y + 0.5 }));
      svg.append(s('text', { class: 'tick', x: padLeft - 6, y: y + 4, 'text-anchor': 'end' }, fmtNum(Math.round(t))));
    }
  } else {
    svg.append(s('line', { class: 'baseline', x1: 0, x2: width, y1: plotH - 0.5, y2: plotH - 0.5 }));
  }

  const tip = h('div', { class: 'tooltip hidden' });
  points.forEach((p, i) => {
    const x = padLeft + i * (barW + gap) + gap / 2;
    const bh = Math.max(p.value > 0 ? 2 : 0, (p.value / (sparkline ? max : niceMax)) * (plotH - (sparkline ? 1 : 0)));
    const y = plotH - bh;
    const hit = s('rect', { x: x - gap / 2, y: 0, width: barW + gap, height: plotH, fill: 'transparent', tabindex: sparkline ? null : 0 });
    const bar = s('path', { class: 'bar', d: roundedTopBar(x, y, barW, bh, sparkline ? 2 : 4) });
    const g = s('g', {}, s('title', {}, `${p.label}: ${fmtNum(p.value)} ${valueLabel}`), bar, hit);
    if (!sparkline) {
      const show = () => {
        tip.replaceChildren(h('strong', { text: `${fmtNum(p.value)} ${valueLabel}` }), h('div', { text: p.label }),
          p.detail ? h('div', { text: p.detail }) : null);
        tip.classList.remove('hidden');
        tip.style.left = `${Math.min(x + barW + 8, width - 40)}px`;
        tip.style.top = `${Math.max(0, y - 10)}px`;
      };
      hit.addEventListener('pointerenter', show);
      hit.addEventListener('focus', show);
      hit.addEventListener('pointerleave', () => tip.classList.add('hidden'));
      hit.addEventListener('blur', () => tip.classList.add('hidden'));
      if (onBarClick) {
        hit.style.cursor = 'pointer';
        hit.addEventListener('click', () => onBarClick(p));
        hit.addEventListener('keydown', (e) => { if (e.key === 'Enter') onBarClick(p); });
      }
      svg.append(s('text', { class: 'tick', x: x + barW / 2, y: height - 6, 'text-anchor': 'middle' },
        i === 0 || i === points.length - 1 ? p.short || '' : ''));
    }
    svg.append(g);
  });
  wrap.append(svg, tip);
  return wrap;
}

export function trendSparkline(trend, valueKey = 'findings', valueLabel = 'findings') {
  if (!trend || trend.length < 2) return h('span', { class: 'muted', text: trend && trend.length === 1 ? 'one run' : '—' });
  return barChart(trend.map((t) => ({ label: fmtDate(t.at), value: t[valueKey] || 0 })), { height: 24, sparkline: true, valueLabel });
}

export function resultSummary(headline) {
  if (!headline) return h('span', { class: 'muted', text: '—' });
  const parts = [];
  let mark = headline.critical ? 'mark-critical' : headline.findings ? 'mark-medium' : 'mark-good';
  if (headline.ai_incomplete && !headline.findings) mark = 'mark-info'; // zero is not "clean" if the review never ran
  parts.push(h('span', { class: 'badge' }, h('span', { class: `mark ${mark}` }),
    `${fmtNum(headline.findings)} finding${headline.findings === 1 ? '' : 's'}`));
  if (headline.ai_incomplete) {
    parts.push(h('span', { class: 'badge', title: 'The AI review did not complete for every file in this run' }, icon('alert'), 'AI review incomplete'));
  }
  if (headline.tests_passed || headline.tests_failed) {
    parts.push(h('span', { class: 'badge' }, h('span', { class: `mark ${headline.tests_failed ? 'mark-critical' : 'mark-good'}` }),
      `${fmtNum(headline.tests_passed)} passed · ${fmtNum(headline.tests_failed)} failed`));
  }
  return h('span', { class: 'counts' }, parts);
}
