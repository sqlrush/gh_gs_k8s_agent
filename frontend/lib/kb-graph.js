// 知识库大盘的「关系图」页签:五列 —— 案例 / 现象 / 根因 / 处置 / 依据条款,每一行是一个案例的完整链路。
// 边来自 graph/*.yaml 里已确认的三元组(节点已按 canonical.yaml 归一);「依据条款」来自案例文件的 rules 字段。
// 点一个节点只点亮与它相连的那一串;条款是多个案例共用的,只有被点中时才往外走。

import { esc } from './fmt.js';
import { allRules } from './kb-docs.js';

export const KINDS = [['case', '案例', '#2fa79a'], ['symptom', '现象', '#e07a1f'], ['rootcause', '根因', '#d64545'],
  ['action', '处置', '#3fa552'], ['rule', '依据条款', '#4176e6']];
const KC = Object.fromEntries(KINDS.map(([k, , c]) => [k, c]));
const KN = Object.fromEntries(KINDS.map(([k, n]) => [k, n]));
const REL = { exhibits: '表现为', caused_by: '根因是', handled_by: '处置为', cites: '依据' };
const COLS = KINDS.map(([k]) => k);
const GW = 170, GH = 36, GAP = 12, STEP = 205;
const colX = (kind) => 8 + Math.max(0, COLS.indexOf(kind)) * STEP;

export function buildGraph(cat) {
  const nodes = new Map(), edges = [];
  const rules = Object.fromEntries(allRules(cat).map((r) => [r.id, r]));
  const add = (n) => { if (!nodes.has(n.id)) nodes.set(n.id, { k: n.id, kind: n.kind, label: n.label || n.id, full: n.full || n.label || n.id }); return n.id; };
  for (const e of cat.edges || []) {
    if (!COLS.includes(e.src.kind) || !COLS.includes(e.dst.kind)) continue;   // 对象 / GUC 类节点不进这张五列图
    edges.push({ a: add(e.src), b: add(e.dst), rel: e.rel, source: e.source });
  }
  for (const c of cat.cases || []) {
    const a = add({ id: 'case:' + c.id, kind: 'case', label: c.title });
    for (const id of c.rules || []) {
      edges.push({ a, b: add({ id: 'rule:' + id, kind: 'rule', label: id, full: rules[id] ? id + ' ' + rules[id].rule : id }), rel: 'cites', source: 'cases/' + c.id + '.md' });
    }
  }
  return { nodes, edges };
}

function layout(g) {
  const rowOf = new Map();
  [...g.nodes.values()].filter((n) => n.kind === 'case').forEach((c, i) => rowOf.set(c.k, i));
  // 沿边把行号往下游传:现象 / 根因 / 处置跟着自己的案例排在同一行(被多个案例共用的,跟第一个)
  for (let pass = 0; pass < 4; pass++) for (const e of g.edges) {
    if (rowOf.has(e.a) && !rowOf.has(e.b) && g.nodes.get(e.b).kind !== 'rule') rowOf.set(e.b, rowOf.get(e.a));
  }
  const y = (r) => 34 + r * (GH + GAP);
  const pos = new Map(), used = new Set();
  for (const n of g.nodes.values()) {
    if (n.kind === 'rule') continue;
    let r = rowOf.get(n.k) ?? 0;
    while (used.has(n.kind + r)) r++;          // 同一列同一行只放一个,后来的顺延
    used.add(n.kind + r); pos.set(n.k, { x: colX(n.kind), y: y(r) });
  }
  // 条款:放在引用它的案例的平均行高,再按顺序错开不重叠
  const rules = [...g.nodes.values()].filter((n) => n.kind === 'rule').map((n) => {
    const rows = g.edges.filter((e) => e.b === n.k).map((e) => rowOf.get(e.a) ?? 0);
    return { n, r: rows.reduce((a, b) => a + b, 0) / Math.max(1, rows.length) };
  }).sort((a, b) => a.r - b.r);
  let last = -Infinity;
  for (const { n, r } of rules) { const yy = Math.max(y(r), last + GH + 6); pos.set(n.k, { x: colX('rule'), y: yy }); last = yy; }
  const ys = [...pos.values()].map((p) => p.y);
  return { pos, width: colX('rule') + GW + 8, height: (ys.length ? Math.max(...ys) : 0) + GH + 16 };
}

export function related(g, start) {
  const seen = new Set([start]), q = [start];
  while (q.length) {
    const k = q.shift();
    if (g.nodes.get(k).kind === 'rule' && k !== start) continue;
    for (const e of g.edges) for (const [x, y] of [[e.a, e.b], [e.b, e.a]]) if (x === k && !seen.has(y)) { seen.add(y); q.push(y); }
  }
  return seen;
}

