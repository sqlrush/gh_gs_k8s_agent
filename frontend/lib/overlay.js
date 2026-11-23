// 浮层:右侧抽屉、提示条(toast)、悬停提示(data-tip)、下拉菜单。两种风格共用同一套元素,外观由样式表给。
// 元素按需建在 <body> 末尾;页面整页重画时不受影响。

import { esc } from './fmt.js';
import { I } from './ui.js';

const $ = (s) => document.querySelector(s);
function el(id, tag, cls, attrs = '') {
  let e = document.getElementById(id);
  if (!e) { document.body.insertAdjacentHTML('beforeend', `<${tag} id="${id}" class="${cls}"${attrs}></${tag}>`); e = document.getElementById(id); }
  return e;
}

/** 右侧抽屉。title / mark / body / foot 都是 HTML。 */
export function drawer(title, mark, body, foot) {
  const d = el('drawer', 'aside', 'drawer', ' role="dialog" aria-modal="true"');
  d.innerHTML = `<div class="dh"><h2>${title}</h2>${mark || ''}<button class="icon-btn" data-close aria-label="关闭">${I.close}</button></div><div class="db">${body}</div>${foot ? `<div class="df">${foot}</div>` : ''}`;
  el('scrim', 'div', 'scrim').classList.add('on');
  requestAnimationFrame(() => d.classList.add('on'));
}
export function closeDrawer() {
  const d = $('#drawer'), s = $('#scrim'), m = $('#menu');
  if (d) d.classList.remove('on');
  if (s) s.classList.remove('on');
  if (m) m.classList.remove('on');
}
export const drawerOpen = () => !!($('#drawer') && $('#drawer').classList.contains('on'));

let toastTimer = null;
export function toast(msg) {
  const t = el('toast', 'div', 'toast');
  t.innerHTML = `${I.info}<span>${esc(msg)}</span>`;
  t.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('on'), 4000);
}

/** 挂在 anchor 下方的菜单;right=true 时右对齐。 */
export function menu(anchor, html, right) {
  const m = el('menu', 'div', 'menu'), r = anchor.getBoundingClientRect();
  m.innerHTML = html;
  m.classList.add('on');
  m.style.top = r.bottom + 6 + window.scrollY + 'px';
  m.style.left = (right ? r.right - m.offsetWidth : r.left) + window.scrollX + 'px';
}
export const closeMenu = () => { const m = $('#menu'); if (m) m.classList.remove('on'); };

/** 悬停提示:任何带 data-tip 的元素,内容是 HTML(生成时已转义)。只挂一次。 */
export function installTips() {
  if (installTips.done) return;
  installTips.done = true;
  const tip = el('tip', 'div', 'tip');
  document.addEventListener('mousemove', (ev) => {
    const t = ev.target.closest && ev.target.closest('[data-tip]');
    if (!t) { tip.classList.remove('on'); return; }
    tip.innerHTML = t.dataset.tip;
    tip.style.left = Math.min(ev.clientX + 14, window.innerWidth - tip.offsetWidth - 12) + 'px';
    tip.style.top = Math.min(ev.clientY + 16, window.innerHeight - tip.offsetHeight - 12) + 'px';
    tip.classList.add('on');
  });
}
