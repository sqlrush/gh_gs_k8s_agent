// 「智能体解读」:每个大盘一段结论 + 要点 + 建议追问。文字由报告**确定性生成**,数字只取报告,级别只用脚本判定的;
// 不调模型、不编数。要点里的小签(.ev)带 data-dim / data-xdim / data-sql / data-wdim,点开看明细。

import { esc, stamp, cn, pangu, joinCn, fmtInt, fmtMs } from './fmt.js';
import { I, glyph, lname } from './ui.js';
import * as H from '../pages/health.js';
import * as T from '../pages/topsql.js';
import * as W from '../pages/wdr.js';
import { missLabel } from '../pages/kb.js';

const sevWord = (s) => (s >= 3 ? '达到严重级别' : s === 2 ? '越过告警阈值' : '需要关注');
const sqlChip = (id) => `<span class="ev" data-sql="${esc(id)}">${I.code}${esc(id)}</span>`;
const parseCmp = (t) => { const m = /^\s*(>=|>|<=|<)\s*([\d,.]+)\s*(%|ms|s|MB|GB|MiB|GiB)?\s*$/.exec(String(t || '')); return m ? { op: m[1], v: +m[2].replace(/,/g, '') } : null; };
const parseVal = (t) => { const m = /^\s*([\d,.]+)\s*(%|ms|s|MB|GB|MiB|GiB)?\s*$/.exec(String(t || '')); return m ? +m[1].replace(/,/g, '') : null; };
/** 发现值是阈值的几倍;组合条件或非「大于」型阈值返回 null。 */
export function times(f) { const thr = parseCmp(f.threshold), v = parseVal(f.value); return thr && v !== null && thr.v && thr.op[0] === '>' ? v / thr.v : null; }
export const thresholdOf = (f) => { const thr = parseCmp(f.threshold), v = parseVal(f.value); return thr && v !== null ? { thr: thr.v, v } : null; };

