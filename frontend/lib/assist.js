// ⌘J 智能体侧栏:带上当前大盘的上下文(实例、报告时间、发现数),给几条按报告生成的建议问题。
// 点建议或输入回车 = 在新标签页开一个已发出首句的 opencode 会话(lib/dig.js);侧栏里只记下问过什么,不在这里伪造回答。

import { esc } from './fmt.js';
import { I } from './ui.js';
import { openDig } from './dig.js';

let api = null, asked = [];
const panel = () => {
  let a = document.getElementById('assist');
  if (!a) { document.body.insertAdjacentHTML('beforeend', '<aside class="assist" id="assist" aria-label="智能体"></aside>'); a = document.getElementById('assist'); }
  return a;
};

function draw() {
  const c = api.context();
  panel().innerHTML = `<div class="ah"><span class="orb">${I.spark}</span><div><b>智能体</b><small>基于当前大盘的上下文</small></div><button class="icon-btn" data-assist-close aria-label="关闭" style="margin-left:auto">${I.close}</button></div>
    <div class="ab"><div class="ctx">${c.ctx.map((x) => `<span>${esc(x)}</span>`).join('')}</div>
      <div class="hello">你好，${esc(api.who() || '')}<br><span>想先弄清哪件事？</span></div>
      <p class="lead">问题会带着当前报告的上下文，在新标签页里开一个 opencode 会话来回答。</p>
      <div class="sugg">${c.sugg.map(([t, p]) => `<button data-assist-ask="${esc(p)}">${I.spark}<span>${esc(t)}</span></button>`).join('')}</div>
      ${asked.length ? `<div class="sugg-h">刚才问过（已在新标签页打开）</div>${asked.slice(-5).reverse().map((q) => `<div class="msg me">${esc(q)}</div>`).join('')}` : ''}</div>
    <div class="af"><form class="askbox" data-assist-form>${I.spark}<input name="q" autocomplete="off" placeholder="输入问题，回车在会话里问"><button class="send" aria-label="发送">${I.send}</button></form><div class="hint">回答由你本人的 opencode 会话生成 · 级别以脚本判定为准</div></div>`;
}
function ask(q, prompt) {
  if (!q) return;
  openDig(prompt || api.askPrefix() + q, '大盘提问');          // 必须在手势里同步调用:它先开空标签页
  asked = [...asked, q];
  draw();
}
export function open() { draw(); panel().classList.add('on'); setTimeout(() => { const i = panel().querySelector('input'); if (i) i.focus({ preventScroll: true }); }, 300); }
export const close = () => { const a = document.getElementById('assist'); if (a) a.classList.remove('on'); };
const isOpen = () => !!(document.getElementById('assist') && document.getElementById('assist').classList.contains('on'));
export const toggle = () => (isOpen() ? close() : open());

/** 只挂一次。a = { context() → {ctx:[..], sugg:[[标题, 首句]]}, askPrefix(), who() } */
export function install(a) {
  api = a;
  if (install.done) return;
  install.done = true;
  document.addEventListener('click', (ev) => {
    const t = ev.target.closest && ev.target.closest('[data-assist-ask],[data-assist-close]');
    if (!t) return;
    ev.preventDefault();
    if (t.dataset.assistAsk !== undefined) ask(t.textContent.trim(), t.dataset.assistAsk); else close();
  });
  document.addEventListener('submit', (ev) => {
    const f = ev.target.closest && ev.target.closest('[data-assist-form]');
    if (!f) return;
    ev.preventDefault();
    const i = f.querySelector('input'), q = i.value.trim();
    i.value = '';
    ask(q);
  });
  document.addEventListener('keydown', (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'j') { ev.preventDefault(); toggle(); }
    else if (ev.key === 'Escape' && isOpen()) close();
  });
}
