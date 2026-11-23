// 普通风格的版式部件:页头、便当盒卡片、智能体解读卡、主视觉统计、空 / 出错状态、报告选择菜单。

import { esc, stamp } from '../../lib/fmt.js';
import { I, glyph, lname, primary } from '../../lib/ui.js';
import { rise } from '../../lib/motion.js';
import { menu } from '../../lib/overlay.js';
import { getModel } from '../../lib/store.js';
import { entryLevel, entryLabel } from '../../pages/health.js';
import { label as rangeLabel, windowLabel } from '../../lib/range.js';
import { goneText } from '../../lib/missed.js';

export function pageHead(crumb, statusTag, actions, desc) {
  return `<div class="crumb">诊断大盘<span>/</span><b>${esc(crumb)}</b></div>
    <div class="page-head"><h1>${esc(crumb)}</h1>${statusTag || ''}<div class="actions">${actions || ''}</div></div>
    <div class="page-desc">${desc.map(([k, v]) => `<span>${esc(k)}：<b>${v}</b></span>`).join('')}</div>`;
}

export const tile = (o) => `<section class="tile ${o.span || 'b12'} rise ${o.cls || ''}"${rise()}${o.id ? ` id="${o.id}"` : ''}>${o.title ? `<div class="tile-h"><h3>${o.title}</h3>${o.desc ? `<span class="desc">${o.desc}</span>` : ''}<div class="tools">${o.tools || ''}</div></div>` : ''}<div class="tile-b ${o.flush ? 'flush' : ''}">${o.body}</div></section>`;

/** 智能体解读卡:结论、要点(带证据小签)、建议追问、提问框。提问框与追问都开 opencode 会话(data-dig / data-askform)。 */
export const insightCard = (o) => `<div class="glass insight rise"${rise()}>
    <div class="who"><span class="orb">${I.spark}</span><b>智能体解读</b><span class="src">${esc(o.src)}</span></div>
    <h2>${o.head}</h2>
    <ul class="lines">${o.lines.filter(Boolean).map((l) => `<li><span>${l}</span></li>`).join('')}</ul>
    <div class="chips">${o.chips.map(([l, p]) => `<button class="chip-ask" data-dig="${esc(p)}">${I.spark}${esc(l)}</button>`).join('')}</div>
    <form class="askbox" data-askform data-ctx="${esc(o.ctx || '')}">${I.spark}<input name="q" autocomplete="off" placeholder="${esc(o.ask)}"><button class="send" aria-label="在会话里问">${I.send}</button></form>
  </div>`;

export const heroStats = (items) => `<div class="hero-stats">${items.map((x) => `<div class="glass rise"${rise()}><div class="l">${x.l}</div><div class="v num">${x.v}<small>${esc(x.u || '')}</small></div><div class="s">${x.s || ''}</div></div>`).join('')}</div>`;

export const emptyHero = (title, text, prompt, label) => `<section class="hero rise"${rise()}><div class="empty-state">${glyph(null)}<h4>${esc(title)}</h4><p>${esc(text)}</p>${primary(prompt, label)}</div></section>`;
/** 选了时间段、时间段内一份都没有:明说是时间段的缘故(不是「还没跑过」),给一键看全部。 */
export const rangeHero = (r) => `<section class="hero rise"${rise()}><div class="empty-state">${glyph(null)}<h4>所选时间段（${esc(rangeLabel(r))}）内没有这类报告</h4><p>时间段只筛选已经跑过的报告，不会替你去取数。要看这段时间的情况，在会话里对它做一次分析；或者先看全部报告。</p><button class="btn" data-range-clear>${I.cal}看全部报告</button></div></section>`;
export const errorHero = (error) => `<section class="hero"><div class="empty-state">${glyph(2)}<h4>报告读取失败</h4><p>${esc(error || '')}</p></div></section>`;
/** 索引里登记着、文件一份都取不到(reason 'gone',见 lib/data.js fetchFirst):说清楚是文件不在了,给一键重跑;extra 是另外的出路。 */
export const goneHero = (m, prompt, label, extra = '') => `<section class="hero rise"${rise()}><div class="empty-state">${glyph(2)}<h4>报告文件读取不到</h4><p>${esc(goneText(m))}</p>${primary(prompt, label)}${extra}</div></section>`;

/** 「巡检 09-22 14:03 ▾」/「时段体检 10-17 10:46 – 11:06 ▾」:点开列出最近的报告,选一份即切过去(data-pick)。 */
export const runPicker = (skill, entry, noun) => (entry ? `<button class="btn" data-run-menu="${skill}">${I.cal}${esc(noun)} ${esc(entry.mode === 'span' && entry.window ? windowLabel(entry.window) : stamp(entry.at))}${I.chev}</button>` : '');
export function openRunMenu(anchor, skill) {
  const m = getModel(skill); if (!m || !m.idx) return;
  // 健康检查列全部(此刻巡检与时段体检都在,时段体检写评估窗口);走势只算同一种口径,见 pages/health.js:model
  menu(anchor, `<div class="mh">${skill === 'wdr' ? '分析过的窗口' : '巡检报告'}，最近的在上</div>${[...(m.all || m.idx)].reverse().map((e) =>
    `<div class="mi ${m.entry && m.entry.file === e.file ? 'on' : ''}" data-pick="${esc(skill)}|${esc(e.file)}">${glyph(entryLevel(e))}<span class="num">${esc(entryLabel(e))}</span><span class="r">${esc(lname(entryLevel(e)))}</span></div>`).join('')}`, true);
}