/* ------------------------------------------------------------ 健康检查 */
function hChip(f, m) {
  const d = m.dims.find((x) => x.dimension === f.dimension);
  return `<span class="ev"${d ? ` data-dim="${d.i}"` : ` data-xdim="${esc(f.dimension)}"`}>${glyph(f.severity)}${esc(H.title(f.dimension))}</span>`;
}
function hLine(f, m) {
  const [name, unit] = H.METRIC[f.metric] || [f.metric, ''], x = times(f);
  const ev = !f.sql_id && String(f.evidence || '').length <= 32 ? `（${esc(cn(f.evidence))}）` : '';
  return `${hChip(f, m)} ${esc(cn(name))} <b>${esc(f.value)}${esc(unit)}</b>，阈值 ${esc(f.threshold)}${x && x >= 1.05 ? `，是阈值的 <b>${x.toFixed(1)} 倍</b>` : ''}${ev}${f.sql_id ? `，涉及 ${sqlChip(f.sql_id)}` : ''}。`;
}
export function healthSince(m) {
  const noun = m.span ? '时段体检' : '巡检';
  if (!m.prev) return m.span ? '这是该实例第一份时段体检报告。' : '这是该实例第一份健康检查报告。';
  const pc = m.prev.counts || {}, cc = (m.entry && m.entry.counts) || {};
  const bits = [[3, '严重'], [2, '告警'], [1, '关注']].map(([s, n]) => { const d = (+cc[s] || 0) - (+pc[s] || 0); return d > 0 ? `多 <b>${d}</b> 条${n}` : d < 0 ? `少 <b>${-d}</b> 条${n}` : ''; }).filter(Boolean);
  return `与上次${noun}（${esc(H.entryLabel(m.prev))}，${esc(lname(H.entryLevel(m.prev)))}）相比，${bits.length ? bits.join('，') : '发现没有变化'}。`;
}
export function healthHead(m) {
  const fs = m.findings;
  if (m.level === null) return '一个维度都没采到——没有发现，不等于没有问题';
  if (!fs.length) return `${m.dims.length} 个维度都在阈值以内`;
  const top = fs[0].severity | 0, names = [...new Set(fs.filter((f) => (f.severity | 0) === top).map((f) => H.title(f.dimension)))];
  const rest = fs.filter((f) => (f.severity | 0) < top), restLv = [...new Set(rest.map((f) => f.severity | 0))];
  return pangu(`${esc(joinCn(names))}${sevWord(top)}`) + (rest.length ? `，另有 ${rest.length} 项${restLv.length === 1 ? sevWord(restLv[0]) : '级别较低的发现'}` : '');
}
export function health(m) {
  const fs = m.findings, failed = m.subs.filter((s) => !s.ok), fname = (s) => s.skill.replace('gaussdb-', '');
  const src = m.span ? `依据 ${H.entryLabel(m.entry)} 时段体检 · 级别由脚本判定` : `依据 ${stamp(m.entry && m.entry.at)} 巡检报告 · 级别由脚本判定`,
    ask = '问问这次巡检，比如：先处理哪一条？';
  if (m.level === null) {
    const why = [...new Set(m.dims.map((d) => cn(d.note || d.headline)))].join('；');
    return { src, ask, head: healthHead(m), lines: [`${m.dims.length} 个维度全部采集失败，原因：<b>${esc(why)}</b>。`, '常见是中间件不通、执行账号没有权限，或实例地址填错。',
      failed.length ? `子技能 ${esc(failed.map(fname).join('、'))} 也都没有执行成功。` : ''],
      chips: [['排查采集为什么失败', `请排查${H.hSrc(m)}为什么一个维度都没有采到:${why}`], ['检查中间件连通性', `请检查当前实例到中间件的连通性,并说明怎么修复(${H.hSrc(m)}一个维度都没有采到)。`], ['重新巡检', H.hRedoPrompt(m)]] };
  }
  // 追问的首句都带上这份报告的时间(口径见 lib/prompts.js)
  const chips = [];
  if (fs[0] && fs[0].sql_id) chips.push([`解释 ${fs[0].sql_id} 为什么慢`, H.fPrompt(fs[0], m)]);
  chips.push(['给出处置顺序', H.hDigPrompt(m)]);
  if (m.prev) chips.push(['和上次比变了什么', `请对比本次(${H.hWhen(m)})与上次(${H.entryWhen(m.prev)})的${m.span ? '时段体检' : '健康检查'},说明新增和消失的发现。`]);
  if (failed[0]) chips.push([`${fname(failed[0])} 为什么失败`, `请分析${H.hSrc(m)}里子技能 ${failed[0].skill} 为什么执行失败(${failed[0].error})。`]);
  return { src, ask, head: healthHead(m), chips, lines: [fs[0] && hLine(fs[0], m), fs[1] && hLine(fs[1], m),
    healthSince(m) + (fs.length > 2 ? ` 其余 ${fs.length - 2} 条见下方发现表。` : ''),
    failed.length ? `子技能 <b>${esc(failed.map(fname).join('、'))}</b> 执行失败（${esc(cn(failed[0].error))}），它的检查项这次没有纳入汇总。` : ''] };
}

