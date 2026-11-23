// 外壳:按风格搭页面框架(普通 = R6 顶栏 + 侧栏;文艺 = R10 报头 + 版面导航),按 pathname 装页面,
// 实例切换、页面内导航(不刷新)、风格滑块,以及全页的点击委托(明细抽屉、切报告、排序页签、复制)。
//
// 页面在 /dash/ 下端出:/dash/health、/dash/topsql、/dash/wdr、/dash/kb(nginx try_files 都落到 index.html)。

import { fetchWhoami, fetchTargets, fetchIndex, fetchMe } from './data.js';
import { setDigTarget, openDig, installDig, CHAT_HOME, setChatHome } from './dig.js';
import { esc, stamp } from './fmt.js';
import { I } from './ui.js';
import { currentStyle, saveStyle, applyStyle, styleSwitch } from './style.js';
import { toast, menu, closeMenu, closeDrawer, installTips } from './overlay.js';
import { clearModels, getModel } from './store.js';
import { enter, resetRise } from './motion.js';
import * as D from './details.js';
import * as cmdk from './cmdk.js';
import * as assist from './assist.js';
import * as insight from './insight.js';
import { writeBy, BY, tAbout } from '../pages/topsql.js';
import { hAbout } from '../pages/health.js';
import { wAbout } from '../pages/wdr.js';
import { readRange, saveRange, valid as rangeValid, label as rangeLabel, fromQuery } from './range.js';

const PAGES = [
  { name: 'health', label: '健康检查', icon: 'pulse', skill: 'health', sec: 'sec-health' },
  { name: 'topsql', label: 'Top SQL', icon: 'list', skill: 'topsql', sec: 'sec-topsql' },
  { name: 'wdr', label: 'WDR 分析', icon: 'clock', skill: 'wdr', sec: 'sec-wdr' },
  { name: 'kb', label: '知识库', icon: 'book', skill: null, sec: 'sec-kb' },   // 全体共享,不分实例
  { name: 'resources', label: '资源大盘', icon: 'grid', skill: null, sec: null, ops: true },   // 只给运维管理员(导航按 /dash/api/me 显示)
];
export const DEFAULT_PRODUCT = 'GaussDB 智能体';
const KEY_STORE = 'dash.instance';
const S = { run: {}, ctx: null, targets: [], whoami: '', me: null };
const product = () => (S.me && S.me.product_name) || DEFAULT_PRODUCT;
const isOps = () => !!(S.me && S.me.ops_admin);
/** 报头:「GaussDB 智能体」→「GaussDB」+「智能体日报」;没有拉丁前缀的整个名字放进中文那一段。 */
export function mastTitle(name) {
  const mm = /^([A-Za-z0-9][A-Za-z0-9 .-]*?)\s*([^A-Za-z0-9 .-].*)$/.exec(name || '');
  return mm ? `${esc(mm[1])}<span class="cjk">${esc(mm[2])}日报</span>` : `<span class="cjk">${esc(name || '')}日报</span>`;
}
// 首次进入时地址里的 ?run=<文件> / ?by=<维度> / ?from=&to=:技能存档后打印的「已存入大盘」直达链接
// (reports.dash_link)与带时间段的链接。只在第一次装页面时读一次 —— 之后以页面上的选择为准。
let booted = false;
const RUN_FILE = /^[A-Za-z0-9_.-]+\.json$/;

export function pageName() {
  const seg = location.pathname.replace(/\/+$/, '').split('/').pop();
  return PAGES.some((p) => p.name === seg) ? seg : 'health';
}
function readKey() { try { return localStorage.getItem(KEY_STORE) || ''; } catch (_) { return ''; } }
function writeKey(k) { try { localStorage.setItem(KEY_STORE, k); } catch (_) { /* 隐私模式等,忽略 */ } }
/** 当前实例:地址里 ?inst=<key>(便于把某个实例的链接发给别人)> localStorage 里的那个 > 最近跑过的那个;都要在 targets 里。 */
export function pickKey(targets) {
  const q = new URLSearchParams(location.search).get('inst');
  if (q && targets.some((t) => t.key === q)) return q;
  const saved = readKey();
  if (targets.some((t) => t.key === saved)) return saved;
  return targets.length ? targets[0].key : '';
}

