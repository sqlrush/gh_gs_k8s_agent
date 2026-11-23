// 普通风格的图形:检查项表盘、巡检节奏、耗时树图、对比哑铃、调用×耗时散点、覆盖环、关系缩略图。
// 全部确定性地画成 SVG / 绝对定位的 HTML,数据只来自报告。颜色走样式表变量(base.css 的 --h-* / --tm-* / --db-* 等)。

import { esc, stamp, mmdd, fmtInt, fmtMs, fmtSec } from './fmt.js';
import { G, lk, lname, tipAttr, cu, I } from './ui.js';
import { entryLevel, countsOf } from '../pages/health.js';
import { change } from '../pages/wdr.js';

const f1 = (v) => v.toFixed(1);

/** 检查项表盘:每项一个「状态图标 + 名称」的点,等距排在一圈细线上(位置只表示固定顺序,不表示比例);
 *  异常项放大、带光晕和指向圆心的细线,名称下写级别;正常项小而暗,只留勾形图标。中心是总体级别。
 *  按卡片实际宽度(约 460px)1:1 绘制,文字不被缩放;高度按标签实际落点算。checks = [{name, lv, attr, tip}]。 */
export function dial(checks, lvl, sub) {
  const W = 460, CX = 210, RO = 88, RI = 52, n = checks.length || 1;
  const gl = (lv, x, y, s) => G[lv].replace('<svg viewBox="0 0 16 16"', `<svg class="gl" data-lv="${lv}" x="${f1(x - s / 2)}" y="${f1(y - s / 2)}" width="${s}" height="${s}" viewBox="0 0 16 16"`);
  const pts = checks.map((s, i) => {
    const a = -Math.PI / 2 + (i * 2 * Math.PI) / n, c = Math.cos(a), sn = Math.sin(a), bad = s.lv !== '0', hot = bad && s.lv !== 'na';
    const lr = RO + (bad ? 21 : 15), lx = CX + lr * c, ly = lr * sn;
    const anchor = c > 0.2 ? 'start' : c < -0.2 ? 'end' : 'middle', word = s.lv === 'na' ? (s.naWord || '采集失败') : bad ? lname(+s.lv) : '';
    const ty = anchor !== 'middle' ? ly + (word ? -4 : 6) : sn < 0 ? ly - (word ? 21 : 5) : ly + 16;
    return { s, c, sn, hot, x: CX + RO * c, y: RO * sn, lx, ty, anchor, word, top: ty - 16, bottom: ty + (word ? 23 : 5) };
  });
  const top = Math.min(-RO - 16, ...pts.map((p) => p.top)), CY = 6 - top, H = Math.ceil(CY + Math.max(RO + 16, ...pts.map((p) => p.bottom)) + 6);
  const nodes = pts.map(({ s, c, sn, hot, x, y, lx, ty, anchor, word }, i) => `<g class="node${s.lv !== '0' ? ' bad' : ''}" data-lv="${s.lv}" style="--i:${i}"${s.attr}${tipAttr(s.tip)}>
      ${hot ? `<line class="spoke" x1="${f1(CX + RI * c)}" y1="${f1(CY + RI * sn)}" x2="${f1(x - 17 * c)}" y2="${f1(CY + y - 17 * sn)}"/><circle class="halo" cx="${f1(x)}" cy="${f1(CY + y)}" r="17"/>` : ''}
      <circle class="hit" cx="${f1(x)}" cy="${f1(CY + y)}" r="24"/>${gl(s.lv, x, CY + y, hot ? 22 : s.lv === 'na' ? 17 : 15)}
      <text class="nm" x="${f1(lx)}" y="${f1(CY + ty)}" text-anchor="${anchor}">${esc(s.name)}</text>${word ? `<text class="wd" x="${f1(lx)}" y="${f1(CY + ty + 18)}" text-anchor="${anchor}">${esc(word)}</text>` : ''}</g>`).join('');
  return `<svg class="dial" data-lv="${lk(lvl)}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(`总体${lname(lvl)}，${sub}`)}">
    <defs><radialGradient id="dial-core"><stop offset="0" style="stop-color:var(--hc);stop-opacity:.30"/><stop offset="1" style="stop-color:var(--hc);stop-opacity:0"/></radialGradient></defs>
    <circle cx="${CX}" cy="${f1(CY)}" r="${RO - 8}" fill="url(#dial-core)"/>
    <circle class="orbit" cx="${CX}" cy="${f1(CY)}" r="${RO}"/><circle class="inner" cx="${CX}" cy="${f1(CY)}" r="${RI}"/>${nodes}
    <text class="lvl${lname(lvl).length > 2 ? ' long' : ''}" x="${CX}" y="${f1(CY + 7)}" text-anchor="middle">${esc(lname(lvl))}</text>
    <text class="sub" x="${CX}" y="${f1(CY + 31)}" text-anchor="middle">${esc(sub)}</text></svg>`;
}

