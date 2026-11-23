// 知识库大盘的「案例」「条款」两个页签:左边列表(搜索 / 筛选),右边原文;以及各页签共用的类型 / 专题标签。
// 数据是 /reports/_kb/catalog.json(用户 Pod 从共享知识库现读现算)。
// 只出 HTML 字符串;交互由 pages/kb.js 统一用 data-* 属性委托:
//   data-case="<案例 id>"  data-rule="<条款 id>"  data-sys="<系统>"  data-node="<关系图节点 id>"  input.kb-q
//   data-item="<条目 id>"  data-ktype="<类型>"  data-topic="<专题>"(知识条目页签,lib/kb-items.js)

import { esc } from './fmt.js';

export const SEV = { S1: 'crit', S2: 'warn', S3: 'notice', S4: 'notice' };
export const RSEV = { error: ['crit', '必须'], warn: ['warn', '建议'], info: ['notice', '提示'] };
const SECTION_ORDER = ['现场', '判断', '处置', '复发标志'];
// 知识类型:代码、简称、全称,顺序与 common/kb/types.py 的 TYPE_NAMES 一致(有守卫测试)
export const KTYPES = [['errata', '修正', '公司内部规范(修正)'], ['norm', '规范', '公司内部规范'], ['runbook', '手册', '处置与恢复手册'],
  ['defect', '缺陷', '已知缺陷与版本例外'], ['baseline', '基线', '指标口径与正常基线'], ['case', '案例', '历史案例排查思路'],
  ['product', '产品文档', '高斯产品文档'], ['wiki', 'Wiki', '存量 Wiki 经验'], ['profile', '画像', '环境与业务画像'], ['guide', '指南', '指南(类型待确认)']];
// 八类知识(总览上即使为 0 也列出来,缺哪类一眼可见);修正、旧指南只在有的时候出现
export const EIGHT = ['norm', 'case', 'profile', 'baseline', 'defect', 'runbook', 'product', 'wiki'];
const KT = Object.fromEntries(KTYPES.map(([k, s, n]) => [k, { short: s, name: n }]));
export const typeShort = (k) => (KT[k] ? KT[k].short : k);
export const typeName = (k) => (KT[k] ? KT[k].name : k);
export const typePill = (k) => `<span class="pill kt" title="${esc(typeName(k))}">${esc(typeShort(k))}</span>`;
export const topicName = (cat, slug) => { const t = (cat.topics || []).find((x) => x.slug === slug); return t ? t.name : slug; };
export const topicTags = (cat, slugs) => ((slugs || []).length ? `<span>专题 ${slugs.map((s) => esc(topicName(cat, s))).join(' / ')}</span>` : '');

export const allRules = (cat) => (cat.groups || []).flatMap((g) => (g.rules || []).map((r) => ({ ...r, group: g.title, file: g.file })));
export const citedBy = (cat, rid) => (cat.cases || []).filter((c) => (c.rules || []).includes(rid));

const sevPill = (s) => `<span class="pill ${SEV[s] || 'dim'}">${esc(s || '—')}</span>`;
const rulePill = (s) => { const x = RSEV[s]; return `<span class="pill ${x ? x[0] : 'dim'}">${x ? x[1] : esc(s || '—')}</span>`; };
const has = (hay, q) => !q || hay.join(' ').toLowerCase().includes(q);

// ---------------------------------------------------------------- 案例
export function casesView(cat, st) {
  const cases = [...(cat.cases || [])].sort((a, b) => String(b.occurred_at).localeCompare(String(a.occurred_at)));
  if (!cases.length) return `<div class="card"><div class="empty"><b>知识库里还没有案例</b>案例由知识库管理员在导入环境从工单导入。</div></div>`;
  const q = (st.q || '').trim().toLowerCase();
  const systems = [...new Set(cases.map((c) => c.system).filter(Boolean))];
  const list = cases.filter((c) => (!st.sys || c.system === st.sys) &&
    has([c.title, c.primary_factor, c.id, ...(c.signals || []), ...(c.objects || []), ...Object.values(c.sections || {})], q));
  const cur = cases.find((c) => c.id === st.caseId) || list[0] || cases[0];
  return `<div class="kb-split">
    <div class="card kb-list"><div class="kb-tools">
        <input class="kb-q" placeholder="搜索现象、对象、处置……" value="${esc(st.q || '')}">
        <div class="kb-chips"><span data-sys="" class="${st.sys ? '' : 'on'}">全部系统</span>${systems.map((s) => `<span data-sys="${esc(s)}" class="${st.sys === s ? 'on' : ''}">${esc(s)}</span>`).join('')}</div>
        <div class="sub" style="margin:0">共 ${list.length} 条 · 按发生时间倒序</div></div>
      <div class="kb-items">${list.map((c) => `<div class="kb-item ${c.id === cur.id ? 'on' : ''}" data-case="${esc(c.id)}">
          ${sevPill(c.severity)}<div class="t">${esc(c.title)}</div>
          <div class="m">${esc(c.system)} · ${esc(c.occurred_at)} · ${esc(c.conclusion)}${(c.rules || []).length ? ` · 依据 ${c.rules.length} 条条款` : ''}${(c.topics || []).length ? ` · ${esc(c.topics.map((s) => topicName(cat, s)).join(' / '))}` : ''}</div></div>`).join('')
        || '<div class="kb-item m">没有匹配的案例</div>'}</div></div>
    ${caseDoc(cat, cur)}</div>`;
}