/* ------------------------------------------------------------ Top SQL */
export const bizTop = (m) => [...m.rows].filter((r) => !T.isSkipped(m.tune[r.sql_id])).sort((a, b) => b.total_sec - a.total_sec)[0];
export function topsql(m) {
  const pct = (r) => m.share(r).toFixed(1), byT = [...m.rows].sort((a, b) => b.total_sec - a.total_sec), top = byT[0], b0 = bizTop(m);
  const sys = (r) => T.isSkipped(m.tune[r.sql_id]), tuned = (r) => m.tune[r.sql_id] && m.tune[r.sql_id].evidence;
  const slow = [...m.rows].sort((a, b) => b.avg_ms - a.avg_ms)[0], lines = [];
  if (!top) return { src: '', ask: '', head: '榜单是空的', lines: [], chips: [] };
  lines.push(sys(top) ? `${sqlChip(top.sql_id)} 占上榜耗时 <b>${pct(top)}%</b>，只引用系统对象 ${esc((m.tune[top.sql_id].system_objects || []).join('、'))}，按策略不调优。`
    : `${sqlChip(top.sql_id)} 占上榜耗时 <b>${pct(top)}%</b>，调用 ${esc(fmtInt(top.calls))} 次，平均 ${esc(fmtMs(top.avg_ms))}。`);
  if (b0 && b0 !== top) lines.push(`业务语句里总耗时最高的是 ${sqlChip(b0.sql_id)}：占 <b>${pct(b0)}%</b>，平均 ${esc(fmtMs(b0.avg_ms))}，调用 ${esc(fmtInt(b0.calls))} 次，${tuned(b0) ? '已有调优结论' : '还没有调优过'}。`);
  if (slow && slow !== b0 && slow !== top) {
    const f = tuned(slow) && (tuned(slow).findings || [])[0];
    lines.push(`单次最慢的是 ${sqlChip(slow.sql_id)}：平均 <b>${esc(fmtMs(slow.avg_ms))}</b>${f ? `，已调优：${esc(cn(f.detail))}` : `，调用 ${esc(fmtInt(slow.calls))} 次`}。`);
  }
  // 等待占比高的(口径见 pages/topsql.js selfSec):是被挡住的,不是自己慢 —— 指去看锁阻塞,调它帮不上忙
  const heavy = m.rows.filter(T.waitHeavy), waitPct = (r) => Math.round(T.waitRatio(r) * 100);
  if (heavy.length) {
    lines.push(`${heavy.slice(0, 3).map((r) => `${sqlChip(r.sql_id)}（等待 ${waitPct(r)}%）`).join('、')}${heavy.length > 3 ? ` 等 ${heavy.length} 条` : ''}`
      + `的耗时 ${T.WAIT_DOMINANT * 100}% 以上不是 CPU / 数据 IO，<b>主要在等</b>（等锁、等轻量锁、排队等 CPU 都算在内）：`
      + `是被挡住的，不是自己慢，先看锁阻塞${T.SELF_KEYS.includes(m.by) ? '' : '；不含等待的排名看「自身耗时」页签'}。`);
  }
  lines.push(m.span ? `口径是 <b>${esc(m.win || '这段时间')}</b> 窗口内的增量，不是累计值；新出现的语句整条计入。`
    : '口径是累计统计视图，不是时间窗口；看某段时间的负载用 <a href="/dash/wdr">WDR 分析</a>。');
  // 主要在等的语句不给「调优」,给「被谁挡住」
  const lock = (r) => [`${r.sql_id} 被谁挡住`, T.lockPrompt(r, m)];
  const chips = [];
  if (b0) chips.push(T.waitHeavy(b0) ? lock(b0) : [`调优 ${b0.sql_id}`, T.tunePrompt(b0, m)]);
  if (heavy[0] && heavy[0] !== b0) chips.push(lock(heavy[0]));
  if (sys(top)) chips.push(['系统 SQL 为什么占这么多', `请分析${T.tSrc(m)}里系统语句 ${top.sql_id} 为什么占上榜耗时 ${pct(top)}%,是否需要调整监控采集频率。`]);
  if (slow && slow !== b0 && slow !== heavy[0]) chips.push(T.waitHeavy(slow) ? lock(slow) : [`${slow.sql_id} 怎么优化`, T.tunePrompt(slow, m)]);
  chips.push(['先调哪一条', T.tDigPrompt(m)]);
  return { src: m.span ? `依据 ${m.win || stamp(m.entry.at)} 时段增量 · 按${T.byLabel(m.by)}排序` : `依据 ${stamp(m.entry.at)} 取数 · 按${T.byLabel(m.by)}排序`, ask: '问问这份榜单，比如：先调哪一条？', chips: chips.slice(0, 4), lines,
    head: sys(top) && b0 ? `耗时第一的是系统 SQL，业务语句先看 ${esc(b0.sql_id)}`
      : T.waitHeavy(top) ? `${esc(top.sql_id)} 占上榜耗时 ${pct(top)}%，但它主要在等，先看锁阻塞` : `${esc(top.sql_id)} 占上榜耗时 ${pct(top)}%，最值得先看` };
}

