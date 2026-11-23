// 普通风格 · WDR:主视觉 = DB Time 与构成 + 智能体解读 + 八个指标的对比哑铃;其下 = 窗口 Top SQL、结论、检查点、窗口历史、IO。

import { esc, cn, num, fmtInt, colFmt } from '../../lib/fmt.js';
import { I, glyph, tag, tipAttr, dig, cu, primary } from '../../lib/ui.js';
import { rise } from '../../lib/motion.js';
import { setModel } from '../../lib/store.js';
import { dumbbell, rhythm } from '../../lib/viz.js';
import * as insight from '../../lib/insight.js';
import { KPIS, kpiVal, cpuShare, showK, change, dimOf, wPrompt, wDigPrompt, wAbout, fmtDbt } from '../../pages/wdr.js';
import { pageHead, tile, insightCard, emptyHero, errorHero, runPicker, rangeHero, goneHero } from './common.js';
import { missedBanner } from '../../lib/missed.js';

/** 横向条形清单:dim 的每一行一根,valCol 是取值列。hero=true 时不写附加列(主视觉里放不下)。 */
export function hb(dim, valCol, unit, hero) {
  if (!dim || !(dim.rows || []).length) return `<p class="t3">${esc(cn((dim && dim.note) || '未采集'))}</p>`;
  const h = dim.headers || [], F = h.map((x, i) => colFmt(dim.rows, i)), mx = Math.max(1, ...dim.rows.map((r) => num(r[valCol]) || 0)), other = h.map((x, i) => i).filter((i) => i && i !== valCol);
  return `<div class="hb">${dim.rows.slice(0, 8).map((r) => `<div class="r"${tipAttr(`<b>${esc(r[0])}</b>${h.map((x, i) => (i ? `<div class="d">${esc(x)}：${esc(F[i](r[i]))}</div>` : '')).join('')}`)}><div class="l">${esc(r[0])}${other.length && !hero ? `<small>${other.map((i) => `${esc(h[i])} ${esc(F[i](r[i]))}`).join('，')}</small>` : ''}</div><div class="b"><i class="grow" style="width:${((num(r[valCol]) || 0) / mx) * 100}%"></i></div><div class="v">${esc(F[valCol](r[valCol]))}${unit ? `<small>${esc(unit)}</small>` : ''}</div></div>`).join('')}</div>`;
}

/** 窗口间 DB time 趋势。只读 index.json 里的 db_time_s —— 画趋势不该把几十份报告全下载下来。
 *
 * 为什么不用已有的「窗口历史」格子图代替:那张图画的是**发现条数**,而用户要问的是
 * 「是不是越来越卡」—— 阈值没变的话发现条数可以一直是 3,回答不了那个问题。
 * 老报告的索引里没有这个字段(是后加的),所以不足两点时整块不画,不画一条假的平线。
 */
