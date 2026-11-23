// 普通风格 · Top SQL:主视觉 = 耗时树图 + 智能体解读 + 四个统计;其下 = 榜单(七个排序页签;报告里有自身耗时时
// 加「自身耗时」「等待占比」两列)、调用×耗时散点、调优进度。
// 口径:累计统计视图,不是时间窗口;一份报告一个排序维度,没取过的维度一键去会话里取。
// 直达链接打开的时段榜 / 增量榜(m.span):数是窗口内增量,页头写评估时段与口径,不画页签(页签是累计榜)。

import { esc, stamp, cn, fmtInt, fmtSec, fmtMs, split } from '../../lib/fmt.js';
import { I, glyph, tipAttr, dig, cu, primary } from '../../lib/ui.js';
import { rise } from '../../lib/motion.js';
import { setModel } from '../../lib/store.js';
import { treemap, scatter } from '../../lib/viz.js';
import * as insight from '../../lib/insight.js';
import { BY, byLabel, tunePrompt, tDigPrompt, tRedoPrompt, tAbout, isSkipped, READS_NOTE, SELF_KEYS, SELF_NOTE, WAIT_DOMINANT,
  selfSec, waitRatio, waitHeavy, hasSelf } from '../../pages/topsql.js';
import { pageHead, tile, insightCard, heroStats, emptyHero, errorHero, rangeHero } from './common.js';
import { missedBanner, goneText, reportLabel } from '../../lib/missed.js';

// 自身耗时与等待占比(口径见 pages/topsql.js selfSec):报告里有才画这两列;没有这两列的老报告不画,不写 0
const WAIT_TIP = `<b>等待占比</b><div class="d">= 1 − 自身耗时 / 总耗时。${SELF_NOTE}；等待里有等锁、等轻量锁、排队等 CPU，分不出是哪一种</div>`;
const HEAVY_TIP = '<b>这条主要在等</b><div class="d">耗时大多不是 CPU / 数据 IO：它是被挡住的，不是自己慢。看锁阻塞（谁挡住了它），调 SQL 本身帮不上忙</div>';
const selfCell = (r, by) => { const s = selfSec(r); return s === null ? '—' : by === 'self_avg' ? (+r.calls > 0 ? fmtMs((s * 1000) / r.calls) : '—') : fmtSec(s); };
function waitCell(r) {
  const w = waitRatio(r);
  if (w === null) return '<span class="t3">—</span>';
  const p = Math.round(w * 100);
  return w >= WAIT_DOMINANT ? `<span class="tag" data-lv="2"${tipAttr(HEAVY_TIP)}>${glyph(2)}${p}%</span>` : `${p}%`;
}

/** 七个排序页签都能点:取过的切过去(data-by),没取过的写「未取」,点了也切过去并给「去会话里取」。 */
const byTabs = (m) => (m.span ? `<button class="btn" data-by="${esc(m.by)}">看累计榜</button>` : `<div class="seg">${BY.map(([b, l]) => `<button data-by="${b}" class="${b === m.by ? 'on' : ''}"${m.per[b] ? '' : tipAttr(`还没按「${esc(l)}」取过<div class="d">点一下看怎么取</div>`)}>${esc(l)}${m.per[b] ? '' : '<small class="t3"> 未取</small>'}</button>`).join('')}</div>`);