/* ------------------------------------------------------------ WDR */
export function wdr(m) {
  const { d, p, findings: fs } = m, K0 = W.KPIS[0];
  const dbt = W.kpiVal(K0, d), pdb = W.kpiVal(K0, p), cpu = W.cpuShare(d), pcpu = p ? W.cpuShare(p) : null;
  const fl = (f) => `<span class="ev" data-wdim="${esc(f.dimension)}">${glyph(f.severity)}${esc(W.WT[f.dimension] || f.dimension)}</span> ${esc(cn(f.metric))} <b>${esc(f.value)}</b>，阈值 ${esc(f.threshold)}${(() => { const x = times(f); return x && x >= 1.05 ? `，是阈值的 <b>${x.toFixed(1)} 倍</b>` : ''; })()}。`;
  // 平均活跃会话由 DB Time 换算,标题已说过,从第三项起;明显变坏的排前面
  const moved = p ? W.KPIS.slice(2).map((k) => ({ k, c: W.kpiVal(k, d), p: W.kpiVal(k, p) })).filter((x) => x.c !== null && x.p !== null && x.k.worse)
    .map((x) => ({ ...x, ch: W.change(x.k, x.c, x.p) })).filter((x) => x.ch.cls === 'bad' || x.ch.cls === 'worse').sort((a, b) => (b.ch.cls === 'bad') - (a.ch.cls === 'bad')) : [];
  const mv = moved.map((x) => { const [a, au] = W.showK(x.p, x.k.u), [b, bu] = W.showK(x.c, x.k.u);
    return x.k.u === '%' ? `${x.k.l}从 ${a}% 到 ${b}%（${x.ch.diff > 0 ? '+' : '−'}${x.ch.text}）` : `${x.k.l}从 ${a} ${au} 到 <b>${b} ${bu}</b>（${x.ch.text}）`; });
  const head0 = pdb && dbt ? (dbt / pdb >= 1.5 ? `DB Time 是上一窗口的 ${(dbt / pdb).toFixed(1)} 倍` : dbt / pdb <= 0.67 ? `DB Time 降到上一窗口的 ${((dbt / pdb) * 100).toFixed(0)}%` : 'DB Time 与上一窗口相当') + (fs[0] ? `，${esc(cn(fs[0].metric))} ${esc(fs[0].value)}` : '')
    : fs[0] ? `${esc(cn(fs[0].metric))} ${esc(fs[0].value)}，${lname(fs[0].severity)}` : '窗口内没有越过阈值的发现';
  // 追问的首句都带上窗口与快照号(W.wWhen);说「变了 / 上升」的带上对比的那个窗口(W.wPrev)
  const chips = [], ww = W.wWhen(m), pw = W.wPrev(m);
  const sid = fs.map((f) => (/sqlid\s+(\w+)/.exec(f.evidence || '') || [])[1]).find(Boolean);
  if (sid) chips.push([`${sid} 为什么占这么多 DB Time`, `请分析${ww}内 sql_id 为 ${sid} 的语句为什么占这么多 DB Time。`]);
  if (fs.some((f) => /IO_EVENT/.test(f.metric))) chips.push(['IO 等待从哪来', `请分析${ww}里 IO_EVENT 等待的来源:哪些对象、哪些文件、哪些语句。`]);
  if (moved.some((x) => x.k.l === '临时溢出')) chips.push(['临时溢出怎么降', `请找出${ww}里临时溢出的语句,并给出 work_mem 或改写建议:临时溢出比${pw || '上一窗口'}明显上升。`]);
  chips.push(['负载为什么变了', `请解读${ww},说明${pw ? `与${pw}相比` : ''}负载变化的主要原因。`]);
  return { src: `依据 snap ${m.w.begin_id}–${m.w.end_id} 窗口报告 · 级别由脚本判定`, ask: '问问这个窗口，比如：负载为什么变了？', head: pangu(head0), chips: chips.slice(0, 4),
    lines: [fs[0] && fl(fs[0]), fs[1] && fl(fs[1]), mv.length ? `与上一窗口相比，${mv.slice(0, 3).join('，')}。` : p ? '其余指标与上一窗口相比没有明显变坏。' : m.sameSkipped ? '之前的报告分析的都是同一个快照窗口，没有可对比的窗口；对更早的窗口做一次分析就能对比。' : '这是第一份报告，没有可对比的窗口。',
      cpu !== null && pcpu !== null && Math.abs(cpu - pcpu) >= 5 ? `CPU 占 DB Time 从 ${pcpu}% ${cpu < pcpu ? '降' : '升'}到 ${cpu}%，等待占比${cpu < pcpu ? '升' : '降'}到 <b>${(100 - cpu).toFixed(0)}%</b>。` : ''] };
}

