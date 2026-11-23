// 文艺风格的内页:A2 要闻·健康检查、B1 慢语句·Top SQL、C1 负载·WDR、D1 知识·知识库。
// 每版一条版头,正文分栏;表格用报纸排法(粗线起止、细线分行)。点表格行、案例标题照常打开明细抽屉。

import { esc, stamp, cn, fmtInt, fmtMs, fmtSec, num, colFmt } from '../../lib/fmt.js';
import { lk, status, primary } from '../../lib/ui.js';
import * as insight from '../../lib/insight.js';
import * as P from '../../lib/press-charts.js';
import * as H from '../../pages/health.js';
import * as T from '../../pages/topsql.js';
import * as W from '../../pages/wdr.js';
import { missLabel } from '../../pages/kb.js';
import { KTYPES } from '../../lib/kb-docs.js';
import { paras } from './bits.js';
import { missedBanner, goneText } from '../../lib/missed.js';

const sheet = (id, pg, title, sub, body, tools = '') => `<section class="sheet" id="${id}"><div class="sheet-h"><span class="pg">${pg}</span><b>${esc(title)}</b><span class="sub">${sub}</span><span class="tools">${tools}</span></div>${body}</section>`;
const none = (text, prompt, label) => `<div class="sheet-empty"><p>${esc(text)}</p>${prompt ? primary(prompt, label) : ''}</div>`;
const fail = (m) => (m.reason === 'error' ? `报告读取失败：${m.error || ''}` : m.reason === 'range' ? '所选时间段内没有这一类报告（在报头的「时间段」里改选或清除）。' : m.reason === 'gone' ? goneText(m) : '');

/* ------------------------------------------------------------ A2 要闻 · 健康检查 */
function secHealth(hm) {
  if (!hm.ok) return sheet('sec-health', 'A2', '要闻 · 健康检查', '', none(fail(hm) || '本期没有巡检报告。', fail(hm) && hm.reason !== 'gone' ? '' : '请对当前库做一次健康检查。', '在会话里跑一次健康检查'));
  const open = (f) => { const d = hm.dims.find((x) => x.dimension === f.dimension); return d ? ` data-dim="${d.i}"` : ` data-xdim="${esc(f.dimension)}"`; };
  const table = hm.findings.length ? `<table class="ptable"><thead><tr><th>级别</th><th>维度</th><th>指标</th><th class="r">读数</th><th class="r">阈值</th><th class="r">倍数</th><th>证据</th></tr></thead><tbody>${hm.findings.map((f) => { const x = insight.times(f);
    return `<tr${open(f)}><td>${status(f.severity)}</td><td><b>${esc(H.title(f.dimension))}</b></td><td class="mt" title="${esc(cn(f.metric))}">${esc(cn(H.metricName(f)))}</td><td class="r num"><b>${esc(f.value)}</b></td><td class="r num t2">${esc(f.threshold)}</td><td class="r num">${x ? `${x.toFixed(1)}×` : '—'}</td><td class="ev2" title="${esc(cn(f.evidence))}">${esc(cn(f.evidence))}</td></tr>`; }).join('')}</tbody></table>`
    : `<p class="t2">${hm.level === null ? '这次什么都没采到，所以也没有发现——这份报告不能说明库是健康的。' : '本期没有越线发现。'}</p>`;
  const smalls = `<div class="smalls">${hm.dims.map((d) => { const k = H.keyFig(d);
    return `<button class="sm" data-lv="${lk(d.level)}" data-dim="${d.i}"><span class="n">${esc(H.title(d.dimension))}</span><span class="v num">${k ? esc(k.v) : '—'}<small>${k ? esc(k.u) : ''}</small></span><span class="c">${esc(k ? k.cap : cn(d.headline || '未采集'))}</span>${status(d.level, d.na || undefined)}</button>`; }).join('')}</div>`;
  const okS = hm.subs.filter((s) => s.ok).map((s) => s.skill.replace('gaussdb-', '')), bad = hm.subs.filter((s) => !s.ok), unc = hm.d.uncovered_capabilities || [];
  const note = `<p>本期巡检采集 ${hm.dims.length} 个维度，采到 ${hm.dims.filter((d) => d.level !== null).length} 个。${hm.subs.length ? `${okS.length ? `子技能 ${esc(okS.join('、'))} 执行成功，已纳入汇总` : '子技能都没有执行成功'}${bad.length ? `；${esc(bad.map((s) => `${s.skill.replace('gaussdb-', '')}（${cn(s.error || '失败')}）`).join('、'))}未纳入` : ''}。` : '本期未运行子技能。'}级别由巡检脚本按阈值判定，智能体只负责整理与解释。</p>
    ${unc.length ? `<p>不在巡检里的能力：${esc(unc.map((x) => x.replace('gaussdb-', '')).join('、'))}，需要时在会话里单独运行。</p>` : ''}`;
  const past = `<ol class="past">${[...hm.idx].reverse().slice(0, 20).map((e) => { const on = hm.entry && e.file === hm.entry.file;
    return `<li class="${on ? 'on' : ''}" data-run="health|${esc(e.file)}"><span class="d num">${esc(H.entryLabel(e))}</span>${status(H.entryLevel(e))}<span class="n num">${H.countsOf(e)} 条</span></li>`; }).join('')}</ol>`;
  return sheet('sec-health', 'A2', '要闻 · 健康检查', hm.span ? `${esc(hm.name)} · ${esc(hm.win)} 时段体检全文（窗口两端相减）` : `${esc(hm.name)} · ${esc(stamp(hm.entry && hm.entry.at))} 巡检全文`,
    `<div class="sheet-b"><div class="main"><h3 class="sh3">全部越线发现　<span>${hm.findings.length} 条，按级别从高到低；点一行看明细</span></h3>${table}
      <h3 class="sh3">${hm.dims.length} 个维度　<span>每格是该维度的关键读数</span></h3>${smalls}</div>
      <aside class="rcol"><div class="box"><h4>编者按：本期过程</h4>${note}</div>${hm.idx.length ? `<div class="box"><h4>${hm.span ? '往期时段体检' : '往期巡检'}</h4>${past}</div>` : ''}</aside></div>`);
}