function dbTrend(idx, curFile) {
  const pts = (idx || []).slice(-20).filter((e) => typeof e.db_time_s === 'number');
  if (pts.length < 2) return `<p class="t3">窗口数不足,或这些报告是加入该指标之前存档的。</p>`;
  const W = 520, H = 120, L = 34, T = 10, B = 20, pw = W - L - 8, ph = H - T - B;
  const mx = Math.max(1, ...pts.map((e) => e.db_time_s));
  const X = (i) => L + (pts.length === 1 ? pw / 2 : (i * pw) / (pts.length - 1));
  const Y = (v) => T + ph - (v / mx) * ph;
  const line = pts.map((e, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(e.db_time_s).toFixed(1)}`).join(' ');
  const dots = pts.map((e, i) => `<circle cx="${X(i).toFixed(1)}" cy="${Y(e.db_time_s).toFixed(1)}" r="${e.file === curFile ? 4 : 2.5}" fill="${e.file === curFile ? 'var(--acc,#c33)' : 'currentColor'}"><title>${esc(String(e.at || '').slice(0, 16))}：DB time ${fmtInt(Math.round(e.db_time_s))} 秒</title></circle>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;color:var(--t3,#888)">
    <line x1="${L}" x2="${L + pw}" y1="${Y(0)}" y2="${Y(0)}" stroke="currentColor" stroke-width="0.5" opacity="0.4"/>
    <text x="${L - 6}" y="${(Y(mx) + 4).toFixed(1)}" text-anchor="end" font-size="9" fill="currentColor">${esc(fmtInt(Math.round(mx)))}</text>
    <text x="${L - 6}" y="${(Y(0) + 4).toFixed(1)}" text-anchor="end" font-size="9" fill="currentColor">0</text>
    <path d="${line}" fill="none" stroke="currentColor" stroke-width="1.6" opacity="0.85"/>${dots}
    <text x="${L}" y="${H - 4}" font-size="9" fill="currentColor">${esc(String(pts[0].at || '').slice(5, 16))}</text>
    <text x="${L + pw}" y="${H - 4}" font-size="9" text-anchor="end" fill="currentColor">${esc(String(pts[pts.length - 1].at || '').slice(5, 16))}</text></svg>`;
}

function hero(m) {
  const { d, p, w } = m, dbt = kpiVal(KPIS[0], d), pdb = kpiVal(KPIS[0], p), cpu = cpuShare(d), waits = dimOf(d, 'waits');
  const ch = dbt !== null && pdb !== null ? change(KPIS[0], dbt, pdb) : null;
  const comp = cpu === null ? '' : `<div class="stackbar"><i style="flex:${cpu};background:var(--sb-cpu)"${tipAttr(`<b>CPU ${cpu.toFixed(1)}%</b>`)}></i><i style="flex:${100 - cpu};background:var(--sb-wait)"${tipAttr(`<b>等待 ${(100 - cpu).toFixed(1)}%</b><div class="d">锁、IO、睡眠等</div>`)}></i></div>
    <div class="legend2"><span><i class="sw" style="background:var(--sb-cpu)"></i>CPU ${cpu.toFixed(1)}%</span><span><i class="sw" style="background:var(--sb-wait)"></i>等待 ${(100 - cpu).toFixed(1)}%</span></div>`;
  const dbBox = `<div class="glass dbt rise"${rise()}><div class="title">${I.clock}DB Time<span class="t3">${esc(w.duration_min)} 分钟窗口</span></div>
    <div class="big num">${dbt === null ? '—' : cu(fmtDbt(dbt))}<small>秒</small></div>
    <div class="sub">平均 <b>${w.duration_min && dbt !== null ? (dbt / (w.duration_min * 60)).toFixed(dbt / (w.duration_min * 60) < 1 ? 2 : 1) : '—'}</b> 个活跃会话${ch ? `<span class="chg ${ch.cls}">${ch.dir > 0 ? I.up : ch.dir < 0 ? I.dn : ''}${esc(ch.text)}</span><span class="t3">上一窗口 ${esc(fmtDbt(pdb))} 秒</span>` : ''}</div>
    ${comp}<div class="subt">等待按等待类拆开（占等待总时间）</div>${hb(waits, waits ? (waits.headers || []).length - 1 : 0, '%', true)}</div>`;
  const dumbs = `<div class="db-head"><b>负载对比</b><span>空心点是上一窗口${p ? `（snap ${esc(p.window.begin_id)}–${esc(p.window.end_id)}）` : ''}，实心点是本窗口；比例尺从 0 开始</span><span class="legend2" style="margin:0 0 0 auto"><span><i class="sw" style="background:var(--h-crit)"></i>明显变坏</span><span><i class="sw" style="background:var(--h-warn)"></i>变坏</span><span><i class="sw" style="background:var(--h-ok)"></i>变好</span><span><i class="sw" style="background:var(--h-t3)"></i>无好坏之分</span></span></div>
    <div class="dumbbells">${KPIS.map((k) => dumbbell(k, kpiVal(k, d), kpiVal(k, p), showK, rise)).join('')}</div>`;
  return `<section class="hero"><div class="hero-grid">${dbBox}${insightCard({ ...insight.wdr(m), ctx: wAbout(m) })}</div>${dumbs}</section>`;
}

export function render(root, m, ctx) {
  const head = (st, act, desc) => pageHead('WDR 分析', st, act, desc);
  if (!m.ok) {
    root.innerHTML = head('', '', []) + (m.reason === 'error' ? errorHero(m.error) : m.reason === 'range' ? rangeHero(ctx.range) : m.reason === 'gone' ? goneHero(m, '请列出最近的 WDR 快照,并对最近一个窗口做分析。', '在会话里跑一次 WDR 分析') : emptyHero(ctx.key ? '这个实例还没有 WDR 报告' : '还没有 WDR 报告', '在会话里对一个快照窗口做分析，报告会自动出现在这里。', '请列出最近的 WDR 快照,并对最近一个窗口做分析。', '在会话里跑一次 WDR 分析'));
    return;
  }
  if (m.w.wdr_enabled === false) {
    root.innerHTML = head('', '', []) + emptyHero('这个库没有开启 WDR', '需要把 enable_wdr_snapshot 设为 on 并有快照权限；开启后在会话里说「做一次 WDR 分析」即可。', '请检查当前库的 WDR 快照是否开启,说明如何开启以及需要的权限。', '在会话里检查');
    return;
  }
  setModel('wdr', m);
  const { d, w, p, findings: fs, native } = m, ts = dimOf(d, 'topsql');
  const verdicts = fs.length ? `<div class="list">${fs.map((f) => `<div class="it">${glyph(f.severity)}<div class="m"><b>${esc(cn(f.metric))}：${esc(f.value)}</b><small>阈值 ${esc(f.threshold)}。${esc(cn(f.evidence))}</small></div><a href="#" class="r"${dig(wPrompt(f, m), 'WDR')}>深挖</a></div>`).join('')}</div>` : `<div class="empty-state" style="padding:24px">${glyph(0)}<p style="margin-top:10px">窗口内没有越过阈值的发现。</p></div>`;
  const tsh = (ts && ts.headers) || [], shareI = tsh.findIndex((h) => String(h).startsWith('占DB')), TF = tsh.map((x, i) => colFmt((ts && ts.rows) || [], i));
  const tsTable = ts && (ts.rows || []).length ? `<div class="scroll"><table class="tbl"><thead><tr>${tsh.map((h, i) => `<th class="${i && i !== tsh.length - 1 && i !== shareI ? 'r' : ''}">${esc(h)}</th>`).join('')}</tr></thead><tbody>${ts.rows.slice(0, 10).map((r) => `<tr>${r.map((c, i) => (i === 0 ? `<td class="id"><a href="#" data-sql="${esc(c)}">${esc(c)}</a></td>` : i === shareI ? `<td><div class="share"><div class="b"><i class="grow" style="width:${Math.min(100, num(c) || 0)}%"></i></div><b>${esc(c)}%</b></div></td>` : i === r.length - 1 ? `<td class="q" title="${esc((m.sqlTexts && m.sqlTexts[r[0]]) || c)}">${esc(c)}</td>` : `<td class="r">${esc(TF[i](c))}</td>`)).join('')}</tr>`).join('')}</tbody></table></div>` : `<p class="t3" style="padding:0 20px">${esc(cn((ts && ts.note) || '未采集'))}</p>`;
  const ck = dimOf(d, 'checkpoint');
  const ckBody = ck && (ck.rows || [])[0] ? `<p class="t2" style="margin:0 0 14px;font-size:13px">${esc(cn(ck.headline || ''))}</p><div class="kvs" style="--cols:${Math.min(4, (ck.headers || []).length)}">${(ck.headers || []).slice(0, 4).map((h, i) => `<div><div class="kv"><div class="k">${esc(h)}</div><div class="v num" style="font-size:20px">${esc(ck.rows[0][i])}</div></div></div>`).join('')}</div>` : `<p class="t3">${esc(cn((ck && ck.note) || '未采集'))}</p>`;
  const nat = native.generated ? (m.nativeUrl ? `<a class="btn" href="${esc(m.nativeUrl)}" target="_blank" rel="noopener">${I.down}原生报告 ${fmtInt(Math.round((native.bytes || 0) / 1024))} KB</a>` : `<span class="tag gray">原生报告已生成，${fmtInt(Math.round((native.bytes || 0) / 1024))} KB</span>`) : `<span class="tag gray"${tipAttr(esc(native.note || ''))}>原生报告未生成</span>`;
  const target = m.name;
  root.innerHTML = head(tag(d.overall | 0), runPicker('wdr', m.entry, '窗口') + nat + primary(wDigPrompt(m), '在会话里深挖', 'WDR'),
    [['实例', esc(target)], ['窗口', `snap ${esc(w.begin_id)} 至 ${esc(w.end_id)}`], ['时间', `${esc(w.begin_ts || '')} 至 ${esc(String(w.end_ts || '').slice(11, 16))}，${esc(w.duration_min)} 分钟`], ['范围', `${esc(w.scope || '')} ${esc(w.node || '')}`], ['对比', p ? `snap ${esc(p.window.begin_id)} 至 ${esc(p.window.end_id)}` : m.sameSkipped ? '没有（之前分析的是同一个窗口）' : '没有上一窗口（第一份报告）']])
    + missedBanner(m) + hero(m)
    + `<div class="bento">${tile({ title: '窗口内 Top SQL', desc: esc(cn((ts && ts.headline) || '')), body: tsTable, flush: true })}
      ${tile({ span: 'b4', title: `结论<span class="cnt">${fs.length}</span>`, desc: '脚本按阈值判定', body: verdicts })}
      ${tile({ span: 'b4', title: '检查点与 Redo', body: ckBody })}
      ${tile({ span: 'b4', title: '窗口历史', desc: `最近 ${Math.min(20, m.idx.length)} 个窗口，点格子切换`, body: rhythm('wdr', m.idx, m.entry && m.entry.file) })}
      ${tile({ span: 'b6', title: 'DB time 趋势', desc: '最近若干窗口的 DB time（秒）。回答「是不是越来越卡」', body: dbTrend(m.idx, m.entry && m.entry.file) })}
      ${tile({ span: 'b6', title: '等待类占比', desc: '按窗口内总等待时长。只看 pg_locks 看不到轻量锁,这里能看到', body: hb(dimOf(d, 'waits'), 3, '%') })}
      ${tile({ span: 'b6', title: '物理读最多的对象', body: hb(dimOf(d, 'cache'), 1, '块') })}
      ${tile({ span: 'b6', title: '文件 IO', desc: '按物理读', body: hb(dimOf(d, 'fileio'), 1, '') })}</div>`;
}