/** 巡检节奏:上面是发现条数的平滑面积线,下面一排圆角格是每次的级别,两者同一横轴。只画最近 20 次。
 *  点一格(data-run)切到那一份报告。 */
export function rhythm(skill, index, curFile, W = 360) {
  const idx = index.slice(-20);
  if (!idx.length) return '<p class="t3">还没有历史</p>';
  const n = idx.length, H = 150, T = 18, B = 82, CY = 96, slot = W / n, cs = Math.min(22, slot - 5);
  const mx = Math.max(2, ...idx.map(countsOf)), X = (i) => slot * i + slot / 2, Y = (v) => B - (v / mx) * (B - T);
  const pts = idx.map((e, i) => [X(i), Y(countsOf(e))]);
  const curve = pts.map(([x, y], i) => (i ? `C${f1((pts[i - 1][0] + x) / 2)},${f1(pts[i - 1][1])} ${f1((pts[i - 1][0] + x) / 2)},${f1(y)} ${f1(x)},${f1(y)}` : `M${f1(x)},${f1(y)}`)).join(' ');
  const cols = idx.map((e, i) => {
    const l = entryLevel(e), lv = lk(l), cur = e.file === curFile, x = X(i), c = e.counts || {};
    const tip = `<b>${esc(stamp(e.at))}</b><div class="d">${esc(lname(l))}；严重 ${+c['3'] || 0}，告警 ${+c['2'] || 0}，关注 ${+c['1'] || 0}${e.dims && e.dims.na ? `；未采集 ${e.dims.na}/${e.dims.total} 个维度` : ''}</div>`;
    return `<g data-lv="${lv}" data-run="${esc(skill)}|${esc(e.file)}" style="cursor:pointer"${tipAttr(tip)}>
      <rect x="${f1(x - slot / 2)}" y="0" width="${f1(slot)}" height="${H}" fill="transparent"/><circle class="dot" cx="${f1(x)}" cy="${f1(pts[i][1])}" r="${cur ? 4.5 : 3.5}"/>
      ${cur ? `<rect x="${f1(x - cs / 2 - 3.5)}" y="${CY - 3.5}" width="${f1(cs + 7)}" height="${f1(cs + 7)}" rx="9" fill="none" stroke="var(--primary)" stroke-width="1.6"/>` : ''}
      <rect class="cell" x="${f1(x - cs / 2)}" y="${CY}" width="${f1(cs)}" height="${f1(cs)}" rx="6" fill="${lv === '0' ? 'var(--gray-mark)' : lv === 'na' ? 'none' : 'var(--c)'}" stroke="${lv === 'na' ? 'var(--na)' : 'none'}" stroke-width="1.4"${lv === 'na' ? ' stroke-dasharray="3 2"' : ''}/></g>`;
  }).join('');
  const ci = idx.findIndex((e) => e.file === curFile);
  const labs = [...new Set([0, n - 1, ci].filter((i) => i >= 0))].map((i) => `<text x="${f1(Math.min(W - 18, Math.max(18, X(i))))}" y="${CY + cs + 18}" text-anchor="middle"${i === ci ? ' class="on"' : ''}>${esc(mmdd(idx[i].at))}</text>`).join('');
  return `<svg class="rhythm" viewBox="0 0 ${W} ${H}">
    <defs><linearGradient id="tg-${esc(skill)}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--primary);stop-opacity:.22"/><stop offset="1" style="stop-color:var(--primary);stop-opacity:0"/></linearGradient></defs>
    <line class="base" x1="0" x2="${W}" y1="${B}" y2="${B}"/><text x="0" y="10">发现条数，最多 ${mx}</text>
    <path d="${curve} L${f1(X(n - 1))},${B} L${f1(X(0))},${B} Z" fill="url(#tg-${esc(skill)})"/><path class="line" d="${curve}"/>${cols}${labs}</svg>
    <div class="legend"><span><i class="sw calm"></i>正常</span><span data-lv="1"><i class="sw"></i>关注</span><span data-lv="2"><i class="sw"></i>告警</span><span data-lv="3"><i class="sw"></i>严重</span><span data-lv="na"><i class="sw hollow"></i>采集失败</span></div>`;
}

