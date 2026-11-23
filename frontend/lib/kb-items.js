// 知识库大盘的「知识条目」页签:八类知识放在一张清单里,按类型 / 专题筛选、搜索;右边是条目全文。
// 案例、条款也在清单里(右边沿用 kb-docs.js 的案例 / 条款原文),其余六类与修正、旧指南用本文件的 itemDoc。
// 交互:data-item="<id>"  data-ktype="<类型>"  data-topic="<专题>"  input.kb-q(由 views/normal/kb.js 委托)

import { esc } from './fmt.js';
import { KTYPES, allRules, caseDoc, ruleDoc, typePill, typeName, topicName, topicTags, RSEV } from './kb-docs.js';

const ORDER = Object.fromEntries(KTYPES.map(([k], i) => [k, i]));
const VERIFY = { 已验证: 'ok', 已确认: 'ok', 未验证: 'dim', 待确认: 'dim', 已过时: 'warn' };
const FACTS = [['product', '产品'], ['versions', '适用版本'], ['deployment', '部署形态'], ['version', '版本'], ['system', '系统'],
  ['affected_versions', '受影响版本'], ['fixed_in', '修复版本'], ['vendor_status', '厂商结论'], ['metric', '指标'], ['unit', '单位'],
  ['metric_source', '取数来源'], ['window', '采样窗口'], ['value_kind', '取值类型']];
const APPLIES = [['product', '产品'], ['versions', '版本'], ['systems', '系统'], ['deployment', '部署形态']];
const show = (v) => (Array.isArray(v) ? v.join(' / ') : String(v));

/** 清单:六类条目 + 修正 / 指南 + 案例 + 条款,统一成 {id, type, title, summary, topics, verification, ref}。 */
export function entries(cat) {
  const items = (cat.items || []).map((x) => ({ ...x, ref: x }));
  const cases = (cat.cases || []).map((c) => ({ id: c.id, type: 'case', title: c.title, summary: c.primary_factor || '', topics: c.topics || [],
    verification: c.conclusion || '', hay: [c.system, ...(c.signals || []), ...Object.values(c.sections || {})], ref: c }));
  const rules = allRules(cat).map((r) => ({ id: r.id, type: 'norm', title: r.rule, summary: r.rationale || '', topics: r.topics || [],
    verification: RSEV[r.severity] ? RSEV[r.severity][1] : '', hay: [r.criteria, ...(r.keywords || [])], ref: r }));
  return [...items, ...cases, ...rules].sort((a, b) => (ORDER[a.type] ?? 99) - (ORDER[b.type] ?? 99) || String(a.id).localeCompare(String(b.id)));
}

const chip = (attr, val, on, label) => `<span data-${attr}="${esc(val)}" class="${on ? 'on' : ''}">${label}</span>`;

export function itemsView(cat, st) {
  const all = entries(cat);
  if (!all.length) return `<div class="card"><div class="empty"><b>知识库里还没有知识条目</b>条目由知识库管理员在导入环境导入。</div></div>`;
  const q = (st.q || '').trim().toLowerCase();
  const hit = (e) => !q || [e.id, e.title, e.summary, e.source, ...(e.topics || []).map((s) => topicName(cat, s)),
    ...Object.values(e.sections || {}), ...(e.hay || [])].join(' ').toLowerCase().includes(q);
  const list = all.filter((e) => (!st.ktype || e.type === st.ktype) && (!st.topic || (st.topic === 'other' ? !(e.topics || []).length : (e.topics || []).includes(st.topic))) && hit(e));
  const n = (k) => all.filter((e) => e.type === k).length;
  const nt = (s) => all.filter((e) => (s === 'other' ? !(e.topics || []).length : (e.topics || []).includes(s))).length;
  const kinds = KTYPES.filter(([k]) => n(k));
  const cur = all.find((e) => e.id === st.itemId) || list[0] || all[0];
  return `<div class="kb-split">
    <div class="card kb-list"><div class="kb-tools">
        <input class="kb-q" placeholder="搜索标题、摘要、编号……" value="${esc(st.q || '')}">
        <div class="kb-chips">${chip('ktype', '', !st.ktype, '全部类型')}${kinds.map(([k, s]) => chip('ktype', k, st.ktype === k, `${esc(s)} ${n(k)}`)).join('')}</div>
        <div class="kb-chips">${chip('topic', '', !st.topic, '全部专题')}${(cat.topics || []).filter((t) => nt(t.slug)).map((t) => chip('topic', t.slug, st.topic === t.slug, `${esc(t.name)} ${nt(t.slug)}`)).join('')}</div>
        <div class="sub" style="margin:0">共 ${list.length} 条${list.length !== all.length ? ` / ${all.length}` : ''} · 按类型排列</div>
        ${(cat.docsets || []).length ? `<div class="sub" style="margin:0">官方文档集 ${(cat.docsets || []).map((d) => `${esc(d.code)} ${d.items} 条`).join('、')}:在<a href="#" data-tab="outline">「目录」页签</a>按专题 / 原书目录逐层浏览</div>` : ''}</div>
      <div class="kb-items">${list.map((e) => `<div class="kb-item ${e.id === cur.id ? 'on' : ''}" data-item="${esc(e.id)}">
          ${typePill(e.type)} <b style="font-size:12.5px">${esc(e.id)}</b><div class="t kb-clamp">${esc(e.title)}</div>
          <div class="m">${(e.topics || []).length ? esc(e.topics.map((s) => topicName(cat, s)).join(' / ')) : '未归专题'}${e.verification ? ` · ${esc(e.verification)}` : ''}</div></div>`).join('')
        || '<div class="kb-item m">没有匹配的条目</div>'}</div></div>
    ${cur.type === 'case' ? caseDoc(cat, cur.ref) : cur.type === 'norm' ? ruleDoc(cat, cur.ref) : itemDoc(cat, cur.ref)}</div>`;
}

