// 知识库大盘「目录」页签:按渐进式披露 L0 → L1 → L2 → L3 一层层展开(2026-10-25)。
// 左边目录树:L0 总索引 / 专题导航(L1)/ 官方文档集按原书目录(L1);右边是当前层的内容。
// 数据按需取(lib/data.js):outline.json 只有骨架,点到哪层才取那层 —— 几千条官方文档不一次端出来。
// 节点编码(地址 #outline=<节点>):L0 · T:<专题> · D:<文档集> · P:<文档集>|<目录路径> · Q:<专题> · I:<ID> · E:<ID>

import { esc, fmtInt } from './fmt.js';
import { fetchKbOutline, fetchKbList, fetchKbItem, fetchKbEvidence } from './data.js';
import { typePill, typeName, KTYPES, EIGHT } from './kb-docs.js';

const SEP = ' › ';
const PAGE = 50;
const cache = new Map();
async function cached(key, fn) {
  if (!cache.has(key)) cache.set(key, fn());
  const r = await cache.get(key);
  if (!r.ok) cache.delete(key);                  // 失败的不缓存,下次点再取
  return r;
}

const lv = (n) => `<span class="ol-lv">L${n}</span>`;
const err = (r) => `<div class="empty"><b>读取失败</b>${esc(r.error || '')}${r.status === 404 ? '(当前镜像版本不提供这一层的数据)' : ''}</div>`;
const node = (o, id) => o.topics.find((t) => t.slug === id);
const findToc = (items, path) => {                  // 原书目录里按路径找节点
  let cur = { children: items };
  for (const seg of path) { cur = (cur.children || []).find((c) => c.name === seg); if (!cur) return null; }
  return cur;
};
function subTable(code, n) {
  if (!n || !n.children.length) return '';
  return `<table class="ol-tbl" style="margin-bottom:12px"><thead><tr><th>子目录</th><th class="r">条目</th><th></th></tr></thead><tbody>
    ${n.children.map((c) => `<tr class="ol-row" data-ol="P:${esc(code)}|${esc(c.path.join(SEP))}"><td>${esc(c.name)}</td><td class="r">${fmtInt(c.n)}</td><td>${c.children.length ? `${c.children.length} 个子目录` : ''}</td></tr>`).join('')}</tbody></table>
    <h3>本目录下全部条目</h3>`;
}
const dsOf = (o, code) => (o.docsets || []).find((d) => d.code === code);

function parse(key) {
  const k = key || 'L0';
  const [kind, rest = ''] = k.includes(':') ? [k.slice(0, k.indexOf(':')), k.slice(k.indexOf(':') + 1)] : [k, ''];
  if (kind === 'P') { const [code, path = ''] = rest.split('|'); return { kind, code, path }; }
  return { kind, arg: rest };
}

// ---------------------------------------------------------------- 左边目录树
function tocNodes(code, items, cur, depth) {
  return items.map((c) => {
    const key = `P:${code}|${c.path.join(SEP)}`;
    const onPath = cur.kind === 'P' && cur.code === code && (cur.path + SEP).startsWith(c.path.join(SEP) + SEP);
    const kids = onPath && c.children.length ? `<div class="ol-kids">${tocNodes(code, c.children, cur, depth + 1)}</div>` : '';
    return `<div class="ol-n d${depth} ${cur.kind === 'P' && cur.path === c.path.join(SEP) ? 'on' : ''}" data-ol="${esc(key)}">
      ${c.children.length ? `<i class="ol-car ${onPath ? 'open' : ''}"></i>` : '<i class="ol-car leaf"></i>'}${esc(c.name)}<small>${fmtInt(c.n)}</small></div>${kids}`;
  }).join('');
}

