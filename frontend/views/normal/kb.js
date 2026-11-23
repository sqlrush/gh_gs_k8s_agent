// 普通风格 · 知识库:总览(主视觉 = 条目数、覆盖环、关系缩略图 + 智能体解读;其下 = 最近案例、健康自检、最近检索、缺口)
// + 知识条目 / 案例 / 条款 / 关系图四个页签(lib/kb-items.js、lib/kb-docs.js、lib/kb-graph.js)。页签记在地址的 # 上(#items=<id>、#cases=<id> 等),
// 刷新与后退都停在原处。

import { esc, stamp, cn, fmtInt } from '../../lib/fmt.js';
import { I, glyph, tag, cu, dig } from '../../lib/ui.js';
import { rise } from '../../lib/motion.js';
import { setModel } from '../../lib/store.js';
import { covring, minigraph } from '../../lib/viz.js';
import * as insight from '../../lib/insight.js';
import { casesView, rulesView, KTYPES, EIGHT } from '../../lib/kb-docs.js';
import { itemsView, entries } from '../../lib/kb-items.js';
import { mountOutline } from '../../lib/kb-outline.js';
import { graphView } from '../../lib/kb-graph.js';
import { missLabel } from '../../pages/kb.js';
import { pageHead, tile, insightCard, emptyHero } from './common.js';