/* ------------------------------------------------------------ B1 慢语句 · Top SQL */
function secTop(tm) {
  if (!tm.ok) return sheet('sec-topsql', 'B1', '慢语句 · Top SQL', '', none(fail(tm) || '本期没有 Top SQL 榜单。', fail(tm) && tm.reason !== 'gone' ? '' : `请列出当前库的 Top SQL(按${T.byLabel(tm.by || 'time')})。`, '在会话里取一次 Top SQL'));
  const by = T.BY.map(([k, l]) => `<a href="#" data-by="${k}" class="${k === tm.by ? 'on' : ''}">按${esc(l)}${tm.per[k] ? '' : '（未取）'}</a>`).join('');
  if (tm.missing) return sheet('sec-topsql', 'B1', '慢语句 · Top SQL', '', none(`「${T.byLabel(tm.by)}」这个维度还没取过。大盘只刊登在会话里真正取过的维度。`, `请按${T.byLabel(tm.by)}取一次 Top SQL。`, `在会话里按${T.byLabel(tm.by)}取一次`), `<span class="byx">${by}</span>`);
  const rows = tm.rows, ins = insight.topsql(tm), b = insight.bizTop(tm), label = T.byLabel(tm.by);
  const tg = (r) => { const x = tm.tune[r.sql_id]; return T.isSkipped(x) ? '系统 SQL' : x ? '已调优' : '未调优'; };
  // 等待占比(口径见 pages/topsql.js selfSec):报告里有自身耗时才排这一列,老报告不排、不写 0;主要在等的加粗
  const withSelf = T.hasSelf(rows);
  const wcell = (r) => { const w = T.waitRatio(r); return w === null ? '—' : T.waitHeavy(r) ? `<b>${Math.round(w * 100)}%</b>` : `${Math.round(w * 100)}%`; };
  const table = `<table class="ptable"><thead><tr><th>#</th><th>sql_id</th><th>语句</th><th class="r">调用</th><th class="r">总耗时</th><th class="r">平均</th><th class="r">返回行</th>${withSelf ? '<th class="r">等待占比</th>' : ''}<th>调优</th></tr></thead><tbody>${rows.slice(0, 20).map((r, i) => `<tr data-sql="${esc(r.sql_id)}"><td class="t3 num">${i + 1}</td><td><b>${esc(r.sql_id)}</b></td><td class="q mono" title="${esc(T.fullQuery(r.query))}">${esc(r.query)}</td><td class="r num">${fmtInt(r.calls)}</td><td class="r num">${fmtSec(r.total_sec)}</td><td class="r num">${fmtMs(r.avg_ms)}</td><td class="r num">${fmtInt(r.rows)}</td>${withSelf ? `<td class="r num">${wcell(r)}</td>` : ''}<td>${tg(r)}</td></tr>`).join('')}</tbody></table>`;
  const notes = `<ol class="notes">${rows.slice(0, 10).map((r) => { const x = tm.tune[r.sql_id], f = x && x.evidence && (x.evidence.findings || [])[0];
    return `<li data-sql="${esc(r.sql_id)}"><b>${esc(r.sql_id)}</b>　${T.isSkipped(x) ? `系统 SQL，只引用 ${esc((x.system_objects || []).join('、'))}，按策略不调优。` : f ? `${esc(cn(f.detail || ''))}。${f.advice ? `建议：${esc(cn(f.advice))}。` : ''}` : x ? '已调优。' : '还没有调优过。'}</li>`; }).join('')}</ol>`;
  return sheet('sec-topsql', 'B1', '慢语句 · Top SQL', tm.span ? `${esc(tm.name)} · ${esc(tm.win || stamp(tm.entry.at))} 窗口内增量，按${esc(label)}排序` : `${esc(tm.name)} · ${esc(stamp(tm.entry.at))} 取数 · 累计统计视图，按${esc(label)}排序`,
    `${missedBanner(tm)}<div class="sheet-b"><div class="main"><h3 class="hl3">${ins.head}</h3><div class="body cols2">${paras(ins.lines)}</div>
      <figure class="fig">${P.shares(rows, tm.tune, T.isSkipped, b && b.sql_id)}<figcaption>图　上榜语句占上榜总耗时的比例（报告只有 Top N，没有全量合计）。斜纹为系统 SQL；红色为业务语句里耗时最高的一条。${tm.by === 'reads' ? esc(T.READS_NOTE) + '。' : ''}</figcaption></figure></div>
      <aside class="rcol"><div class="box"><h4>调优札记</h4>${notes}</div></aside></div>
      <h3 class="sh3">${rows.length > 20 ? `榜单前 20 条（共 ${rows.length} 条）` : '完整榜单'}　<span>按${esc(label)}排序；语句满 ${T.QUERY_CAP} 字的是取数时截断的，悬停看原文${rows.some(T.waitHeavy) ? `；等待占比 ${T.WAIT_DOMINANT * 100}% 以上的加粗：主要在等（锁等），看锁阻塞` : ''}</span></h3>${table}
      ${rows.length > 20 ? `<p class="fine"><a href="/dash/topsql?style=normal">其余 ${rows.length - 20} 条见普通风格的 Top SQL 页 →</a></p>` : ''}`, `<span class="byx">${by}</span>`);
}