export function render(root, m, ctx) {
  const target = m.name || (ctx.target && ctx.target.label) || '';
  const head = (act, desc) => pageHead('Top SQL', '', act, desc);
  if (!m.ok && m.reason === 'gone') {
    // 这一页签的报告文件一份都取不到(lib/data.js fetchFirst):页签照常能切;有时段 / 增量榜就指一条路,不自动顶替
    const label = byLabel(m.by);
    const alt = m.alt ? `<button class="btn" data-pick="topsql|${esc(m.alt.file)}">${I.cal}看最近一份时段榜（${esc(reportLabel(m.alt))}）</button>` : '';
    root.innerHTML = head('', [['实例', esc(target)]])
      + `<div class="bento">${tile({ title: 'SQL 榜单', tools: byTabs(m), body: `<div class="empty-state" style="padding:40px">${glyph(2)}<h4>「${esc(label)}」的报告文件读取不到</h4><p>${esc(goneText(m))}</p>${primary(`请按${label}取一次 Top SQL。`, `在会话里按${label}取一次`, 'Top SQL')}${alt}</div>` })}</div>`;
    return;
  }
  if (!m.ok) {
    root.innerHTML = head('', []) + (m.reason === 'error' ? errorHero(m.error) : m.reason === 'range' ? rangeHero(ctx.range) : emptyHero(ctx.key ? '这个实例还没有 Top SQL 报告' : '还没有 Top SQL 报告', '在会话里取一次，结果会自动出现在这里。', '请列出当前库的 Top SQL(按总耗时)。', '在会话里取一次 Top SQL'));
    return;
  }
  if (m.missing) {
    const label = byLabel(m.by);
    root.innerHTML = head('', [['实例', esc(target)], ['已取过', esc(m.avail.map(([, l]) => l).join('、'))]])
      + `<div class="bento">${tile({ title: 'SQL 榜单', tools: byTabs(m), body: `<div class="empty-state" style="padding:40px">${glyph(null)}<h4>「${esc(label)}」这个维度还没取过</h4><p>大盘只展示在会话里真正取过的维度。</p>${primary(`请按${label}取一次 Top SQL。`, `在会话里按${label}取一次`, 'Top SQL')}</div>` })}</div>`;
    return;
  }
  setModel('topsql', m);
  const rows = m.rows, label = byLabel(m.by), field = (BY.find(([b]) => b === m.by) || [])[2], withSelf = hasSelf(rows);
  const sumC = rows.reduce((a, r) => a + (+r.calls || 0), 0), top = [...rows].sort((a, b) => b.total_sec - a.total_sec)[0], top1 = top ? m.share(top) : 0;
  const [tv, tu] = split(fmtSec(m.sum)), [cv, cu2] = split(fmtInt(sumC));
  const stats = [{ l: '上榜语句', v: cu(String(rows.length)), u: '条', s: `按${esc(label)}取前 ${esc(m.rep.limit)} 条` },
    { l: '上榜总调用', v: cu(cv), u: `${cu2 ? cu2 + ' ' : ''}次`, s: m.span ? '窗口内' : '自统计视图重置以来' },
    { l: '上榜总耗时', v: cu(tv), u: tu, s: m.sum >= 60 ? `合计 ${fmtInt(Math.round(m.sum))} 秒` : '上榜语句合计' },
    { l: '耗时第一占上榜', v: cu(top1.toFixed(1)), u: '%', s: `${esc(top ? top.sql_id : '')}${top && isSkipped(m.tune[top.sql_id]) ? '，系统 SQL' : ''}` }];
  const tm = `<div class="glass tmbox rise"${rise()}><div class="title">${I.grid}上榜耗时构成<span class="t3">面积 = 总耗时占上榜合计 · 斜纹为系统 SQL · 点块看详情</span></div>${treemap(rows, m.tune, isSkipped)}</div>`;
  const th = (name, f) => `<th class="r">${name}${f === field ? I.sortd : ''}</th>`;
  const tagOf = (r) => { const x = m.tune[r.sql_id]; return isSkipped(x) ? '<span class="tag gray">系统 SQL</span>' : x ? '<span class="tag blue">已调优</span>' : '<span class="t3">未调优</span>'; };
  const table = `<div class="scroll"><table class="tbl"><thead><tr><th>排名</th><th>sql_id</th><th>语句</th>${th('调用次数', 'calls')}${th('总耗时', 'total_sec')}${th('平均耗时', 'avg_ms')}${th('返回行', 'rows')}${withSelf ? `${th(m.by === 'self_avg' ? '平均自身耗时' : '自身耗时', 'self_sec')}<th class="r"${tipAttr(WAIT_TIP)}>等待占比</th>` : ''}<th>占上榜耗时</th><th>调优</th><th class="r">操作</th></tr></thead><tbody>${rows.map((r, i) => { const p = m.share(r).toFixed(1);
    return `<tr><td class="t3">${i + 1}</td><td class="id"><a href="#" data-sql="${esc(r.sql_id)}">${esc(r.sql_id)}</a>${r.is_new ? ' <span class="tag gray"' + tipAttr('窗口起点时还没有这条语句的统计，整条计入') + '>新</span>' : ''}</td><td class="q" title="${esc(r.query)}">${esc(r.query)}</td><td class="r">${fmtInt(r.calls)}</td><td class="r">${fmtSec(r.total_sec)}</td><td class="r">${fmtMs(r.avg_ms)}</td><td class="r">${fmtInt(r.rows)}</td>${withSelf ? `<td class="r">${selfCell(r, m.by)}</td><td class="r">${waitCell(r)}</td>` : ''}
      <td><div class="share"><div class="b"><i class="grow" style="width:${p}%"></i></div><b>${p}%</b></div></td><td>${tagOf(r)}</td><td class="ops"><a href="#" data-sql="${esc(r.sql_id)}">详情</a>${isSkipped(m.tune[r.sql_id]) ? '' : `<a href="#"${dig(tunePrompt(r, m), 'SQL 调优')}>调优</a>`}</td></tr>`; }).join('')}</tbody></table></div>`;
  const progress = `<div class="list">${rows.slice(0, 12).map((r) => { const x = m.tune[r.sql_id], f = x && x.evidence && (x.evidence.findings || [])[0];
    return `<div class="it">${isSkipped(x) ? glyph(null) : x ? glyph(0) : '<span class="glyph pending"></span>'}<div class="m"><b>${esc(r.sql_id)}</b><small>${isSkipped(x) ? `系统 SQL，引用 ${esc((x.system_objects || []).join('、'))}，按策略不调优` : f ? `${esc(cn(f.detail || ''))}${f.advice ? `；建议${esc(cn(f.advice))}` : ''}` : x ? '已调优' : '还没有调优过'}</small></div>${isSkipped(x) ? `<a href="#" class="r" data-sql="${esc(r.sql_id)}">查看</a>` : `<a href="#" class="r"${dig(tunePrompt(r, m), 'SQL 调优')}>${x ? '继续调优' : '调优'}</a>`}</div>`; }).join('')}</div>`;
  const nTuned = rows.filter((r) => m.tune[r.sql_id] && !isSkipped(m.tune[r.sql_id])).length, nNone = rows.filter((r) => !m.tune[r.sql_id]).length;
  const desc = m.span
    ? [['实例', esc(target)], ['评估时段', esc(m.win || '—')], ['口径', m.span === 'span' ? '窗口两端相减的增量，不是累计值' : '两次采样相减的增量，不是累计值'], ['存档时间', esc(stamp(m.entry.at))]]
    : [['实例', esc(target)], ['取数时间', esc(stamp(m.entry.at))], ['口径', '累计统计视图，不是时间窗口；含系统与监控 SQL（调优按策略跳过这类）']];
  // 实际评估的是哪一段、为什么(技能写的 span_note):用户问的时间段与实际窗口可能不一样,页面上也要看得到
  const note = m.span && m.rep.span_note ? tile({ title: '评估窗口', desc: '你要看的时间段与实际评估的窗口', body: `<p>${esc(cn(m.rep.span_note))}</p>` }) : '';
  root.innerHTML = pageHead('Top SQL', m.span ? '<span class="tag blue">时段榜</span>' : '', `<button class="btn"${dig(tRedoPrompt(m), 'Top SQL')}>${I.redo}重新取数</button>` + primary(tDigPrompt(m), '在会话里深挖', 'Top SQL'), desc)
    + missedBanner(m) + `<section class="hero"><div class="hero-grid tm">${tm}${insightCard({ ...insight.topsql(m), ctx: tAbout(m) })}</div>${heroStats(stats)}</section>`
    + `<div class="bento">${note}${tile({ title: 'SQL 榜单', desc: `按${esc(label)}排序，点 sql_id 查看语句与调优结论${m.by === 'reads' ? `；${esc(READS_NOTE)}` : ''}${SELF_KEYS.includes(m.by) ? `；${esc(SELF_NOTE)}` : ''}${rows.some(waitHeavy) ? `；等待占比 ${WAIT_DOMINANT * 100}% 以上的已标出：主要在等（锁等），看锁阻塞` : ''}`, body: table, tools: byTabs(m), flush: true })}
      ${tile({ span: 'b7', title: '调用次数 × 平均耗时', desc: '找又多又慢的语句', body: scatter(rows, m.tune, isSkipped, 700) })}
      ${tile({ span: 'b5', title: '调优进度', desc: `${nTuned} 条已调优，${nNone} 条未调优`, body: progress })}</div>`;
}