const TABS = [['overview', '总览'], ['outline', '目录'], ['items', '知识条目'], ['cases', '案例'], ['rules', '条款'], ['graph', '关系图']];
const S = { tab: 'overview', caseId: '', ruleId: '', itemId: '', node: '', q: '', sys: '', ktype: '', topic: '', ol: 'L0', olOffset: 0 };
function readHash() {
  const [tab, arg] = decodeURIComponent(location.hash.replace(/^#/, '')).split('=');
  S.tab = TABS.some(([k]) => k === tab) ? tab : 'overview';
  if (S.tab === 'outline') S.ol = arg || 'L0';
  if (arg && S.tab === 'items') S.itemId = arg;
  if (arg && S.tab === 'cases') S.caseId = arg;
  if (arg && S.tab === 'rules') S.ruleId = arg;
  if (S.tab === 'graph') S.node = arg || '';
}
function writeHash() {
  const arg = S.tab === 'outline' ? (S.ol === 'L0' ? '' : S.ol) : S.tab === 'items' ? S.itemId : S.tab === 'cases' ? S.caseId : S.tab === 'rules' ? S.ruleId : S.tab === 'graph' ? S.node : '';
  const h = '#' + S.tab + (arg ? '=' + encodeURIComponent(arg) : '');
  if (location.hash !== h) history.replaceState(null, '', h);
}

// 总览上的类型计数:八类即使为 0 也列出(灰显),修正 / 旧指南有才列;点一类进「知识条目」并按它筛选
function typeSplit(m) {
  const n = (k) => +m.types[k] || 0;
  return KTYPES.filter(([k]) => EIGHT.includes(k) || n(k)).map(([k, s, name]) =>
    `<span${n(k) ? '' : ' class="t3"'}><a href="#" data-ktype="${k}" title="${name}">${s}</a> <b>${fmtInt(n(k))}</b></span>`).join('')
    + (m.docsets || []).map((d) => `<span><a href="#" data-tab="outline" title="${esc(d.title)}">官方 ${esc(d.code)}</a> <b>${fmtInt(d.items)}</b></span>`).join('');
}

function hero(m) {
  const total = m.types ? Object.values(m.types).reduce((a, b) => a + (+b || 0), 0) : m.counts.case + m.counts.rule;
  const left = `<div class="glass kbbox rise"${rise()}><div class="title">${I.book}知识条目<span class="t3">全体共享，${m.d.readonly === false ? '可写' : '只读'}</span></div>
    <div class="kbtop"><div><div class="big num">${cu(fmtInt(total))}<small>条</small></div>
      <div class="kbsplit">${m.types ? typeSplit(m) : `<span>案例 <b>${fmtInt(m.counts.case)}</b></span><span>条款 <b>${fmtInt(m.counts.rule)}</b></span>`}${m.counts.raw ? `<span class="t3">整理自 ${fmtInt(m.counts.raw)} 份原始工单</span>` : ''}</div></div>
      <div class="kbcov">${covring(m.cov, m.cov === null ? (m.ok ? '文件模式只有关键词' : '查一次状态可见') : '向量覆盖')}</div></div>
    ${m.catOk ? `<div class="mg-h"><span>关系图 · ${Number.isFinite(m.gEdges) ? fmtInt(m.gEdges) : '—'} 条已确认的边（${esc(m.gKind)}）</span><a href="#" data-tab="graph">展开${I.arrow}</a></div>${minigraph(m.cat)}` : ''}</div>`;
  return `<section class="hero"><div class="hero-grid">${left}${insightCard({ ...insight.kb(m), ctx: '关于共享知识库:' })}</div></section>`;
}

function overview(m) {
  // 没有状态报告、但目录读得到(知识已导入,只是这个人还没查过状态):照常显示目录里的数,状态与缺口两处提示去查一次。
  // 以前整块「还没有知识库状态报告」,管理员导完知识一看以为没导进去(2026-11-03 行方反馈)
  const noHealth = !m.ok && m.status === 404 && m.catOk;
  const askHealth = `<p class="t3">还没有知识库状态报告：在会话里查一次知识库状态，索引、覆盖率和待处理项会显示在这里。</p><p><button class="btn"${dig('请查看知识库的健康状态。', '知识库')}>在会话里查一次知识库状态</button></p>`;
  if (!m.ok && !noHealth) return emptyHero(m.status === 404 ? '还没有知识库状态报告' : '知识库状态报告读取失败', m.status === 404 ? '在会话里查一次知识库状态，结果会出现在这里。' : (m.error || ''), '请查看知识库的健康状态。', '在会话里查一次知识库状态');
  if (!noHealth && !m.attached) return emptyHero('知识库未接入', `${m.st.reason || ''}${m.d.inbox ? ` 收件目录：${m.d.inbox}` : ''}`, '请查看知识库为什么没有接入。', '在会话里检查');
  const cases = `<div class="scroll"><table class="tbl"><thead><tr><th>级别</th><th>案例</th><th>系统</th><th>发生时间</th><th>结论</th><th class="r">操作</th></tr></thead><tbody>${m.cases.slice(0, 5).map((x) =>
    `<tr><td><span class="tag line">${esc(x.severity || '—')}</span></td><td class="title" title="${esc(x.title)}"><a href="#" data-case="${esc(x.id)}">${esc(x.title)}</a></td><td>${esc(x.system)}</td><td class="num">${esc(x.occurred_at)}</td><td>${esc(x.conclusion)}</td><td class="ops"><a href="#" data-case="${esc(x.id)}">查看</a></td></tr>`).join('')}</tbody></table></div>`;
  const health = `<div class="list">${m.pending.map((s) => `<div class="it">${glyph(1)}<div class="m"><b>待处理</b><small>${esc(cn(s))}</small></div></div>`).join('')}
    ${m.warns.map((s) => `<div class="it">${glyph(2)}<div class="m"><b>坏文件</b><small>${esc(cn(s))}</small></div></div>`).join('')}
    ${m.cov !== null && m.cov < 100 ? `<div class="it">${glyph(1)}<div class="m"><b>向量覆盖 ${m.cov}%</b><small>未覆盖的只能靠关键词命中，管理员在导入环境重建索引即可</small></div></div>` : ''}
    ${!m.issues ? `<div class="it">${glyph(0)}<div class="m"><b>没有待处理项</b></div></div>` : ''}</div>`;
  const q = m.queries.length ? `<div class="scroll"><table class="tbl"><thead><tr><th>时间</th><th>检索内容</th><th class="r">命中案例</th><th class="r">命中条款</th><th>方式</th></tr></thead><tbody>${m.queries.map((r) =>
    `<tr><td class="num t2">${esc(stamp(r.at))}</td><td>${esc(r.q)}</td><td class="r">${esc(r.hits_cases)}</td><td class="r">${esc(r.hits_rules)}</td><td>${r.how === 'semantic' ? '<span class="tag blue">语义</span>' : '<span class="tag gray">关键词</span>'}${!r.hits_cases && !r.hits_rules ? ' <span class="t3">未命中</span>' : ''}</td></tr>`).join('')}</tbody></table></div>` : '<p class="t3" style="padding:0 20px 16px">近 7 天没有检索记录</p>';
  const mx = Math.max(1, ...m.misses.map((x) => +x.n || 0));
  const gaps = m.misses.length ? `<div class="hb">${m.misses.slice(0, 10).map((x) => `<div class="r"><div class="l" title="${esc(missLabel(x.code))}">${esc(missLabel(x.code))}</div><div class="b"><i class="grow" style="width:${((+x.n || 0) / mx) * 100}%"></i></div><div class="v">${esc(x.n)}<small>次</small></div></div>`).join('')}</div><div class="note">缺口会随诊断自动累计；补上案例或条款后，下次同样的发现就能引用到知识（全体共享）。</div>` : '<p class="t3">没有缺口记录</p>';
  return hero(m) + `<div class="bento">${m.catOk ? tile({ span: 'b8', title: '最近的案例', desc: '按发生时间', body: cases, tools: '<a href="#" data-tab="cases">查看全部</a>', flush: true }) : tile({ span: 'b8', title: '最近的案例', body: `<p class="t3">读不到知识库目录：${esc(m.catErr || '')}</p>` })}
    ${tile({ span: 'b4', title: '健康自检', desc: '降级即发现', body: noHealth ? askHealth : health })}
    ${tile({ span: 'b8', title: '最近检索', desc: '本人，近 7 天', body: q, flush: !!m.queries.length })}
    ${tile({ span: 'b4', title: '缺口清单', desc: '诊断发现或检索词在库里查不到知识的次数', body: noHealth ? '<p class="t3">查一次知识库状态后显示</p>' : gaps })}</div>`;
}

export function render(root, m, ctx) {
  setModel('kb', m);
  readHash();
  const counts = m.catOk ? { items: entries(m.cat).length, cases: m.cases.length, rules: m.rules.length, graph: (m.cat.edges || []).length } : {};
  const draw = () => {
    writeHash();
    let body;
    if (S.tab === 'overview') body = overview(m);
    else if (!m.catOk) body = `<div class="kbx"><div class="card"><div class="empty"><b>读不到知识库目录</b>${esc(m.catErr || '')}</div></div></div>`;
    else if (S.tab === 'outline') body = '<div class="kbx"><div class="ol-mount"></div></div>';
    else body = `<div class="kbx">${S.tab === 'items' ? itemsView(m.cat, S) : S.tab === 'cases' ? casesView(m.cat, S) : S.tab === 'rules' ? rulesView(m.cat, S) : graphView(m.cat, S)}</div>`;
    const tabs = `<div class="tabs">${TABS.map(([k, l]) => `<button data-tab="${k}" class="${S.tab === k ? 'on' : ''}">${l}${counts[k] !== undefined ? `<span class="n">${counts[k]}</span>` : ''}</button>`).join('')}</div>`;
    const st = m.ok && m.attached ? (m.issues ? tag(1, `${m.issues} 项待处理`) : tag(0, '健康')) : '';
    const desc = S.tab === 'overview'
      ? [['模式', esc(m.st.mode || '—') + (m.st.reason ? `（${esc(m.st.reason)}）` : '')], ['版本', esc(m.st.version || '—')], ['本环境', m.d.readonly === false ? '可写' : '只读'], ...(m.ix.indexed_at ? [['上次索引', esc(m.ix.indexed_at)]] : []), ['维护', '全体共享，由知识库管理员在导入环境维护']]
      : [['目录生成于', esc((m.cat && m.cat.built_at) || '—')], ['本环境', '只读'], ...(m.cat && m.cat.warnings ? [['注意', `${esc(m.cat.warnings)} 个文件解析有问题，未列出`]] : [])];
    root.innerHTML = pageHead('知识库', st, `<button class="btn"${dig('请在知识库里检索:', '知识库')}>${I.search}在会话里检索</button>`, desc) + tabs + body;
    if (S.tab === 'outline') {
      const el = root.querySelector('.ol-mount');
      const nav = (patch) => { Object.assign(S, patch); writeHash(); mountOutline(el, S, nav).then(() => el.scrollIntoView({ block: 'start' })); };
      mountOutline(el, S, nav);
    }
  };
  const go = (patch) => { Object.assign(S, patch); draw(); window.scrollTo(0, 0); };
  root._kbDraw = draw; root._kbGo = go;
  if (!root.dataset.kbBound) {                    // 事件只挂一次:root 在切实例、切风格时会被整页重画
    root.dataset.kbBound = '1';
    root.addEventListener('click', (ev) => {
      const t = ev.target.closest('[data-tab],[data-item],[data-ktype],[data-topic],[data-case],[data-rule],[data-node],[data-sys]');
      if (!t || !root.contains(t) || t.hasAttribute('data-dig') || !root._kbGo) return;
      ev.preventDefault();
      if (t.dataset.tab !== undefined) root._kbGo({ tab: t.dataset.tab, q: '', node: t.dataset.tab === 'graph' ? S.node : '', ktype: '', topic: '' });
      else if (t.dataset.item !== undefined) root._kbGo({ tab: 'items', itemId: t.dataset.item });
      else if (t.dataset.ktype !== undefined || t.dataset.topic !== undefined) {
        const f = t.dataset.ktype !== undefined ? { ktype: t.dataset.ktype } : { topic: t.dataset.topic };
        if (S.tab === 'items') { Object.assign(S, f, { itemId: '' }); root._kbDraw(); }
        else root._kbGo({ tab: 'items', itemId: '', q: '', ktype: '', topic: '', ...f });
      }
      else if (t.dataset.case !== undefined) root._kbGo({ tab: 'cases', caseId: t.dataset.case, q: S.tab === 'cases' ? S.q : '', sys: S.tab === 'cases' ? S.sys : '' });
      else if (t.dataset.rule !== undefined) root._kbGo({ tab: 'rules', ruleId: t.dataset.rule, q: S.tab === 'rules' ? S.q : '' });
      else if (t.dataset.node !== undefined) { S.tab = 'graph'; S.node = t.dataset.node; root._kbDraw(); }
      else if (t.dataset.sys !== undefined) { S.sys = t.dataset.sys; root._kbDraw(); }
    });
    root.addEventListener('input', (ev) => {
      if (!ev.target.classList.contains('kb-q') || !root._kbDraw) return;
      const pos = ev.target.selectionStart;
      S.q = ev.target.value; root._kbDraw();
      const i = root.querySelector('.kb-q'); if (i) { i.focus(); i.setSelectionRange(pos, pos); }
    });
  }
  draw();
}