/** 方形化树图(squarified):面积 = 上榜总耗时占比。返回带 x/y/w/h 的新数组,不改入参。 */
function squarify(items, box) {
  const out = [];
  let { x, y, w, h } = box, rest = items;
  while (rest.length) {
    const short = Math.min(w, h);
    const worst = (row) => { const s = row.reduce((a, b) => a + b.a, 0), mx = Math.max(...row.map((z) => z.a)), mn = Math.min(...row.map((z) => z.a)); return Math.max((short * short * mx) / (s * s), (s * s) / (short * short * mn)); };
    let k = 1;
    while (k < rest.length && worst(rest.slice(0, k + 1)) <= worst(rest.slice(0, k))) k++;
    const row = rest.slice(0, k), s = row.reduce((a, b) => a + b.a, 0);
    if (w >= h) { const cw = s / h; let cy = y; row.forEach((z) => { const ch = z.a / cw; out.push({ ...z, x, y: cy, w: cw, h: ch }); cy += ch; }); x += cw; w -= cw; }
    else { const rh = s / w; let cx = x; row.forEach((z) => { const cw = z.a / rh; out.push({ ...z, x: cx, y, w: cw, h: rh }); cx += cw; }); y += rh; h -= rh; }
    rest = rest.slice(k);
  }
  return out;
}
/** 耗时树图:前 12 条各一块,其余合成一块「其余 N 条」;业务 SQL 品牌蓝,系统 SQL 斜纹灰。点块看详情(data-sql)。 */
export function treemap(rows, tune, isSys) {
  const VW = 1000, VH = 440, sum = rows.reduce((a, r) => a + (+r.total_sec || 0), 0) || 1;
  const sorted = [...rows].sort((a, b) => b.total_sec - a.total_sec), head = sorted.slice(0, 12), tail = sorted.slice(12);
  const items = head.map((r) => ({ r, a: Math.max(1e-6, (+r.total_sec || 0) / sum) * VW * VH }));
  if (tail.length) items.push({ rest: tail, a: Math.max(1e-6, tail.reduce((a, r) => a + (+r.total_sec || 0), 0) / sum) * VW * VH });
  return `<div class="treemap">${squarify(items, { x: 0, y: 0, w: VW, h: VH }).map((c, i) => {
    const pos = `--i:${i};left:calc(${f1((c.x / VW) * 100)}% + 3px);top:calc(${f1((c.y / VH) * 100)}% + 3px);width:calc(${f1((c.w / VW) * 100)}% - 6px);height:calc(${f1((c.h / VH) * 100)}% - 6px)`;
    const size = c.w < 150 || c.h < 90 ? 'xs' : c.w < 260 || c.h < 140 ? 'sm' : '';
    const pct = (c.a / (VW * VH)) * 100;
    if (c.rest) return `<div class="cell ${size}" style="${pos};background:var(--tm-sys)"${tipAttr(`<b>其余 ${c.rest.length} 条</b><div class="d">合计占上榜耗时 ${f1(pct)}%</div>`)}><span class="id">其余 ${c.rest.length} 条</span><span class="pct num">${f1(pct)}<small>%</small></span></div>`;
    const r = c.r, sys = isSys(tune[r.sql_id]);
    const tip = `<b>${esc(r.sql_id)}</b>${sys ? '<span class="demo-mark">系统 SQL</span>' : ''}<div class="d">占上榜耗时 ${f1(pct)}%，总耗时 ${esc(fmtSec(r.total_sec))}</div><div class="d">调用 ${esc(fmtInt(r.calls))} 次，平均 ${esc(fmtMs(r.avg_ms))}</div>`;
    return `<button class="cell ${size}" data-sql="${esc(r.sql_id)}" style="${pos};background:${sys ? 'var(--tm-sys)' : 'var(--tm-biz)'}"${tipAttr(tip)}>
      <span class="id">${esc(r.sql_id)}${sys && size !== 'xs' ? '<span class="sys">系统</span>' : ''}</span><span class="pct num">${f1(pct)}<small>%</small></span>
      <span class="q">${esc(r.query)}</span><span class="m num">${esc(fmtInt(r.calls))} 次 · 平均 ${esc(fmtMs(r.avg_ms))}</span></button>`;
  }).join('')}</div>`;
}