/* ------------------------------------------------------------ C1 负载 · WDR */
function secWdr(wm) {
  if (!wm.ok) return sheet('sec-wdr', 'C1', '负载 · WDR 分析', '', none(fail(wm) || '本期没有 WDR 窗口分析。', fail(wm) && wm.reason !== 'gone' ? '' : '请列出最近的 WDR 快照,并对最近一个窗口做分析。', '在会话里跑一次 WDR 分析'));
  if (wm.w.wdr_enabled === false) return sheet('sec-wdr', 'C1', '负载 · WDR 分析', '', none('这个库没有开启 WDR：需要把 enable_wdr_snapshot 设为 on 并有快照权限。', '请检查当前库的 WDR 快照是否开启,说明如何开启以及需要的权限。', '在会话里检查'));
  const { d, w, p, findings: fs } = wm, ins = insight.wdr(wm), dbt = W.kpiVal(W.KPIS[0], d), cpu = W.cpuShare(d), waits = W.dimOf(d, 'waits');
  const wItems = waits ? (waits.rows || []).slice(0, 6).map((r) => [String(r[0]), +((num(r[r.length - 1]) || 0).toFixed(1)), '%', '']) : [];
  const ts = W.dimOf(d, 'topsql'), tsh = (ts && ts.headers) || [], TF = tsh.map((x, i) => colFmt((ts && ts.rows) || [], i));
  const tsTable = ts && (ts.rows || []).length ? `<table class="ptable"><thead><tr>${tsh.map((h, i) => `<th class="${i && i < tsh.length - 1 ? 'r' : ''}">${esc(h)}</th>`).join('')}</tr></thead><tbody>${ts.rows.slice(0, 10).map((r) => `<tr>${r.map((c, i) => (i === 0 ? `<td><b>${esc(c)}</b></td>` : i === r.length - 1 ? `<td class="q mono" title="${esc(c)}">${esc(c)}</td>` : `<td class="r num">${esc(TF[i](c))}</td>`)).join('')}</tr>`).join('')}</tbody></table>` : `<p class="t3">${esc(cn((ts && ts.note) || '未采集'))}</p>`;
  const fl = fs.map((f) => `<li data-wdim="${esc(f.dimension)}">${status(f.severity)}<b>${esc(cn(f.metric))}：${esc(f.value)}</b>　阈值 ${esc(f.threshold)}。${esc(cn(f.evidence))}</li>`).join('');
  const nat = wm.native.generated ? (wm.nativeUrl ? `<a href="${esc(wm.nativeUrl)}" target="_blank" rel="noopener">原生报告（${fmtInt(Math.round((wm.native.bytes || 0) / 1024))} KB）→</a>` : `原生报告已生成，${fmtInt(Math.round((wm.native.bytes || 0) / 1024))} KB`) : '';
  return sheet('sec-wdr', 'C1', '负载 · WDR 分析', `${esc(wm.name)} · snap ${esc(w.begin_id)}–${esc(w.end_id)}，${esc(w.duration_min)} 分钟，${esc(w.scope || '')} ${esc(w.node || '')}${p ? ` · 对比 snap ${esc(p.window.begin_id)}–${esc(p.window.end_id)}` : wm.sameSkipped ? ' · 没有可对比的窗口（之前分析的是同一个窗口）' : ' · 没有上一窗口'}`,
    `${missedBanner(wm)}<div class="sheet-b"><div class="main"><h3 class="hl3">${ins.head}</h3><div class="body cols2">${paras(ins.lines)}</div>
      <h3 class="sh3">八项负载指标前后对照　<span>墨线从上一窗口画到本窗口；比例尺从 0 开始</span></h3>${P.pairs(W.KPIS, d, p, W.kpiVal, W.showK)}</div>
      <aside class="rcol"><div class="box"><h4>DB Time</h4><div class="numb big"><span class="v">${dbt === null ? '—' : esc(W.fmtDbt(dbt))}</span><span class="l">秒，平均 ${w.duration_min && dbt !== null ? (dbt / (w.duration_min * 60)).toFixed(dbt / (w.duration_min * 60) < 1 ? 2 : 1) : '—'} 个活跃会话</span><span class="s">${cpu === null ? '' : `CPU ${cpu.toFixed(0)}%，等待 ${(100 - cpu).toFixed(0)}%`}</span></div></div>
        ${wItems.length ? `<div class="box"><h4>等待构成</h4>${P.bars(wItems, true)}</div>` : ''}${fs.length ? `<div class="box"><h4>越线发现</h4><ul class="flist">${fl}</ul></div>` : ''}${nat ? `<p class="fine">${nat}</p>` : ''}</aside></div>
      <h3 class="sh3">窗口内 Top SQL　<span>${esc(cn((ts && ts.headline) || ''))}</span></h3>${tsTable}`);
}