function itemDoc(cat, x) {
  const facts = { ...(x.facts || {}) };
  const approval = [].concat(facts.approval || []);
  const aliases = [].concat(facts.aliases || []);
  const secs = Object.entries(x.sections || {}).filter(([k, v]) => k !== '摘要' && v);
  const kv = (pairs, src) => pairs.filter(([k]) => src[k] !== undefined && src[k] !== '').map(([k, l]) => `<span>${l} ${esc(show(src[k]))}</span>`).join('');
  const applies = kv(APPLIES, x.applies || {});
  const v = x.verification ? `<span class="pill ${VERIFY[x.verification] || 'dim'}">${esc(x.verification)}</span>` : '';
  return `<div class="card kb-doc">
    ${typePill(x.type)} ${v}<h1>${esc(x.title)}</h1>
    <div class="kb-meta"><span>${esc(x.id)}</span><span>${esc(typeName(x.type))}</span>${topicTags(cat, x.topics)}${x.as_of ? `<span>截至 ${esc(x.as_of)}</span>` : ''}${kv(FACTS, facts)}</div>
    ${x.summary ? `<div class="kb-factor kb-rule"><b>摘要</b> ${esc(x.summary)}</div>` : ''}
    ${applies || x.not_applies ? `<div class="kb-sec"><h3>适用范围</h3>${applies ? `<div class="kb-meta">${applies}</div>` : ''}${x.not_applies ? `<p>不适用:${esc(x.not_applies)}</p>` : ''}</div>` : ''}
    ${secs.map(([k, s]) => `<div class="kb-sec"><h3>${esc(k)}</h3><p>${esc(s)}</p></div>`).join('')}
    ${aliases.length ? `<div class="kb-sec"><h3>${x.type === 'profile' ? '连接别名(诊断时按它匹配本实例)' : '旧编号'}</h3><div class="kb-tags">${aliases.map((a) => `<span>${esc(a)}</span>`).join('')}</div></div>` : ''}
    ${approval.length ? `<div class="kb-sec"><h3>审批依据</h3>${approval.map((id) => `<a class="kb-ref" data-rule="${esc(id)}"><b>${esc(id)}</b>查看条款</a>`).join('')}</div>` : ''}
    ${(x.evidence || []).length ? `<div class="kb-sec"><h3>证据材料</h3><div class="kb-tags">${x.evidence.map((e) => `<span>${esc(e)}</span>`).join('')}</div></div>` : ''}
    <div class="kb-btns"><a class="kb-btn dig" href="#" data-dig="${esc('请在知识库里查看 ' + x.id + '(' + x.title + '),并说明它对本环境是否适用。')}" data-dig-title="知识库">在会话里问这条 ↗</a></div>
    <div class="kb-src">出处 ${esc(x.source || '—')} · 文件 ${esc(x.file || '—')}</div></div>`;
}