/* ------------------------------------------------------------ 知识库 */
export function kb(m) {
  const q = m.queries, sem = q.filter((x) => x.how === 'semantic').length, none = q.filter((x) => !x.hits_cases && !x.hits_rules).length;
  // 没有状态报告(大盘拿目录里的数顶上):缺口、覆盖、坏文件都不知道 —— 不能说「诊断发现都查得到知识」
  const head = !m.ok ? '知识已导入，还没查过知识库状态' : !m.attached ? '知识库未接入' : m.misses.length ? `知识库可用，有 ${m.misses.length} 类诊断发现在库里查不到知识` : '知识库可用，诊断发现都查得到知识';
  const first = m.misses.find((x) => !String(x.code).startsWith('q:'));
  return { src: m.ok ? `依据知识库自检${m.st.version ? ` · 版本 ${m.st.version}` : ''}` : '依据知识库目录', ask: '问问知识库，比如：无主键大表怎么处理？', head,
    lines: [!m.ok ? '缺口、向量覆盖和待处理项要在会话里查一次知识库状态才知道。' : '', m.misses.length ? `${m.misses.slice(0, 3).map((x) => `<b>${esc(missLabel(x.code))}</b> ${esc(x.n)} 次`).join('、')}没有查到对应的案例或条款，可以请管理员补充。` : '',
      m.cov !== null ? `向量覆盖 <b>${m.cov}%</b>${m.cov < 100 ? '：没覆盖的条目只能靠关键词命中，管理员在导入环境重建索引即可' : ''}。` : '',
      ...m.warns.slice(0, 2).map((x) => `文件格式有问题：${esc(cn(x))}。`),
      `你近 7 天检索 ${q.length} 次${q.length ? `：语义 ${sem} 次、关键词 ${q.length - sem} 次${none ? `，其中 <b>${none}</b> 次什么都没查到` : ''}` : ''}。`],
    chips: [...(!m.ok ? [['查一次知识库状态', '请查看知识库的健康状态。']] : []), ...(first ? [[`${first.code} 怎么处理`, `知识库里查不到 ${first.code} 的案例,请根据经验说明怎么处理,并给出可以沉淀成案例的要点。`]] : []),
      ['帮我起草一个案例', '请按案例文件的格式(现场、判断、处置、复发标志)帮我起草一个案例,内容来自最近一次健康检查的发现。'],
      ['向量覆盖怎么算', '知识库的向量覆盖率是怎么计算的?没覆盖的条目会怎样?']] };
}