/** 实例列表:普通风格按本页技能取;文艺风格一份报纸含三个技能,取并集(按最近报告时间倒序)。 */
async function loadTargets(page, style) {
  const skills = style === 'press' ? ['health', 'topsql', 'wdr'] : page.skill ? [page.skill] : [];
  const lists = await Promise.all(skills.map((s) => fetchTargets(s).then((t) => (t.ok && Array.isArray(t.data) ? t.data : []))));
  const by = new Map();
  lists.flat().forEach((t) => { const o = by.get(t.key); if (!o || String(t.last_at) > String(o.last_at)) by.set(t.key, { ...o, ...t }); });
  return [...by.values()].sort((a, b) => String(b.last_at).localeCompare(String(a.last_at)));
}

const rangeBtn = (r) => `<button class="inst range${r ? ' on' : ''}" id="range" title="只看这段时间内跑过的报告"><span class="t3">时间段</span>${esc(rangeLabel(r))}<span class="chev">${I.chev}</span></button>`;
function rangeMenu(anchor) {
  const r = S.ctx.range || {};
  menu(anchor, `<div class="mh">只看这段时间内跑过的报告（WDR 与时段报告按评估窗口算，窗口时间是数据库所在时区）</div>
    <div class="rg"><label>从<input type="datetime-local" id="rg-from" value="${esc(r.from || '')}"></label>
      <label>到<input type="datetime-local" id="rg-to" value="${esc(r.to || '')}"></label>
      <div class="rg-act"><button class="btn" data-range-clear>看全部</button><button class="btn primary" data-range-apply>应用</button></div></div>`);
}
function applyRange(r) {
  if (r && !rangeValid(r)) return toast('时间段不对：至少填一头，开始不能晚于结束。');
  saveRange(r);
  S.run = {};                                      // 选过的那份报告可能不在新时间段里
  closeMenu();
  return mount();
}

const instBtn = (t, has) => (has ? `<button class="inst" id="inst"><span class="t3">实例</span>${esc((t && (t.label || t.conn)) || '—')}<span class="chev">${I.chev}</span></button>` : '<span class="inst" style="cursor:default"><span class="t3">还没有报告</span></span>');