function tree(o, cur) {
  const t = o.topics.map((x) => `<div class="ol-n d1 ${cur.kind === 'T' && cur.arg === x.slug || cur.kind === 'Q' && cur.arg === x.slug ? 'on' : ''}" data-ol="T:${esc(x.slug)}">
      <i class="ol-car leaf"></i>${esc(x.name)}<small>${x.entries.length}${x.official ? ` · 官方 ${fmtInt(x.official)}` : ''}</small></div>`).join('');
  const d = (o.docsets || []).map((s) => {
    const open = (cur.kind === 'D' && cur.arg === s.code) || (cur.kind === 'P' && cur.code === s.code);
    return `<div class="ol-n d1 ${cur.kind === 'D' && cur.arg === s.code ? 'on' : ''}" data-ol="D:${esc(s.code)}">
      <i class="ol-car ${open ? 'open' : ''}"></i>${esc(s.code)}<small>${fmtInt(s.items)}</small></div>${open ? `<div class="ol-kids">${tocNodes(s.code, s.toc, cur, 2)}</div>` : ''}`;
  }).join('');
  return `<div class="ol-n d0 ${cur.kind === 'L0' ? 'on' : ''}" data-ol="L0">${lv(0)}知识库总索引</div>
    <div class="ol-h">${lv(1)}专题导航</div>${t}
    ${d ? `<div class="ol-h">${lv(1)}官方文档集 · 原书目录</div>${d}` : ''}`;
}

// ---------------------------------------------------------------- 右边各层
function crumb(o, cur, extra) {
  const parts = [`<a data-ol="L0">${lv(0)}总索引</a>`];
  if (cur.kind === 'T' || cur.kind === 'Q') parts.push(`<a data-ol="T:${esc(cur.arg)}">${lv(1)}${esc((node(o, cur.arg) || {}).name || cur.arg)}</a>`);
  if (cur.kind === 'D') parts.push(`<span>${lv(1)}${esc(cur.arg)}</span>`);
  if (cur.kind === 'P') {
    parts.push(`<a data-ol="D:${esc(cur.code)}">${lv(1)}${esc(cur.code)}</a>`);
    parts.push(`<span>${esc(cur.path.split(SEP).slice(-1)[0])}</span>`);
  }
  if (extra) parts.push(extra);
  return `<div class="ol-crumb">${parts.join('<b>›</b>')}</div>`;
}

function l0(o) {
  const t = o.types || {};
  const cell = (k, s, n) => `<div class="ol-stat ${n ? '' : 'dim'}"><b>${fmtInt(n)}</b><span>${esc(s)}</span></div>`;
  const types = KTYPES.filter(([k]) => EIGHT.includes(k) || t[k]).map(([k, s]) => {
    const n = (t[k] || 0) - (k === 'product' ? (o.official || 0) : 0);
    return cell(k, s, n);
  }).join('') + (o.official ? cell('official', '官方文档', o.official) : '');
  const sets = (o.docsets || []).map((s) => `<tr data-ol="D:${esc(s.code)}" class="ol-row"><td><b>${esc(s.code)}</b></td><td>${esc(s.title)}</td>
      <td>${esc(s.product)} ${esc(s.versions)}${s.kernel_version ? `(内核 ${esc(s.kernel_version)})` : ''}</td><td class="r">${fmtInt(s.items)}</td>
      <td>${s.index_complete ? '<span class="pill ok">索引完整</span>' : '<span class="pill warn">索引未完成</span>'}</td></tr>`).join('');
  const prof = (o.profiles || []).map((p) => `<tr><td>${esc(p.system)}</td><td>${esc(p.product)} ${esc(p.version)}</td><td>${esc(p.aliases.join('、'))}</td><td>${esc(p.id)}</td></tr>`).join('');
  const topics = o.topics.map((x) => `<div class="ol-card" data-ol="T:${esc(x.slug)}"><b>${esc(x.name)}</b>
      <span>${x.entries.length} 条本行知识${x.official ? ` · 官方 ${fmtInt(x.official)}` : ''}${x.causes.length ? ` · 候选原因 ${x.causes.length}` : ''}</span>
      <small>${esc(x.synonyms.slice(0, 4).join(' · '))}</small></div>`).join('');
  return `<h2>${lv(0)}知识库总索引</h2>
    <p class="sub">诊断时模型先读这一层(八类知识各多少、本实例是什么环境、有哪些专题),再按专题(L1)下钻到条目(L2),核对原文时才读原始证据(L3)。点任意一项往下看。</p>
    ${o.test_switch ? '<div class="ol-warn">⚠ 测试开关开着:官方文档集对所有实例都按「适用性未知」处理(只许在测试环境)</div>' : ''}
    <h3>知识范围</h3><div class="ol-stats">${types}</div>
    ${prof ? `<h3>适用环境(画像)</h3><table class="ol-tbl"><thead><tr><th>系统</th><th>产品与版本</th><th>连接别名</th><th>画像</th></tr></thead><tbody>${prof}</tbody></table>` : ''}
    ${sets ? `<h3>官方文档集(标准知识库,随工具版本安装)</h3><table class="ol-tbl"><thead><tr><th>代号</th><th>文档</th><th>产品与版本</th><th class="r">条目</th><th>索引</th></tr></thead><tbody>${sets}</tbody></table>` : ''}
    <h3>专题入口(L1)</h3><div class="ol-cards">${topics}</div>`;
}

