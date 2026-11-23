// ⌘K 命令面板:搜页面、实例、维度、发现、SQL、WDR 部分、案例;输入一句话回车 = 带着它开一个 opencode 会话。
// 条目来自已取到的报告模型(lib/store.js);动作(翻页、切实例、开侧栏)由 shell 通过 install 传入。

import { esc, cn, stamp } from './fmt.js';
import { I, glyph, lname } from './ui.js';
import { getModel } from './store.js';
import { openDig } from './dig.js';
import * as D from './details.js';
import { title, metricName } from '../pages/health.js';
import { WT } from '../pages/wdr.js';

let api = null, st = { items: [], i: 0 };
const $ = (s) => document.querySelector(s);

function el() {
  let c = document.getElementById('cmdk');
  if (!c) {
    document.body.insertAdjacentHTML('beforeend', '<div class="scrim cmdk-scrim" id="cmdk-scrim"></div><div class="cmdk" id="cmdk" role="dialog" aria-label="命令面板"></div>');
    c = document.getElementById('cmdk');
    document.getElementById('cmdk-scrim').addEventListener('click', close);
  }
  return c;
}

function items(q) {
  const base = [
    ...api.pages.map(([p, l, ic]) => ({ grp: '页面', label: l, sub: api.page() === p ? '当前页面' : '', ic, go: () => api.goPage(p) })),
    ...api.instances().map((t) => ({ grp: '实例', label: t.label || t.conn || t.key, sub: `最近报告 ${stamp(t.last_at)}`, ic: 'db', go: () => api.switchInstance(t.key) })),
    { grp: '操作', label: '问智能体', sub: '⌘J', ic: 'spark', go: () => api.openAssist() },
    { grp: '操作', label: '新对话', sub: '在新标签页打开本人的 opencode', ic: 'chat', go: () => window.open(api.chatHome, '_blank', 'noopener') },
  ];
  const h = getModel('health'), t = getModel('topsql'), w = getModel('wdr'), k = getModel('kb');
  const data = [
    ...(h ? h.findings.map((f) => ({ grp: '发现', label: `${title(f.dimension)} · ${cn(metricName(f))} ${f.value}`, sub: `${lname(f.severity)}，阈值 ${f.threshold}`, lv: f.severity,
      go: () => { const d = h.dims.find((x) => x.dimension === f.dimension); if (d) D.openDim(d.i); else D.openXdim(f.dimension); } })) : []),
    ...(h ? h.dims.map((d) => ({ grp: '维度', label: title(d.dimension), sub: cn(d.headline || ''), lv: d.level, go: () => D.openDim(d.i) })) : []),
    ...(t ? t.rows.map((r) => ({ grp: 'SQL', label: r.sql_id, sub: r.query, ic: 'code', go: () => D.openSql(r.sql_id, api.key()) })) : []),
    ...(w ? (w.d.dims || []).map((x) => ({ grp: 'WDR', label: WT[x.dimension] || x.dimension, sub: cn(x.headline || ''), ic: 'clock', go: () => D.openWdim(x.dimension) })) : []),
    ...(k ? k.cases.slice(0, 50).map((x) => ({ grp: '案例', label: x.title, sub: `${x.severity || '—'} · ${x.system} · ${x.occurred_at}`, ic: 'book', go: () => D.openCase(x.id) })) : []),
  ];
  if (!q) return base.concat(data.filter((x) => x.grp === '发现').slice(0, 5));
  const kq = q.toLowerCase(), hit = [...base, ...data].filter((c) => `${c.label} ${c.sub || ''}`.toLowerCase().includes(kq));
  const per = {}, capped = hit.filter((c) => (per[c.grp] = (per[c.grp] || 0) + 1) <= 5);
  return [{ grp: '智能体', label: `问智能体：“${q}”`, sub: '在新标签页开一个会话', ic: 'spark', ai: true, go: () => openDig(api.askPrefix() + q, '大盘提问') }, ...capped];
}

function draw() {
  const q = $('#cmdk .in input').value.trim();
  const list = items(q);
  st = { items: list, i: Math.min(st.i, Math.max(0, list.length - 1)) };
  let g = '';
  $('#cmdk .res').innerHTML = st.items.length ? st.items.map((c, i) => {
    const h = c.grp !== g ? `<div class="grp">${esc(c.grp)}</div>` : ''; g = c.grp;
    const ic = c.lv !== undefined ? glyph(c.lv) : I[c.ic] || I.arrow;
    return `${h}<div class="opt${i === st.i ? ' act' : ''}${c.ai ? ' ai' : ''}" data-cmd="${i}"><span class="ic">${ic}</span><span class="lb">${esc(c.label)}</span>${c.sub ? `<span class="sub">${esc(c.sub)}</span>` : ''}</div>`;
  }).join('') : '<div class="empty">没有匹配的结果</div>';
  const act = $('#cmdk .opt.act'); if (act) act.scrollIntoView({ block: 'nearest' });
}

export function open(q = '') {
  const c = el();
  st = { items: [], i: 0 };
  c.innerHTML = `<div class="in">${I.search}<input placeholder="搜索页面、实例、SQL、维度、案例，或直接提问" autocomplete="off"><span class="kbd"><kbd>esc</kbd></span></div><div class="res"></div>
    <div class="foot"><span><kbd>↑</kbd><kbd>↓</kbd> 选择</span><span><kbd>↵</kbd> 打开</span><span>输入问题再回车，直接问智能体</span></div>`;
  $('#cmdk .in input').value = q;
  c.classList.add('on'); document.getElementById('cmdk-scrim').classList.add('on');
  draw();
  setTimeout(() => $('#cmdk .in input').focus(), 30);
}
export function close() { const c = document.getElementById('cmdk'); if (c) c.classList.remove('on'); const s = document.getElementById('cmdk-scrim'); if (s) s.classList.remove('on'); }
const isOpen = () => !!(document.getElementById('cmdk') && document.getElementById('cmdk').classList.contains('on'));
function run(i) { const c = st.items[i]; if (!c) return; close(); c.go(); }

/** 只挂一次。a = { pages:[[name,label,icon]], page(), key(), instances(), goPage(p), switchInstance(k), openAssist(), askPrefix(), chatHome } */
export function install(a) {
  api = a;
  if (install.done) return;
  install.done = true;
  document.addEventListener('click', (ev) => { const o = ev.target.closest && ev.target.closest('#cmdk [data-cmd]'); if (o) run(+o.dataset.cmd); });
  document.addEventListener('input', (ev) => { if (ev.target.closest && ev.target.closest('#cmdk .in')) { st.i = 0; draw(); } });
  document.addEventListener('keydown', (ev) => {
    const mod = ev.metaKey || ev.ctrlKey;
    if (mod && ev.key.toLowerCase() === 'k') { ev.preventDefault(); return isOpen() ? close() : open(); }
    if (!isOpen()) return;
    const n = st.items.length;
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') { ev.preventDefault(); st.i = (st.i + (ev.key === 'ArrowDown' ? 1 : -1) + n) % Math.max(1, n); draw(); }
    else if (ev.key === 'Enter') { ev.preventDefault(); run(st.i); }
    else if (ev.key === 'Escape') close();
  });
}