/** 对比哑铃:空心点上一窗口,实心点本窗口,连线颜色说变好还是变坏。比例尺从 0 开始,不放大差异。 */
export function dumbbell(k, cur, prev, showK, rise) {
  if (cur === null) return `<div class="db-row glass"><div class="top"><span class="n">${esc(k.l)}</span><span class="v t3">未采集</span></div></div>`;
  const W = 240, L = 8, PW = W - 16, mx = k.u === '%' ? 100 : Math.max(cur, prev || 0) * 1.1 || 1, X = (v) => L + (Math.max(0, v) / mx) * PW;
  const has = prev !== null && prev !== undefined, ch = has ? change(k, cur, prev) : null;
  const col = { bad: 'var(--h-crit)', worse: 'var(--h-warn)', better: 'var(--h-ok)', flat: 'var(--h-t3)' }[ch ? ch.cls : 'flat'];
  const [cv, cu2] = showK(cur, k.u), [pv, pu] = has ? showK(prev, k.u) : ['', ''];
  const sp = (u) => (u === '%' ? u : u ? ' ' + u : '');
  return `<div class="db-row glass rise"${rise()}${tipAttr(`<b>${esc(k.l)}</b><div class="d">本窗口 ${esc(cv + sp(cu2))}</div>${has ? `<div class="d">上一窗口 ${esc(pv + sp(pu))}</div>` : ''}`)}>
    <div class="top"><span class="n">${esc(k.l)}</span><span class="v num">${cu(cv)}<small>${esc(cu2)}</small></span></div>
    <svg viewBox="0 0 ${W} 22"><line class="trk" x1="${L}" x2="${W - L}" y1="11" y2="11" stroke-width="4" stroke-linecap="round"/>
      ${has ? `<line x1="${f1(X(prev))}" x2="${f1(X(cur))}" y1="11" y2="11" stroke="${col}" stroke-width="4" stroke-linecap="round"/><circle class="pv" cx="${f1(X(prev))}" cy="11" r="5" stroke-width="2"/>` : ''}
      <circle class="cv" cx="${f1(X(cur))}" cy="11" r="6" fill="${col}" stroke-width="1.5"/></svg>
    <div class="cap"><span>${has ? `上一窗口 ${esc(pv + sp(pu))}` : '没有可对比的窗口'}</span>${ch ? `<span class="chg ${ch.cls}">${ch.dir > 0 ? I.up : ch.dir < 0 ? I.dn : ''}${esc(ch.text)}</span>` : ''}</div></div>`;
}