function itemRow(x) {
  return `<div class="ol-item" data-ol="I:${esc(x.id)}">${typePill(x.type)} <b>${esc(x.id)}</b>${x.category ? `<span class="ol-cat">${esc(x.category)}</span>` : ''}
    <div class="t">${esc(x.title)}</div>${x.summary ? `<div class="m">${esc(x.summary)}</div>` : ''}</div>`;
}

function l1Topic(o, x) {
  const syn = x.synonyms.length ? `<div class="kb-tags">${x.synonyms.map((s) => `<span>${esc(s)}</span>`).join('')}</div>` : '';
  const causes = x.causes.length ? `<table class="ol-tbl"><thead><tr><th>现象</th><th>候选根因</th><th class="r">案例</th><th>区分证据</th></tr></thead><tbody>
      ${x.causes.map((c) => `<tr><td>${esc(c.symptom)}</td><td>${esc(c.rootcause)}</td><td class="r">${c.cases.length}</td><td>${esc(c.discriminator || (x.lookalikes.includes(c.symptom) ? '⚠ 未写区分证据' : ''))}</td></tr>`).join('')}</tbody></table>
      ${x.lookalikes.length ? `<p class="note">⚠ 同一现象在本行有过不止一种根因:${esc(x.lookalikes.join('、'))}——先按区分证据取证。</p>` : ''}` : '<p class="sub">本专题还没有本行确认过的因果链</p>';
  const own = x.entries.length ? x.entries.map(itemRow).join('') : '<p class="sub">(本行还没有这个专题的知识)</p>';
  const cats = Object.entries(x.official_categories || {}).map(([k, v]) => `<span>${esc(k)} ${fmtInt(v)}</span>`).join('');
  const off = x.official ? `<h3>官方文档 · ${fmtInt(x.official)} 条</h3><div class="kb-tags">${cats}</div>
      <p class="sub" style="margin-top:8px">代表条目:</p>${x.official_top.map(itemRow).join('')}
      <button class="kb-btn" data-ol="Q:${esc(x.slug)}">逐条浏览全部 ${fmtInt(x.official)} 条 →</button>` : '';
  return `<h2>${lv(1)}${esc(x.name)}<small>专题导航 · ${esc(x.slug)}</small></h2>${syn}
    <h3>候选原因(本行确认过的因果链)</h3>${causes}
    <h3>本行知识 · ${x.entries.length} 条</h3>${own}${off}`;
}

