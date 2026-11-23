#!/usr/bin/env node
// 四个大盘的真浏览器验收:经「模拟 SSO」打开真集群里的大盘,逐页查、截图、切实例、点深挖。
//
// 为什么不是 --dump-dom:那只能看到最终 HTML。这里要的是
//   · 控制台错误与未捕获异常(页面脚本炸了,dump-dom 仍能返回半截 HTML);
//   · 每个失败的请求(大盘是浏览器自己取数,一个 404 就是一块空白);
//   · 页面上漏出来的 undefined / NaN / [object Object] —— 单测与契约守卫都看不见这类;
//   · 手机宽度下有没有横向滚动;
//   · 切实例、切页签、点「深挖」这些要真点的交互,以及深挖开出的新标签页落在哪、首句发没发。
//
// 零依赖:Node 22+ 自带 WebSocket,直接说 Chrome DevTools 协议。
//   BASE=http://127.0.0.1:18090 OUT=/tmp/dash-shots DIG=1 node scripts/k8s/dash-browser-check.mjs
// DIG=1 才点深挖(会真建一个会话、真调一次模型)。
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BASE = (process.env.BASE || 'http://127.0.0.1:18090').replace(/\/+$/, '');
const OUT = process.env.OUT || '/tmp/dash-shots';
const CH = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const DIG = process.env.DIG === '1';
const PORT = +(process.env.CDP_PORT || 0);      // 0 = Chrome 自选空闲端口
const PAGES = {
  health: ['hd-head', 'hd-ring', 'hd-band', 'hd-sub', 'hd-findings', 'hd-dims', 'hd-history', 'hd-trend'],
  topsql: ['ts-tabs', 'ts-kpis', 'ts-stack', 'ts-rows', 'ts-cards'],
  wdr: ['wd-head', 'wd-kpis', 'wd-dbt', 'wd-verdict', 'wd-tsql', 'wd-waits', 'wd-small', 'wd-history'],
  kb: ['kb-head', 'kb-top', 'kb-stores', 'kb-health', 'kb-misses', 'kb-queries'],
};
// 已知会 404、且页面按「没有」处理的请求:没调优过的 SQL 没有 sqltune 报告;EMPTY 模式(新工号)下所有报告都还没有
const EMPTY = process.env.EMPTY === '1';
const EXPECTED_404 = EMPTY ? [/\/reports\//] : [/\/reports\/sqltune\//];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const problems = [];
const bad = (where, what) => { problems.push(`${where}: ${what}`); console.log(`  ✗ ${what}`); };
const good = (what) => console.log(`  ✓ ${what}`);

// ---------------------------------------------------------------- CDP 客户端
class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.wait = new Map(); this.subs = [];
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && this.wait.has(m.id)) {
        const { ok, no } = this.wait.get(m.id); this.wait.delete(m.id);
        m.error ? no(new Error(`${m.error.message}`)) : ok(m.result);
      } else if (m.method) this.subs.forEach((f) => f(m));
    };
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    return new Promise((ok, no) => this.wait.set(id, { ok, no }));
  }
  on(f) { this.subs.push(f); }
}

async function launch() {
  // 端口让 Chrome 自己挑(0),再从它自己的数据目录里读 DevToolsActivePort ——
  // 写死端口的第一版连上过这台机器上**别人**的 Chrome(另一个项目占着 9333):
  // 页面开在了人家的浏览器里,还读到人家的缓存,报出一个莫名其妙的旧会话 404。
  const dir = mkdtempSync(join(tmpdir(), 'dash-chrome-'));
  const proc = spawn(CH, ['--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${dir}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu', 'about:blank'], { stdio: 'ignore' });
  for (let i = 0; i < 50; i++) {
    try {
      const port = PORT || readFileSync(join(dir, 'DevToolsActivePort'), 'utf8').split('\n')[0].trim();
      const v = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
      const ws = new WebSocket(v.webSocketDebuggerUrl);
      await new Promise((ok, no) => { ws.onopen = ok; ws.onerror = no; });
      return { cdp: new CDP(ws), stop: async () => {
        proc.kill();
        await new Promise((r) => { proc.once("exit", r); setTimeout(r, 3000); });
        try { rmSync(dir, { recursive: true, force: true }); } catch { /* Chrome 偶尔晚一步松手,留给系统清 */ }
      } };
    } catch { await sleep(200); }
  }
  proc.kill(); throw new Error('Chrome 起不来');
}

