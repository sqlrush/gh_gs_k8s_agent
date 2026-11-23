// 文艺风格的图表:墨色线条、带注释的走势、横排份额、份额带、前后对照表、条形清单。
// 只有墨色与一处强调红;状态仍配图标与文字。数据只来自报告。

import { esc, stamp, mmdd, fmtInt, fmtMs, fmtSec } from './fmt.js';
import { lname, tipAttr } from './ui.js';
import { entryLevel, countsOf } from '../pages/health.js';
import { change } from '../pages/wdr.js';

const f1 = (v) => v.toFixed(1);

/** 走势:每份报告的越线条数,折线 + 本期注释(引线拉到右侧页边)。点一份(data-run)翻到那一期。 */
export function trend(skill, index, curFile, unit, W = 720, H = 210) {
  const idx = index.slice(-30);
  if (!idx.length) return '';
  const n = idx.length, L = 28, RM = 150, T = 16, B = 30, pw = W - L - RM, ph = H - T - B;
  const mx = Math.max(2, ...idx.map(countsOf)), X = (i) => L + (n === 1 ? pw / 2 : (i * pw) / (n - 1)), Y = (v) => T + ph - (v / mx) * ph;
  const grid = [0, mx / 2, mx].map((v) => `<line class="g" x1="${L}" x2="${L + pw}" y1="${f1(Y(v))}" y2="${f1(Y(v))}"/><text class="yl" x="${L - 8}" y="${f1(Y(v) + 4)}" text-anchor="end">${+v.toFixed(1)}</text>`).join('');
  const line = idx.map((e, i) => `${i ? 'L' : 'M'}${f1(X(i))},${f1(Y(countsOf(e)))}`).join(' ');
  const ci = idx.findIndex((e) => e.file === curFile), cur = idx[ci], cv = cur ? countsOf(cur) : 0;
  const top = Math.max(...idx.map(countsOf)), ties = idx.filter((e) => countsOf(e) === top).length;
  const note2 = n === 1 ? '第一份报告' : cv === top && ties === 1 ? `近 ${n} 次中最多` : cv === top ? `与近 ${n} 次最高持平` : `近 ${n} 次最多 ${top} 条`;
  const pts = idx.map((e, i) => {
    const l = entryLevel(e), c = e.counts || {}, isCur = i === ci;
    const tip = `<b>${esc(stamp(e.at))}</b><div class="d">${esc(lname(l))}；严重 ${+c['3'] || 0}，告警 ${+c['2'] || 0}，关注 ${+c['1'] || 0}</div>`;
    return `<g data-run="${esc(skill)}|${esc(e.file)}" style="cursor:pointer"${tipAttr(tip)}><rect x="${f1(X(i) - pw / n / 2)}" y="${T}" width="${f1(pw / n)}" height="${ph}" fill="transparent"/>
      <circle class="${isCur ? 'pc' : l === null ? 'pd' : 'pr'}" cx="${f1(X(i))}" cy="${f1(Y(countsOf(e)))}" r="${isCur ? 5 : 3.5}"/></g>`;
  }).join('');
  const ann = cur ? `<path class="lead" d="M${f1(X(ci) + 7)},${f1(Y(cv))} L${L + pw + 18},${f1(Y(cv))}"/><text class="an" x="${L + pw + 24}" y="${f1(Y(cv) - 2)}">本期 ${cv} ${esc(unit)}</text><text class="an2" x="${L + pw + 24}" y="${f1(Y(cv) + 16)}">${esc(note2)}</text>` : '';
  return `<svg class="ptrend" viewBox="0 0 ${W} ${H}">${grid}<path class="ln" d="${line}"/>${pts}${ann}
    <text class="xl" x="${L}" y="${H - 6}">${esc(mmdd(idx[0].at))}</text><text class="xl" x="${L + pw}" y="${H - 6}" text-anchor="end">${esc(mmdd(idx[n - 1].at))}</text></svg>`;
}

/** 横排份额:每条语句一行,按总耗时占上榜合计;系统 SQL 斜纹,accent 那一条用强调红。最多 12 行。 */
export function shares(rows, tune, isSys, accent) {
  const sum = rows.reduce((a, r) => a + (+r.total_sec || 0), 0) || 1, list = [...rows].sort((a, b) => b.total_sec - a.total_sec).slice(0, 12);
  const mx = ((+(list[0] || {}).total_sec || 0) / sum) * 100 || 1;
  return `<div class="pshares">${list.map((r) => { const p = ((+r.total_sec || 0) / sum) * 100, sys = isSys(tune[r.sql_id]);
    return `<button class="sh${sys ? ' sys' : ''}${r.sql_id === accent ? ' acc' : ''}" data-sql="${esc(r.sql_id)}"${tipAttr(`<b>${esc(r.sql_id)}</b>${sys ? '<span class="demo-mark">系统 SQL</span>' : ''}<div class="d">调用 ${esc(fmtInt(r.calls))} 次，平均 ${esc(fmtMs(r.avg_ms))}，总耗时 ${esc(fmtSec(r.total_sec))}</div>`)}>
      <span class="id">${esc(r.sql_id)}${sys ? '<small>系统</small>' : ''}</span><span class="bar"><i style="width:${f1((p / mx) * 100)}%"></i></span><span class="pct num">${f1(p)}%</span></button>`; }).join('')}</div>`;
}