function chromeNormal(page, style, ctx) {
  const nav = PAGES.map((p) => `<a href="/dash/${p.name}" data-go="${p.name}" class="${p.name === page.name ? 'on' : ''}">${I[p.icon]}${esc(p.label)}</a>`);
  const ops = isOps() ? `<div class="grp">运维</div>${nav[4]}` : '';
  return `<header class="topbar">
      <div class="logo"><span class="mark">${I.db}</span><b>${esc(product())}</b></div>
      ${page.skill ? instBtn(ctx.target, ctx.targets.length) : ''}${page.skill && ctx.targets.length ? rangeBtn(ctx.range) : ''}
      <button class="cmdk-trigger" id="cmdk-btn">${I.search}搜索页面、SQL、维度、案例…<span class="kbd"><kbd>⌘</kbd><kbd>K</kbd></span></button>
      <div class="right">${page.ops ? '' : styleSwitch(style)}<button class="ask-btn" id="ask-btn"><span class="spark">${I.spark}</span>问智能体<span class="kbd"><kbd>⌘</kbd><kbd>J</kbd></span></button>
        <div class="user"><span class="av">${esc((ctx.whoami || '?').slice(0, 1).toUpperCase())}</span><span>${esc(ctx.whoami || '—')}</span></div></div>
    </header>
    <div class="shell"><nav class="side" id="nav">
      <div class="grp">诊断大盘</div>${nav.slice(0, 3).join('')}<div class="grp">知识</div>${nav[3]}${ops}
      <div class="grp">对话</div><a href="${CHAT_HOME}" target="_blank" rel="noopener">${I.plus}新对话<span class="ext">${I.ext}</span></a><a href="${CHAT_HOME}" target="_blank" rel="noopener">${I.chat}最近对话<span class="ext">${I.ext}</span></a>
      <div class="side-ai"><b>随时问智能体</b><p>它带着你当前打开的报告去回答，在新标签页里开会话。</p><span class="kbd"><kbd>⌘</kbd><kbd>J</kbd></span><span class="t3">打开侧栏</span></div>
    </nav><main class="content" id="main"><div class="empty-state">加载中…</div></main></div>`;
}
function chromePress(page, style, ctx) {
  const nav = [['health', '头版'], ['', 'A2 要闻 · 健康检查', 'sec-health'], ['topsql', 'B1 慢语句 · Top SQL'], ['wdr', 'C1 负载 · WDR'], ['kb', 'D1 知识 · 知识库']]
    .map(([p, l, jump]) => (jump ? `<a href="#" data-jump="${jump}">${esc(l)}</a>` : `<a href="/dash/${p}" data-go="${p}" data-p="${p}" class="${p === page.name ? 'on' : ''}">${esc(l)}</a>`)).join('');
  return `<header class="mast"><div class="mast-top"><span id="issue-date"></span><span class="dot">·</span><span id="issue-no"></span><span class="dot">·</span><span>实例版</span>${instBtn(ctx.target, ctx.targets.length)}${ctx.targets.length ? rangeBtn(ctx.range) : ''}
      <span class="r">${styleSwitch(style)}<button class="mt-btn" id="cmdk-btn">检索<kbd>⌘K</kbd></button><button class="mt-btn" id="ask-btn">问智能体<kbd>⌘J</kbd></button><span class="sub">订阅者 <b>${esc(ctx.whoami || '—')}</b></span></span></div>
      <h1 class="title">${mastTitle(product())}</h1><div class="mast-sub">每日巡检与诊断　·　由智能体编写，级别由巡检脚本判定</div></header>
    <nav class="secnav" id="nav">${nav}${isOps() ? '<a href="/dash/resources" data-go="resources">运维 · 资源大盘</a>' : ''}<a class="proto" href="${CHAT_HOME}" target="_blank" rel="noopener">新对话 ↗</a></nav>
    <main class="paper" id="main"><div class="sheet-empty">加载中…</div></main>`;
}

function scrollToSection(id, smooth) {
  const el = id && document.getElementById(id);
  window.scrollTo({ top: el ? el.getBoundingClientRect().top + window.scrollY - 64 : 0, behavior: smooth ? 'smooth' : 'auto' });
}

/** 装页面:取工号与实例 → 按风格搭框架 → 画当前路由。切风格、切实例、切报告都走这里(原地重画,不刷新)。 */
export async function mount() {
  const page = PAGES.find((p) => p.name === pageName());
  const style = page.ops ? 'normal' : currentStyle();        // 资源大盘只有普通风格
  applyStyle(style);
  await loadMe();
  // 工号优先取网关答的 /dash/api/me:不经用户的环境(/reports/whoami.json 会把暂停的沙箱拉起来)
  if (!S.whoami) { const who = S.me && S.me.user_id ? { ok: true, data: S.me } : await fetchWhoami(); S.whoami = who.ok ? who.data.user_id : ''; }
  document.title = page.ops ? `${page.label} · ${product()}` : `${product()} · 大盘`;
  const boot = booted ? null : new URLSearchParams(location.search);
  booted = true;
  if (boot) { const r = fromQuery(boot); if (r) saveRange(r); }        // 链接里带了时间段
  const targets = await loadTargets(page, style);
  const key = style === 'press' || page.skill ? pickKey(targets) : '';
  if (boot) await useBootParams(boot, page, key);
  const ctx = { page: page.name, key, targets, whoami: S.whoami, target: targets.find((t) => t.key === key) || null, run: S.run, style,
    range: readRange(), product: product(), me: S.me };
  S.ctx = ctx;
  setDigTarget(ctx.target);          // 深挖首句带上「先登录哪个库」;没有实例时传 null
  clearModels(); closeDrawer(); closeMenu(); resetRise();
  document.getElementById('app').innerHTML = style === 'press' ? chromePress(page, style, ctx) : chromeNormal(page, style, ctx);
  const main = document.getElementById('main');
  try {
    if (style === 'press') {
      const paper = await import('../views/press/paper.js');
      await paper.render(main, ctx);
      if (page.name !== 'health') scrollToSection(page.sec, false); else window.scrollTo(0, 0);
    } else {
      const mod = await import(`../pages/${page.name}.js`);
      await mod.render(main, ctx);
    }
    enter(main);
  } catch (e) {
    main.innerHTML = `<div class="empty-state">${I.info}<h4>页面加载失败</h4><p>${esc(e && e.message || e)}</p></div>`;
  }
}