// 一个标签页:记下它的异常、控制台错误、失败请求与在途请求数(判断「取数取完了」)
async function openTab(cdp, sessionId) {
  const tab = { sessionId, errors: [], failed: [], inflight: new Set(), loaded: false, urls: new Map() };
  cdp.on((m) => {
    if (m.sessionId !== sessionId) return;
    const p = m.params || {};
    if (m.method === 'Runtime.exceptionThrown') tab.errors.push(`未捕获异常:${p.exceptionDetails?.exception?.description || p.exceptionDetails?.text}`);
    if (m.method === 'Runtime.consoleAPICalled' && p.type === 'error') tab.errors.push(`console.error:${(p.args || []).map((a) => a.value ?? a.description).join(' ')}`);
    if (m.method === 'Network.requestWillBeSent') { tab.inflight.add(p.requestId); tab.urls.set(p.requestId, p.request?.url || ''); }
    if (m.method === 'Network.loadingFinished' || m.method === 'Network.loadingFailed') tab.inflight.delete(p.requestId);
    // 带上地址:只有错误文字时根本查不出是哪个请求(2026-10-14 对话页 ERR_INVALID_HTTP_RESPONSE 就卡在这)
    if (m.method === 'Network.loadingFailed' && !p.canceled) tab.failed.push(`请求失败 ${p.errorText} ${(tab.urls.get(p.requestId) || '?').replace(BASE, '')}`);
    if (m.method === 'Network.responseReceived' && p.response.status >= 400) {
      const u = p.response.url;
      if (!(p.response.status === 404 && EXPECTED_404.some((re) => re.test(u)))) tab.failed.push(`HTTP ${p.response.status} ${u.replace(BASE, '')}`);
    }
    if (m.method === 'Page.loadEventFired') tab.loaded = true;
  });
  for (const d of ['Page', 'Runtime', 'Network']) await cdp.send(`${d}.enable`, {}, sessionId);
  return tab;
}

async function idle(tab, maxMs = 20000) {
  const t0 = Date.now(); let quiet = 0;
  while (Date.now() - t0 < maxMs) {
    if (tab.loaded && tab.inflight.size === 0) { quiet += 100; if (quiet >= 1200) return; } else quiet = 0;
    await sleep(100);
  }
}

async function evaluate(cdp, tab, expr) {
  const r = await cdp.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true, userGesture: true }, tab.sessionId);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}

async function go(cdp, tab, url) { tab.loaded = false; await cdp.send('Page.navigate', { url }, tab.sessionId); await idle(tab); }

async function shot(cdp, tab, name) {
  const r = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true }, tab.sessionId);
  writeFileSync(join(OUT, name + '.png'), Buffer.from(r.data, 'base64'));
}

async function viewport(cdp, tab, w, h, mobile = false) {
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile }, tab.sessionId);
}

// 页面里跑的体检:区块非空、漏出来的坏值、横向溢出
const INSPECT = (ids) => `(() => {
  const blocks = {};
  for (const id of ${JSON.stringify(ids)}) { const el = document.getElementById(id); blocks[id] = el ? (((el.innerText ?? el.textContent) || '').trim().length || el.childElementCount) : -1; }
  const text = document.body.innerText;
  const leaks = [];
  for (const re of [/\\bundefined\\b/g, /\\bNaN\\b/g, /\\[object Object\\]/g, /\\bnull\\b/g, /Invalid Date/g, /Infinity/g, /\\bnone\\b/g, /https?:\\/\\/\\S+/g, /\\/(?:[A-Za-z0-9_-]+\\/){3,}/g]) {
    let m; while ((m = re.exec(text))) leaks.push(text.slice(Math.max(0, m.index - 30), m.index + 30).replace(/\\s+/g, ' '));
  }
  const failed = text.includes('页面加载失败') ? text.slice(text.indexOf('页面加载失败'), text.indexOf('页面加载失败') + 120) : '';
  return { blocks, leaks, failed, overflow: document.documentElement.scrollWidth - window.innerWidth,
           digs: document.querySelectorAll('[data-dig]').length,
           digTexts: [...document.querySelectorAll('[data-dig]')].filter((a) => !a.closest('.empty')).map((a) => a.dataset.dig),
           inst: [...document.querySelectorAll('#inst option')].map((o) => ({ v: o.value, t: o.textContent })),
           allNa: (() => { const p = [...document.querySelectorAll('#hd-dims .pill')]; return p.length > 0 && p.every((x) => x.textContent === '不可用'); })(),
           badge: ((document.querySelector('#hd-head .badge, #wd-head .badge') || {}).textContent || '') };
})()`;

