// 文艺风格:四个大盘排成一份「GaussDB智能体日报」。头版 = 头条(健康检查)、本期数字、八项检查一览,折线以下三个小栏目;
// 内页 A2–D1 见 sections.js。路由只决定翻到哪一版(/dash/topsql → B1),所以这里一次并行取四个技能的报告。

import { esc, stamp, cn, pangu, fmtInt, fullDate } from '../../lib/fmt.js';
import { lname, status, primary, instName } from '../../lib/ui.js';
import { setModel } from '../../lib/store.js';
import * as insight from '../../lib/insight.js';
import * as P from '../../lib/press-charts.js';
import * as H from '../../pages/health.js';
import * as T from '../../pages/topsql.js';
import * as W from '../../pages/wdr.js';
import * as K from '../../pages/kb.js';
import { sections } from './sections.js';
import { jump, kicker, paras } from './bits.js';
import { missedBanner, goneText } from '../../lib/missed.js';

/* ------------------------------------------------------------ 头条 */
function lead(hm) {
  if (!hm.ok && hm.reason === 'range') return `<article class="lead">${kicker('头条 · 健康检查')}<h2 class="hl">所选时间段内没有巡检报告</h2>
    <p class="deck">时间段只筛选已经跑过的报告，不会替你去取数。可以在报头的「时间段」里改选，或者看全部报告。</p><button class="btn" data-range-clear>看全部报告</button></article>`;
  if (!hm.ok) return `<article class="lead">${kicker('头条 · 健康检查')}<h2 class="hl">${hm.reason === 'error' ? '巡检报告读取失败' : hm.reason === 'gone' ? '巡检报告的文件读取不到' : '本期没有巡检报告'}</h2>
    <p class="deck">${esc(hm.reason === 'error' ? hm.error || '' : hm.reason === 'gone' ? goneText(hm) : '这个实例还没有健康检查报告。在会话里做一次巡检，下一期就会刊出。')}</p>${hm.reason === 'error' ? '' : primary('请对当前库做一次健康检查。', '在会话里跑一次健康检查', '健康检查')}</article>`;
  const ins = insight.health(hm), failed = hm.subs.filter((s) => !s.ok).map((s) => s.skill.replace('gaussdb-', ''));
  const d = hm.prev ? hm.findings.length - H.countsOf(hm.prev) : null;
  const deck = hm.level === null ? `${hm.name} 本期 ${hm.dims.length} 个维度全部采集失败${failed.length ? `，子技能 ${failed.join('、')} 也未执行成功` : ''}。`
    : `${hm.name} 本期 ${hm.findings.length} 条越线发现，${d === null ? '这是第一份报告' : d > 0 ? `比上期多 ${d} 条` : d < 0 ? `比上期少 ${-d} 条` : '与上期持平'}；${failed.length ? `子技能 ${failed.join('、')} 未能执行，相关检查未纳入。` : '子技能全部执行成功。'}`;
  const when = stamp(hm.entry && hm.entry.at);
  const intro = hm.span && hm.level !== null
    ? `本期是一份时段体检：在 ${hm.name} 实例上回看 ${hm.win}，用窗口两端的累计值相减，共 ${hm.checks.length} 项，巡检脚本按阈值判定总体为「${lname(hm.level)}」。连接、锁、膨胀这类只有此刻的值，回看不了的项单独标出。以下各条由智能体依据本次报告整理，数字均取自报告原文。`
    : hm.level === null
    ? `本期例行巡检于 ${when} 在 ${hm.name} 实例上进行，计划检查 ${hm.dims.length} 个维度，一个都没有采到，因此无法判级。以下各条由智能体依据本次报告整理。`
    : `本期例行巡检于 ${when} 在 ${hm.name} 实例上进行，共检查 ${hm.checks.length} 项，巡检脚本按阈值判定总体为「${lname(hm.level)}」。以下各条由智能体依据本次报告整理，数字均取自报告原文。`;
  // 走势至少要两份报告;这次什么都没采到时「0 条」没有意义,不画
  const showTrend = hm.idx.length >= 2 && hm.level !== null;
  return `<article class="lead">${kicker('头条 · 健康检查')}<h2 class="hl">${ins.head}</h2><p class="deck">${esc(pangu(deck))}</p>
    <div class="byline">智能体 撰　·　${esc(stamp(hm.entry && hm.entry.at))}　·　级别由巡检脚本按阈值判定，模型不改</div>${missedBanner(hm)}
    ${showTrend ? `<figure class="fig">${P.trend('health', hm.idx, hm.entry && hm.entry.file, '条')}<figcaption>图　近 ${Math.min(30, hm.idx.length)} 次${hm.span ? '时段体检' : '巡检'}的越线发现条数。点一次巡检可翻到那一期。</figcaption></figure>` : ''}
    <div class="body cols2"><p class="dropcap">${esc(pangu(intro))}</p>${paras(ins.lines)}<p class="more">${jump('sec-health', '全部发现与各维度见 A2 版')}</p></div></article>`;
}