/** 直达链接里的那一份报告:在本实例的索引里才用(只保留最近 50 份,老链接可能已过期),否则提示并显示最新一份。 */
async function useBootParams(boot, page, key) {
  const run = boot.get('run'), by = boot.get('by');
  if (by && page.name === 'topsql' && BY.some(([b]) => b === by)) writeBy(by);
  if (!run || !page.skill || !key || !RUN_FILE.test(run)) return;
  const idx = await fetchIndex(page.skill, key);
  if (idx.ok && Array.isArray(idx.data) && idx.data.some((e) => e.file === run)) S.run = { ...S.run, [page.skill]: run };
  else toast('链接里的那份报告已不在存档里（每个实例只保留最近 50 份），显示的是最新一份。');
}

function goPage(p) {
  const target = PAGES.find((x) => x.name === p), from = PAGES.find((x) => x.name === pageName());
  if (p === pageName() && currentStyle() !== 'press') return;
  history.pushState(null, '', `/dash/${p}`);
  if (currentStyle() === 'press' && !(target && target.ops) && !(from && from.ops)) {   // 同一份报纸:翻到那一版即可,不重取
    const page = PAGES.find((x) => x.name === p);
    document.querySelectorAll('#nav a[data-p]').forEach((a) => a.classList.toggle('on', a.dataset.p === p));
    scrollToSection(p === 'health' ? null : page.sec, true);
  } else mount();
}
const rerun = (skill, file) => { S.run = { ...S.run, [skill]: file }; mount(); };
// ⌘J / ⌘K 里直接提问的前缀:取到了本页报告就与解读卡提问框同一句(带报告时间,口径见 lib/prompts.js)
const ABOUT = { health: hAbout, topsql: tAbout, wdr: wAbout };
const askPrefix = () => {
  const c = S.ctx, page = c && PAGES.find((p) => p.name === c.page), m = c && getModel(c.page);
  if (!c || !c.target) return '';
  return ABOUT[c.page] && m && m.ok !== false && !m.missing ? ABOUT[c.page](m) : `关于实例 ${c.target.label || c.target.conn} 的${page ? page.label : '大盘'}报告:`;
};
function assistContext() {
  const c = S.ctx, page = PAGES.find((p) => p.name === c.page), m = getModel(c.page);
  const ins = m && m.ok !== false && !m.missing ? insight[c.page](m) : null;
  const when = m && m.entry ? stamp(m.entry.at) : '';
  return { ctx: [page.label, ...(c.target ? [`实例 ${c.target.label || c.target.conn}`] : []), ...(when ? [`报告 ${when}`] : [])],
    sugg: ins ? ins.chips : [['这个页面怎么看', `请介绍一下大盘的「${page.label}」页面怎么看。`]] };
}