/** 一条 100% 份额带,宽段下方标注;用在头版的小栏目里。 */
export function stack(rows, tune, isSys, accent) {
  const sum = rows.reduce((a, r) => a + (+r.total_sec || 0), 0) || 1, list = [...rows].sort((a, b) => b.total_sec - a.total_sec);
  const seg = list.map((r) => ({ r, p: ((+r.total_sec || 0) / sum) * 100, sys: isSys(tune[r.sql_id]) }));
  const big = seg.filter((s) => s.p >= 12), small = seg.length - big.length;
  return `<div class="pstack">${seg.map((s) => `<i class="${s.sys ? 'sys' : s.r.sql_id === accent ? 'acc' : ''}" style="flex:${f1(Math.max(0.3, s.p))}"${tipAttr(`<b>${esc(s.r.sql_id)}</b><div class="d">占上榜耗时 ${f1(s.p)}%</div>`)}></i>`).join('')}</div>
    <div class="pstack-l">${big.map((s) => `<span style="flex:${f1(s.p)}"><b class="num">${f1(s.p)}%</b> ${esc(s.r.sql_id)}${s.sys ? '（系统）' : ''}</span>`).join('')}${small ? `<span class="rest">其余 ${small} 条</span>` : ''}</div>`;
}

/** 前后对照表:上一窗口 → 本窗口,中间一根墨线;变坏的标强调红,变好的标绿,无好坏之分的用墨色。
 *  compact 用在头版小栏目:只留「上一窗口 → 本窗口」与变化两列。 */
export function pairs(kpis, d, p, val, show, compact) {
  const sp = (u) => (u === '%' ? u : u ? ' ' + u : '');
  return `<table class="ptable pairs${compact ? ' compact' : ''}"><thead><tr><th>指标</th>${compact ? '<th class="r">上一窗口 → 本窗口</th>' : '<th class="r">上一窗口</th><th></th><th class="r">本窗口</th>'}<th class="r">变化</th></tr></thead><tbody>${kpis.map((k) => {
    const c = val(k, d), pv = val(k, p);
    if (c === null) return `<tr><td>${esc(k.l)}</td><td colspan="${compact ? 2 : 4}" class="t3">未采集</td></tr>`;
    const has = pv !== null && pv !== undefined, ch = has ? change(k, c, pv) : null, mx = k.u === '%' ? 100 : Math.max(c, pv || 0) * 1.1 || 1, X = (v) => 6 + (Math.max(0, v) / mx) * 128;
    const [cv, cu] = show(c, k.u), [pvv, pu] = has ? show(pv, k.u) : ['—', ''];
    const chg = ch ? esc((ch.dir > 0 ? '↑ ' : ch.dir < 0 ? '↓ ' : '') + ch.text) : '—';
    if (compact) return `<tr class="${ch ? ch.cls : ''}"><td>${esc(k.l)}</td><td class="r num"><span class="t2">${esc(pvv + sp(pu))}</span> → <b>${esc(cv + sp(cu))}</b></td><td class="r chg">${chg}</td></tr>`;
    return `<tr class="${ch ? ch.cls : ''}"><td>${esc(k.l)}</td><td class="r num t2">${esc(pvv + sp(pu))}</td>
      <td class="sl"><svg viewBox="0 0 140 14"><line class="trk" x1="6" x2="134" y1="7" y2="7"/>${has ? `<line class="mv" x1="${f1(X(pv))}" x2="${f1(X(c))}" y1="7" y2="7"/><circle class="pv" cx="${f1(X(pv))}" cy="7" r="3.5"/>` : ''}<circle class="cv" cx="${f1(X(c))}" cy="7" r="4"/></svg></td>
      <td class="r num"><b>${esc(cv + sp(cu))}</b></td><td class="r chg">${chg}</td></tr>`;
  }).join('')}</tbody></table>`;
}

/** 条形清单:[标签, 数值, 单位, 小字];accentFirst 时第一根用强调红。 */
export function bars(items, accentFirst) {
  const mx = Math.max(1, ...items.map((x) => +x[1] || 0));
  return `<div class="pbars">${items.map(([l, v, u, sub], i) => `<div class="pb${accentFirst && !i ? ' acc' : ''}"><span class="l" title="${esc(l)}">${esc(l)}${sub ? `<small>${esc(sub)}</small>` : ''}</span><span class="bar"><i style="width:${f1(((+v || 0) / mx) * 100)}%"></i></span><span class="v num">${esc(String(v))}<small>${esc(u || '')}</small></span></div>`).join('')}</div>`;
}