/** 调用次数 × 平均耗时(双对数),气泡面积 = 总耗时。业务 SQL 品牌蓝,系统 SQL 灰。 */
export function scatter(rows, tune, isSys, W = 640) {
  if (!rows.length) return '';
  const H = 310, L = 58, Rt = 24, T = 34, B = 44, pw = W - L - Rt, ph = H - T - B, PAD = 0.35;
  const lg = (v) => Math.log10(Math.max(v, 1e-3));
  const xs = rows.map((r) => lg(+r.calls || 1)), ys = rows.map((r) => lg(+r.avg_ms || 0.01));
  const x0 = Math.floor(Math.min(...xs) - PAD), x1 = Math.ceil(Math.max(...xs) + PAD), y0 = Math.floor(Math.min(...ys) - PAD), y1 = Math.ceil(Math.max(...ys) + PAD);
  const X = (v) => L + ((v - x0) / Math.max(1, x1 - x0)) * pw, Y = (v) => T + ph - ((v - y0) / Math.max(1, y1 - y0)) * ph;
  const mt = Math.max(...rows.map((r) => +r.total_sec || 0)) || 1;
  const lab = (e) => (e >= 4 ? fmtInt(10 ** e) : e >= 0 ? String(10 ** e) : String(+(10 ** e).toFixed(3)));
  const gx = Array.from({ length: x1 - x0 + 1 }, (_, i) => x0 + i).map((e) => `<line x1="${f1(X(e))}" x2="${f1(X(e))}" y1="${T}" y2="${T + ph}"/><text x="${f1(X(e))}" y="${T + ph + 18}" text-anchor="middle">${lab(e)}</text>`).join('');
  const gy = Array.from({ length: y1 - y0 + 1 }, (_, i) => y0 + i).map((e) => `<line x1="${L}" x2="${L + pw}" y1="${f1(Y(e))}" y2="${f1(Y(e))}"/><text x="${L - 8}" y="${f1(Y(e) + 4)}" text-anchor="end">${lab(e)}</text>`).join('');
  const dots = rows.map((r, i) => {
    const sys = isSys(tune[r.sql_id]), cx = X(xs[i]), cy = Y(ys[i]), rr = 5 + 22 * Math.sqrt((+r.total_sec || 0) / mt);
    // 右侧 100px 内有别的气泡就把标签放左边,避免压住它
    const crowded = rows.some((_, j) => j !== i && X(xs[j]) > cx && X(xs[j]) - cx < 100 && Math.abs(Y(ys[j]) - cy) < 24);
    const right = !crowded && cx + rr + 70 < L + pw;
    return `<g data-sql="${esc(r.sql_id)}" style="cursor:pointer"${tipAttr(`<b>${esc(r.sql_id)}</b>${sys ? '<span class="demo-mark">系统 SQL</span>' : ''}<div class="d">调用 ${esc(fmtInt(r.calls))} 次，平均 ${esc(fmtMs(r.avg_ms))}</div><div class="d">总耗时 ${esc(fmtSec(r.total_sec))}</div>`)}>
      <circle cx="${f1(cx)}" cy="${f1(cy)}" r="${f1(rr)}" class="${sys ? 'sys' : 'biz'}"/>${i < 12 ? `<text class="id" x="${f1(right ? cx + rr + 6 : cx - rr - 6)}" y="${f1(cy + 4)}" text-anchor="${right ? 'start' : 'end'}">${esc(r.sql_id)}</text>` : ''}</g>`;
  }).join('');
  return `<svg class="scatter" viewBox="0 0 ${W} ${H}"><g class="grid">${gx}${gy}</g>${dots}
    <text class="ax" x="${L + pw}" y="${H - 4}" text-anchor="end">调用次数（对数）→</text><text class="ax" x="${L - 50}" y="14">平均耗时 ms（对数）↑</text></svg>
    <div class="legend"><span><i class="sw" style="--c:var(--primary)"></i>业务 SQL</span><span><i class="sw" style="--c:#b8c0cc"></i>系统 SQL</span><span class="t3">气泡越大，总耗时越多；右上角是又多又慢的</span></div>`;
}

