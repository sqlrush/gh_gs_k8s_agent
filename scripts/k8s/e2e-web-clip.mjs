// 网页剪藏端到端的浏览器那一半:经 Chrome DevTools 协议打开书签安装页、登录模拟 Wiki、点书签、在剪藏窗口里点「导入」。
// 用法: node scripts/k8s/e2e-web-clip.mjs <门户地址(模拟 SSO)> <Wiki 地址> <截图目录>
// 「点书签」= 在 Wiki 页面里执行安装页生成的那段 javascript: 代码(带用户手势,弹窗才放行)—— 与点书签栏上的书签等价。
// 最后一行输出 JSON(各步结果、批次、会话号),由 e2e-web-clip.sh 判定。CDP 用随机端口(9333 是别的项目的 Chrome)。
import { spawn } from 'node:child_process';
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';

const [PORTAL, WIKI, OUT] = process.argv.slice(2);
const PROFILE = '/tmp/cdp-webclip';
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
rmSync(PROFILE, { recursive: true, force: true });
mkdirSync(OUT, { recursive: true });
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--remote-debugging-port=0', `--user-data-dir=${PROFILE}`,
  '--window-size=1400,1000', '--no-first-run', 'about:blank'], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function devport() {
  for (let i = 0; i < 80; i++) {
    try { return readFileSync(PROFILE + '/DevToolsActivePort', 'utf8').split('\n')[0]; } catch (_) { /* 还没起来 */ }
    await sleep(250);
  }
  throw new Error('chrome 没起来');
}
const targets = async (port) => (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
async function findPage(port, pred) {
  for (let i = 0; i < 60; i++) {
    const t = (await targets(port)).find((x) => x.type === 'page' && pred(x));
    if (t) return t;
    await sleep(250);
  }
  return null;
}

class Tab {
  constructor(url) { this.url = url; this.id = 0; this.pending = new Map(); this.errors = []; }
  async open() {
    this.ws = new WebSocket(this.url);
    await new Promise((ok, no) => { this.ws.addEventListener('open', ok); this.ws.addEventListener('error', no); });
    this.ws.addEventListener('message', (m) => {
      const d = JSON.parse(m.data);
      if (d.id && this.pending.has(d.id)) {
        const p = this.pending.get(d.id); this.pending.delete(d.id);
        if (d.error) p.no(new Error(d.error.message)); else p.ok(d.result);
      }
      if (d.method === 'Runtime.exceptionThrown') {
        const x = d.params.exceptionDetails; this.errors.push(x.exception ? x.exception.description : x.text);
      }
      if (d.method === 'Page.javascriptDialogOpening') {
        this.errors.push('对话框:' + d.params.message);
        this.send('Page.handleJavaScriptDialog', { accept: true });
      }
    });
    await this.send('Page.enable'); await this.send('Runtime.enable');
    return this;
  }
  send(method, params = {}) {
    return new Promise((ok, no) => { const i = ++this.id; this.pending.set(i, { ok, no }); this.ws.send(JSON.stringify({ id: i, method, params })); });
  }
  async ev(expr, gesture = false) {
    const r = await this.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true, userGesture: gesture });
    if (r.exceptionDetails) {
      const x = r.exceptionDetails; throw new Error((x.exception ? x.exception.description : x.text) + ' @ ' + expr.slice(0, 60));
    }
    return r.result.value;
  }
  async go(url) { await this.send('Page.navigate', { url }); await sleep(1500); }
  async shot(name) {
    const r = await this.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(`${OUT}/${name}.png`, Buffer.from(r.data, 'base64'));
  }
  async until(expr, ms = 30000, step = 500) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      try { const v = await this.ev(expr); if (v) return v; } catch (_) { /* 导航中 */ }
      await sleep(step);
    }
    return null;
  }
}

const res = { steps: [], batch: '', session: '' };
const step = (name, ok, detail = '') => { res.steps.push({ name, ok: !!ok, detail }); console.log(ok ? '  ✓' : '  ✗', name, detail); };
const text = (id) => `(document.getElementById(${JSON.stringify(id)}) || {}).textContent || ''`;

try {
  const port = await devport();
  const a = await new Tab((await findPage(port, () => true)).webSocketDebuggerUrl).open();

  await a.go(PORTAL + '/kb-import/clip/install');
  const code = await a.until(`(document.getElementById('code') || {}).value`, 15000);
  const state = await a.ev(text('state'));
  step('安装页按本系统地址生成书签代码', code && code.startsWith('javascript:') && decodeURIComponent(code).includes(JSON.stringify(PORTAL)),
    `${(code || '').length} 字符`);
  step('安装页列出允许剪藏的网站', state.includes(new URL(WIKI).origin), state.slice(0, 80));
  await a.shot('1-install');

  await a.go(WIKI + '/display/OPS/lock-incident');
  step('没登录被带到登录页', (await a.ev('location.pathname')) === '/login.action');
  await a.ev(`document.getElementById('user').value = 'ops'; document.getElementById('password').value = 'ops-wiki';
    document.querySelector('form').submit(); true`);
  const title = await a.until(`document.title.includes('事件复盘') && document.title`, 15000);
  step('登录后打开事件复盘页', !!title, title || '');
  await a.shot('2-wiki');

  await a.ev(decodeURIComponent(code.slice('javascript:'.length)), true);
  const pop = await findPage(port, (t) => t.url.startsWith(PORTAL + '/kb-import/clip'));
  step('点书签弹出剪藏窗口', !!pop, pop ? pop.url : '');
  if (!pop) throw new Error('没有剪藏窗口');
  const b = await new Tab(pop.webSocketDebuggerUrl).open();
  const n = await b.until(`!document.getElementById('card').hidden && ${text('n')}`, 60000);
  step('剪藏窗口收到网页', !!n, `${await b.ev(text('t'))} | ${await b.ev(text('u'))} | ${n}`);
  step('同源截图取回、跨源图片写明没取回', /图片 1 张/.test(n || '') && /另有 1 张没取回/.test(n || ''), n || '');
  await b.shot('3-popup');

  await b.ev(`document.getElementById('go').click(); true`, true);
  const msg = await b.until(`(() => { const m = ${text('msg')}; return m && !m.startsWith('正在导入') ? m : ''; })()`, 240000, 1000);
  step('导入成功并在导入环境里新开会话', /已存进.*新开会话/.test(msg || ''), msg || await b.ev(text('msg')));
  res.batch = ((msg || '').match(/批次 (clip-[0-9A-Za-z-]+)/) || [])[1] || '';
  const url = await b.until(`location.pathname.includes('/session/') && location.href`, 20000);
  step('剪藏窗口跳到那个会话', !!url, url || '');
  res.session = ((url || '').match(/\/session\/([A-Za-z0-9_-]+)/) || [])[1] || '';
  await sleep(8000);
  await b.shot('4-session');
  step('会话页打开的是导入环境的 opencode(不是错误页)', await b.ev(`!/无法|失败|不是知识库管理员/.test(document.body.innerText.slice(0, 300))`));
  res.errors = [...a.errors, ...b.errors].filter((e) => !/favicon/.test(e));
} catch (e) {
  step('异常', false, String((e && e.stack) || e));
} finally {
  chrome.kill('SIGKILL');
}
console.log(JSON.stringify(res));