export function caseDoc(cat, c) {
  const rules = Object.fromEntries(allRules(cat).map((r) => [r.id, r]));
  const secs = c.sections || {};
  const keys = [...SECTION_ORDER.filter((k) => secs[k]), ...Object.keys(secs).filter((k) => !SECTION_ORDER.includes(k))];
  const tags = (title, xs) => (xs && xs.length ? `<div class="kb-sec"><h3>${title}</h3><div class="kb-tags">${xs.map((s) => `<span>${esc(s)}</span>`).join('')}</div></div>` : '');
  return `<div class="card kb-doc">
    ${sevPill(c.severity)}<h1>${esc(c.title)}</h1>
    <div class="kb-meta">${typePill('case')}<span>系统 ${esc(c.system || '—')}</span><span>发生 ${esc(c.occurred_at || '—')}</span><span>结论 ${esc(c.conclusion || '—')}</span>${topicTags(cat, c.topics)}</div>
    ${c.primary_factor ? `<div class="kb-factor"><b>主因</b> ${esc(c.primary_factor)}</div>` : ''}
    ${keys.map((k) => `<div class="kb-sec"><h3>${esc(k)}</h3><p>${esc(secs[k])}</p></div>`).join('')}
    ${tags('识别信号', c.signals)}${tags('涉及对象', c.objects)}
    ${(c.rules || []).length ? `<div class="kb-sec"><h3>依据条款</h3>${c.rules.map((id) =>
      `<a class="kb-ref" data-rule="${esc(id)}"><b>${esc(id)}</b>${esc(rules[id] ? rules[id].rule : '(不在现行条款清单:可能已废止)')}</a>`).join('')}</div>` : ''}
    <div class="kb-btns"><span class="kb-btn" data-node="case:${esc(c.id)}">在关系图中查看 →</span>
      <a class="kb-btn dig" href="#" data-dig="${esc('请在知识库里找与「' + c.title + '」类似的案例,并说明处置要点。')}" data-dig-title="知识库">在会话里问类似问题 ↗</a></div>
    <div class="kb-src">出处 ${esc(c.source || '—')} · 案例文件 cases/${esc(c.id)}.md</div></div>`;
}

// ---------------------------------------------------------------- 条款
export function rulesView(cat, st) {
  const rules = allRules(cat);
  if (!rules.length) return `<div class="card"><div class="empty"><b>知识库里还没有现行条款</b>条款由知识库管理员在导入环境从规范文件导入。</div></div>`;
  const q = (st.q || '').trim().toLowerCase();
  const hit = (r) => has([r.id, r.rule, r.rationale, r.criteria, ...(r.keywords || [])], q);
  const cur = rules.find((r) => r.id === st.ruleId) || rules.find(hit) || rules[0];
  const n = rules.filter(hit).length;
  return `<div class="kb-split">
    <div class="card kb-list"><div class="kb-tools">
        <input class="kb-q" placeholder="搜索条文、关键词或编号……" value="${esc(st.q || '')}">
        <div class="sub" style="margin:0">${(cat.groups || []).length} 份规范 · 现行 ${rules.length} 条${q ? ` · 命中 ${n}` : ''}</div></div>
      <div class="kb-items">${(cat.groups || []).map((g) => { const rs = (g.rules || []).filter(hit);
        return rs.length ? `<div class="kb-grp">${esc(g.title)} · ${rs.length}</div>` + rs.map((r) =>
          `<div class="kb-item ${r.id === cur.id ? 'on' : ''}" data-rule="${esc(r.id)}">${rulePill(r.severity)} <b style="font-size:12.5px">${esc(r.id)}</b>
            <div class="m kb-clamp">${esc(r.rule)}</div></div>`).join('') : ''; }).join('') || '<div class="kb-item m">没有匹配的条款</div>'}</div></div>
    ${ruleDoc(cat, cur)}</div>`;
}

export function ruleDoc(cat, r) {
  const cs = citedBy(cat, r.id);
  const x = RSEV[r.severity];
  return `<div class="card kb-doc">
    <span class="pill ${x ? x[0] : 'dim'}">${x ? x[1] + '执行' : esc(r.severity || '—')}</span><h1>${esc(r.id)}</h1>
    <div class="kb-meta">${typePill('norm')}<span>${esc(r.group)}</span>${r.check ? `<span>${r.check === 'deterministic' ? '可由脚本判定' : '需人工判断'}</span>` : ''}${topicTags(cat, r.topics)}</div>
    <div class="kb-factor kb-rule"><b>条文</b> ${esc(r.rule)}</div>
    ${r.rationale ? `<div class="kb-sec"><h3>为什么</h3><p>${esc(r.rationale)}</p></div>` : ''}
    ${r.criteria ? `<div class="kb-sec"><h3>怎么判定违规</h3><p>${esc(r.criteria)}</p></div>` : ''}
    ${(r.keywords || []).length ? `<div class="kb-sec"><h3>关键词</h3><div class="kb-tags">${r.keywords.map((k) => `<span>${esc(k)}</span>`).join('')}</div></div>` : ''}
    <div class="kb-sec"><h3>引用它的案例 · ${cs.length}</h3>${cs.length ? cs.map((c) =>
      `<a class="kb-ref" data-case="${esc(c.id)}">${sevPill(c.severity)} ${esc(c.title)}</a>`).join('') : '<div class="sub">还没有案例引用这条</div>'}</div>
    ${cs.length ? `<div class="kb-btns"><span class="kb-btn" data-node="rule:${esc(r.id)}">在关系图中查看 →</span></div>` : ''}
    <div class="kb-src">出处 ${esc(r.source || '—')} · 条款文件 rules/${esc(r.file)}</div></div>`;
}