/* ------------------------------------------------------------ 右栏:本期数字、各项检查一览 */
function numbers(hm, tm, wm, km) {
  const items = [];
  if (hm.ok) {
    if (hm.level === null) items.push([`${hm.dims.length}/${hm.dims.length}`, '个维度采集失败', '一个维度都没有采到']);
    else {
      const d = hm.prev ? hm.findings.length - H.countsOf(hm.prev) : null;
      items.push([String(hm.findings.length), '条越线发现', d === null ? '第一份报告' : d ? `比上期${d > 0 ? '多' : '少'} ${Math.abs(d)} 条` : '与上期持平']);
      const best = hm.findings.map((f) => ({ f, x: insight.times(f) })).filter((o) => o.x).sort((a, b) => b.x - a.x)[0];
      if (best) items.push([`${best.x.toFixed(1)}×`, `${H.title(best.f.dimension)}超出阈值`, `${H.metricName(best.f)} ${best.f.value}，阈值 ${best.f.threshold}`]);
    }
  }
  // DB Time 变化只在真有变化时上「本期数字」(×1.5 以上或降到三分之二以下);零点几秒的空闲窗口互比没有意义
  if (wm.ok && wm.p) { const a = W.kpiVal(W.KPIS[0], wm.d), b = W.kpiVal(W.KPIS[0], wm.p); if (a !== null && b && (a / b >= 1.5 || a / b <= 0.67)) items.push([`${(a / b).toFixed(1)}×`, 'DB Time 对上一窗口', `${W.fmtDbt(b)} → ${W.fmtDbt(a)} 秒`]); }
  if (tm.ok && !tm.missing && tm.rows.length) { const top = [...tm.rows].sort((a, b) => b.total_sec - a.total_sec)[0];
    items.push([`${tm.share(top).toFixed(1)}%`, '耗时第一的语句占上榜', `${top.sql_id}${T.isSkipped(tm.tune[top.sql_id]) ? '，系统 SQL' : ''}`]); }
  if (km.ok && km.cov !== null) items.push([`${km.cov}%`, '知识库向量覆盖', `${km.misses.length} 类诊断发现查不到知识`]);
  if (!items.length) return '';
  return `<div class="box"><h4>本期数字</h4>${items.slice(0, 5).map(([v, l, s]) => `<div class="numb"><span class="v">${esc(v)}</span><span class="l">${esc(l)}</span><span class="s">${esc(pangu(cn(s)))}</span></div>`).join('')}</div>`;
}
function dimsBox(hm) {
  if (!hm.ok || !hm.dims.length) return '';
  return `<div class="box"><h4>${hm.dims.length} 项检查一览</h4><table class="ptable mini"><tbody>${hm.dims.map((d) => { const k = H.keyFig(d);
    return `<tr data-dim="${d.i}"><td>${esc(H.title(d.dimension))}</td><td class="r num">${k ? `${esc(k.v)}<small>${esc(String(k.u).split(' ')[0])}</small>` : '—'}</td><td class="r">${status(d.level)}</td></tr>`; }).join('')}</tbody></table>
    <p class="fine">点一行看明细。读数为各维度的关键数，单位见明细。</p></div>`;
}

