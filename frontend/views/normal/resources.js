// 普通风格 · 资源大盘(运维管理员):主视觉 = 沙箱容量 + 智能体解读;其下 = 架构大盘(K8s 命名空间 / 平台 / 宿主机,
// 沙箱组按「承载 Pod × 沙箱」画)、工号分布、资源明细、安全与平台前提、数据来源。每 30 秒刷新一次,抽屉开着不受影响。

import { esc, stamp } from '../../lib/fmt.js';
import { I, tipAttr, glyph } from '../../lib/ui.js';
import { rise } from '../../lib/motion.js';
import { drawer } from '../../lib/overlay.js';
import { insightCard } from './common.js';
import { REFRESH_MS, KIND, STATE, BUSY_S, shortPod, RUNNING, SCALE_NOTE, groupCaps } from '../../pages/resources.js';

const ORDER = ['use', 'idle', 'pause', 'nosync', 'other'];
const SW = { use: 'var(--s-use)', idle: 'var(--s-idle)', pause: 'var(--s-keep)', nosync: 'var(--s-bad)', other: 'var(--s-other)' };
const KNOWN = ['gateway', 'frontend', 'statesvc', 'credproxy'];
let timer = null, last = null;

const pct = (a, b) => (b ? Math.max(0, Math.min(100, (a / b) * 100)) : 0);
const fmtCpu = (c) => (c === null || c === undefined ? '—' : c >= 1 ? `${+c.toFixed(c >= 10 ? 0 : 1)} 核` : `${Math.round(c * 1000)}m`);
const fmtMem = (mi) => (mi === null || mi === undefined ? '—' : mi >= 1024 ? `${(mi / 1024).toFixed(mi >= 10240 ? 0 : 1)} GiB` : `${Math.round(mi)} MiB`);
const fmtBytes = (b) => (b === null || b === undefined ? '—' : b >= 1073741824 ? `${(b / 1073741824).toFixed(1)} GiB` : b >= 1048576 ? `${Math.round(b / 1048576)} MiB` : `${Math.round(b / 1024)} KiB`);
const when = (ts) => (ts ? stamp(new Date(ts * 1000).toISOString()) : '—');
function ago(sec) {
  if (sec === null || sec === undefined || sec < 0) return '—';
  if (sec < 60) return '刚刚';
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
  return `${Math.floor(sec / 86400)} 天前`;
}
const left = (sec) => (sec === null || sec === undefined ? '—' : sec <= 0 ? '已过保留期' : `${Math.floor(sec / 86400)} 天 ${Math.floor((sec % 86400) / 3600)} 小时`);
const src = (t, k = '') => `<span class="rs-src ${k}">${esc(t)}</span>`;
const na = (b) => src(b && b.reason ? b.reason : '不可用', /授权/.test((b && b.reason) || '') ? 'need' : 'miss');
/** 表格里只放短标签,完整原因放悬停提示(数据来源表里有全文)。 */
const naShort = (b) => { const why = (b && b.reason) || '不可用'; const t = /metrics-server/.test(why) ? '未装 metrics-server' : /授权/.test(why) ? '需授权只读' : '不可用'; return `<span class="rs-src ${/授权/.test(why) ? 'need' : 'miss'}" title="${esc(why)}">${t}</span>`; };
const ok = (b) => !!(b && b.available !== false);
const comp = (m, name) => m.comps.find((c) => c.name === name);
const node = (id, icon, title, kind, desc, body = '', cls = '') => `<div class="rs-node ${cls}" ${id ? `data-rs="${id}" data-rid="${id}"` : ''}><h4>${icon}${esc(title)}${kind ? `<span class="rs-kind">${esc(kind)}</span>` : ''}</h4>${desc ? `<p>${desc}</p>` : ''}${body}</div>`;

// ---------------------------------------------------------------- 主视觉

function head(m) {
  const d = m.d;
  return `<div class="crumb">运维<span>/</span><b>资源大盘</b></div>
    <div class="page-head"><h1>资源大盘</h1><span class="tag blue">${esc(m.fuyao ? 'Fuyao 沙箱' : `运行模式 ${d.mode}`)}</span>
      <div class="actions"><span class="t3" style="font-size:12.5px">数据时刻 ${esc(when(d.generated_at))} · 每 30 秒刷新</span><button class="btn" data-rs-refresh>${I.redo}刷新</button></div></div>
    <div class="page-desc"><span>版本：<b>${esc(d.version.user_image_tag)}</b></span><span>命名空间：<b>${esc(d.namespace)}</b></span>
      <span>网关：<b>${(() => { const g = comp(m, 'gateway'); return g ? `${g.ready}/${g.replicas} 就绪` : '—'; })()}</b></span>
      <span>实际用量：<b>${ok(d.metrics) ? 'metrics-server' : '<span style="color:var(--notice)">未装 metrics-server,只显示配额</span>'}</b></span><span>可见范围：<b>仅运维管理员</b></span></div>`;
}

function ring(t) {
  if (!t.cap) return '';
  const R = 54, C = 2 * Math.PI * R, seg = (n) => (n / t.cap) * C;
  let off = 0;
  const arcs = RUNNING.map((k) => { const len = seg(t[k]); const a = len ? `<circle cx="75" cy="75" r="${R}" fill="none" stroke="${{ use: 'url(#rs-rg)', idle: '#9db8f5', pause: '#f0b43c', nosync: '#f26b63', other: '#c9cdd4' }[k]}" stroke-width="12" stroke-dasharray="${len} ${C}" stroke-dashoffset="${-off}" transform="rotate(-90 75 75)"/>` : ''; off += len; return a; }).join('');
  return `<svg viewBox="0 0 150 150" width="150" height="150"><defs><linearGradient id="rs-rg" x1="0" x2="1"><stop offset="0" stop-color="#7aa2ff"/><stop offset="1" stop-color="#a088ff"/></linearGradient></defs>
    <circle cx="75" cy="75" r="${R}" fill="none" stroke="rgba(255,255,255,.10)" stroke-width="12"/>${arcs}
    <text x="75" y="72" text-anchor="middle" fill="#fff" style="font:600 24px var(--mono)">${Math.round((t.occupied / t.cap) * 100)}%</text>
    <text x="75" y="93" text-anchor="middle" fill="#b6c2d9" style="font:12px var(--sans)">容量占用</text></svg>`;
}