function onClick(ev) {
  const t = ev.target.closest && ev.target.closest('[data-style-to],[data-go],[data-jump],[data-inst],#inst,#range,[data-range-apply],[data-range-clear],[data-run-menu],[data-pick],[data-run],[data-by],[data-dim],[data-xdim],[data-sql],[data-wdim],[data-open-case],[data-close],#scrim,[data-copy],#cmdk-btn,#ask-btn');
  if (!t) { if (!(ev.target.closest && ev.target.closest('#menu'))) closeMenu(); return; }
  const ds = t.dataset;
  if (ds.go && (ev.metaKey || ev.ctrlKey || ev.shiftKey)) return;      // 新标签页打开,交给浏览器
  ev.preventDefault();
  if (ds.styleTo) { if (ds.styleTo !== currentStyle()) { saveStyle(ds.styleTo); mount(); } return; }
  if (ds.go) return goPage(ds.go);
  if (ds.jump) return scrollToSection(ds.jump, true);
  if (ds.inst) { writeKey(ds.inst); S.run = {}; closeMenu(); return mount(); }
  if (t.id === 'range') return rangeMenu(t);
  if (ds.rangeApply !== undefined) return applyRange({ from: document.getElementById('rg-from').value, to: document.getElementById('rg-to').value });
  if (ds.rangeClear !== undefined) return applyRange(null);
  if (t.id === 'inst') return menu(t, `<div class="mh">切换实例（右侧是最近一份报告的时间）</div>${S.ctx.targets.map((x) => `<div class="mi ${x.key === S.ctx.key ? 'on' : ''}" data-inst="${esc(x.key)}">${esc(x.label || x.conn || x.key)}<span class="r">${esc(stamp(x.last_at))}</span></div>`).join('')}`);
  if (ds.runMenu) return import('../views/normal/common.js').then((c) => c.openRunMenu(t, ds.runMenu));
  if (ds.pick || ds.run) { const [sk, f] = (ds.pick || ds.run).split('|'); closeMenu(); return rerun(sk, f); }
  // 切排序页签 = 回到累计榜:直达链接打开的时段 / 增量榜(S.run.topsql)不再钉住
  if (ds.by) { writeBy(ds.by); const { topsql: _pinned, ...rest } = S.run; S.run = rest; return mount(); }
  if (ds.dim !== undefined) return D.openDim(ds.dim);
  if (ds.xdim) return D.openXdim(ds.xdim);
  if (ds.sql) return D.openSql(ds.sql, S.ctx && S.ctx.key);
  if (ds.wdim) return D.openWdim(ds.wdim);
  if (ds.openCase) return D.openCase(ds.openCase);
  if (ds.close !== undefined || t.id === 'scrim') return closeDrawer();
  if (ds.copy) { if (navigator.clipboard) navigator.clipboard.writeText(ds.copy); return toast(`已复制 ${ds.copy}`); }
  if (t.id === 'cmdk-btn') return cmdk.open();
  if (t.id === 'ask-btn') return assist.toggle();
}

/** /dash/api/me 只取一次:取不到(旧网关没有这个接口)就用默认产品名、不显示运维入口。 */
async function loadMe() {
  if (S.me) return;
  const r = await fetchMe();
  S.me = r.ok && r.data && typeof r.data === 'object' && r.data.user_id ? r.data : { product_name: DEFAULT_PRODUCT, ops_admin: false };
  setChatHome(S.me.chat_home);
}

export async function start() {
  installDig(); installTips();
  await loadMe();
  cmdk.install({ pages: PAGES.filter((p) => !p.ops || isOps()).map((p) => [p.name, p.label, p.icon]), page: () => S.ctx && S.ctx.page, key: () => S.ctx && S.ctx.key,
    instances: () => (S.ctx ? S.ctx.targets : []), goPage, switchInstance: (k) => { writeKey(k); S.run = {}; mount(); },
    openAssist: () => assist.open(), askPrefix, chatHome: CHAT_HOME });
  assist.install({ context: assistContext, askPrefix, who: () => S.whoami });
  document.addEventListener('click', onClick);
  // 智能体解读卡里的提问框:带上这份报告的上下文开会话(openDig 在提交事件里同步开标签页)
  document.addEventListener('submit', (ev) => {
    const f = ev.target.closest && ev.target.closest('[data-askform]');
    if (!f) return;
    ev.preventDefault();
    const i = f.querySelector('input'), q = i.value.trim();
    if (!q) return;
    i.value = '';
    openDig((f.dataset.ctx || askPrefix()) + q, '大盘提问');
  });
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { closeDrawer(); closeMenu(); } });
  window.addEventListener('popstate', mount);
  mount();
}