function l1Docset(s) {
  const rows = s.toc.map((c) => `<tr class="ol-row" data-ol="P:${esc(s.code)}|${esc(c.path.join(SEP))}"><td>${esc(c.name)}</td><td class="r">${fmtInt(c.n)}</td><td>${c.children.length} 个子目录</td></tr>`).join('');
  return `<h2>${lv(1)}${esc(s.code)}<small>${esc(s.title)}</small></h2>
    <div class="kb-meta"><span>${esc(s.product)} ${esc(s.versions)}</span>${s.doc_version ? `<span>文档 ${esc(s.doc_version)}</span>` : ''}${s.kernel_version ? `<span>内核 ${esc(s.kernel_version)}</span>` : ''}
      <span>${fmtInt(s.items)} 条</span>${s.model_summaries ? `<span>重点页摘要精修 ${fmtInt(s.model_summaries)}</span>` : ''}</div>
    <p class="sub">按原书目录逐级下钻;左边目录树同步展开。官方文档只说明产品怎么工作,与本行规范、案例不一致时以本行为准。</p>
    <table class="ol-tbl"><thead><tr><th>目录</th><th class="r">条目</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function listPanel(params, title, offset, before = '') {
  const r = await cached(`list:${JSON.stringify(params)}:${offset}`, () => fetchKbList({ ...params, offset, limit: PAGE }));
  if (!r.ok) return err(r);
  const d = r.data;
  const pager = d.total > PAGE ? `<div class="ol-pager">${offset > 0 ? `<button class="kb-btn" data-olpage="${offset - PAGE}">← 上一页</button>` : ''}
      <span>${offset + 1}–${Math.min(offset + PAGE, d.total)} / ${fmtInt(d.total)}</span>
      ${offset + PAGE < d.total ? `<button class="kb-btn" data-olpage="${offset + PAGE}">下一页 →</button>` : ''}</div>` : '';
  return `<h2>${title}<small>${fmtInt(d.total)} 条</small></h2>${before}${pager}${d.items.map(itemRow).join('') || '<p class="sub">(没有条目)</p>'}${pager}`;
}

async function l2(id) {
  const r = await cached(`item:${id}`, () => fetchKbItem(id));
  if (!r.ok) return err(r);
  const x = r.data;
  const meta = [`<span>${esc(typeName(x.type))}</span>`, ...(x.topics || []).map((t) => `<span>专题 ${esc(t)}</span>`),
    x.product ? `<span>${esc(x.product)} ${esc(x.versions || '')}</span>` : '', x.deployment ? `<span>${esc(x.deployment)}</span>` : '',
    x.verification ? `<span>${esc(x.verification)}</span>` : '', x.as_of ? `<span>截至 ${esc(x.as_of)}</span>` : '',
    ...Object.entries(x.facts || {}).map(([k, v]) => `<span>${esc(k)} ${esc(v)}</span>`)].join('');
  const secs = (x.sections || []).map((s, i) => `<details class="ol-sec" ${s.text.length < 1500 || i < 1 ? 'open' : ''}><summary>${esc(s.name)}<small>${fmtInt(s.text.length)} 字</small></summary>
      <pre>${esc(s.text)}</pre></details>`).join('');
  return `<h2>${lv(2)}${esc(x.title)}</h2>${typePill(x.type)} <b>${esc(x.id)}</b>
    <div class="kb-meta" style="margin-top:8px">${meta}</div>
    ${x.doc_path && x.doc_path.length ? `<p class="sub">目录:${esc(x.doc_path.join(SEP))}</p>` : ''}
    <div class="kb-factor kb-rule"><b>摘要</b> ${esc(x.summary)}${x.summary_by === 'model' ? '<small class="ol-by">(模型依据原页撰写,脚本已核对)</small>' : ''}</div>
    ${secs}
    <div class="kb-btns"><span class="kb-btn" data-ol="E:${esc(x.id)}">${lv(3)}看原始证据 →</span>
      <a class="kb-btn dig" href="#" data-dig="${esc('请在知识库里查看 ' + x.id + '(' + x.title + '),并说明它对本环境是否适用。')}" data-dig-title="知识库">在会话里问这条 ↗</a></div>
    <div class="kb-src">出处 ${esc(x.source || '—')} · 文件 ${esc(x.file || '—')}</div>`;
}

async function l3(id) {
  const r = await cached(`ev:${id}`, () => fetchKbEvidence(id));
  if (!r.ok) return err(r);
  const x = r.data;
  if (x.kind === 'page') {
    return `<h2>${lv(3)}原始证据<small>${esc(x.id)}</small></h2><p class="sub">出处:${esc(x.source)}</p>
      <pre class="ol-page">${esc(x.text)}</pre>${x.truncated ? '<p class="note">(原页过长,只显示前一部分)</p>' : ''}`;
  }
  return `<h2>${lv(3)}原始证据<small>${esc(x.id)}</small></h2>
    ${(x.refs || []).length ? `<ul class="ol-refs">${x.refs.map((s) => `<li><code>${esc(s)}</code></li>`).join('')}</ul>` : '<p class="sub">(这条没有登记出处或证据)</p>'}
    <p class="note">${esc(x.note || '')}</p>`;
}

// ---------------------------------------------------------------- 挂载
export async function mountOutline(el, S, onNavigate) {
  if (!el) return;
  el.innerHTML = '<div class="card"><div class="empty">正在读取知识库目录…</div></div>';
  const r = await cached('outline', fetchKbOutline);
  if (!r.ok) { el.innerHTML = `<div class="card">${err(r)}</div>`; return; }
  const o = r.data;
  if (!o.attached) { el.innerHTML = `<div class="card"><div class="empty"><b>知识库未接入</b>${esc(o.reason || '')}</div></div>`; return; }
  const cur = parse(S.ol);
  let main;
  if (cur.kind === 'T' && node(o, cur.arg)) main = crumb(o, cur) + l1Topic(o, node(o, cur.arg));
  else if (cur.kind === 'Q' && node(o, cur.arg)) main = crumb(o, cur, '<span>官方文档</span>') + await listPanel({ topic: cur.arg }, `${lv(1)}${esc(node(o, cur.arg).name)} · 官方文档`, S.olOffset || 0);
  else if (cur.kind === 'D' && dsOf(o, cur.arg)) main = crumb(o, cur) + l1Docset(dsOf(o, cur.arg));
  else if (cur.kind === 'P' && dsOf(o, cur.code)) {
    const sub = (S.olOffset || 0) ? '' : subTable(cur.code, findToc(dsOf(o, cur.code).toc, cur.path.split(SEP)));
    main = crumb(o, cur) + await listPanel({ docset: cur.code, path: cur.path }, `${lv(1)}${esc(cur.path.split(SEP).slice(-1)[0])}`, S.olOffset || 0, sub);
  }
  else if (cur.kind === 'I') main = crumb(o, { kind: 'L0' }, `<span>${lv(2)}${esc(cur.arg)}</span>`) + await l2(cur.arg);
  else if (cur.kind === 'E') main = crumb(o, { kind: 'L0' }, `<a data-ol="I:${esc(cur.arg)}">${lv(2)}${esc(cur.arg)}</a><b>›</b><span>${lv(3)}原文</span>`) + await l3(cur.arg);
  else main = crumb(o, { kind: 'L0' }) + l0(o);
  el.innerHTML = `<div class="ol"><aside class="card ol-tree">${tree(o, cur)}</aside><section class="card ol-main">${main}</section></div>`;
  if (!el.dataset.olBound) {
    el.dataset.olBound = '1';
    el.addEventListener('click', (ev) => {
      const t = ev.target.closest('[data-ol],[data-olpage]');
      if (!t || !el.contains(t)) return;
      ev.preventDefault();
      if (t.dataset.olpage !== undefined) onNavigate({ olOffset: +t.dataset.olpage });
      else onNavigate({ ol: t.dataset.ol, olOffset: 0 });
    });
  }
}
