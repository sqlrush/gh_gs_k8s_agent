// 界面原子:级别图标(四档 + 采集失败,形状各不相同)、状态与标签、线性图标、小部件。纯函数,返回 HTML 字符串。
// 两种风格共用;颜色由样式表按 data-lv 给,这里不写颜色。

import { esc } from './fmt.js';

const LVN = ['正常', '关注', '告警', '严重'];
/** 级别图标的原始 SVG(viewBox 0 0 16 16,填色用 currentColor);图形里内嵌时用。 */
export const G = {
  0: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="currentColor"/><path d="m5.1 8.1 2 2 3.9-4" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  1: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="currentColor"/><path d="M8 4.6v4.1" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="11.2" r=".95" fill="#fff"/></svg>',
  2: '<svg viewBox="0 0 16 16"><path d="M7.1 1.9a1 1 0 0 1 1.8 0l6.2 11.2a1 1 0 0 1-.9 1.5H1.8a1 1 0 0 1-.9-1.5z" fill="currentColor"/><path d="M8 6v3.6" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="11.8" r=".9" fill="#fff"/></svg>',
  3: '<svg viewBox="0 0 16 16"><path d="M5.2 1h5.6L15 5.2v5.6L10.8 15H5.2L1 10.8V5.2z" fill="currentColor"/><path d="m5.8 5.8 4.4 4.4m0-4.4-4.4 4.4" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/></svg>',
  na: '<svg viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.4" stroke="currentColor" stroke-width="1.5" stroke-dasharray="2.4 2"/><path d="M5.4 8h5.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
};
/** 级别 → 样式键:null/undefined = 采集失败('na')。 */
export const lk = (l) => (l === null || l === undefined ? 'na' : String(Math.max(0, Math.min(3, l | 0))));
export const lname = (l) => (l === null || l === undefined ? '采集失败' : LVN[l | 0] || '正常');
export const glyph = (l) => `<span class="glyph" data-lv="${lk(l)}">${G[lk(l)]}</span>`;
export const status = (l, text) => `<span class="status" data-lv="${lk(l)}">${glyph(l)}${esc(text ?? lname(l))}</span>`;
export const tag = (l, text) => `<span class="tag" data-lv="${lk(l)}">${glyph(l)}${esc(text ?? lname(l))}</span>`;

const sv = (d, w = 1.9) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
export const I = {
  chev: sv('<path d="m6 9 6 6 6-6"/>', 2),
  chat: sv('<path d="M4 5h16v11H9l-5 4z"/>'),
  redo: sv('<path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5"/>'),
  close: sv('<path d="M6 6l12 12M18 6 6 18"/>'),
  down: sv('<path d="M12 4v12m-5-5 5 5 5-5M5 20h14"/>'),
  info: sv('<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><circle cx="12" cy="8" r=".6" fill="currentColor"/>'),
  cal: sv('<rect x="3.5" y="5" width="17" height="15" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>', 1.8),
  search: sv('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>', 2),
  send: sv('<path d="M5 12h13M13 6l6 6-6 6"/>', 2.2),
  arrow: sv('<path d="M5 12h14M13 6l6 6-6 6"/>', 2),
  ext: sv('<path d="M8 16 16 8M9 8h7v7"/>', 2),
  plus: sv('<path d="M12 5v14M5 12h14"/>', 1.7),
  spark: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2.5c.5 4.6 2.9 7 7.5 7.5-4.6.5-7 2.9-7.5 7.5-.5-4.6-2.9-7-7.5-7.5 4.6-.5 7-2.9 7.5-7.5z"/><path d="M19 15.5c.2 1.8 1.1 2.7 2.9 2.9-1.8.2-2.7 1.1-2.9 2.9-.2-1.8-1.1-2.7-2.9-2.9 1.8-.2 2.7-1.1 2.9-2.9z" opacity=".7"/></svg>',
  up: '<svg viewBox="0 0 12 12" width="10" height="10"><path d="M6 2.5 10 8H2z" fill="currentColor"/></svg>',
  dn: '<svg viewBox="0 0 12 12" width="10" height="10"><path d="M6 9.5 2 4h8z" fill="currentColor"/></svg>',
  sortd: '<svg width="10" height="10" viewBox="0 0 12 12" style="margin-left:4px;vertical-align:-1px"><path d="M6 9.5 2 4h8z" fill="currentColor"/></svg>',
  pulse: sv('<path d="M3 12h4l2.5-6 4 12 2.5-6H21"/>', 1.7),
  list: sv('<path d="M4 6h16M4 12h11M4 18h6"/>', 1.7),
  clock: sv('<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3.5 2"/>', 1.7),
  book: sv('<path d="M5 4.5h10a3 3 0 0 1 3 3V20H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h10"/>', 1.7),
  db: sv('<ellipse cx="12" cy="5.5" rx="7.5" ry="3"/><path d="M4.5 5.5v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6"/><path d="M4.5 11.5v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6"/>', 1.8),
  code: sv('<path d="m8 8-4 4 4 4M16 8l4 4-4 4"/>', 1.8),
  grid: sv('<rect x="4" y="4" width="7" height="7" rx="2"/><rect x="13" y="4" width="7" height="7" rx="2"/><rect x="4" y="13" width="7" height="7" rx="2"/><rect x="13" y="13" width="7" height="7" rx="2"/>', 1.7),
};

/** 实例在页面上的名字:targets.json 的 label(如「10.0.0.9 / postgres」);报告里的 conn 是内部键,只在没有 label 时兜底。 */
export const instName = (ctx, conn) => (ctx && ctx.target && (ctx.target.label || ctx.target.conn)) || conn || '—';
export const tipAttr = (h) => ` data-tip="${esc(h)}"`;
/** 深挖:点击后在新标签页开一个已发出首句的 opencode 会话(lib/dig.js 统一处理)。 */
export const dig = (prompt, title) => ` data-dig="${esc(prompt)}"${title ? ` data-dig-title="${esc(title)}"` : ''}`;
export const primary = (prompt, label, title) => `<button class="btn primary"${dig(prompt, title)}>${I.chat}${esc(label)}</button>`;
/** 滚动数字:元素里放最终文字(关动效时就是它),入场时从 0 滚到它。 */
export const cu = (text) => `<span data-count>${esc(text)}</span>`;
