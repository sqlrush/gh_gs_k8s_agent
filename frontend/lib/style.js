// 页面风格:普通(R6「智能运维舱」)与文艺(R10「运维日报」)。
// 记在 localStorage「dash.style」,地址里 ?style=press|normal 可临时覆盖(便于分享与验收);
// 当前风格写在 <html data-style>,样式表据此生效。切换不刷新页面,由 shell 原地重画。

import { esc } from './fmt.js';

const KEY = 'dash.style';
export const STYLES = [['normal', '普通'], ['press', '文艺']];
const valid = (s) => STYLES.some(([k]) => k === s);

export function currentStyle() {
  const q = new URLSearchParams(location.search).get('style');
  if (valid(q)) return q;
  try { const s = localStorage.getItem(KEY); if (valid(s)) return s; } catch (_) { /* 隐私模式等 */ }
  return 'normal';
}
export function saveStyle(s) {
  if (!valid(s)) return;
  try { localStorage.setItem(KEY, s); } catch (_) { /* 忽略 */ }
  const u = new URL(location.href);
  if (u.searchParams.has('style')) { u.searchParams.delete('style'); history.replaceState(null, '', u); }
}
export const applyStyle = (s) => { document.documentElement.dataset.style = s; };

/** 顶部滑块:两档,点哪档切到哪档(data-style-to 由 shell 统一处理)。 */
export function styleSwitch(cur) {
  return `<div class="style-switch" role="radiogroup" aria-label="页面风格" data-on="${esc(cur)}">${STYLES.map(([k, l]) =>
    `<button type="button" role="radio" aria-checked="${k === cur}" data-style-to="${k}">${esc(l)}</button>`).join('')}<i class="thumb" aria-hidden="true"></i></div>`;
}