export function graphView(cat, st) {
  const g = buildGraph(cat);
  if (!g.nodes.size) return `<div class="card"><div class="empty"><b>关系图还是空的</b>导入案例并确认关系边之后,这里会画出现象 → 根因 → 处置的链路。</div></div>`;
  const L = layout(g);
  const on = st.node && g.nodes.has(st.node) ? related(g, st.node) : null;
  const cut = (s, n) => (s.length > n ? s.slice(0, n) + '…' : s);
  const edgesSvg = g.edges.map((e) => {
    // 「依据条款」从同一行的处置列右侧出发:同一行就是同一个案例,不用一根长线横穿整张图
    const p0 = L.pos.get(e.a), q = L.pos.get(e.b); if (!p0 || !q) return '';
    const p = e.rel === 'cites' ? { x: colX('action'), y: p0.y } : p0;
    const x1 = p.x + GW, y1 = p.y + GH / 2, x2 = q.x, y2 = q.y + GH / 2, mx = (x1 + x2) / 2;
    const cls = on ? (on.has(e.a) && on.has(e.b) ? 'hot' : 'dim') : '';
    return `<path class="kb-edge ${cls}" d="M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}"><title>${REL[e.rel] || esc(e.rel)}</title></path>`;
  }).join('');
  const nodesSvg = [...g.nodes.values()].map((n) => {
    const p = L.pos.get(n.k); if (!p) return '';
    const cls = on ? (on.has(n.k) ? (n.k === st.node ? 'hot' : '') : 'dim') : '';
    return `<g class="kb-node ${cls}" data-node="${esc(n.k)}">
      <rect x="${p.x}" y="${p.y}" width="${GW}" height="${GH}" rx="8" fill="#fff" stroke="${KC[n.kind]}"/>
      <rect x="${p.x}" y="${p.y}" width="5" height="${GH}" rx="2" fill="${KC[n.kind]}" stroke="none"/>
      <text x="${p.x + 12}" y="${p.y + GH / 2 + 4}">${esc(cut(n.label, 12))}</text><title>${esc(n.full)}</title></g>`;
  }).join('');
  const heads = KINDS.map(([k, n]) => `<text class="kb-colh" x="${colX(k) + 4}" y="20">${n}</text>`).join('');
  return `<div class="card" style="padding:10px 14px;display:flex;gap:16px;align-items:center;flex-wrap:wrap">
      <div class="legend">${KINDS.map(([, n, c]) => `<span style="--c:${c}">${n}</span>`).join('')}</div>
      <span class="sub" style="margin:0">每一行是一个案例的完整链路:现象 → 根因 → 处置,右边是它依据的条款。点任一节点,只看与它相连的那一串。</span>
      ${on ? `<span class="kb-btn" data-node="">显示全部</span>` : ''}</div>
    <div class="card kb-gcard" id="kb-graph">
      <svg width="${L.width}" height="${L.height}" viewBox="0 0 ${L.width} ${L.height}">${heads}${edgesSvg}${nodesSvg}</svg>
      ${on ? `<div class="kb-gpanel">${nodePanel(g, st.node, on)}</div>` : ''}</div>`;
}

function nodePanel(g, key, on) {
  const n = g.nodes.get(key);
  const outs = g.edges.filter((e) => e.a === n.k), ins = g.edges.filter((e) => e.b === n.k);
  const line = (other) => { const o = g.nodes.get(other); return `<a class="kb-ref" data-node="${esc(o.k)}"><span style="color:${KC[o.kind]};font-weight:600">${KN[o.kind]}</span> ${esc(o.label)}</a>`; };
  const bare = n.k.slice(n.k.indexOf(':') + 1);
  let open = '';
  if (n.kind === 'case') open = `<span class="kb-btn" data-case="${esc(bare)}">看案例原文 →</span>`;
  else if (n.kind === 'rule') open = `<span class="kb-btn" data-rule="${esc(bare)}">看条款原文 →</span>`;
  else {
    const e = [...ins, ...outs].find((x) => /^cases\//.test(x.source || ''));
    const cid = e && (e.source.match(/^cases\/(.+?)\.md/) || [])[1];
    if (cid) open = `<span class="kb-btn" data-case="${esc(cid)}">看出处原文 →</span>`;
  }
  return `<span class="kb-x" data-node="">×</span><span class="pill" style="background:${KC[n.kind]}22;color:${KC[n.kind]}">${KN[n.kind]}</span>
    <h2 style="margin-top:8px;line-height:1.45">${esc(n.label)}</h2>
    ${ins.length ? `<div class="kb-sec"><h3>上游</h3>${ins.map((e) => line(e.a)).join('')}</div>` : ''}
    ${outs.length ? `<div class="kb-sec"><h3>下游</h3>${outs.map((e) => line(e.b)).join('')}</div>` : ''}
    <div class="kb-btns">${open}</div>
    <div class="kb-src">点亮 ${on.size} 个节点 · 点 × 恢复全部</div>`;
}