/** 主视觉带里的小覆盖环。 */
export function covring(pct, label) {
  const r = 36, c = 2 * Math.PI * r, v = Math.max(0, Math.min(100, pct || 0));
  return `<svg class="covring" viewBox="0 0 96 96" width="96" height="96"><defs><linearGradient id="cg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" style="stop-color:var(--cov-1)"/><stop offset=".6" style="stop-color:var(--cov-2)"/><stop offset="1" style="stop-color:var(--cov-3)"/></linearGradient></defs>
    <circle class="trk" cx="48" cy="48" r="${r}" fill="none" stroke-width="8"/>
    <circle class="cov" cx="48" cy="48" r="${r}" fill="none" stroke="url(#cg)" stroke-width="8" stroke-linecap="round" stroke-dasharray="${f1((c * v) / 100)} ${f1(c)}" transform="rotate(-90 48 48)" style="--len:${f1(c)}"/>
    <text class="pct" x="48" y="52" text-anchor="middle" font-size="17" font-weight="600">${pct === null || pct === undefined ? '—' : esc(pct) + '%'}</text></svg><div class="cov-l">${esc(label)}</div>`;
}

/** 关系缩略图:五列(案例 / 现象 / 根因 / 处置 / 条款)的点与线,点它打开关系图页签。最多每列 8 个点。 */
const COLS = [['case', '案例'], ['symptom', '现象'], ['rootcause', '根因'], ['action', '处置'], ['rule', '条款']];
export function minigraph(cat) {
  const nodes = new Map(), links = [], add = (k, id) => { if (!nodes.has(id)) nodes.set(id, { k, id }); };
  (cat.edges || []).forEach((e) => { add(e.src.kind, e.src.id); add(e.dst.kind, e.dst.id); links.push([e.src.id, e.dst.id]); });
  (cat.cases || []).forEach((c) => (c.rules || []).forEach((r) => { add('rule', 'rule:' + r); add('case', 'case:' + c.id); links.push(['case:' + c.id, 'rule:' + r]); }));
  const W = 420, H = 118, pos = new Map();
  COLS.forEach(([k], ci) => { const list = [...nodes.values()].filter((n) => n.k === k).slice(0, 8); list.forEach((n, i) => pos.set(n.id, { x: 22 + ci * ((W - 44) / (COLS.length - 1)), y: 22 + ((i + 1) * (H - 26)) / (list.length + 1) })); });
  return `<svg class="minigraph" viewBox="0 0 ${W} ${H}" data-tab="graph" style="cursor:pointer"${tipAttr('<b>关系图</b><div class="d">点击打开关系图页签</div>')}>
    ${COLS.map(([, l], i) => `<text x="${22 + i * ((W - 44) / (COLS.length - 1))}" y="12" text-anchor="middle">${l}</text>`).join('')}
    ${links.map(([a, b]) => { const p = pos.get(a), q = pos.get(b); return p && q ? `<line x1="${f1(p.x)}" y1="${f1(p.y)}" x2="${f1(q.x)}" y2="${f1(q.y)}"/>` : ''; }).join('')}
    ${[...pos.values()].map((p, i) => `<circle cx="${f1(p.x)}" cy="${f1(p.y)}" r="5.5" style="--i:${i}"/>`).join('')}</svg>`;
}
