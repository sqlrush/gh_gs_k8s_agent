// 明细抽屉:维度、子技能发现、SQL、WDR 部分、知识库案例。两种风格共用,内容只来自已取到的报告模型(lib/store.js);
// SQL 明细在当前页没有 Top SQL 模型时(比如从健康检查点过来)现取一次。

import { esc, cn, colFmt, fmtInt, fmtSec, fmtMs } from './fmt.js';
import { I, glyph, lk, tag, dig } from './ui.js';
import { drawer } from './overlay.js';
import { getModel, setModel } from './store.js';
import * as H from '../pages/health.js';
import * as T from '../pages/topsql.js';
import * as W from '../pages/wdr.js';
import { fetchSqltune } from './data.js';

const sev = (f) => `<div class="alert" data-lv="${lk(f.severity)}">${glyph(f.severity)}<div><b>${esc(cn(H.metricName(f)))}：${esc(f.value)}，阈值 ${esc(f.threshold)}</b><p>${esc(cn(f.evidence))}</p></div></div>`;
const closeBtn = '<button class="btn" data-close>关闭</button>';
const askBtn = (prompt, label = '在会话里深挖') => `<button class="btn primary"${dig(prompt)}>${I.spark}${esc(label)}</button>`;

/** 维度的原始表:列按整列量级格式化,长文本截断、悬停看全文。 */
export function dimTable(d) {
  const h = d.headers || [], F = h.map((x, j) => colFmt(d.rows || [], j));
  if (!(d.rows || []).length) return `<p class="t3">${esc(cn(d.note || '没有数据行'))}</p>`;
  return `<div class="scroll boxed"><table class="tbl"><thead><tr>${h.map((x) => `<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${d.rows.map((row) => `<tr>${row.map((c, j) =>
    `<td class="${/^-?[\d.,]+$/.test(String(c)) ? 'r' : String(c).length > 40 ? 'q' : ''}" title="${esc(c)}">${esc(F[j](c))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

export function openDim(i) {
  const m = getModel('health'); if (!m) return;
  const d = m.dims[+i]; if (!d) return;
  const fs = m.findings.filter((f) => f.dimension === d.dimension);
  drawer(esc(H.title(d.dimension)), tag(d.level), `<div class="sec">概况</div><p class="t2" style="margin:0">${esc(cn(d.headline || ''))}</p>${fs.length ? `<div class="sec">发现</div>${fs.map(sev).join('')}` : ''}<div class="sec">明细</div>${dimTable(d)}`,
    (d.level === null ? '' : askBtn(H.dimPrompt(d, m))) + closeBtn);
}

export function openXdim(name) {
  const m = getModel('health'); if (!m) return;
  const fs = m.findings.filter((f) => f.dimension === name), l = fs.reduce((a, f) => Math.max(a, f.severity | 0), 0);
  drawer(esc(H.title(name)), tag(l), `<div class="sec">来源</div><p class="t2" style="margin:0">由子技能报出，不属于巡检维度。</p><div class="sec">发现</div>${fs.map(sev).join('')}`,
    (fs[0] ? askBtn(H.fPrompt(fs[0], m)) : '') + closeBtn);
}

const SV = { info: 1, warn: 2, crit: 3 };
function tuneHtml(t) {
  if (T.isSkipped(t)) return `<div class="alert" data-lv="na">${glyph(null)}<div><b>系统 SQL，按策略不调优</b><p>只引用了系统对象 ${esc((t.system_objects || []).join('、'))}。这类慢多半是采集频率或系统压力，不是 SQL 本身的问题。</p></div></div>`;
  if (t && t.evidence) {
    const ev = t.evidence, meta = [ev.analyzed === undefined ? '' : ev.analyzed ? '实际执行' : '估算', ev.version || ''].filter(Boolean).join('，');
    return (ev.findings || []).map((f) => `<div class="alert" data-lv="${lk(SV[f.severity] ?? 1)}">${glyph(SV[f.severity] ?? 1)}<div><b>${esc(cn(f.detail || f.kind || ''))}</b>${f.advice ? `<p>建议：${esc(cn(f.advice))}</p>` : ''}</div></div>`).join('')
      + (ev.plan ? `<div class="sec">执行计划${meta ? `<span class="t3" style="font-weight:400;font-size:13px;margin-left:8px">${esc(meta)}</span>` : ''}</div><div class="plan">${esc(String(ev.plan).slice(0, 4000))}</div>` : '');
  }
  return `<div class="alert" data-lv="na">${glyph(null)}<div><b>还没有调优过</b><p>在会话里调优后，建议与执行计划会出现在这里。</p></div></div>`;
}
/** SQL 抽屉里的语句出自哪一份报告(深挖首句带它的时间,口径见 lib/prompts.js),与抽屉里显示的数同源:
 *  在 Top SQL 榜上(tm 给了)→ 那份榜;否则 WDR 窗口里有它 → 那个窗口;否则健康检查的发现里有它 → 那份报告;
 *  都不是、或那份报告没有时间 → ''(不猜)。 */
export function seenIn(id, tm) {
  if (tm) return T.tWhen(tm) ? T.tSrc(tm) : '';
  const wm = getModel('wdr'), ts = wm && W.dimOf(wm.d, 'topsql');
  if (wm && ((wm.sqlTexts || {})[id] || (ts && (ts.rows || []).some((x) => String(x[0]) === String(id))))) return wm.w && wm.w.begin_ts ? W.wWhen(wm) : '';
  const hm = getModel('health');
  if (hm && hm.findings.some((f) => String(f.sql_id) === String(id))) return H.hWhen(hm) ? H.hSrc(hm) : '';
  return '';
}

const KW = /\b(SELECT|FROM|WHERE|GROUP BY|ORDER BY|LEFT JOIN|JOIN|ON|AND|OR|UPDATE|SET|INSERT INTO|VALUES|DELETE|AS|IN|LIMIT|HAVING|sum|count)\b/gi;
const hl = (sql) => esc(sql).replace(KW, (x) => `<span class="kw">${x}</span>`).replace(/(\$\d+)/g, '<span class="ph">$1</span>');

/** SQL 明细:语句、执行统计、调优结论。key 是当前实例(当前页没有 Top SQL 模型时现取)。 */
export async function openSql(id, key) {
  let m = getModel('topsql');
  if (!m && key) { m = await T.load(key, T.readBy()); if (m.ok && !m.missing) setModel('topsql', m); }
  const r = m && m.ok && !m.missing ? m.rows.find((x) => x.sql_id === id) : null;
  let t = r ? m.tune[id] : null;
  if (!r && key) { const x = await fetchSqltune(key, id); t = x.ok ? x.data : null; }
  // Top SQL 榜里没有这条时,再看 WDR 报告留下的全文(WDR 采集时按 sql_id 存了 sql_texts)。
  // 从 WDR 页点进来的 sql_id 常常不在 Top SQL 榜里 —— 以前这里只能显示「不在榜单里」,
  // 全文无处可查,正是要补的那一块。
  const wm = getModel('wdr');
  const wText = !r && wm && wm.sqlTexts ? wm.sqlTexts[id] : '';
  const kv = (k, v) => `<div><div class="kv"><div class="k">${k}</div><div class="v num">${v}</div></div></div>`;
  const body = r ? `<div class="sec">语句</div><div class="sql">${hl(T.fullQuery(r.query))}</div>
      <div class="sec">执行统计</div><div class="kvs" style="--cols:4">${kv('调用次数', fmtInt(r.calls))}${kv('总耗时', fmtSec(r.total_sec))}${kv('平均耗时', fmtMs(r.avg_ms))}${kv('返回行', fmtInt(r.rows))}</div>`
    : wText ? `<div class="sec">语句（来自 WDR 窗口采集）</div><div class="sql">${hl(wText)}</div>`
    : `<div class="sec">语句</div><p class="t2" style="margin:0">这条语句不在当前的 Top SQL 榜单里${t && t.original_sql ? '，下面是调优记录里的原文' : ''}。</p>${t && t.original_sql ? `<div class="sql">${hl(t.original_sql)}</div>` : ''}`;
  const mark = T.isSkipped(t) ? '<span class="tag gray">系统 SQL</span>' : t ? '<span class="tag blue">已调优</span>' : '';
  const from = seenIn(id, r && m), fromText = from ? `这条语句出自${from}。` : '';
  drawer(`SQL 明细 ${esc(id)}`, mark, `${body}<div class="sec">调优结论</div>${tuneHtml(t)}`,
    (T.isSkipped(t) ? '' : askBtn(r ? T.tunePrompt(r, m) : `请调优 sql_id 为 ${id} 的语句。${fromText}`, t ? '在会话里继续调优' : '在会话里调优'))
    // 执行计划要**实时连库**才能出,而大盘是纯静态站、egress 为空,不碰数据库 ——
    // 所以计划不在大盘里生成,点一下开一个会话去取。这是当初定的隔离,不要为了省一次点击破掉。
    + askBtn(`请取 sql_id 为 ${id} 的执行计划并解读（必要时对比改写前后的计划）。${fromText}`, '在会话里看执行计划')
    + `<button class="btn" data-copy="${esc(id)}">复制 sql_id</button>`);
}

export function openWdim(name) {
  const m = getModel('wdr'); if (!m) return;
  const x = (m.d.dims || []).find((y) => y.dimension === name); if (!x) return;
  const fs = m.findings.filter((f) => f.dimension === name), l = fs.reduce((a, f) => Math.max(a, f.severity | 0), 0);
  drawer(esc(W.WT[name] || name), tag(l), `<div class="sec">概况</div><p class="t2" style="margin:0">${esc(cn(x.headline || ''))}</p>${fs.length ? `<div class="sec">发现</div>${fs.map((f) =>
    `<div class="alert" data-lv="${lk(f.severity)}">${glyph(f.severity)}<div><b>${esc(cn(f.metric))}：${esc(f.value)}，阈值 ${esc(f.threshold)}</b><p>${esc(cn(f.evidence))}</p></div></div>`).join('')}` : ''}<div class="sec">明细</div>${dimTable(x)}`,
    askBtn(fs[0] ? W.wPrompt(fs[0], m) : W.wDimPrompt(name, x, m)) + closeBtn);
}

export function openCase(id) {
  const m = getModel('kb'); const x = m && m.cases.find((c) => c.id === id); if (!x) return;
  const kv = (k, v) => `<div><div class="kv"><div class="k">${k}</div><div class="v">${esc(v || '—')}</div></div></div>`;
  const secs = Object.entries(x.sections || {}).map(([k, v]) => `<div class="sec">${esc(k)}</div><p class="t2" style="margin:0;white-space:pre-wrap">${esc(cn(v))}</p>`).join('');
  drawer(esc(x.title), `<span class="tag line">${esc(x.severity || '—')}</span>`, `<div class="kvs" style="--cols:3">${kv('系统', x.system)}${kv('发生时间', x.occurred_at)}${kv('结论', x.conclusion)}</div>
    <div class="sec">主因</div><p style="margin:0">${esc(cn(x.primary_factor))}</p>${secs}
    ${(x.rules || []).length ? `<div class="sec">依据条款</div><p style="margin:0">${x.rules.map((r) => `<span class="tag gray" style="margin-right:6px">${esc(r)}</span>`).join('')}</p>` : ''}
    <div class="sec">出处</div><p class="t3" style="margin:0;word-break:break-all">${esc(x.source)}</p>`,
    askBtn(`请参考案例「${x.title}」,判断当前实例有没有同样的复发标志:${(x.sections || {})['复发标志'] || ''}`, '对照当前实例') + closeBtn);
}