function trend(m) {
  if (!ok(m.ops)) return `<div class="rs-trend"><div class="l"><span>近 24 小时沙箱数与请求</span>${na(m.ops)}</div></div>`;
  const pts = m.ops.trend || [], n = pts.length;
  if (!n) return '';
  const max = Math.max(1, ...pts.map((p) => (p.running || 0) + (p.paused || 0)), m.totals.cap || 0), W = 520, H = 60;
  const y = (v) => H - 4 - (v / max) * (H - 10), x = (i) => (i / (n - 1)) * W;
  const known = pts.map((p, i) => [i, p]).filter(([, p]) => p.running !== null && p.running !== undefined);
  const run = known.map(([i, p]) => `${x(i)},${y(p.running)}`).join(' L'), pau = known.map(([i, p]) => `${x(i)},${y(p.paused || 0)}`).join(' L');
  const reqMax = Math.max(1, ...pts.map((p) => p.requests || 0));
  const bars = pts.map((p, i) => (p.requests ? `<rect x="${x(i) - 2}" y="${H - 2 - (p.requests / reqMax) * 14}" width="4" height="${(p.requests / reqMax) * 14}" fill="rgba(255,255,255,.28)"/>` : '')).join('');
  const peak = Math.max(0, ...known.map(([, p]) => p.running));
  return `<div class="rs-trend"><div class="l"><span>近 24 小时:运行(面积)与暂停(虚线)台数,底部是每半小时的请求数</span><span>运行峰值 <b style="color:var(--h-t1)" class="num">${peak}</b> · 今天请求 <b style="color:var(--h-t1)" class="num">${m.ops.requests.total}</b></span></div>
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><defs><linearGradient id="rs-tg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7aa2ff" stop-opacity=".45"/><stop offset="1" stop-color="#7aa2ff" stop-opacity="0"/></linearGradient></defs>${bars}
    ${known.length > 1 ? `<path d="M${known[0][0] ? x(known[0][0]) : 0},${H} L${run} L${x(known[known.length - 1][0])},${H} z" fill="url(#rs-tg)"/><path d="M${run}" fill="none" stroke="#a9c3ff" stroke-width="1.6"/><path d="M${pau}" fill="none" stroke="#f0b43c" stroke-width="1.4" stroke-dasharray="4 4"/>` : ''}</svg>
    ${known.length < 2 ? '<div class="t3" style="font-size:12px">台数从网关的清扫器上报开始累计,点还不够画线。</div>' : ''}</div>`;
}

function hero(m) {
  const t = m.totals, d = m.d, nas = ok(m.ops) && m.ops.nas ? m.ops.nas : null;
  const nasBytes = nas ? Object.values(nas.dirs).reduce((a, x) => a + (x.bytes || 0), 0) : null;
  const groupsLine = (m.groups || []).filter((g) => !g.missing).map((g) => `${g.kinds.map((k) => KIND[k]).join('与')} ${g.agent_pods} 个承载 Pod × ${g.per_pod} = ${g.capacity}`).join(' · ');
  const cap = `<div class="glass rs-cap rise"${rise()}>
      <div class="title">${I.grid}沙箱容量<span class="r">${esc(groupsLine)} ${m.groups ? src('沙箱组 CRD') : na(d.platform)}</span></div>
      <div class="rs-capw"><div>
        <div class="rs-big num">${t.run}<small>台运行中</small></div>
        ${groupCaps(m.groups).map((c) => `<div class="rs-split"><span>${esc(c.label)}:容量 <b class="num">${c.cap}</b> · 运行 <b class="num">${c.run}</b> · 空余 <b class="num">${c.free}</b>${c.free === 0 && c.cap ? ' · 已满' : ''}</span></div>`).join('')}
        <div class="rs-split">${RUNNING.filter((k) => k !== 'other' || t.other).map((k) => `<span><i style="background:${SW[k]}"></i>${esc(STATE[k].replace('运行·', ''))}<b class="num">${t[k]}</b></span>`).join('')}</div>
        <div class="rs-split"><span><i style="background:${SW.pause}"></i>暂停<b class="num">${t.paused}</b>${t.nosync ? `(没回写成 ${t.nosync})` : ''} · 不占容量</span></div>
        <div class="rs-split" style="margin-top:6px">${ok(m.fy) ? src('/dash/api/resources') : na(m.fy)}</div></div>${ring(t)}</div>
      <div class="rs-mini">
        <div><div class="l">CPU · 智能体预留</div><div class="v num">${m.reserve.known ? fmtCpu(m.reserve.cpu) : '—'}</div><div class="n">节点容量未取(要集群级只读)</div></div>
        <div><div class="l">内存 · 智能体预留</div><div class="v num">${m.reserve.known ? fmtMem(m.reserve.mem) : '—'}</div><div class="n">含 K8s 组件${m.reserve.withPlatform ? '与平台为沙箱预留的资源' : '(沙箱预留读不到)'}</div></div>
        <div><div class="l">共享存储 · 已用</div><div class="v num">${nas ? fmtBytes(nasBytes) : '—'}</div><div class="n">${nas ? `${esc(ago(m.now - nas.at))}算的 · 容量未取` : ok(m.ops) ? '状态服务还没算完(启动后约 5 秒)' : esc((m.ops && m.ops.reason) || '')}</div></div>
      </div>${trend(m)}</div>`;
  return `<section class="hero"><div class="hero-grid">${cap}${insightCard({ ...m.insight, ctx: '关于资源大盘:' })}</div></section>`;
}

// ---------------------------------------------------------------- 架构大盘

function bar(label, req, lim, used, fmt) {
  const u = used === null || used === undefined ? '' : `<i class="u" style="width:${pct(used, lim)}%"></i>`;
  return `<div class="rs-rl"><span>${label}</span><div class="rs-rb"><i class="q" style="width:${pct(req, lim)}%"></i>${u}<i class="lim"></i></div><span class="rs-rv">${used === null || used === undefined ? `预留 ${fmt(req)}` : `${fmt(used)} / ${fmt(req)}`}</span></div>`;
}

function compNode(m, name, icon, title, desc, extra, cls) {
  const c = comp(m, name);
  if (!c) return node('', icon, title, '未部署', `本命名空间里没有 Deployment ${esc(name)}。`, '', cls);
  const met = ok(m.d.metrics) ? m.d.metrics.pods : null, n = c.replicas, r = c.per_replica;
  const use = (k) => (met ? c.pods.reduce((a, p) => a + ((met[p.name] || {})[k] || 0), 0) : null);
  const reps = `<div class="rs-reps">${c.pods.map((p) => `<div class="rs-rep"><b>${esc(p.name)}</b>${met && met[p.name] ? `${fmtCpu(met[p.name].cpu)} · ${fmtMem(met[p.name].mem)}` : `<span class="${p.ready ? '' : 'bad'}">${esc(p.phase)}${p.ready ? '' : ' · 未就绪'}${p.restarts ? ` · 重启 ${p.restarts}` : ''}</span>`}</div>`).join('') || '<div class="rs-rep">没有 Pod</div>'}</div>`;
  const res = r.requests.cpu !== null && r.limits.cpu !== null ? `<div class="rs-res">${bar('CPU', r.requests.cpu * n, r.limits.cpu * n, use('cpu'), fmtCpu)}${bar('内存', r.requests.mem * n, r.limits.mem * n, use('mem'), fmtMem)}</div>` : '';
  return node(`comp:${name}`, icon, title, `Deployment ${c.ready}/${c.replicas}`, desc, reps + res + (extra || ''), cls);
}

function cell(g, s, k, big) {
  if (!s) return `<i class="rs-slot free${big ? ' big' : ''}">${big ? '空余' : ''}</i>`;
  const tip = `<b>${esc(s.user)} · ${KIND[s.kind] || esc(s.kind)}</b><div class="d">${esc(STATE[s.view])} · 最后活动 ${esc(ago(m0.now - s.last_active))}<br>模板 ${esc(s.template)}${s.outdated ? '(旧版)' : ''}${s.paused_at ? `<br>保留剩余 ${esc(left(s.retain_left))} · 暂停前回写 ${s.synced === false ? '✗' : '✓'}` : ''}</div>`;
  return `<i class="rs-slot ${s.view}${s.outdated ? ' old' : ''}${big ? ' big' : ''}" data-rs="sbx:${esc(s.id)}"${tipAttr(tip)}>${big ? `${esc(s.user)}<br><em>${KIND[s.kind] || ''}</em>` : ''}</i>`;
}
let m0 = null;                          // 当前这一份模型(格子提示里算「多久前」用)

