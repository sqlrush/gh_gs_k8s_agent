// 「在会话里深挖 →」:在新标签页打开 opencode Web 的一个新会话,首句已经发出去。
//
// 顺序不能改:**先在点击事件里同步开一个空标签页**,再去建会话。浏览器只放行用户手势里同步
// 发起的 window.open;等 fetch 回来再开,会被当弹窗拦掉。建会话失败就把那个空页关掉,
// 别留一个 about:blank 给用户。
//
// 这里没有任何创建 / 查询 Pod 的逻辑:请求经网关,网关的 ensure_ready 复用已有 Pod
//(在跑 → 直接用;缩到 0 → 拉起;没建过 → 才建)。前端不知道也不需要知道 Pod 的事。

import { toast } from './overlay.js';

// opencode Web 的项目路由 = base64url(工作目录);容器里工作目录固定是 /data/state/workspace
const WORKSPACE = '/data/state/workspace';
const b64url = (s) => btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
export let CHAT_HOME = '/' + b64url(WORKSPACE);
/** 网关在 /dash/api/me 里给出本运行模式下对话页的落地路径(Fuyao 沙箱的工作目录不同);形状不对就不用。 */
export function setChatHome(p) { if (typeof p === 'string' && /^\/[A-Za-z0-9_-]+$/.test(p)) CHAT_HOME = p; }

// 当前大盘的实例(targets.json 里的一项)。三个诊断大盘按实例分开,而深挖开的新会话还没登录:
// 首句不说是哪个库,模型只能反过来问用户。login 由存档时写入(如「登录 10.0.0.9 的 postgres 库」),
// 取不到就不加 —— 宁可让模型问,不猜。
let digTarget = null;
export function setDigTarget(t) { digTarget = t || null; }

export function withLogin(prompt, target = digTarget) {
  const login = target && target.login;
  if (!login) return prompt;
  return `请先${login}，然后${String(prompt).replace(/^请/, '')}`;
}

async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const text = await r.text();
  return text ? JSON.parse(text) : null;
}

/**
 * 深挖:建会话 + 发首句 + 跳过去(新标签页)。
 * @param {string} prompt 首句。**只放这条发现的数据**,不放任何平台配置取值。
 * @param {string} [title] 会话标题
 */
export async function openDig(prompt, title = '大盘深挖') {
  const tab = window.open('about:blank');          // 必须在手势里同步开
  try {
    const s = await post('/session', { title });
    if (!s || !s.id) throw new Error('会话没有 id');
    await post(`/session/${s.id}/prompt_async`, { parts: [{ type: 'text', text: withLogin(prompt) }] });
    const url = `${CHAT_HOME}/session/${s.id}`;
    if (tab) tab.location = url; else window.location.assign(url);
  } catch (e) {
    if (tab) tab.close();
    toast(`打开会话失败:${e && e.message || e}`);
  }
}

/** 深挖统一走事件委托:页面、抽屉、命令面板里任何带 data-dig 的元素都能点,重画后不必重新绑定。只挂一次。 */
export function installDig() {
  if (installDig.done) return;
  installDig.done = true;
  document.addEventListener('click', (ev) => {
    const el = ev.target.closest && ev.target.closest('[data-dig]');
    if (!el) return;
    ev.preventDefault();
    openDig(el.getAttribute('data-dig'), el.getAttribute('data-dig-title') || '大盘深挖');
  });
}