function report(where, tab, r, ids) {
  const empty = ids.filter((id) => r.blocks[id] <= 0);
  empty.length ? bad(where, `缺/空区块 ${empty.join(',')}`) : good(`${ids.length} 个区块都有内容`);
  r.failed ? bad(where, `页面加载失败:${r.failed}`) : null;
  if (r.allNa) (r.badge === '正常' ? bad(where, '所有维度都没采到,标题却显示「正常」') : good(`所有维度都没采到,标题显示「${r.badge}」而不是「正常」`));
  r.leaks.length ? bad(where, `页面上漏出坏值:${[...new Set(r.leaks)].slice(0, 4).join(' | ')}`) : good('没有 undefined/NaN/null 漏到页面上');
  // 所有深挖入口的首句都带报告的时间(用户 2026-10-19 提;口径见 frontend/lib/prompts.js)
  if (r.digTexts && r.digTexts.length && /^(health|topsql|wdr)/.test(where)) {
    const miss = r.digTexts.filter((t) => !/\d{2}-\d{2} \d{2}:\d{2}/.test(t));
    miss.length ? bad(where, `${miss.length}/${r.digTexts.length} 个深挖入口首句没带报告时间:${miss[0].slice(0, 60)}`)
      : good(`${r.digTexts.length} 个深挖入口首句都带报告时间`);
  }
  tab.errors.length ? bad(where, `脚本错误 ${tab.errors.slice(0, 3).join(' | ')}`) : good('控制台零错误');
  tab.failed.length ? bad(where, `失败请求 ${[...new Set(tab.failed)].slice(0, 5).join(' | ')}`) : good('没有失败的请求');
  tab.errors.length = 0; tab.failed.length = 0;
}

// ---------------------------------------------------------------- 各项检查
// 新工号、什么都没跑过:四页都应是「还没有…报告」+ 引导去会话里跑一次,不能报错、不能空白
async function checkEmptyPages(cdp, tab) {
  for (const name of Object.keys(PAGES)) {
    console.log(`\n▸ ${name}(空状态)`);
    await viewport(cdp, tab, 1440, 900);
    await go(cdp, tab, `${BASE}/dash/${name}`);
    const r = await evaluate(cdp, tab, `(() => { const e = document.querySelector(".empty"); return { empty: e ? e.innerText.replace(/\\s+/g, " ").slice(0, 80) : "", dig: document.querySelectorAll(".empty [data-dig]").length, text: document.body.innerText } })()`);
    r.empty ? good(`空状态卡:${r.empty}`) : bad(name, "新工号打开没有空状态提示");
    r.dig ? good("有「在会话里跑一次」引导") : bad(name, "空状态没有引导链接");
    /加载失败|undefined|NaN/.test(r.text) ? bad(name, "空状态页面有报错字样") : good("没有报错字样");
    tab.errors.length ? bad(name, `脚本错误 ${tab.errors.slice(0, 3).join(" | ")}`) : good("控制台零错误");
    tab.failed.length ? bad(name, `失败请求 ${[...new Set(tab.failed)].slice(0, 5).join(" | ")}`) : good("除「还没有报告」的 404 外没有失败请求");
    tab.errors.length = 0; tab.failed.length = 0;
    await shot(cdp, tab, `${name}-empty`);
  }
}