function groupCard(m, g) {
  if (g.missing) return `<div class="rs-node rs-pool rs-s12"><h4>${I.grid}${esc(g.name)}</h4><div class="rs-unk" style="margin-top:8px">${esc(g.reason)}</div></div>`;
  const big = g.per_pod <= 4, c = g.counts;
  const segs = RUNNING.map((k) => (c[k] ? `<i class="${k}" style="width:${pct(c[k], g.capacity)}%"${tipAttr(`${esc(STATE[k])} ${c[k]} 台`)}></i>` : '')).join('');
  const pods = g.pods.map((p, i) => {
    const run = p.sbx.length, pau = p.paused.length;
    const cells = Array.from({ length: g.per_pod }, (_, k) => cell(g, p.sbx[k], k, big)).join('');
    const pz = pau ? `<div class="rs-pz"${tipAttr(`<b>暂停(不占名额)</b><div class="d">${p.paused.map((s) => esc(`${s.user} · ${KIND[s.kind] || ''}${s.view === 'nosync' ? ' · 没回写成' : ''}`)).join('<br>')}</div>`)}>暂停 ${pau}${big ? `:${p.paused.map((s) => esc(s.user)).join('、')}` : ''}</div>` : '';
    return `<div class="rs-pod${p.name ? '' : ' anon'}"${p.name ? ` data-rs="pod:${esc(g.name)}:${esc(p.name)}"` : ''}${p.name ? '' : tipAttr('这个承载 Pod 上现在没有本智能体的沙箱(平台只报总数,不报名字)')}>
      <div class="pn" title="${esc(p.name)}">${esc(p.short || `#${i + 1}`)}<span class="st${g.ready_agent_pods >= g.agent_pods ? '' : ' unk'}"></span></div>
      <div class="rs-slots" style="--k:${big ? g.per_pod : 5}">${cells}</div>
      <div class="pu"><span>运行 <b>${run}</b>${big ? '' : ` · 暂停 <b>${pau}</b>`}</span>${big ? `<span>空余 <b>${g.per_pod - p.sbx.length}</b></span>` : ''}</div>${big ? pz : ''}</div>`;
  }).join('');
  const extra = g.agent_pods < g.pod_max
    ? `<div class="rs-more"><b>当前 ${g.agent_pods} / 最大 ${g.pod_max} 个承载 Pod</b><span>最小 ${g.pod_min} · 实测平台不按需自动扩到最大值</span><span>扩容:请平台把最小值调到需要的个数</span></div>`
    : `<div class="rs-more full"><b>已到最大 ${g.pod_max} 个承载 Pod</b><span>再扩要平台调大最大值和最小值</span></div>`;
  const unplaced = g.unplaced.length ? `<div class="rs-note"><b>还没分到承载 Pod:</b>${g.unplaced.map((s) => `<span class="rs-u" data-rs="sbx:${esc(s.id)}" style="cursor:pointer">${esc(s.user)} · ${esc(STATE[s.view])}</span>`).join('')}</div>` : '';
  const req = g.per_sandbox.requests, lim = g.per_sandbox.limits;
  return `<div class="rs-node rs-pool rs-s12" data-rs="grp:${esc(g.name)}" data-rid="grp:${esc(g.name)}">
    <h4>${I.grid}${esc(g.kinds.map((k) => KIND[k]).join('与'))}沙箱组 ${esc(g.name)}<span class="rs-kind">SandboxGroup · 承载 Pod ${g.agent_pods}/${g.pod_max} × 每 Pod ${g.per_pod} 台</span></h4>
    <div class="rs-caplbl"><span>运行 <b>${g.occupied}</b> / 容量 <b>${g.capacity}</b></span>${RUNNING.filter((k) => c[k]).map((k) => `<span>${esc(STATE[k])} <b>${c[k]}</b></span>`).join('')}<span>空余 <b>${g.free}</b></span><span>暂停 <b>${g.paused}</b>(不占容量)</span>${c.old ? `<span>旧版模板 <b style="color:var(--warn)">${c.old}</b></span>` : ''}<span class="r">${src('容量:沙箱组 CRD')} ${src('沙箱:网关清扫器')}</span></div>
    <div class="rs-capbar">${segs}</div>
    <div class="rs-podcap"><b>承载 Pod(平台管理)</b><span>${g.agent_pods} 个 × 每 Pod ${g.per_pod} 台 = 同时运行的容量 ${g.capacity} · 最小 ${g.pod_min} / 最大 ${g.pod_max} · 就绪 ${g.ready_agent_pods}/${g.desired_agent_pods || g.agent_pods}</span>
      <em>与原固定池化不同:每格是一台独立虚拟机(独立内核),不在用户之间复用,删除即销毁。格子只放运行中的沙箱,暂停的不占名额(2026-11-18 实测);${esc(SCALE_NOTE)}。</em></div>
    <div class="rs-pods${big ? '' : ' dense'}" style="--pc:${big ? (g.pods.length === 1 ? 2 : 3) : 7}">${pods}${extra}</div>${unplaced}
    <div class="rs-note"><span>暂停的沙箱不占名额</span><span>每台 request <b>${fmtCpu(req.cpu)} / ${fmtMem(req.mem)}</b> · limit <b>${fmtCpu(lim.cpu)} / ${fmtMem(lim.mem)}</b></span>${g.agent_reserve ? `<span>每个 Pod 预留 <b>${fmtCpu(g.agent_reserve.requests.cpu + 0.1)} / ${fmtMem(g.agent_reserve.requests.mem + 128)}</b></span>` : ''}<span>虚拟机规格由模板决定</span></div></div>`;
}

/** 平台读不到(没授权)时:沙箱照样按状态列出来(来自网关自己的摘要),容量与承载 Pod 那部分写明读不到。 */
function fallbackGroups(m) {
  const by = {};
  m.sbx.forEach((s) => { (by[s.kind] = by[s.kind] || []).push(s); });
  const kinds = Object.keys(by);
  return `<div class="rs-node rs-pool rs-s12" data-rid="grp:fallback"><h4>${I.grid}沙箱(按网关自己的摘要)<span class="rs-kind">${m.sbx.length} 台</span></h4>
    <div class="rs-unk" style="margin-top:8px">容量、承载 Pod 与每台所在的 Pod 要读平台的 sandbox.flux.io:${na(m.d.platform)}<br>平台管理员 apply k8s/fuyao/platform-read.yaml(只读 ClusterRole)后,这里自动换成「承载 Pod × 沙箱」的格子。</div>
    ${kinds.length ? kinds.map((k) => `<div class="rs-note"><b>${KIND[k] || esc(k)}</b></div><div class="rs-tiles">${by[k].map((s) => `<i class="rs-slot ${s.view}${s.outdated ? ' old' : ''}" data-rs="sbx:${esc(s.id)}"${tipAttr(`<b>${esc(s.user)}</b><div class="d">${esc(STATE[s.view])}</div>`)}>${esc(s.user)}</i>`).join('')}</div>`).join('') : '<div class="rs-note">目前没有沙箱。</div>'}</div>`;
}

function topo(m) {
  const fy = m.fuyao, nasDirs = ok(m.ops) && m.ops.nas ? m.ops.nas.dirs : null;
  const users = ok(m.fy) ? m.sbx.length : 0;
  const gw = compNode(m, 'gateway', I.grid, '智能网关', '认身份 · 按工号找 / 建 / 恢复沙箱 · 反向代理 · 清扫器(闲置暂停、换版、保留期)',
    `<div class="rs-serve">服务 <b class="num">${users}</b> 个工号的沙箱<span class="x">全体共用</span></div>`, 'rs-s5');
  const fe = compNode(m, 'frontend', I.grid, '前端', '大盘静态页面;数据经网关取', '', 'rs-s3');
  const sa = node('', I.info, '服务账号', 'RBAC', '网关的权限只在本命名空间;读沙箱组另有只读 ClusterRole',
    `<div class="rs-tags"><span class="rs-u">deployments</span><span class="rs-u">pods</span><span class="rs-u">configmaps</span><span class="rs-u">secrets · 无 list</span><span class="rs-u${ok(m.d.platform) ? '' : ' warn'}">sandbox.flux.io · ${ok(m.d.platform) ? '只读' : '未授权'}</span></div>`, 'rs-s4');
  const nas = node('nas', I.db, '共享存储', 'NAS', `只有状态服务读写;沙箱挂不了 NAS,用本地盘 + 回写。${nasDirs ? '' : na(m.ops)}`,
    nasDirs ? `<div class="rs-dirs">${['users', 'admins', 'kb'].map((k) => { const x = nasDirs[k] || {}; return `<div class="rs-dir"><b>${k}/</b>${x.exists === false ? '不存在' : `${x.entries} 项 · ${x.files} 个文件`}<br><span class="num" style="color:var(--t2)">${fmtBytes(x.bytes)}</span></div>`; }).join('')}</div>` : '', 'rs-s6');
  const jobs = ok(m.d.jobs) ? m.d.jobs.items : null;
  const job = node('', I.book, '标准知识库', 'Job', jobs ? (jobs.length ? jobs.map((j) => `${esc(j.name)} · <b style="color:${j.status === 'Complete' ? 'var(--ok)' : j.status === 'Failed' ? 'var(--crit)' : 'var(--notice)'}">${esc(j.status)}</b>`).join('<br>') : '没有 Job') : na(m.d.jobs), '', m.comps.some((c) => !KNOWN.includes(c.name)) ? 'rs-s3' : 'rs-s6');
  const other = m.comps.filter((c) => !KNOWN.includes(c.name));
  const oth = other.length ? node('', I.grid, '其他组件', `${other.length} 个`, other.map((c) => `${esc(c.name)} · ${c.ready}/${c.replicas}`).join('<br>'), '', 'rs-s3') : '';
  const st = compNode(m, 'statesvc', I.redo, '状态服务', '独家读写 NAS:按沙箱令牌只放行本人目录;载入、每 60 秒增量回写、每 5 秒报到、知识库;资源大盘的统计', '', 'rs-s6');
  const cp = compNode(m, 'credproxy', I.info, '凭据代理', '持有中间件令牌、SM2 私钥、模型密钥;按令牌里的工号代办,工号头强制改写', '', 'rs-s6');
  const cluster = `<div class="rs-cluster" id="rs-cluster"><div class="gl">${I.grid}Kubernetes · 智能体 <span class="ns">namespace: ${esc(m.d.namespace)}</span>${ok(m.d.components) ? src('K8s API') : na(m.d.components)}</div>${gw}${fe}${sa}${nas}${job}${oth}${fy ? st + cp : ''}</div>`;
  let plat = '';
  if (fy) {
    const groups = m.groups ? m.groups.map((g) => groupCard(m, g)).join('') : fallbackGroups(m);
    const tpl = ok(m.fy) ? m.fy.templates || {} : {};
    const pnodes = [
      node('osb', I.grid, 'OpenSandbox Server', 'API', '建 / 列举 / 暂停 / 恢复 / 删沙箱;网关只调它', '', 'rs-s3'),
      node('', I.grid, 'Flux', 'Controller · Scheduler', '按沙箱组把运行中的沙箱调度到承载 Pod;承载 Pod 数跟着最小值走,实测不按需自动扩到最大值', '', 'rs-s3'),
      node('', I.grid, 'E2B 服务', 'API · Edge · PG · Redis', '沙箱元数据、节点调度与访问入口;平台方运维,不计入智能体', '', 'rs-s3'),
      node('tpl', I.book, '模板', 'Template Manager', '诊断、导入各一份,随版本发布;虚拟机规格由模板决定',
        `<div class="rs-tags">${Object.entries(tpl).map(([k, v]) => `<span class="rs-u tpl" title="${esc(v)}">${KIND[k] || esc(k)} · ${esc(v)}</span>`).join('')}${m.totals.old ? `<span class="rs-u warn">旧版沙箱 ${m.totals.old} 台</span>` : ''}</div>`, 'rs-s3'),
    ].join('');
    plat = `<div class="rs-cluster plat" id="rs-plat"><div class="gl">${I.grid}openFuyao 沙箱平台 <span class="ns">${esc((ok(m.d.platform) && m.d.platform.namespace) || 'flux-system')} · 平台方管理,智能体只调用接口与只读</span>${ok(m.d.platform) ? src('sandbox.flux.io · 只读') : naShort(m.d.platform)}</div>${groups}${pnodes}</div>
      <div class="rs-host" id="rs-host"><div class="gl">${I.grid}宿主机 <span class="ns">节点信息要集群级只读权限,这里只画运行路径</span></div>
        <div class="rs-chain" style="grid-column:span 4"><div class="t">普通 Pod(网关、状态服务、凭据代理、前端)</div><span class="c">kubelet</span><span class="a">→</span><span class="c">containerd</span><span class="a">→</span><span class="c">容器</span></div>
        <div class="rs-chain" style="grid-column:span 5"><div class="t">沙箱(每人一台微型虚拟机)</div><span class="c">承载 Pod(平台)</span><span class="a">→</span><span class="c">CRI multiplex</span><span class="a">→</span><span class="c">E2B Orchestrator</span><span class="a">→</span><span class="c vm">Firecracker 虚拟机</span></div>
        <div class="rs-chain" data-rid="build" style="grid-column:span 3"><div class="t">模板构建(构建主机上跑脚本)</div><span class="c">镜像压成一层</span><span class="a">→</span><span class="c">本机临时仓库</span><span class="a">→</span><span class="c">注册模板</span></div></div>`;
  }
  const band = node('', I.grid, '浏览器', '', '对话 · 大盘 · 知识库导入', '', 'ext').replace('class="rs-node ext"', 'class="rs-node ext" data-rid="user"')
    + node('', I.info, '统一身份认证', '', '认人 · 加工号头与信任凭据', '', 'ext').replace('class="rs-node ext"', 'class="rs-node ext" data-rid="sso"');
  const col = [['llm', '大模型服务', 'OpenAI 兼容 · 经凭据代理'], ['mw', '安全中间件', '白名单脚本 · 签名鉴权 · 按人令牌'], ['db', '数据库实例', '被诊断对象 · 只读访问']]
    .map(([id, t, dsc]) => node('', id === 'db' ? I.db : I.grid, t, '', dsc, '', 'ext').replace('class="rs-node ext"', `class="rs-node ext" data-rid="${id}"`)).join('');
  const legend = `<div class="rs-legend">${RUNNING.map((k) => `<span><i class="rs-sw ${k}"></i>${esc(STATE[k])}</span>`).join('')}<span><i class="rs-sw free"></i>空余</span><span><i class="rs-sw old"></i>旧版模板</span><span class="t3">暂停的不占格子,列在各承载 Pod 下方</span></div>`;
  return `<section class="tile b12 rise"${rise()}><div class="tile-h"><h3>${fy ? 'Fuyao 架构大盘' : '组件'}</h3><span class="desc">${esc(m.d.namespace)} · 点组件、承载 Pod 或沙箱看详情 · 在用 = ${BUSY_S / 60} 分钟内有交互</span><div class="tools">${fy ? legend : ''}</div></div>
    <div class="tile-b"><div class="rs-topo" id="rs-topo"><svg class="rs-links" id="rs-links"></svg><div class="rs-band">${band}</div>
      <div class="rs-lanes">${cluster}${plat}</div><div class="rs-col" id="rs-col">${fy ? col : ''}</div></div></div></section>`;
}

function drawLinks(root) {
  const topo = root.querySelector('#rs-topo'), svg = root.querySelector('#rs-links');
  if (!topo || !svg) return;
  const base = topo.getBoundingClientRect();
  const box = (sel) => { const el = topo.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.left - base.left, y: r.top - base.top, w: r.width, h: r.height }; };
  const P = {}; ['user', 'sso', 'comp:gateway', 'comp:frontend', 'comp:statesvc', 'comp:credproxy', 'nas', 'osb', 'tpl', 'build', 'llm', 'mw', 'db'].forEach((k) => (P[k] = box(`[data-rid="${k}"]`)));
  const G = box('.rs-cluster.plat [data-rid^="grp:"]'), CL = box('#rs-cluster'), PL = box('#rs-plat');
  const R = (b, f = 0.5) => [b.x + b.w, b.y + b.h * f], L = (b, f = 0.5) => [b.x, b.y + b.h * f], T = (b, f = 0.5) => [b.x + b.w * f, b.y], B = (b, f = 0.5) => [b.x + b.w * f, b.y + b.h];
  const cv = ([x1, y1], [x2, y2], v) => (v ? `M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}` : `M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2},${y2}`);
  const orth = (pts) => { let d = `M${pts[0][0]},${pts[0][1]}`; for (let i = 1; i < pts.length; i++) { const [x0, y0] = pts[i - 1], [x1, y1] = pts[i]; if (i < pts.length - 1) { const [x2, y2] = pts[i + 1]; const r = 10, dx1 = Math.sign(x1 - x0), dy1 = Math.sign(y1 - y0), dx2 = Math.sign(x2 - x1), dy2 = Math.sign(y2 - y1); d += ` L${x1 - dx1 * r},${y1 - dy1 * r} Q${x1},${y1} ${x1 + dx2 * r},${y1 + dy2 * r}`; } else d += ` L${x1},${y1}`; } return d; };
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const C = { gray: '#86909c', blue: '#2462e8', vio: '#7c5cff', org: '#e2621f', teal: '#16b4c8', green: '#1f9d63' };
  const L1 = [];
  const add = (cond, l) => { if (cond) L1.push(l); };
  add(P.user && P.sso, { d: P.user && P.sso && cv(R(P.user), L(P.sso)), c: C.gray, label: '登录', at: P.user && P.sso && mid(R(P.user), L(P.sso)) });
  const gw = P['comp:gateway'], st = P['comp:statesvc'], cp = P['comp:credproxy'], fe = P['comp:frontend'];
  add(P.sso && gw, { d: P.sso && gw && cv(B(P.sso), T(gw, 0.4), true), c: C.blue, label: '工号 + 信任凭据', at: P.sso && gw && mid(B(P.sso), T(gw, 0.4)) });
  add(gw && fe, { d: gw && fe && cv(R(gw, 0.3), L(fe, 0.3)), c: C.blue });
  if (gw && CL && P.osb) {
    const outX = CL.x - 11, inX = CL.x + 8;
    L1.push({ d: orth([L(gw, 0.35), [outX, gw.y + gw.h * 0.35], [outX, P.osb.y + P.osb.h / 2], L(P.osb)]), c: C.vio, label: '建 / 暂停 / 恢复 / 删', at: [outX, (CL.y + CL.h + PL.y) / 2] });
    if (G) L1.push({ d: orth([L(gw, 0.7), [inX, gw.y + gw.h * 0.7], [inX, G.y + 26], [G.x, G.y + 26]]), c: C.blue, label: '按工号转发 · envd 启动收尾', at: [inX + 96, P.nas ? (gw.y + gw.h + P.nas.y) / 2 : gw.y + gw.h + 7] });
  }
  if (G && st) L1.push({ d: cv(T(G, 0.3), B(st)), c: C.green, label: '沙箱令牌 · 载入 / 回写 / 报到', at: mid(T(G, 0.3), B(st)) });
  if (G && cp) L1.push({ d: cv(T(G, 0.72), B(cp)), c: C.org, label: '沙箱令牌 · 模型 / 中间件代办', at: mid(T(G, 0.72), B(cp)) });
  if (st && P.nas) L1.push({ d: cv(T(st, 0.3), B(P.nas, 0.3), true), c: C.green, label: '独家读写', at: mid(T(st, 0.3), B(P.nas, 0.3)) });
  if (cp && P.llm) L1.push({ d: cv(R(cp, 0.3), L(P.llm)), c: C.teal, label: '推理', at: mid(R(cp, 0.3), L(P.llm)) });
  if (cp && P.mw) L1.push({ d: cv(R(cp, 0.6), L(P.mw)), c: C.org, label: '加令牌 · SM2 签名', at: mid(R(cp, 0.6), L(P.mw)) });
  if (P.mw && P.db) L1.push({ d: cv(B(P.mw), T(P.db), true), c: C.org, label: '白名单只读', at: mid(B(P.mw), T(P.db)) });
  if (P.build && P.tpl) L1.push({ d: cv(T(P.build), B(P.tpl), true), c: C.gray, label: '注册模板', at: mid(T(P.build), B(P.tpl)), flow: 0 });
  svg.setAttribute('viewBox', `0 0 ${base.width} ${base.height}`);
  const cols = Object.values(C);
  svg.innerHTML = `<defs>${cols.map((c, i) => `<marker id="rs-ar${i}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="${c}"/></marker>`).join('')}</defs>`
    + L1.map((l) => `<path d="${l.d}" stroke="${l.c}" stroke-opacity=".8" class="${l.flow === 0 ? '' : 'flow'}" marker-end="url(#rs-ar${cols.indexOf(l.c)})"/>`).join('')
    + L1.filter((l) => l.label && l.at).map((l) => { const tw = [...l.label].reduce((a, ch) => a + (ch.charCodeAt(0) > 255 ? 12 : 6.5), 0) + 16; return `<g transform="translate(${l.at[0] - tw / 2},${l.at[1] - 10})"><rect width="${tw}" height="20" rx="10" fill="#fff" stroke="${l.c}" stroke-opacity=".4"/><text x="${tw / 2}" y="14" text-anchor="middle">${esc(l.label)}</text></g>`; }).join('');
  const col = root.querySelector('#rs-col');
  if (col && cp) col.style.paddingTop = `${Math.max(0, cp.y - (box('.rs-band') ? box('.rs-band').h + 26 : 0) - 150)}px`;
}

// ---------------------------------------------------------------- 表

function usersTable(m, q) {
  const order = { use: 0, idle: 1, nosync: 2, pause: 3, other: 4 };
  const list = m.sbx.filter((s) => !q || s.user.includes(q) || s.id.includes(q) || (s.agent_pod || '').includes(q))
    .sort((a, b) => order[a.view] - order[b.view] || b.requests - a.requests || a.user.localeCompare(b.user));
  const reqOk = ok(m.ops);
  return `<div class="scroll" style="max-height:440px;overflow:auto"><table class="tbl"><thead><tr><th>工号</th><th>环境</th><th>沙箱</th><th>状态</th><th>模板</th><th>最后活动</th><th>暂停于 / 保留剩余</th><th>暂停前回写</th><th class="r">今日请求</th><th>所在承载 Pod</th></tr></thead><tbody>${list.map((s) => `<tr data-rs="sbx:${esc(s.id)}" style="cursor:pointer">
    <td class="num">${esc(s.user)}${s.kind === 'kb-import' ? ' <span class="tag gray">管理员</span>' : ''}</td><td>${KIND[s.kind] || esc(s.kind)}</td><td class="num mut" title="${esc(s.id)}">${esc(s.id.slice(0, 8))}</td>
    <td><span class="rs-st ${s.view}"><i></i>${esc(STATE[s.view])}</span></td><td class="num mut">${esc(s.template)}${s.outdated ? ' <span class="tag" data-lv="1">旧版</span>' : ''}</td>
    <td class="mut">${esc(ago(m.now - s.last_active))}</td><td class="mut">${s.paused_at ? `${esc(when(s.paused_at))} · <span style="color:${s.view === 'nosync' ? 'var(--crit)' : 'var(--t2)'}">${esc(left(s.retain_left))}</span>` : '—'}</td>
    <td>${s.synced === true ? '<span style="color:var(--ok)">✓</span>' : s.synced === false ? '<span style="color:var(--crit)">✗ 不自动删</span>' : '<span class="t3">运行中</span>'}</td>
    <td class="r num">${reqOk ? s.requests : naShort(m.ops)}</td><td class="num mut" title="${esc(s.agent_pod)}">${s.agent_pod ? esc(shortPod(s.agent_pod)) : ok(m.d.platform) ? '—' : naShort(m.d.platform)}</td></tr>`).join('')
    || `<tr><td colspan="10" class="t3">${ok(m.fy) ? '没有沙箱' : esc(m.fy.reason || '')}</td></tr>`}</tbody></table></div>`;
}

function resTable(m) {
  const met = ok(m.d.metrics) ? m.d.metrics.pods : null;
  const pair = (a, b, f) => `${a === null ? '<span class="t3">未声明</span>' : f(a)} <span class="t3">/</span> ${b === null ? '<span class="t3">未声明</span>' : f(b)}`;
  const rows = m.comps.map((c) => {
    const n = c.replicas, r = c.per_replica, mul = (v) => (v === null ? null : v * n);
    const use = met ? `${fmtCpu(c.pods.reduce((a, p) => a + ((met[p.name] || {}).cpu || 0), 0))} · ${fmtMem(c.pods.reduce((a, p) => a + ((met[p.name] || {}).mem || 0), 0))}` : naShort(m.d.metrics);
    return `<tr><td><b style="font-weight:500">${esc(c.name)}</b></td><td class="mut">Deployment ${c.ready}/${c.replicas}</td><td class="r num">${pair(mul(r.requests.cpu), mul(r.limits.cpu), fmtCpu)}</td><td class="r num">${pair(mul(r.requests.mem), mul(r.limits.mem), fmtMem)}</td><td class="num">${use}</td><td>${src('K8s API')}</td></tr>`;
  });
  (m.groups || []).filter((g) => !g.missing && g.agent_reserve).forEach((g) => {
    const cpu = (g.agent_reserve.requests.cpu + 0.1) * g.agent_pods, mem = (g.agent_reserve.requests.mem + 128) * g.agent_pods;
    rows.push(`<tr><td><b style="font-weight:500">${esc(g.name)} · 承载 Pod 预留</b></td><td class="mut">${g.agent_pods} 个承载 Pod × (${g.per_pod} 台 + 平台代理)</td><td class="r num">${fmtCpu(cpu)}</td><td class="r num">${fmtMem(mem)}</td><td>${src('需平台提供(E2B 侧)', 'miss')}</td><td>${src('沙箱组 CRD')}</td></tr>`);
  });
  const total = m.reserve.known ? `<tr><td><b>智能体合计</b></td><td class="mut">${esc(m.d.nodes.reason || '')}</td><td class="r num"><b>${fmtCpu(m.reserve.cpu)}</b></td><td class="r num"><b>${fmtMem(m.reserve.mem)}</b></td><td colspan="2" class="mut">平台组件(OpenSandbox、Flux、E2B)不计入</td></tr>` : '';
  return `<div class="scroll"><table class="tbl"><thead><tr><th>组件</th><th>形态</th><th class="r">CPU 预留 / 上限</th><th class="r">内存 预留 / 上限</th><th>实际 CPU · 内存</th><th>来源</th></tr></thead><tbody>${rows.join('')}${total}</tbody></table></div>`;
}

function security() {
  const must = [['envd 无认证', '能连到虚拟机网段的一方可以 root 身份在任一沙箱执行命令,包括别人的沙箱。', '平台给 envd 开认证;网关下发启动 / 收尾时带令牌。'],
    ['沙箱出网不受限', '模型生成的命令可把数据发到任意地址、探测集群与内网;K8s 网络策略管不到虚拟机。', '宿主机侧只放行状态服务 8090、凭据代理 8091 与平台自身地址。'],
    ['OpenSandbox 未开认证', '集群内任何 Pod 都能建、删沙箱。', '平台开启后把 Key 放进 Secret fuyao-api,不用改代码。']];
  const done = [['恢复后接口返回旧地址', '只认沙箱恢复后报到的地址'], ['模板构建不处理 whiteout', '模板镜像压成一层'], ['寿命到点直接删、不能续期', '默认不设寿命,由清扫器管理'],
    ['单台查询不带 metadata', '一律走列举再过滤'], ['env、volumes 不传入虚拟机', 'envd 下发配置;状态服务承接 NAS'], ['模板磁盘大小参数不生效', '模板内放占位文件,首次启动删除'], ['构建服务没有根证书', '本机临时仓库注册模板']];
  return `<div class="rs-sec"><div><div class="t3" style="font-size:12.5px;margin-bottom:8px">上线前必须由平台落实 · 不影响功能测试</div>${must.map(([h, a, b]) => `<div class="rs-si"><h6>${esc(h)}<span class="tag" data-lv="3">待平台落实</span></h6><p><span class="t3">影响:</span>${esc(a)}</p><p><span class="t3">处理:</span>${esc(b)}</p></div>`).join('')}</div>
    <div><div class="t3" style="font-size:12.5px;margin-bottom:8px">平台行为 · 我方已绕开(平台修复后可撤掉绕法)</div><ul class="rs-ml">${done.map(([a, b]) => `<li>${esc(a)}<span>${esc(b)}</span></li>`).join('')}</ul>
      <div class="rs-dnote">这一块是随版本发布的静态清单(2026-11 端到端实测),不是实时探测;平台落实后由发版更新。</div></div></div>`;
}

const sourcesTable = (m) => `<div class="scroll"><table class="tbl"><thead><tr><th>内容</th><th>来源</th><th>现状</th></tr></thead><tbody>${m.sources.map((r) => `<tr><td class="wrap">${esc(r.what)}</td><td class="wrap mut">${esc(r.from)}</td><td class="wrap">${r.ok ? src('已有') : src(r.why, /授权/.test(r.why) ? 'need' : 'miss')}</td></tr>`).join('')}</tbody></table></div>`;

// ---------------------------------------------------------------- 抽屉

const kv = (rows) => `<div class="rs-kv">${rows.map(([k, v]) => `<span>${esc(k)}</span><div>${v}</div>`).join('')}</div>`;
const OPS = [['stop', '只做收尾回写,不停服务'], ['pause', '收尾回写后暂停,记下回写是否成功'], ['resume', '恢复;清掉暂停与回写标记'],
  ['delete', '在跑的先收尾回写并放开独占,成功才删;暂停的看暂停前回写成没成;否则回 409 不删。确认可丢最后一次成功同步之后的改动,才带 {"force": true}']];

function sbxDrawer(m, id) {
  const s = m.sbx.find((x) => x.id === id);
  if (!s) return;
  const procs = [['gsdb-agent', 'agent', s.kind === 'kb-import' ? '4096 对话 · 4097 报告 · 4095 剪藏' : '4096 对话 · 4097 报告', 'opencode 与技能;环境里没有密钥'],
    ['gsdb-credrelay', 'root', '4099 · credproxy.sock', '带沙箱令牌把模型、中间件请求转给凭据代理'], ['gsdb-statesync', 'root', '—', '每 60 秒增量回写、每 5 秒报到、知识库'],
    ['envd(平台)', 'root', '49983', '网关下发启动、收尾命令']];
  drawer(`${esc(s.user)} · ${KIND[s.kind] || ''}沙箱`, `<span class="tag gray">${esc(STATE[s.view])}</span>`,
    `<h4 class="t3" style="font-size:12px;font-weight:500;margin:0 0 8px">概况 ${src('网关清扫器')}</h4>${kv([
      ['沙箱', `<span class="num">${esc(s.id)}</span>`], ['模板', `<span class="num">${esc(s.template)}</span>${s.outdated ? ' <span class="tag" data-lv="1">旧版 · 闲置后换新</span>' : ''}`],
      ['最后活动', esc(`${ago(m.now - s.last_active)}(${when(s.last_active)})`)], ['暂停于', s.paused_at ? esc(when(s.paused_at)) : '—(运行中)'], ['保留剩余', esc(left(s.retain_left))],
      ['暂停前回写', s.synced === true ? '<span style="color:var(--ok)">✓ 已回写 NAS</span>' : s.synced === false ? '<span style="color:var(--crit)">✗ 没回写成 · 清扫器不自动删除</span>' : '运行中 · 每 60 秒回写'],
      ['今日请求', ok(m.ops) ? `${s.requests} 次` : na(m.ops)],
      ['承载 Pod', s.agent_pod ? `<span class="num">${esc(s.agent_pod)}</span><br><span class="t3" style="font-size:12px">平台管理,与原固定池化无关</span> ${src('assignedSandboxAgentPod')}` : na(m.d.platform)],
      ['平台状态', s.phase ? esc(s.phase) : '—']])}
    <h4 class="t3" style="font-size:12px;font-weight:500;margin:16px 0 8px">沙箱内部(systemd 单元)</h4><table class="tbl"><thead><tr><th>单元</th><th>用户</th><th>端口</th><th>作用</th></tr></thead><tbody>${procs.map((r) => `<tr>${r.map((c, i) => `<td class="${i ? 'wrap' : 'num'}">${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody></table>
    <h4 class="t3" style="font-size:12px;font-weight:500;margin:16px 0 8px">运维动作(POST /admin/fuyao/&lt;动作&gt;,需运维密钥,不在大盘上直接点)</h4>${OPS.map(([v, dsc]) => `<div class="rs-ct"><div class="cn"><span>${v}</span></div><p>${esc(dsc)}</p></div>`).join('')}
    <div class="rs-dnote">实际 CPU / 内存:${src('需平台提供(E2B 侧)', 'miss')} 虚拟机进程不在任何 K8s 容器的 cgroup 里,metrics-server 看不到。</div>`);
}

function podDrawer(m, gname, pname) {
  const g = (m.groups || []).find((x) => x.name === gname), p = g && g.pods.find((x) => x.name === pname);
  if (!p) return;
  drawer(esc(pname), '<span class="tag gray">承载 Pod · 平台管理</span>',
    `${kv([['沙箱组', esc(g.name)], ['就绪', `子组上报 ${g.ready_agent_pods}/${g.desired_agent_pods || g.agent_pods} 个承载 Pod 就绪 ${src('subsandboxgroups')}<br><span class="t3" style="font-size:12px">逐个 Pod 的状态要读平台命名空间的 Pod(没有申请这项权限)</span>`],
      ['沙箱', `运行 ${p.sbx.length} / ${g.per_pod} 台 · 暂停 ${p.paused.length} 台(不占名额)`]])}
    <h4 class="t3" style="font-size:12px;font-weight:500;margin:16px 0 8px">运行中的沙箱 ${src('assignedSandboxAgentPod')}</h4>${Array.from({ length: g.per_pod }, (_, k) => { const s = p.sbx[k]; return `<div class="rs-ct"${s ? ` data-rs="sbx:${esc(s.id)}" style="cursor:pointer"` : ''}><div class="cn"><span>第 ${k + 1} 格${s ? ` · ${esc(s.user)} · ${KIND[s.kind] || ''}` : ''}</span><span class="rs-st ${s ? s.view : 'other'}"><i></i>${s ? esc(STATE[s.view]) + (s.outdated ? ' · 旧版' : '') : '空余'}</span></div></div>`; }).join('')}
    ${p.paused.length ? `<h4 class="t3" style="font-size:12px;font-weight:500;margin:16px 0 8px">暂停的沙箱(不占名额)</h4>${p.paused.map((s) => `<div class="rs-ct" data-rs="sbx:${esc(s.id)}" style="cursor:pointer"><div class="cn"><span>${esc(s.user)} · ${KIND[s.kind] || ''}</span><span class="rs-st ${s.view}"><i></i>${esc(STATE[s.view])}</span></div></div>`).join('')}` : ''}
    <h4 class="t3" style="font-size:12px;font-weight:500;margin:16px 0 8px">预留构成</h4>
    <div class="rs-ct"><div class="cn"><span>resource-reserve(资源占位)</span><span>${g.agent_reserve ? `${fmtCpu(g.agent_reserve.requests.cpu)} / ${fmtMem(g.agent_reserve.requests.mem)}` : '—'}</span></div><p>= 每 Pod ${g.per_pod} 台 × 每台 request,只用于调度记账</p></div>
    <div class="rs-ct"><div class="cn"><span>sandbox-agent(平台代理)</span><span>100m / 128 MiB</span></div><p>经 CRI multiplex 调本节点 E2B 运行时起停虚拟机(实测值)</p></div>
    <div class="rs-dnote">每格是一台独立 Firecracker 虚拟机(独立内核),不在用户之间复用,删除即销毁;虚拟机的实际占用由 E2B Orchestrator 管,不在本 Pod 的 cgroup 里。承载 Pod 由平台管理(沙箱组最小 ${g.pod_min}、最大 ${g.pod_max} 个;${esc(SCALE_NOTE)}),与原固定池化无关。<b>Pod 故障对其上沙箱的影响尚未实测。</b></div>`);
}

function groupDrawer(m, gname) {
  const g = (m.groups || []).find((x) => x.name === gname);
  if (!g) return;
  drawer(esc(g.name), '<span class="tag gray">SandboxGroup · 平台管理</span>',
    `${kv([['环境', esc(g.kinds.map((k) => KIND[k]).join('、'))], ['容量', `同时运行 ${g.capacity} 台(暂停的不占)`], ['承载 Pod', `当前 ${g.agent_pods} 个(最小 ${g.pod_min}、最大 ${g.pod_max})× 每 Pod ${g.per_pod} 台 · 就绪 ${g.ready_agent_pods}`],
      ['运行', RUNNING.filter((k) => g.counts[k]).map((k) => `${esc(STATE[k])} ${g.counts[k]}`).join(' · ') + ` · 空余 ${g.free}`], ['暂停', `${g.paused} 台(不占容量)`],
      ['每台资源', `request ${fmtCpu(g.per_sandbox.requests.cpu)} / ${fmtMem(g.per_sandbox.requests.mem)} · limit ${fmtCpu(g.per_sandbox.limits.cpu)} / ${fmtMem(g.per_sandbox.limits.mem)}`],
      ['阶段', esc(g.phase)], ['谁维护', '平台管理员 apply;智能体只引用组名(FUYAO_*_GROUP)']])}
    <h4 class="t3" style="font-size:12px;font-weight:500;margin:16px 0 8px">平台承载 Pod(有本智能体沙箱的)</h4>${g.pods.filter((p) => p.name).map((p) => `<div class="rs-ct" data-rs="pod:${esc(g.name)}:${esc(p.name)}" style="cursor:pointer"><div class="cn"><span>${esc(p.name)}</span><span>${p.sbx.length} / ${g.per_pod} 台</span></div></div>`).join('') || '<div class="t3">没有</div>'}
    <div class="rs-dnote">容量 = 承载 Pod 数 × 每 Pod 台数,只算同时运行的沙箱;暂停的不占名额(2026-11-18 实测),所以容量按同时在用的人数规划。${esc(SCALE_NOTE)}。容量满时新工号看到等待页、自动重试,有人闲置暂停后空出名额就进得去;平台满员时建沙箱可能回 500,网关同样按满员处理。<b>承载 Pod 故障对其上沙箱的影响尚未实测。</b></div>`);
}

function compDrawer(m, name) {
  const c = comp(m, name);
  if (!c) return;
  const met = ok(m.d.metrics) ? m.d.metrics.pods : null, r = c.per_replica;
  drawer(esc(name), `<span class="tag gray">Deployment ${c.ready}/${c.replicas}</span>`,
    `${kv([['镜像', c.images.map((x) => `<span class="num">${esc(x)}</span>`).join('<br>')], ['每副本 request', `${fmtCpu(r.requests.cpu)} / ${fmtMem(r.requests.mem)}`], ['每副本 limit', `${fmtCpu(r.limits.cpu)} / ${fmtMem(r.limits.mem)}`]])}
    <h4 class="t3" style="font-size:12px;font-weight:500;margin:16px 0 8px">Pod</h4>${c.pods.map((p) => `<div class="rs-ct"><div class="cn"><span>${esc(p.name)}</span><span>${esc(p.phase)}${p.ready ? ' · 就绪' : ' · 未就绪'}</span></div><p>重启 ${p.restarts} 次${met && met[p.name] ? ` · 实际 ${fmtCpu(met[p.name].cpu)} / ${fmtMem(met[p.name].mem)}` : ''}</p></div>`).join('')}
    ${met ? '' : `<div class="rs-dnote">实际用量:${na(m.d.metrics)}</div>`}`);
}

// ---------------------------------------------------------------- 装页面

function paint(root, m) {
  m0 = m;
  const q = (root.querySelector('#rs-find') || {}).value || '';
  root.innerHTML = `<div class="rs">${head(m)}${hero(m)}<div class="bento">${topo(m)}
    ${m.fuyao ? `<section class="tile b12 rise"${rise()}><div class="tile-h"><h3>工号分布</h3><span class="desc">${m.sbx.length} 个工号有沙箱(每人每种环境一台)${m.fy && m.fy.policy ? ` · 保留期 ${Math.round(m.fy.policy.retain_seconds / 86400)} 天 · 闲置 ${Math.round(m.fy.policy.idle_seconds / 60)} 分钟自动暂停` : ''}</span><div class="tools">${ok(m.ops) ? src('今日请求:状态服务') : na(m.ops)}<input class="rs-find" id="rs-find" placeholder="找工号、沙箱、承载 Pod…" value="${esc(q)}"></div></div><div class="tile-b flush" id="rs-users">${usersTable(m, q)}</div></section>` : ''}
    <section class="tile b12 rise"${rise()}><div class="tile-h"><h3>资源明细</h3><span class="desc">按组件 · request / limit / 实际</span></div><div class="tile-b flush">${resTable(m)}</div></section>
    ${m.fuyao ? `<section class="tile b12 rise"${rise()}><div class="tile-h"><h3>安全与平台前提</h3><span class="desc">上线前必须由平台落实的三项,以及平台行为的处理现状</span></div><div class="tile-b">${security()}</div></section>` : ''}
    <section class="tile b12 rise"${rise()}><div class="tile-h"><h3>数据来源</h3><span class="desc">大盘上每一块从哪里取、现在能不能取到</span></div><div class="tile-b flush">${sourcesTable(m)}</div></section></div></div>`;
  requestAnimationFrame(() => drawLinks(root));
}

function denied(root, m) {
  root.innerHTML = `<div class="rs"><div class="crumb">运维<span>/</span><b>资源大盘</b></div><section class="hero"><div class="empty-state">${glyph(m.status === 403 ? null : 2)}<h4>${m.status === 403 ? '没有权限' : '资源大盘读取失败'}</h4><p>${esc(m.status === 403 ? '资源大盘只给运维管理员看。需要的话请管理员把你的工号加进 ConfigMap agent-roles 的 ops-admins,或在统一认证里给你运维管理员的角色。' : m.error)}</p></div></section></div>`;
}

export async function render(root, ctx, load) {
  clearInterval(timer);
  const m = await load();
  last = m;
  if (m.ok === false) return denied(root, m);
  paint(root, m);
  root.addEventListener('click', (ev) => {
    const t = ev.target.closest && ev.target.closest('[data-rs],[data-rs-refresh]');
    if (!t || !last || last.ok === false) return;
    if (t.dataset.rsRefresh !== undefined) { ev.preventDefault(); return refresh(root, load); }
    const [k, a, b, c] = t.dataset.rs.split(':');
    if (k === 'sbx') sbxDrawer(last, [a, b, c].filter(Boolean).join(':'));
    else if (k === 'pod') podDrawer(last, a, [b, c].filter(Boolean).join(':'));
    else if (k === 'grp') groupDrawer(last, [a, b].filter(Boolean).join(':'));
    else if (k === 'comp') compDrawer(last, a);
  });
  root.addEventListener('input', (ev) => { if (ev.target.id === 'rs-find' && last && last.ok !== false) root.querySelector('#rs-users').innerHTML = usersTable(last, ev.target.value.trim()); });
  const onResize = () => (document.body.contains(root) ? drawLinks(root) : window.removeEventListener('resize', onResize));
  window.addEventListener('resize', onResize);
  timer = setInterval(() => { if (!document.body.contains(root)) { clearInterval(timer); return; } refresh(root, load); }, REFRESH_MS);
}

async function refresh(root, load) {
  const m = await load();
  if (!document.body.contains(root)) return;
  if (m.ok === false) return;            // 一次没取到就沿用上一份,下一轮再取(别把页面换成错误页)
  last = m;
  const y = window.scrollY;
  paint(root, m);
  window.scrollTo(0, y);
}