/* ------------------------------------------------------------ D1 知识 · 知识库 */
function secKb(km) {
  if (!km.ok) return sheet('sec-kb', 'D1', '知识 · 知识库', '', none(km.status === 404 ? '本期没有知识库自检。' : `知识库状态报告读取失败：${km.error || ''}`, km.status === 404 ? '请查看知识库的健康状态。' : '', '在会话里查一次知识库状态'));
  if (!km.attached) return sheet('sec-kb', 'D1', '知识 · 知识库', '', none(`知识库未接入：${km.st.reason || ''}`, '请查看知识库为什么没有接入。', '在会话里检查'));
  const kinds = km.types ? KTYPES.filter(([k]) => +km.types[k]).map(([k, s]) => `${s} ${fmtInt(km.types[k])}`).join(' · ') : '';
  const ins = insight.kb(km), all = kinds
    ? `<a href="/dash/kb?style=normal#items">全部知识（${esc(kinds)}）与关系图，见普通风格的知识库页签 →</a>`
    : `<a href="/dash/kb?style=normal#cases">全部 ${fmtInt(km.cases.length)} 个案例、${fmtInt(km.rules.length)} 条条款与关系图，见普通风格的知识库页签 →</a>`;
  const cases = km.catOk ? `<div class="cases">${km.cases.slice(0, 6).map((x) => `<article class="case"><h4><a href="#" data-open-case="${esc(x.id)}">${esc(x.title)}</a></h4><div class="meta">${esc(x.severity || '—')} · ${esc(x.system)} · ${esc(x.occurred_at)} · ${esc(x.conclusion)}</div>
    <p><b>主因</b>　${esc(cn(x.primary_factor))}</p>${['现场', '处置', '复发标志'].filter((k) => (x.sections || {})[k]).map((k) => `<p><b>${esc(k)}</b>　${esc(cn(x.sections[k]))}</p>`).join('')}${(x.rules || []).length ? `<div class="refs">依据 ${x.rules.map((r) => `<span>${esc(r)}</span>`).join('')}</div>` : ''}</article>`).join('')}</div><p class="fine">${all}</p>`
    : `<p class="t3">读不到知识库目录：${esc(km.catErr || '')}</p>`;
  const q = km.queries.length ? `<table class="ptable mini"><thead><tr><th>时间</th><th>检索内容</th><th class="r">案例</th><th class="r">条款</th><th>方式</th></tr></thead><tbody>${km.queries.slice(0, 10).map((x) => `<tr><td class="num t2">${esc(stamp(x.at))}</td><td class="wrap">${esc(x.q)}</td><td class="r num">${esc(x.hits_cases)}</td><td class="r num">${esc(x.hits_rules)}</td><td>${x.how === 'semantic' ? '语义' : '关键词'}</td></tr>`).join('')}</tbody></table>` : '<p class="t3">近 7 天没有检索记录。</p>';
  const rules = km.rules.length ? `<ol class="notes">${km.rules.slice(0, 8).map((r) => `<li><b>${esc(r.id)}</b>　${esc(cn(r.rule))}<span class="t3">（${r.severity === 'error' ? '必须' : r.severity === 'warn' ? '建议' : '提示'}，${esc(r.group || '')}）</span></li>`).join('')}</ol>` : '';
  return sheet('sec-kb', 'D1', '知识 · 知识库', `${km.st.version ? `版本 ${esc(km.st.version)} · ` : ''}${esc(km.st.mode || '')} · 全体共享，本环境${km.d.readonly === false ? '可写' : '只读'}`,
    `<div class="sheet-b"><div class="main"><h3 class="hl3">${ins.head}</h3><div class="body cols2">${paras(ins.lines)}</div><h3 class="sh3">案例摘编　<span>最近 ${Math.min(6, km.cases.length)} 个，按发生时间；点标题看全文</span></h3>${cases}</div>
      <aside class="rcol">${km.misses.length ? `<div class="box"><h4>缺口</h4>${P.bars(km.misses.slice(0, 6).map((x) => [missLabel(x.code), x.n, '次', '查不到知识']), true)}</div>` : ''}<div class="box"><h4>最近检索</h4>${q}</div>${rules ? `<div class="box"><h4>条款摘录</h4>${rules}</div>` : ''}</aside></div>`);
}

export const sections = (hm, tm, wm, km) => secHealth(hm) + secTop(tm) + secWdr(wm) + secKb(km);