async function checkPages(cdp, tab) {
  for (const [name, ids] of Object.entries(PAGES)) {
    console.log(`\n▸ ${name}`);
    await viewport(cdp, tab, 1440, 900);
    await go(cdp, tab, `${BASE}/dash/${name}`);
    const r = await evaluate(cdp, tab, INSPECT(ids));
    report(name, tab, r, ids);
    await shot(cdp, tab, `${name}-desktop`);
    if (name !== 'kb') {
      r.inst.length ? good(`侧栏实例 ${r.inst.map((o) => o.t).join(' ; ')}`) : bad(name, '侧栏没有实例下拉');
      if (r.inst.some((o) => /^api\//.test(o.t))) bad(name, `侧栏显示的是内部连接名,不是可读实例名:${r.inst[0].t}`);
      for (const o of r.inst.slice(1).concat(r.inst.slice(0, 1))) {          // 切到每一个,最后切回第一个
        await evaluate(cdp, tab, `(() => { const s = document.getElementById('inst'); s.value = ${JSON.stringify(o.v)}; s.dispatchEvent(new Event('change')); })()`);
        await sleep(300); await idle(tab);
        const head = await evaluate(cdp, tab, `(document.querySelector('.meta') || {}).innerText || ''`);
        const label = o.t.split(' · ')[0];
        head.includes(label) ? good(`切到 ${label}:页头目标一致`) : bad(name, `切到 ${label} 后页头写的是「${head.slice(0, 60)}」`);
        const rr = await evaluate(cdp, tab, INSPECT(ids)); report(`${name}@${label}`, tab, rr, ids);
        if (r.inst.length > 1) await shot(cdp, tab, `${name}-${o.v}`);
      }
    }
    if (name === 'health') {
      // 局部运行(--include)曾被当成一次巡检存档,大盘只剩一张卡(user 2026-09-23 反馈)
      const nd = await evaluate(cdp, tab, `document.querySelectorAll('#hd-dims > .card').length`);
      nd >= 8 ? good(`维度卡 ${nd} 张`) : bad('health', `维度卡只有 ${nd} 张(完整巡检是 8 张)`);
    }
    if (name === 'topsql') {
      // 五个页签都要能点:取过的出榜单,没取过的出「还没取过」+ 取数入口(user 反馈过灰字点不动像是数据丢了)
      const tabs = await evaluate(cdp, tab, `[...document.querySelectorAll('#ts-tabs [data-by]')].map((e) => ({ b: e.dataset.by, taken: !e.textContent.includes('未取') }))`);
      tabs.length === 5 ? good('五个页签都在') : bad('topsql', `页签只有 ${tabs.length} 个`);
      for (const { b, taken } of tabs) {
        await evaluate(cdp, tab, `document.querySelector('#ts-tabs [data-by="${b}"]').click()`);
        await sleep(300); await idle(tab);
        const st = await evaluate(cdp, tab, `({ on: (document.querySelector('#ts-tabs .on') || {}).dataset?.by, rows: document.querySelectorAll('#ts-rows tr').length, empty: !!document.querySelector('.empty [data-dig]') })`);
        if (st.on !== b) bad('topsql', `页签 ${b} 点了之后没切过去(on=${st.on})`);
        else if (taken) st.rows > 1 ? good(`页签 ${b}:${st.rows - 1} 行`) : bad('topsql', `页签 ${b} 有数据却没出榜单`);
        else st.empty ? good(`页签 ${b}:未取过,给了取数入口`) : bad('topsql', `页签 ${b} 未取过,却没有取数入口`);
      }
      await evaluate(cdp, tab, `document.querySelector('#ts-tabs [data-by="time"]').click()`); await sleep(300); await idle(tab);
      // 调优入口只有一种说法
      const tl = await evaluate(cdp, tab, `document.body.innerText.includes('在会话里调优这条')`);
      tl ? bad('topsql', '还有「在会话里调优这条」这种第二种说法') : good('调优入口说法统一');
    }
    if (name === 'kb') {
      // 四个页签:案例 / 条款 / 关系图直接展示知识库原文(user 2026-09-23 定:不绕到会话里)
      const click = async (sel) => { await evaluate(cdp, tab, `(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return false; e.click(); return true; })()`); await sleep(250); };
      const txt = (sel) => evaluate(cdp, tab, `((document.querySelector(${JSON.stringify(sel)}) || {}).innerText || '').trim()`);
      const cnt = (sel) => evaluate(cdp, tab, `document.querySelectorAll(${JSON.stringify(sel)}).length`);
      const tabsN = await cnt('#kb-tabs [data-tab]');
      tabsN === 4 ? good('知识库四个页签都在') : bad('kb', `页签只有 ${tabsN} 个`);
      await click('#kb-tabs [data-tab="cases"]');
      const nCases = await cnt('.kb-item[data-case]');
      nCases > 0 ? good(`案例列表 ${nCases} 条`) : bad('kb', '案例页签没有列表');
      const t1 = await txt('.kb-doc h1');
      if (nCases > 1) {
        await click('.kb-item[data-case]:nth-of-type(2)');
        const t2 = await txt('.kb-doc h1');
        t2 && t2 !== t1 ? good(`点第二条,右边原文换成「${t2.slice(0, 20)}」`) : bad('kb', '点列表里另一条,右边原文没变');
      }
      const secs = await cnt('.kb-doc .kb-sec p');
      secs >= 3 ? good(`案例原文 ${secs} 段`) : bad('kb', `案例原文只有 ${secs} 段(现场/判断/处置至少 3 段)`);
      await evaluate(cdp, tab, `(() => { const i = document.querySelector('.kb-q'); i.value = 'zzz-不存在的词'; i.dispatchEvent(new Event('input', { bubbles: true })); })()`); await sleep(250);
      (await cnt('.kb-item[data-case]')) === 0 ? good('搜索能过滤(不存在的词 → 0 条)') : bad('kb', '搜索没有过滤');
      const focused = await evaluate(cdp, tab, `document.activeElement && document.activeElement.classList.contains('kb-q')`);
      focused ? good('输入时搜索框不丢焦点') : bad('kb', '每输一个字搜索框就失去焦点');
      await click('#kb-tabs [data-tab="rules"]');
      const nRules = await cnt('.kb-item[data-rule]');
      nRules > 0 ? good(`条款列表 ${nRules} 条`) : bad('kb', '条款页签没有列表');
      const refCase = await cnt('.kb-doc .kb-ref[data-case]');
      if (refCase) {
        await click('.kb-doc .kb-ref[data-case]');
        const on = await evaluate(cdp, tab, `(document.querySelector('#kb-tabs .on') || {}).dataset?.tab`);
        on === 'cases' ? good('条款里点「引用它的案例」跳到了案例') : bad('kb', `点引用案例后页签是 ${on}`);
      }
      await click('#kb-tabs [data-tab="graph"]');
      const nNodes = await cnt('#kb-graph .kb-node');
      nNodes > 0 ? good(`关系图 ${nNodes} 个节点`) : bad('kb', '关系图没有节点');
      await evaluate(cdp, tab, `document.querySelector('#kb-graph .kb-node[data-node^="case:"]').dispatchEvent(new MouseEvent('click', { bubbles: true }))`); await sleep(250);
      const panel = await cnt('.kb-gpanel'), dimmed = await cnt('#kb-graph .kb-node.dim');
      panel && dimmed ? good(`点节点:弹出详情,其余 ${dimmed} 个淡化`) : bad('kb', `点节点后详情 ${panel} 个、淡化 ${dimmed} 个`);
      await click('.kb-gpanel .kb-btn[data-case]');
      const on2 = await evaluate(cdp, tab, `(document.querySelector('#kb-tabs .on') || {}).dataset?.tab`);
      on2 === 'cases' ? good('详情里「看案例原文」跳到了案例') : bad('kb', `「看案例原文」后页签是 ${on2}`);
      await shot(cdp, tab, 'kb-cases');
      await click('#kb-tabs [data-tab="graph"]'); await shot(cdp, tab, 'kb-graph');
      await click('#kb-tabs [data-tab="overview"]');
      tab.errors.length ? bad('kb', `知识库页签交互有脚本错误 ${tab.errors.slice(0, 2).join(' | ')}`) : good('知识库页签交互控制台零错误');
      tab.errors.length = 0;
    }
    if (name === 'wdr') {
      const href = await evaluate(cdp, tab, `(document.querySelector('a[href*=".native.html"]') || {}).href || ''`);
      if (href) {
        const st = await evaluate(cdp, tab, `fetch(${JSON.stringify(href)}).then(async (r) => [r.status, (await r.text()).length])`);
        st[0] === 200 && st[1] > 1000 ? good(`原生 WDR 下载 200,${st[1]} 字节`) : bad('wdr', `原生 WDR 下载 ${st}`);
      } else {
        const gen = await evaluate(cdp, tab, `(document.getElementById("wd-head") || {}).innerText || ""`);
        gen.includes("已生成") ? bad("wdr", "原生 WDR 已生成,却没有下载链接") : console.log("  · 本份 WDR 没有生成原生报告(页面已写明)");
      }
    }
    // PC 是主场景:常见窗口宽度逐个查(1366 笔记本开 125% 缩放 ≈ 1090,分屏半边 ≈ 960)
    for (const w of [1920, 1440, 1280, 1024]) {
      await viewport(cdp, tab, w, 900);
      await go(cdp, tab, `${BASE}/dash/${name}`);
      // 页面是取完数才画的:等外层卡片出现再量,否则量到空页(2026-09-24 误报过「最窄卡片 null」)
      for (let i = 0; i < 50 && !(await evaluate(cdp, tab, `document.querySelectorAll('#main .card, #main .kpi').length`)); i++) await sleep(100);
      const pc = await evaluate(cdp, tab, `(() => { const m = document.getElementById('main').getBoundingClientRect(); const cw = [...document.querySelectorAll('.card, .kpi')].filter((e) => !e.parentElement.closest('.card')).map((e) => e.getBoundingClientRect().width).filter((x) => x > 0); const clipped = [...document.querySelectorAll('.kpi .v')].filter((e) => e.scrollWidth > e.clientWidth + 1).map((e) => e.textContent.trim()); return { over: document.documentElement.scrollWidth - innerWidth, right: Math.round(m.right), minCard: Math.round(Math.min(...cw)), clipped }; })()`);
      // 卡片里的小数字块本来就窄(设计如此),只量外层卡片;真正要防的是数字被截断
      pc.over > 1 || pc.right > w + 1 || pc.minCard < 200 || pc.clipped.length ? bad(name, `PC ${w} 宽布局有问题:横向溢出 ${pc.over}px、主区右边 ${pc.right}px、最窄卡片 ${pc.minCard}px、被截断的数字 ${pc.clipped.join(' | ')}`) : good(`PC ${w} 宽:无横向滚动,最窄卡片 ${pc.minCard}px,数字无截断`);
      tab.errors.length = 0; tab.failed.length = 0;
      await shot(cdp, tab, `${name}-pc${w}`);
    }
    // 窄屏只做兜底:不崩、不横向滚动
    await viewport(cdp, tab, 390, 844, true);
    await go(cdp, tab, `${BASE}/dash/${name}`);
    // 与上面 PC 那段同一个等待:没等卡片出现就量,健康检查页(取数最多)会量到「加载中…」的空页,
    // 报「最窄卡片 null」(2026-09-24 PC 段修过,2026-10-14 窄屏段又撞了一次)
    for (let i = 0; i < 50 && !(await evaluate(cdp, tab, `document.querySelectorAll('#main .card, #main .kpi').length`)); i++) await sleep(100);
    const m = await evaluate(cdp, tab, INSPECT(ids));
    m.overflow > 1 ? bad(name, `手机宽度横向溢出 ${m.overflow}px`) : good('手机宽度(390)无横向滚动');
    // 只查横向滚动漏掉过:侧栏一直占左边、三列卡片挤成一字一行,页面并不横向滚动(2026-09-23)
    const lay = await evaluate(cdp, tab, `(() => { const m = document.getElementById('main').getBoundingClientRect(); const w = [...document.querySelectorAll('.card, .kpi')].map((e) => e.getBoundingClientRect().width).filter((x) => x > 0); return { mainLeft: Math.round(m.left), mainW: Math.round(m.width), minCard: Math.round(Math.min(...w)) }; })()`);
    lay.mainLeft > 40 || lay.minCard < 140 || lay.mainW > 391 ? bad(name, `手机宽度布局被挤:主区左边距 ${lay.mainLeft}px、最窄卡片 ${lay.minCard}px`) : good(`手机宽度布局正常:主区宽 ${lay.mainW}px、最窄卡片 ${lay.minCard}px`);
    tab.errors.length = 0; tab.failed.length = 0;
    await shot(cdp, tab, `${name}-mobile`);
  }
}

async function checkChatLink(cdp, tab) {
  console.log('\n▸ 侧栏「新对话 ↗」');
  await viewport(cdp, tab, 1440, 900);
  await go(cdp, tab, `${BASE}/dash/health`);
  const href = await evaluate(cdp, tab, `[...document.querySelectorAll('#side a')].find((a) => a.textContent.includes('新对话')).getAttribute('href')`);
  await go(cdp, tab, BASE + href);
  await sleep(4000);
  const t = await evaluate(cdp, tab, `document.title + ' | ' + document.body.innerText.slice(0, 200).replace(/\\s+/g, ' ')`);
  const broken = tab.failed.some((f) => f.includes(href)) || /Error code: 4\d\d|Not Found|页面加载失败/.test(t);
  broken || t.length < 20 ? bad('chat', `对话页没打开:${t.slice(0, 120)}`) : good(`对话页打开:${t.slice(0, 80)}`);
  tab.failed.length ? bad('chat', `失败请求 ${[...new Set(tab.failed)].slice(0, 5).join(' | ')}`) : good('对话页没有失败的请求');
  tab.errors.length = 0; tab.failed.length = 0;
  await shot(cdp, tab, 'chat-home');
}

async function checkDig(cdp, tab) {
  console.log('\n▸ 深挖(真建会话、真调模型)');
  await viewport(cdp, tab, 1440, 900);
  await go(cdp, tab, `${BASE}/dash/health`);
  const expect = await evaluate(cdp, tab, `(() => { const s = document.getElementById('inst'); return s ? s.options[s.selectedIndex].textContent.split(' · ')[0] : ''; })()`);
  const before = new Set((await cdp.send('Target.getTargets')).targetInfos.map((t) => t.targetId));
  const text = await evaluate(cdp, tab, `(() => { const a = document.querySelector('#hd-findings [data-dig]') || document.querySelector('[data-dig]'); a.click(); return a.dataset.dig; })()`);
  let target = null;
  for (let i = 0; i < 100 && !target; i++) {
    await sleep(200);
    target = (await cdp.send('Target.getTargets')).targetInfos.find((t) => t.type === 'page' && !before.has(t.targetId) && /\/session\/ses_/.test(t.url));
  }
  if (!target) {
    const toast = await evaluate(cdp, tab, `(document.querySelector('.toast') || {}).innerText || ''`);
    return bad('dig', `20 秒内没有新标签页落到会话页${toast ? ',提示:' + toast : ''}`);
  }
  good(`新标签页:${target.url.replace(BASE, '')}`);
  const sid = target.url.match(/ses_[A-Za-z0-9]+/)[0];
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
  const nt = await openTab(cdp, sessionId); nt.loaded = true;
  await viewport(cdp, nt, 1440, 900);
  // 首句:应当就是大盘上那条,前面带「先登录哪个库」
  let first = '', reply = '';
  for (let i = 0; i < 90 && !reply; i++) {
    await sleep(2000);
    const msgs = await evaluate(cdp, tab, `fetch('/session/${sid}/message').then((r) => r.json())`);
    const texts = (role) => msgs.filter((m) => m.info?.role === role).flatMap((m) => m.parts || []).filter((p) => p.type === 'text').map((p) => p.text).join('\n');
    first = texts('user'); reply = texts('assistant');
  }
  first.includes(text.replace(/^请/, '').slice(0, 12)) ? good('首句已发出,内容是大盘上那条') : bad('dig', `首句不对:${first.slice(0, 80)}`);
  if (expect) (expect.includes(' / ') ? first.includes('请先登录') : true) ? good(`首句带了登录说法:${first.slice(0, 40)}`) : bad('dig', `首句没说先登录哪个库(当前实例 ${expect}):${first.slice(0, 60)}`);
  reply ? good(`模型已回复:${reply.replace(/\s+/g, ' ').slice(0, 100)}`) : bad('dig', '3 分钟内模型没有回复');
  await sleep(2000);
  const page = await evaluate(cdp, nt, `document.body.innerText.slice(0, 400).replace(/\\s+/g, ' ')`);
  page.includes(first.slice(0, 10)) ? good('会话页上看得到首句') : bad('dig', `会话页上没看到首句:${page.slice(0, 120)}`);
  await shot(cdp, nt, 'dig-session');
  const back = await evaluate(cdp, tab, `location.pathname`);
  back === '/dash/health' ? good('大盘原页还在,没被跳走') : bad('dig', `大盘原页被带走到 ${back}`);
}

// ---------------------------------------------------------------- 主流程
mkdirSync(OUT, { recursive: true });
const { cdp, stop } = await launch();
try {
  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
  const tab = await openTab(cdp, sessionId);
  if (EMPTY) { await checkEmptyPages(cdp, tab); } else { await checkPages(cdp, tab); await checkChatLink(cdp, tab); }
  if (DIG) await checkDig(cdp, tab);
} catch (e) {
  bad('脚本', `中断:${e.stack || e}`);
} finally {
  await stop();
}
console.log(`\n截图在 ${OUT}`);
// 显式退出:和 Chrome 的 WebSocket 还开着,不退出的话进程会一直挂着(2026-09-23 挂过一次,端口被占到下一轮)
if (problems.length) { console.log(`\n${problems.length} 个问题:\n  ` + problems.join("\n  ")); process.exit(1); }
console.log("浏览器检查 ALL OK");
process.exit(0);