/* ------------------------------------------------------------ 折线以下:三个小栏目 */
const story = (kick, head, body, more) => `<article class="story">${kicker(kick)}<h3>${head}</h3>${body}${more ? `<p class="more">${more}</p>` : ''}</article>`;
function storyWdr(wm) {
  if (!wm.ok || wm.w.wdr_enabled === false) return story('负载 · WDR 分析', wm.ok ? '这个库没有开启 WDR' : '本期无窗口分析', `<p>${wm.ok ? '需要把 enable_wdr_snapshot 设为 on 并有快照权限。' : '这个实例还没有 WDR 报告。在会话里对一个快照窗口做分析，下一期就会刊出。'}</p>`);
  const ins = insight.wdr(wm);
  return story('负载 · WDR 分析', ins.head, P.pairs([0, 5, 4].map((i) => W.KPIS[i]), wm.d, wm.p, W.kpiVal, W.showK, true) + paras(ins.lines.slice(0, 1)), jump('sec-wdr', '八项指标对照见 C1 版'));
}
function storyTop(tm) {
  if (!tm.ok || tm.missing || !tm.rows.length) return story('慢语句 · Top SQL', '本期无 Top SQL 榜单', '<p>这个实例还没有 Top SQL 报告。在会话里取一次，下一期就会刊出。</p>');
  const ins = insight.topsql(tm), b = insight.bizTop(tm);
  return story('慢语句 · Top SQL', ins.head, P.stack(tm.rows, tm.tune, T.isSkipped, b && b.sql_id) + paras(ins.lines.slice(1, 2)), jump('sec-topsql', '完整榜单与调优札记见 B1 版'));
}
function storyKb(km) {
  if (!km.ok || !km.attached) return story('知识 · 知识库', km.ok ? '知识库未接入' : '本期无知识库自检', `<p>${esc(km.ok ? km.st.reason || '' : '在会话里查一次知识库状态，下一期就会刊出。')}</p>`);
  const ins = insight.kb(km);
  return story('知识 · 知识库', ins.head, (km.misses.length ? P.bars(km.misses.slice(0, 4).map((x) => [K.missLabel(x.code), x.n, '次', '查不到知识']), true) : '') + paras(ins.lines.slice(1, 2)), jump('sec-kb', '案例摘编与检索记录见 D1 版'));
}

/** 报头里的日期与期数:取健康检查当前那一份(没有就用 targets 里的最近时间)。 */
function masthead(hm, ctx) {
  const at = hm.ok && hm.entry ? hm.entry.at : (ctx.target && ctx.target.last_at) || '';
  const d = document.getElementById('issue-date'), n = document.getElementById('issue-no');
  if (d) d.textContent = at ? fullDate(at) : '';
  if (n) n.textContent = hm.ok ? `第 ${hm.pos + 1} 期` : '创刊前';
}

export async function render(root, ctx) {
  const [h0, t0, w0, km] = await Promise.all([H.load(ctx.key, ctx.run.health, ctx.range), T.load(ctx.key, T.readBy(), ctx.range, ctx.run.topsql),
    W.load(ctx.key, ctx.run.wdr, ctx.range), K.load()]);
  const hm = h0.ok ? { ...h0, name: instName(ctx, h0.d.conn) } : h0;
  const tm = t0.ok && !t0.missing ? { ...t0, name: instName(ctx, t0.rep.conn) } : t0;
  const wm = w0.ok ? { ...w0, name: instName(ctx, w0.d.conn) } : w0;
  if (hm.ok) setModel('health', hm);
  if (tm.ok && !tm.missing) setModel('topsql', tm);
  if (wm.ok) setModel('wdr', wm);
  setModel('kb', km);
  root.innerHTML = `<section class="front"><div class="front-main">${lead(hm)}</div><aside class="front-side">${numbers(hm, tm, wm, km)}${dimsBox(hm)}</aside></section>
    <div class="fold"><span>折线以下</span></div>
    <section class="below">${storyWdr(wm)}${storyTop(tm)}${storyKb(km)}</section>${sections(hm, tm, wm, km)}
    <footer class="colophon"><b>${esc((ctx && ctx.product) || 'GaussDB 智能体')}日报</b>　由 ${esc((ctx && ctx.product) || 'GaussDB 智能体')}依据巡检、WDR、Top SQL 与知识库报告自动编排。级别由巡检脚本按阈值判定，模型不改；文中数字均取自报告。</footer>`;
  masthead(hm, ctx);
}
