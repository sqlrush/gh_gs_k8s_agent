"use strict";
// 发版台前端:R1 原型的样式与布局,数据全部来自 /api。界面上来自接口的文字一律 esc。

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const setHtml = (el, v) => { if (el && el._v !== v) { el._v = v; el.innerHTML = v; } };
const short = (t) => String(t || "").replace(/^agent-/, "").replace(/-oc[\d.]+$/, "");
const fmtS = (n) => (n >= 60 ? `${Math.floor(n / 60)} 分 ${Math.round(n % 60)} 秒` : `${Math.round(n)} 秒`);
const tm = (t) => (t ? new Date(t * 1000).toTimeString().slice(0, 8) : "—");
const dt = (t) => (t ? new Date(t * 1000).toLocaleString("zh-CN", { hour12: false }) : "—");

const PHASE = { pkg: 0, plan: 0, images: 0, config: 0, review: 1, whitelist: 1, gateway: 2, pool: 2, kbimport: 2, kbstd: 2, smoke: 3, done: 3 };
const PHASES = ["准备", "确认", "替换", "验证"];
const NUM = "①②③④";
const KIND = { deploy: "首次部署", upgrade: "升级", rollback: "回滚", resume: "接着发" };
const POOLMODE = { slot: "按槽位在线替换", pod: "按 Pod 整体替换", fresh: "新建" };
const ST_PILL = { pending: ["p-gray", "未开始"], running: ["p-blue", "进行中"], waiting: ["p-amber", "需要处理"], done: ["p-green", "已完成"],
  skipped: ["p-gray", "已跳过"], failed: ["p-red", "失败"], paused: ["p-amber", "已暂停"], interrupted: ["p-amber", "被中断"] };
const RUN_PILL = { pending: ["p-gray", "未开始"], running: ["p-blue", "进行中"], waiting: ["p-amber", "需要处理"], paused: ["p-amber", "已暂停"],
  failed: ["p-red", "失败"], done: ["p-green", "已完成"], cancelled: ["p-gray", "已取消"] };

const G = { page: "release", ov: null, cfg: null, scan: null, plan: null, sel: "", runId: null, run: null, logs: [], seq: 0,
  cluster: null, clusterAt: 0, view: null, diff: {}, busy: false };

// -- 接口 ----------------------------------------------------------------------------------------
async function api(method, path, body) {
  const opt = { method, headers: {}, cache: "no-store" };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch (_e) { data = text; }
  if (!r.ok) throw new Error((data && data.error) || `${r.status}`);
  return data;
}
async function act(method, path, body, ok) {
  try { const d = await api(method, path, body); if (ok) toast(ok); await refresh(true); return d; }
  catch (e) { toast("失败:" + e.message); return null; }
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), 3600); }
function modal(title, body, buttons) {
  $("#mh").textContent = title; $("#mb").innerHTML = body; $("#mf").innerHTML = "";
  buttons.forEach((b) => { const e = document.createElement("button"); e.className = "btn " + (b.cls || ""); e.textContent = b.t; e.onclick = b.on; $("#mf").appendChild(e); });
  $("#mask").classList.add("on");
}
const closeModal = () => $("#mask").classList.remove("on");
function confirmDo(title, body, label, fn, cls = "danger") {
  modal(title, body, [{ t: "取消", on: closeModal }, { t: label, cls, on: () => { closeModal(); fn(); } }]);
}

// -- 数据刷新 -------------------------------------------------------------------------------------
async function refresh(force) {
  if (G.busy && !force) return;
  G.busy = true;
  try {
    G.ov = await api("GET", "/api/overview");
    G.scan = G.ov.scan;
    pickRun();
    if (G.runId) {
      G.run = await api("GET", `/api/runs/${G.runId}`);
      const more = await api("GET", `/api/runs/${G.runId}/logs?after=${G.seq}`);
      if (more.length) { G.logs = G.logs.concat(more); G.seq = more[more.length - 1].seq; }
    } else { G.run = null; }
    if (!G.run && G.sel && (!G.plan || G.plan._tag !== G.sel || force)) {
      try { G.plan = Object.assign(await api("GET", `/api/plan?tag=${encodeURIComponent(G.sel)}`), { _tag: G.sel }); }
      catch (e) { G.plan = { _tag: G.sel, problems: [e.message], steps: [] }; }
    }
    if (Date.now() - G.clusterAt > 3000 || force) { G.cluster = await api("GET", "/api/cluster"); G.clusterAt = Date.now(); }
  } catch (e) { toast("读取失败:" + e.message); }
  finally { G.busy = false; }
  render();
}
const vnum = (v) => String(v || "").replace(/^agent-v/, "").split(/[.-]/).slice(0, 3).map((x) => parseInt(x, 10) || 0);
const vcmp = (a, b) => { const x = vnum(a), y = vnum(b); for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] - y[i]; return 0; };
function pickRun() {
  // 默认显示最近一次发布(完成了就显示完成与报告),直到运维点「发下一个版本」或换了发布包(G.fresh)
  const runs = (G.ov && G.ov.runs) || [];
  const want = (G.ov && G.ov.active) || G.pinned || (!G.fresh && runs[0] ? runs[0].id : null);
  if (want !== G.runId) { G.runId = want; G.logs = []; G.seq = 0; G.view = null; }
  const pk = (G.ov && G.ov.packages) || [];
  if (!G.sel || !pk.some((p) => p.tag === G.sel)) {
    // 默认选「比当前新」里最新的那个;没有更新的就选当前版本(计划会说「已经是」),不默认弄出一个回滚
    const cur = (G.ov && G.ov.current && G.ov.current.version) || (G.cluster && G.cluster.user_tag) || "";
    const newer = pk.filter((p) => p.ok && !p.legacy && (!cur || vcmp(p.version, cur) > 0));
    const same = pk.find((p) => cur && vcmp(p.version, cur) === 0);
    const cand = newer[0] || same || pk[0];
    G.sel = cand ? cand.tag : "";
  }
}

// -- 发布页:步骤列表(有发布用发布的,没有用计划预览的) ----------------------------------------------
function steps() {
  if (G.run) {
    const meta = (G.run.meta && G.run.meta.steps) || [];
    return G.run.steps.map((s, i) => Object.assign({}, meta[i] || {}, s, { id: s.step, name: (meta[i] || {}).name || s.step }));
  }
  return ((G.plan && G.plan.steps) || []).map((s, i) => Object.assign({}, s, { idx: i, step: s.id, status: s.skip ? "skipped" : "pending",
    subs: (s.subs || []).map((l) => ({ label: l, st: "pending", note: "" })), detail: "", error: "", tries: 0 }));
}
const runMeta = () => (G.run ? G.run.meta || {} : G.plan || {});
const toTag = () => (G.run ? (G.run.meta || {}).to_tag : G.sel);

function renderHead() {
  const m = runMeta();
  setHtml($("#rfrom"), esc(m.from_v || (G.run ? G.run.from_v : "") || "未部署"));
  setHtml($("#rto"), esc(m.to_v || short(G.sel) || "—"));
  const st = G.run ? G.run.status : "pending";
  const [c, t] = RUN_PILL[st] || RUN_PILL.pending;
  const p = $("#rstatus"); p.className = "pill " + c; p.textContent = (G.run ? `#${G.run.id} ${KIND[G.run.kind] || ""} · ` : "") + t;
  const ss = steps(); const n = ss.filter((s) => s.status === "done" || s.status === "skipped").length;
  $("#rprog").style.width = (ss.length ? (n / ss.length) * 100 : 0) + "%";
  setHtml($("#rcount"), `${n} / ${ss.length}`);
  const t0 = G.run && G.run.created; const t1 = G.run && (G.run.finished || (["running", "waiting"].includes(st) ? Date.now() / 1000 : null));
  setHtml($("#relapsed"), "用时 " + (t0 && t1 ? fmtS(t1 - t0) : "—"));
  const sel = $("#pkgsel");
  const opts = ((G.ov && G.ov.packages) || []).map((pk) => `<option value="${esc(pk.tag)}"${pk.tag === G.sel ? " selected" : ""}>${esc(pk.version)}${pk.legacy ? "(旧包)" : ""}${pk.ok ? "" : " · 有问题"}</option>`).join("");
  setHtml(sel, opts || "<option>发布包目录里没有包</option>");
  const active = G.run && ["running", "waiting", "paused"].includes(st);
  sel.disabled = !!active;
  const scanOk = G.scan && G.scan.fresh && G.scan.last && G.scan.last.status === "ok";
  $("#b-scan").disabled = !!active || (G.scan && G.scan.running);
  $("#b-start").disabled = !!active || !scanOk || !G.plan || (G.plan.problems || []).length > 0 || !!G.run && G.run.status === "failed";
  $("#b-pause").disabled = !(G.run && st === "running");
  $("#b-resume").disabled = !(G.run && ["paused", "failed"].includes(st));
  $("#b-rollback").disabled = !(G.run && ["failed", "done", "paused"].includes(st) && rollbackTag());
  $("#navdot").classList.toggle("show", st === "waiting" || st === "failed");
  const cur = G.ov && G.ov.current;
  setHtml($("#curver"), "当前 " + esc(cur ? cur.version : (G.cluster && G.cluster.user_tag ? short(G.cluster.user_tag) : "—")));
  setHtml($("#envpill"), "命名空间 " + esc((G.cfg && G.cfg.config["k8s.namespace"]) || "gaussdb-agent"));
}
function rollbackTag() {
  if (!G.run) return "";
  const from = G.run.kind === "rollback" ? "" : G.run.from_v;
  const pk = ((G.ov && G.ov.packages) || []).find((p) => p.version === from);
  return pk ? pk.tag : "";
}

function renderScan() {
  const sc = G.scan || {}; const last = sc.last;
  const items = last ? last.items : [];
  const okN = items.filter((i) => i.st === "ok").length;
  const head = sc.running ? ["p-blue", "扫描中"] : !last ? ["p-gray", "未扫描"] : last.status === "ok" ? (sc.fresh ? ["p-green", "全部可用"] : ["p-amber", "已过期,请重新扫描"]) : ["p-red", "有资源不可用"];
  const tiles = items.map((r) => `<div class="res r-${r.st === "warn" ? "ok" : esc(r.st)}"><div class="ri"${r.st === "warn" ? ' style="background:#f59e0b"' : ""}>${{ ok: "✓", bad: "✕", warn: "!" }[r.st] || ""}</div>
    <div style="min-width:0"><div class="rn">${esc(r.name)}</div><div class="ra">${esc(r.addr)}</div><div class="rr"${r.st === "warn" ? ' style="color:#b45309"' : ""}>${esc(r.msg)}</div></div></div>`).join("");
  const sum = sc.running ? "逐项检查中…" : last ? `${okN}/${items.length} 项可用 · ${dt(last.at)} 扫描${last.status === "ok" ? "" : " · 不可用的修好之前不能开始发布"}` : "开始发布前,发版台会把资源逐项检查一遍,全部可用才允许开跑";
  setHtml($("#scancard"), `<div class="scan-sum"><h3 style="margin:0">发布前资源扫描</h3><span class="pill ${head[0]}">${head[1]}</span><span class="muted small">${sum}</span>
    <span class="spacer"></span>${last && last.status !== "ok" ? '<button class="btn sm" data-go="config">去资源配置</button>' : ""}</div>
    ${items.length ? `<div class="scan-grid" style="margin-top:12px">${tiles}</div>` : ""}`);
}

function renderPlanBox() {
  const p = G.run ? null : G.plan;
  if (!p) return setHtml($("#planbox"), "");
  const probs = (p.problems || []).map((x) => `<div>✕ ${esc(x)}</div>`).join("");
  const notes = (p.notes || []).map((x) => `<div>! ${esc(x)}</div>`).join("");
  const info = p.kind ? `<div class="card"><h3>本次计划 <span class="sub">${esc(KIND[p.kind] || p.kind)} · ${esc(p.from_v || "未部署")} → ${esc(p.to_v)} · 池子${esc(POOLMODE[p.pool_mode] || "")}</span></h3>
      ${probs ? `<div class="err-box">${probs}</div>` : ""}${notes ? `<div class="note-box">${notes}</div>` : ""}
      ${(p.required || []).length ? `<div class="small muted">必填配置:${(p.required || []).map((r) => esc(r.map + " 的 " + r.key)).join("、")}</div>` : ""}
      ${(p.whitelist_files || []).length ? `<div class="small muted">白名单团队要执行的增量:${(p.whitelist_files || []).map(esc).join("、")}</div>` : ""}</div>`
    : (probs ? `<div class="err-box">${probs}</div>` : "");
  setHtml($("#planbox"), info);
}

function segState(ss, i) {
  const s = ss[i + 1].status;
  return s === "done" || s === "skipped" ? "done" : s === "running" ? "active" : s === "waiting" ? "warn" : s === "failed" ? "err" : "pending";
}
function stepSecs(s) { return s.started ? Math.max(0, (s.finished || Date.now() / 1000) - s.started) : 0; }
function metaOf(s) {
  return { pending: "未开始", running: fmtS(stepSecs(s)), waiting: "等待处理", done: fmtS(stepSecs(s)), skipped: "跳过", failed: "失败",
    paused: "已暂停", interrupted: "被中断" }[s.status] || "";
}
function renderPipe() {
  const ss = steps(); const n = ss.length;
  if (!n) return setHtml($("#flow"), '<div class="empty-state">选一个发布包,计划就会出现在这里</div>');
  const spans = PHASES.map((_, k) => ss.map((s, i) => [i, s]).filter(([, s]) => PHASE[s.id] === k));
  const phases = spans.map((sp, k) => {
    if (!sp.length) return "";
    const done = sp.filter(([, s]) => s.status === "done" || s.status === "skipped").length;
    const cls = done === sp.length ? "ph-done" : sp.some(([, s]) => ["running", "waiting", "failed"].includes(s.status)) ? "ph-active" : "";
    return `<div class="phase ${cls}" style="grid-column:${sp[0][0] + 1} / span ${sp.length}"><div class="pl">${NUM[k]} ${PHASES[k]}<span> ${done}/${sp.length}</span></div><div class="br"></div></div>`;
  }).join("");
  const nodes = ss.map((s, i) => {
    const icon = { done: "✓", waiting: "!", failed: "✕", skipped: "–" }[s.status] || String(i + 1);
    const mini = (s.subs || []).map((x) => `<i class="${esc(x.st)}"></i>`).join("");
    return `<div class="stg s-${esc(s.status)}" data-step="${i}" ${i > 0 ? `data-l="${segState(ss, i - 1)}"` : ""} ${i < n - 1 ? `data-r="${segState(ss, i)}"` : ""} title="点击进入明细">
      <span class="badge">需处理</span><div class="dot">${icon}</div><div class="nm">${esc(s.name)}</div><div class="mt">${esc(metaOf(s))}</div><div class="mini">${mini}</div></div>`;
  }).join("");
  const c = callout(ss);
  setHtml($("#flow"), `<div class="pipe"><div class="phases" style="grid-template-columns:repeat(${n},minmax(0,1fr))">${phases}</div>
    <div class="track" style="grid-template-columns:repeat(${n},minmax(0,1fr))">${nodes}</div>
    <div class="callout ${c.cls}" style="--px:${c.px}">${c.html}</div></div>`);
}
function curIdx(ss) {
  const i = ss.findIndex((s) => ["running", "waiting", "failed", "paused", "interrupted"].includes(s.status));
  if (i >= 0) return i;
  const j = ss.findIndex((s) => s.status === "pending");
  return j >= 0 ? j : ss.length - 1;
}
function callout(ss) {
  const n = ss.length; let i = curIdx(ss); let cls, icon = "", title, body = "", acts = "";
  const st = G.run ? G.run.status : "";
  if (!G.run) {
    const p = G.plan || {}; const scanOk = G.scan && G.scan.fresh && G.scan.last && G.scan.last.status === "ok";
    i = 0; cls = (p.problems || []).length ? "co-fail" : "co-idle"; icon = (p.problems || []).length ? "✕" : "▶";
    title = (p.problems || []).length ? "还不能开始" : `准备就绪 · ${esc(p.from_v || "未部署")} → ${esc(p.to_v || "")}`;
    body = (p.problems || []).length ? esc(p.problems[0]) : scanOk ? `资源扫描已通过。共 ${n} 步自动执行;需要人参与的:变更预览确认、白名单核对未通过时、请求长时间不结束时。` : "先完成资源扫描:全部可用,才能开始发布。";
    acts = scanOk && !(p.problems || []).length ? '<button class="btn pri sm" data-do="start">开始发布</button>' : '<button class="btn pri sm" data-do="scan">扫描资源</button>';
  } else if (st === "done") {
    i = n - 1; cls = "co-done"; icon = "✓"; title = `${KIND[G.run.kind] || ""}完成 · ${esc(G.run.to_v)}`;
    body = `用时 ${fmtS((G.run.finished || 0) - G.run.created)} · 配置版本 #${esc(G.run.config_version || "—")}`;
    acts = `<button class="btn sm" data-view="${n - 1}">发布报告</button><button class="btn sm" data-do="new">发下一个版本</button>`;
  } else if (st === "cancelled") {
    cls = "co-idle"; icon = "–"; title = "已取消"; body = esc(G.run.error || ""); acts = '<button class="btn sm" data-do="new">重新开始</button>';
  } else {
    const s = ss[i]; const kind = s.status === "failed" ? "fail" : s.status === "waiting" ? "wait" : "run";
    cls = "co-" + kind; icon = { fail: "✕", wait: "!", run: "" }[kind];
    title = `${{ fail: "失败", wait: "需要处理", run: st === "paused" ? "已暂停" : "进行中" }[kind]} · 第 ${i + 1} 步 · ${esc(s.name)}`;
    body = esc(s.status === "failed" ? s.error : s.detail || "");
    if ((s.subs || []).length) body += `<div class="chips">${s.subs.map((x) => `<span class="chip c-${esc(x.st)}">${esc(x.label)}${x.note ? " · " + esc(x.note) : ""}</span>`).join("")}</div>`;
    acts = gateButtons(i, false) || `<button class="btn sm" data-view="${i}">查看明细 ›</button>`;
  }
  return { cls, px: (((i + 0.5) / n) * 100).toFixed(2) + "%",
    html: `<div class="co-ic">${icon}</div><div class="co-b"><div class="co-t">${title}</div><div class="co-d">${body}</div></div><div class="co-a">${acts}</div>` };
}
function gateButtons(i, big) {
  const sm = big ? "" : " sm"; const g = G.run && G.run.gate; const s = steps()[i] || {};
  if (g && g.step === s.id) {
    if (g.kind === "review") return big ? `<button class="btn${sm}" data-gate="cancel">取消发布</button><button class="btn pri${sm}" data-gate="ok">确认并继续</button>`
      : `<button class="btn pri${sm}" data-view="${i}">查看变更并确认</button>`;
    if (g.kind === "whitelist") return `<button class="btn${sm}" data-do="wlexport">导出未成功清单</button>${big ? "" : `<button class="btn${sm}" data-view="${i}">查看明细</button>`}<button class="btn pri${sm}" data-gate="recheck">已补登记,重新核对</button>`;
    if (g.kind === "drain") return `<button class="btn${sm}" data-gate="wait">继续等待</button><button class="btn warn${sm}" data-gate="force">强制转保留</button>`;
  }
  if (G.run && G.run.status === "failed" && s.status === "failed") return `<button class="btn pri${sm}" data-do="resume">从这一步续跑</button>${rollbackTag() ? `<button class="btn danger${sm}" data-do="rollback">回滚</button>` : ""}`;
  if (G.run && ["paused"].includes(G.run.status) && i === curIdx(steps())) return `<button class="btn pri${sm}" data-do="resume">续跑</button>`;
  return "";
}

// -- 集群大盘 --------------------------------------------------------------------------------------
const SLOT_TXT = { active: "在用", free: "空闲", retained: "保留", retaining: "转保留中", evicting: "换镜像 / 清理中", binding: "绑定中", resuming: "恢复中", disabled: "停用", booting: "启动中", unknown: "未知" };
function renderK8s() {
  const c = G.cluster; const el = $("#k8s"); const target = toTag();
  if (!c) return setHtml(el, '<div class="empty-state">读取中…</div>');
  if (!c.ok) return setHtml(el, `<div class="err-box">读不到集群:${esc(c.error)}</div>`);
  const isNew = (t) => target && t === target;
  const gw = (c.gateway || []).map((g) => `<div class="pod ${isNew(g.tag) ? "new" : ""}"><div class="pn">${esc(g.name)}</div><div class="pv">${esc(short(g.tag))} · ${g.ready ? "就绪" : "未就绪"}</div></div>`).join("") || '<div class="muted small">没有网关 Pod</div>';
  const pods = (c.pool || []).map((p) => {
    const nNew = p.slots.filter((s) => isNew(s.tag)).length + (isNew(p.steward) ? 1 : 0); const total = p.slots.length + 1;
    const moving = p.slots.some((s) => ["retaining", "evicting", "booting"].includes(s.state));
    const cls = nNew === total ? "new" : moving ? "updating" : "";
    const txt = nNew === total && target ? `${short(target)} · 已全部替换` : nNew ? `新旧混跑 · 新 ${nNew}/${total}` : `${short(p.steward)} · 运行中`;
    const slots = p.slots.map((s) => {
      const k = s.state === "active" ? "busy" : s.state === "retaining" || s.state === "retained" ? "retain" : ["evicting", "booting"].includes(s.state) ? "restart" : "";
      return `<div class="slot ${k}${isNew(s.tag) ? " nv" : ""}">槽位 ${s.slot} · ${esc(SLOT_TXT[s.state] || s.state)}${s.busy ? " · 忙" : ""}</div>`;
    }).join("");
    return `<div class="pod ${cls}"><div class="pn">${esc(p.name)}</div><div class="pv">${esc(txt)}</div><div class="slots">${slots}<div class="slot stw${isNew(p.steward) ? " nv" : ""}">管家 · ${esc(short(p.steward))}</div></div></div>`;
  }).join("") || '<div class="muted small">没有固定池化(按需模式)</div>';
  const inUse = (c.pool || []).reduce((n, p) => n + p.slots.filter((s) => s.state === "active").length, 0);
  const total = (c.pool || []).reduce((n, p) => n + p.slots.length, 0);
  setHtml(el, `<div class="k8s">
    <div class="krow"><div class="kbox" style="width:128px"><b>统一认证 / 前端</b>集群外</div><div class="kconn" style="align-self:center">→</div>
      <div class="kgroup" style="flex:1"><div class="kt">网关 gateway <span>用户镜像标签 ${esc(short(c.user_tag))}</span></div><div class="pods">${gw}</div></div></div>
    <div class="kconn">↓ 按槽位分配会话</div>
    <div class="kgroup"><div class="kt">诊断容器池 agent-pool <span>在用 ${inUse}/${total}</span></div><div class="pods">${pods}</div></div>
    <div class="krow"><div class="kbox" style="flex:1"><b>知识库导入环境</b>${c.kb_import} 个</div>
      <div class="kbox" style="flex:1"><b>知识库标准包 Job</b>${(c.kbstd || []).map((j) => `${esc(j.name)} · ${j.done ? "Completed" : "运行中"}`).join("<br>") || "无"}</div></div>
    <div class="legend"><span><i style="background:#fff;border-color:#93c5fd;box-shadow:inset 3px 0 0 #10b981"></i>绿边 = 目标版本</span><span><i style="background:#2563eb;border-color:#2563eb"></i>在用</span><span><i style="background:#fffbeb;border-color:#f59e0b"></i>保留 / 转保留中</span><span><i style="background:#fff;border-color:#2563eb;border-style:dashed"></i>换镜像中</span><span>工号不在大盘显示</span></div></div>`);
}
function renderConsole() {
  const el = $("#console"); const bottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
  setHtml(el, G.logs.map(logLine).join("") || '<span class="t">还没有日志。先扫描资源,再开始发布。</span>');
  if (bottom) el.scrollTop = el.scrollHeight;
}
const LV = { e: "e", w: "w", g: "g" };
const logLine = (l) => `<div class="ln"><span class="t">${tm(l.at)}</span> <span class="s">[${esc(l.step || "-")}]</span> <span class="${LV[l.level] || ""}">${esc(l.text)}</span></div>`;

// -- 步骤明细 --------------------------------------------------------------------------------------
function openStep(i) { G.view = i; render(); window.scrollTo(0, 0); }
const sh = () => (G.run && G.run.shared) || {};
const kv = (rows) => `<div class="kv">${rows.map(([k, v]) => `<div>${esc(k)}</div><div>${v}</div>`).join("")}</div>`;
const MAIN = {
  pkg() {
    const d = sh().digests || {}; const m = runMeta();
    return `<h3>发布包</h3>${kv([["目标", esc(m.to_tag || G.sel)], ["架构", esc(m.arch || "—")]])}
      <h3 style="margin-top:14px">镜像(平台清单 digest,与 release.yaml 交叉核对)</h3>
      <table><tr><th>镜像</th><th>digest</th></tr>${Object.entries(d).map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td class="mono">${esc(v)}</td></tr>`).join("") || '<tr><td colspan="2" class="muted">还没核对</td></tr>'}</table>`;
  },
  plan() {
    const m = runMeta();
    return `<h3>本次计划</h3>${kv([["类型", esc(KIND[m.kind] || m.kind || "—")], ["当前 → 目标", `${esc(m.from_v || "未部署")} → ${esc(m.to_v)}`], ["池子", esc(POOLMODE[m.pool_mode] || "—")],
      ["必填配置", esc((m.required || []).map((r) => r.map + " 的 " + r.key).join("、") || "无")], ["特有步骤", esc((m.special || []).join("、") || "无")],
      ["白名单增量", esc((m.whitelist_files || []).join("、") || "无")]])}
      ${(m.notes || []).length ? `<div class="note-box">${m.notes.map((x) => "! " + esc(x)).join("<br>")}</div>` : ""}
      <h3 style="margin-top:14px">每一步为什么做</h3><table><tr><th style="width:36px">#</th><th style="width:160px">步骤</th><th>为什么</th></tr>
      ${(m.steps || []).map((s, k) => `<tr><td>${k + 1}</td><td>${esc(s.name)}${s.skip ? ' <span class="pill p-gray">跳过</span>' : ""}</td><td class="small muted">${esc(s.why)}</td></tr>`).join("")}</table>`;
  },
  images() {
    const p = sh().pushed || [];
    if ((G.cfg && G.cfg.config["registry.mode"]) === "preloaded") return '<div class="note-box">资源配置为「镜像已预置在节点上」:没有推送(只用于测试环境)。</div>';
    return `<h3>镜像入库 <span class="sub">从离线包直接推到内网仓库,不需要 Docker;已有的层跳过、共用的层跨仓库挂载</span></h3>
      <table><tr><th>镜像</th><th>上传</th><th>挂载</th><th>已有</th><th>上传量</th><th>digest(推完回读)</th></tr>
      ${p.map((r) => `<tr><td class="mono">${esc(r.repo)}<br><span class="muted">${esc(r.tag)}</span></td><td>${r.uploaded}</td><td>${r.mounted}</td><td>${r.existing}</td><td>${r.mb} MB</td><td class="mono">${esc(String(r.digest).slice(0, 26))}…</td></tr>`).join("") || '<tr><td colspan="6" class="muted">还没推送</td></tr>'}</table>`;
  },
  config() {
    const s = sh();
    return `<h3>配置变化 <span class="sub">集群现值保留 + 新版本新增项 + 资源配置;敏感值只显示「已设置」</span></h3>
      <table><tr><th>对象</th><th>配置项</th><th>原值</th><th>新值</th><th>来源</th></tr>
      ${(s.changes || []).map((c) => `<tr><td class="mono">${esc(c.obj)}</td><td class="mono">${esc(c.key)}</td><td class="mono small">${esc(c.old == null ? "(无)" : c.old)}</td><td class="mono small">${esc(c.new)}</td><td>${esc(c.why)}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">没有变化</td></tr>'}</table>
      <h3 style="margin-top:14px">渲染出的对象 <span class="sub">${(s.objects || []).length} 个</span></h3><div class="small mono">${(s.objects || []).map(esc).join(" · ")}</div>
      ${(s.skipped || []).length ? `<div class="small muted" style="margin-top:6px">跳过:${s.skipped.map(esc).join("、")}</div>` : ""}`;
  },
  review() {
    const rid = G.run && G.run.id;
    if (rid && G.diff[rid] === undefined) { G.diff[rid] = null; api("GET", `/api/runs/${rid}/diff`).then((t) => { G.diff[rid] = t; render(); }).catch(() => { G.diff[rid] = ""; }); }
    const text = rid ? G.diff[rid] : null;
    const body = text == null ? "读取中…" : text ? text.split("\n").map((l) => {
      const cls = l.startsWith("== ") ? "h" : l.startsWith("+") ? "p" : l.startsWith("-") ? "m" : "";
      return `<span class="${cls}">${esc(l) || " "}</span>`;
    }).join("") : "(没有差异)";
    return `<h3>变更预览 <span class="sub">kubectl diff · 确认后才改动集群;Secret 与用户自己的容器不在这里</span></h3><div class="diffv">${body}</div>`;
  },
  whitelist() {
    const w = sh().whitelist; const m = runMeta();
    if (!w) return '<div class="empty-state">还没核对</div>';
    const c = w.counts || {};
    const tile = (n, l, cls) => `<div class="tile ${cls || ""}"><div class="n">${n}</div><div class="l">${l}</div></div>`;
    return `<div class="tiles">${tile(w.expected, "本版本应有")}${tile(c.ok || 0, "已登记且一致", "g")}${tile(c.missing || 0, "未登记", c.missing ? "r" : "")}${tile((c.content || 0) + (c.params || 0) + (c.duplicate || 0) + (c.invalid || 0), "内容 / 参数 / 重名 / 未生效", (c.content || c.params || c.duplicate || c.invalid) ? "r" : "")}</div>
      ${(w.problems || []).length ? `<div class="gatebox err"><b>${w.problems.length} 条没有通过</b>:白名单由白名单团队手工执行${(m.whitelist_files || []).length ? `(本次的增量:${m.whitelist_files.map(esc).join("、")})` : ""};补登记后点「已补登记,重新核对」。</div>` : `<div class="gatebox" style="border-color:#a7f3d0;background:#ecfdf5"><b class="ok">核对通过</b>:${w.expected} 条全部已登记且内容一致。</div>`}
      <table><tr><th>脚本</th><th>结果</th><th>说明</th></tr>${(w.problems || []).map((p) => `<tr class="bad"><td class="mono">${esc(p.name)}</td><td><span class="pill p-red">${esc(p.label)}</span></td><td class="small">${esc(p.detail)}</td></tr>`).join("") || '<tr><td colspan="3" class="muted">没有问题</td></tr>'}</table>
      ${(w.extra || []).length ? `<p class="small muted">中间件里另有 ${w.extra.length} 条本版本不需要的脚本(只列出,不算失败)。</p>` : ""}`;
  },
  gateway() {
    const c = G.cluster || {};
    return `<h3>网关副本</h3><table><tr><th>Pod</th><th>版本</th><th>就绪</th></tr>${(c.gateway || []).map((g) => `<tr><td>${esc(g.name)}</td><td class="mono">${esc(g.tag)}</td><td>${g.ready ? '<span class="pill p-green">就绪</span>' : '<span class="pill p-amber">未就绪</span>'}</td></tr>`).join("")}</table>
      ${kv([["用户镜像标签", esc(c.user_tag || "—")], ["滚动方式", "Deployment 滚动更新,一次一个副本,另一个继续服务"]])}`;
  },
  pool() {
    const c = G.cluster || {}; const target = toTag(); const prog = sh().pool || {};
    const rows = (c.pool || []).map((p) => `<tr><td style="vertical-align:middle"><b>${esc(p.name)}</b></td>${p.slots.map((s) => {
      const k = s.tag === target ? (s.state === "active" ? "new busy" : "new") : s.state === "active" ? "busy" : ["evicting", "booting"].includes(s.state) ? "act" : s.state === "retained" ? "ret" : "";
      return `<td><div class="cell ${k}"><b>槽位 ${s.slot}</b>${esc(SLOT_TXT[s.state] || s.state)}<br><span class="muted">${esc(short(s.tag))}</span></div></td>`;
    }).join("")}<td><div class="cell ${p.steward === target ? "new" : ""}"><b>管家</b>${esc({ ok: "正常", restart: "换镜像中" }[((prog[p.name] || {}).steward || {}).st] || "正常")}<br><span class="muted">${esc(short(p.steward))}</span></div></td></tr>`).join("");
    const m = runMeta();
    return `<h3>池子 <span class="sub">${esc(POOLMODE[m.pool_mode] || "")}</span></h3>${rows ? `<table class="mx">${rows}</table>` : '<div class="muted">没有固定池化(按需模式)</div>'}
      <h3 style="margin-top:12px">每个槽位怎么换</h3><div class="chain">${["在用?等不忙", "转保留:写回 NAS", "腾挪", "换这个容器的镜像", "核验:新容器 ID · 握手 · 空盘", "空闲(新版本)"].map((x) => `<span>${x}</span>`).join("<i>→</i>")}</div>
      <p class="small muted">管家最后换,且等本 Pod 所有槽位都不忙;换管家时在用的人被转保留,下一句自动恢复。请求 30 分钟不结束会停在这里请你选择。</p>`;
  },
  kbimport() { return '<p>删除旧的知识库导入容器(role=kb-import)。NAS 上的数据不动;管理员下次进入导入环境时按新模板建。</p>'; },
  kbstd() {
    const c = G.cluster || {};
    return `<h3>知识库标准包安装 Job</h3><table><tr><th>Job</th><th>状态</th></tr>${(c.kbstd || []).map((j) => `<tr><td class="mono">${esc(j.name)}</td><td>${j.done ? "Completed" : "运行中"}</td></tr>`).join("") || '<tr><td colspan="2" class="muted">无</td></tr>'}</table><p class="small muted">内容相同的版本会「同版本跳过」,一个文件都不写;结果见下方本步日志。</p>`;
  },
  smoke() {
    const r = sh().smoke || [];
    return `<h3>冒烟验证 <span class="sub">经网关走用户的真实路径</span></h3>${r.map((c, k) => `<div class="check ${c.ok ? "c-ok" : "c-bad"}"><div class="ci">${k + 1}</div><div style="flex:1"><b>${esc(c.name)}</b> <span class="small muted">${c.seconds} 秒</span><div class="small">${esc(c.detail)}</div></div></div>`).join("") || '<div class="empty-state">还没跑</div>'}`;
  },
  done() {
    const ss = steps(); const mx = Math.max(1, ...ss.map(stepSecs));
    return `<h3>发布报告</h3>${kv([["类型", esc(KIND[G.run && G.run.kind] || "—")], ["版本", `${esc(G.run && G.run.from_v || "未部署")} → ${esc(G.run && G.run.to_v)}`],
      ["配置版本", "#" + esc(G.run && G.run.config_version || "—")], ["总用时", G.run && G.run.finished ? fmtS(G.run.finished - G.run.created) : "—"]])}
      <h3 style="margin-top:14px">各步用时</h3><div class="bars">${ss.map((s, k) => `<div class="bar"><span>${k + 1}. ${esc(s.name)}</span><i style="width:${Math.max(2, (stepSecs(s) / mx) * 360)}px"></i><em class="small muted">${s.status === "skipped" ? "跳过" : fmtS(stepSecs(s))}</em></div>`).join("")}</div>`;
  },
};
function renderStepView() {
  const sv = $("#stepview"); const ss = steps(); const i = Math.min(G.view, ss.length - 1); const s = ss[i];
  if (!s) return;
  const [pc, pt] = ST_PILL[s.status] || ST_PILL.pending;
  const icon = { done: "✓", failed: "✕", waiting: "!", skipped: "–" }[s.status] || i + 1;
  const nav = ss.map((x, k) => `<button class="${esc(x.status)}${k === i ? " cur" : ""}" title="${esc(x.name)}" data-view="${k}">${k + 1}</button>`).join("");
  const tl = (s.subs || []).map((x, k) => `<div class="tl-i ${esc(x.st)}"><div class="tl-d">${{ done: "✓", failed: "✕", waiting: "!" }[x.st] || k + 1}</div><div><div class="tl-n">${esc(x.label)}</div><div class="tl-s">${esc(x.note || ST_PILL[x.st] ? (x.note || ST_PILL[x.st][1]) : "")}</div></div></div>`).join("");
  const logs = G.logs.filter((l) => l.step === s.id).map(logLine).join("") || '<span class="t">(这一步还没有日志)</span>';
  setHtml(sv, `<div class="sv-top"><button class="btn sm" data-back>← 返回发布全景</button><div class="crumb">${G.run ? `发布 #${G.run.id} ${esc(G.run.from_v || "未部署")} → ${esc(G.run.to_v)}` : "计划预览"} › <b>第 ${i + 1} / ${ss.length} 步 · ${esc(s.name)}</b></div>
      <span class="spacer"></span><div class="sv-nav">${nav}</div><button class="btn sm" data-view="${Math.max(0, i - 1)}">‹ 上一步</button><button class="btn sm" data-view="${Math.min(ss.length - 1, i + 1)}">下一步 ›</button></div>
    <div class="card"><div class="sv-head"><div class="sv-ic ${esc(s.status)}">${icon}</div><div style="flex:1;min-width:0"><div class="sv-title">第 ${i + 1} 步 · ${esc(s.name)} <span class="pill ${pc}">${pt}</span></div>
      <div class="sv-meta"><span>开始 <b>${tm(s.started)}</b></span><span>用时 <b>${s.started ? fmtS(stepSecs(s)) : "—"}</b></span><span>尝试 <b>${s.tries || 0} 次</b></span><span>${esc(s.error || s.detail || s.why || "")}</span></div></div>
      <div class="co-a">${gateButtons(i, true)}</div></div></div>
    <div class="sv-grid"><div class="card"><h3>子步骤</h3>${tl || '<div class="muted small">无</div>'}</div><div class="card">${(MAIN[s.id] || (() => ""))()}</div></div>
    <div class="card"><h3>本步日志 <span class="sub">只看这一步</span></h3><div class="console" style="height:260px">${logs}</div></div>`);
}

// -- 其他页面 --------------------------------------------------------------------------------------
function renderOverview() {
  const o = G.ov || {}; const cur = o.current; const sc = o.scan || {};
  const pk = o.packages || []; const c = G.cluster || {};
  const inUse = (c.pool || []).reduce((n, p) => n + p.slots.filter((s) => s.state === "active").length, 0);
  const total = (c.pool || []).reduce((n, p) => n + p.slots.length, 0);
  setHtml($("#ov"), `<div class="grid g4">
      <div class="card kpi"><div class="l">当前版本</div><div class="v">${esc(cur ? cur.version : short(c.user_tag) || "—")}</div><div class="l">${cur ? "发布 #" + esc(cur.run) + " · " + dt(cur.at) : "由集群识别"}</div></div>
      <div class="card kpi"><div class="l">诊断容器池</div><div class="v">${total ? `${inUse} / ${total}` : "—"}</div><div class="l">在用 / 槽位</div></div>
      <div class="card kpi"><div class="l">发布包</div><div class="v">${pk.length}</div><div class="l">${pk.filter((p) => !p.ok).length ? pk.filter((p) => !p.ok).length + " 个有问题" : "全部可读"}</div></div>
      <div class="card kpi"><div class="l">资源扫描</div><div class="v" style="font-size:18px">${sc.last ? (sc.last.status === "ok" ? "全部可用" : "有问题") : "未扫描"}</div><div class="l">${sc.last ? dt(sc.last.at) : ""}</div></div></div>
    <div class="grid g2"><div class="card"><h3>最近的发布</h3><table><tr><th>#</th><th>类型</th><th>版本</th><th>结果</th><th>开始</th></tr>${(o.runs || []).map((r) => `<tr><td>#${r.id}</td><td>${esc(KIND[r.kind] || r.kind)}</td><td>${esc(r.from_v || "未部署")} → ${esc(r.to_v)}</td><td><span class="pill ${(RUN_PILL[r.status] || RUN_PILL.pending)[0]}">${(RUN_PILL[r.status] || RUN_PILL.pending)[1]}</span></td><td>${dt(r.created)}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">还没有发布</td></tr>'}</table></div>
      <div class="card"><h3>集群架构</h3><div id="k8s-mini"></div></div></div>`);
  const mini = $("#k8s-mini"); if (mini) { const k = $("#k8s"); mini.innerHTML = k ? k.innerHTML : ""; }
}
async function renderPackages() {
  const pk = (G.ov && G.ov.packages) || [];
  const rows = pk.map((p) => `<tr><td><b>${esc(p.version)}</b><div class="small mono muted">${esc(p.tag)}</div></td><td>${esc(p.arches.join(" · "))}</td>
    <td>${p.ok ? '<span class="pill p-green">可用</span>' : `<span class="pill p-red">有问题</span><div class="small bad">${p.problems.map(esc).join(";")}</div>`}</td>
    <td>${p.legacy ? '<span class="pill p-gray">旧包(无 release.yaml,只能作回滚目标)</span>' : `白名单 ${esc(p.whitelist)} 条`}</td><td class="small">${(p.notes || []).map(esc).join("<br>")}</td>
    <td><button class="btn sm" data-preview="${esc(p.tag)}">预览计划</button></td></tr>`).join("");
  setHtml($("#pk"), `<div class="card"><h3>发布包目录里的包 <span class="sub">把离线包目录原样拷进来,发版台自动发现;sha256 在扫描与发布时核对</span></h3>
    <table><tr><th>版本</th><th>架构</th><th>状态</th><th>内容</th><th>本版本提醒</th><th></th></tr>${rows || '<tr><td colspan="6" class="muted">发布包目录里没有包</td></tr>'}</table></div><div id="pkplan"></div>`);
}
async function previewPlan(tag) {
  try {
    const p = await api("GET", `/api/plan?tag=${encodeURIComponent(tag)}`);
    setHtml($("#pkplan"), `<div class="card"><h3>计划预览:${esc(p.to_v)} <span class="sub">${esc(KIND[p.kind] || p.kind)} · ${esc(p.from_v || "未部署")} → ${esc(p.to_v)} · 池子${esc(POOLMODE[p.pool_mode] || "")}</span></h3>
      ${(p.problems || []).length ? `<div class="err-box">${p.problems.map((x) => "✕ " + esc(x)).join("<br>")}</div>` : ""}
      ${(p.notes || []).length ? `<div class="note-box">${p.notes.map((x) => "! " + esc(x)).join("<br>")}</div>` : ""}
      <table><tr><th>#</th><th>步骤</th><th>为什么</th></tr>${p.steps.map((s, k) => `<tr><td>${k + 1}</td><td>${esc(s.name)}${s.skip ? ' <span class="pill p-gray">跳过</span>' : ""}</td><td class="small muted">${esc(s.why)}</td></tr>`).join("")}</table></div>`);
  } catch (e) { toast(e.message); }
}

// 资源配置:[键, 名字, 类型, 可选值];# 开头是密钥(只写),@csv 是白名单导出文件
const SECTIONS = [
  ["k8s", "Kubernetes", [["k8s.namespace", "命名空间", "text"], ["k8s.context", "kubeconfig 上下文(空 = 默认)", "text"], ["k8s.arch", "节点架构(空 = 自动)", "select", ["", "amd64", "arm64"]]]],
  ["reg", "镜像仓库", [["registry.mode", "方式", "select", ["push", "preloaded"]], ["registry.prefix", "仓库前缀(主机/项目)", "text"], ["registry.username", "账号", "text"],
    ["#registry_password", "密码", "secret"], ["registry.plain_http", "明文 HTTP(只给测试)", "bool"], ["registry.insecure", "不校验证书", "bool"], ["registry.ca_file", "CA 文件路径", "text"]]],
  ["model", "模型", [["model.base_url", "服务地址(以 /v1 结尾)", "text"], ["model.id", "模型 ID", "text"], ["model.provider", "供应商名", "text"], ["model.modalities", "输入类型(text,image)", "text"], ["#model_api_key", "API Key", "secret"]]],
  ["grmp", "中间件", [["grmp.host", "地址", "text"], ["grmp.port", "端口", "text"], ["grmp.path_prefix", "路径前缀(不回显)", "text"], ["grmp.appkey", "Appkey", "text"],
    ["grmp.sign_mode", "签名", "select", ["", "full", "headers-only"]], ["grmp.instance_aliases", "实例别名(每行 别名=IP/库名)", "textarea"], ["#grmp_sm2", "SM2 私钥", "secret"]]],
  ["net", "网络策略", [["net.grmp", "中间件网段与端口", "net"], ["net.model", "模型网段与端口", "net"], ["net.model_kb", "导入环境的模型网段", "net"], ["net.apiserver", "k8s API 网段与端口", "net"], ["net.upstream", "上游入口网段", "net"]]],
  ["wl", "白名单核对", [["whitelist.source", "方式", "select", ["csv", "db"]], ["whitelist.db.host", "只读账号:地址", "text"], ["whitelist.db.port", "端口", "text"],
    ["whitelist.db.database", "库名", "text"], ["whitelist.db.user", "账号", "text"], ["#whitelist_password", "密码", "secret"], ["@csv", "导出文件(CSV)", "upload"]]],
  ["sso", "统一认证与冒烟", [["sso.user_header", "工号请求头", "text"], ["sso.roles_header", "角色请求头", "text"], ["#trust_secret", "信任凭据", "secret"],
    ["smoke.user", "冒烟工号", "text"], ["smoke.instance", "冒烟实例(可空:不测诊断报告)", "text"]]],
  ["pool", "池子与网关", [["gateway.runtime_mode", "运行模式", "select", ["", "ondemand", "pool"]], ["pool.pods", "Pod 数(首次部署)", "number"], ["pool.slots", "每 Pod 槽位数", "number"],
    ["gateway.pull_policy", "拉取策略", "select", ["", "IfNotPresent", "Always"]], ["kb.clip_origins", "网页剪藏允许的网站", "text"]]],
];
let cfgTab = "k8s";
async function renderConfig(force) {
  if (!G.cfg || force) G.cfg = await api("GET", "/api/config");
  const c = G.cfg.config; const marks = G.cfg.secrets || {};
  const field = ([k, label, type, opts]) => {
    if (k.startsWith("#")) {
      const m = marks[k.slice(1)];
      return `<div class="cfgrow"><label>${esc(label)}</label><div class="secret"><span class="mask">${m ? `已设置 · 指纹 ${esc(m.fingerprint)} · ${dt(m.set_at)}${m.kept ? "" : " · 已写入集群"}` : "未设置"}</span><button class="btn sm" data-secret="${esc(k.slice(1))}" data-label="${esc(label)}">${m ? "更换" : "设置"}</button></div></div>`;
    }
    if (k === "@csv") return `<div class="cfgrow"><label>${esc(label)}</label><div><input type="file" id="wlfile" accept=".csv,.txt,.tsv"><div class="small muted">请白名单团队执行下面的 SELECT,把结果存成 CSV 上传:<code>SELECT script_name, md5(script_content) AS content_md5, parameter_config, is_valid FROM grmp.script_config WHERE scene = 'AGENT' ORDER BY script_name;</code></div></div></div>`;
    const v = c[k];
    let input;
    if (type === "select") input = `<select data-k="${k}">${opts.map((o) => `<option value="${esc(o)}"${(v || "") === o ? " selected" : ""}>${esc(o || "(空)")}</option>`).join("")}</select>`;
    else if (type === "bool") input = `<input type="checkbox" data-k="${k}"${v ? " checked" : ""} style="width:auto">`;
    else if (type === "textarea") input = `<textarea data-k="${k}">${esc(v || "")}</textarea>`;
    else if (type === "net") input = `<input data-k="${k}" data-net="1" placeholder="网段,逗号分隔 | 端口,逗号分隔 例:10.0.0.0/24 | 443" value="${esc(v ? (v.cidrs || []).join(",") + " | " + (v.ports || []).join(",") : "")}">`;
    else input = `<input data-k="${k}" type="${type === "number" ? "number" : "text"}" value="${esc(v == null ? "" : v)}">`;
    return `<div class="cfgrow"><label>${esc(label)}<small>${esc(k)}</small></label>${input}</div>`;
  };
  setHtml($("#cf"), `<div class="card"><h3>资源配置 <span class="sub">一次配好,发布前自动逐项扫描;密钥只能设置 / 更换,不能查看</span></h3>
    <div class="tabs">${SECTIONS.map(([id, name]) => `<button data-tab="${id}" class="${id === cfgTab ? "on" : ""}">${esc(name)}</button>`).join("")}</div>
    ${SECTIONS.map(([id, , fields]) => `<div class="cfgsec${id === cfgTab ? " on" : ""}" data-sec="${id}">${fields.map(field).join("")}</div>`).join("")}
    <div class="savebar"><span class="muted small">配置版本:${(G.cfg.versions || []).slice(0, 1).map((x) => "#" + x.id + " · " + esc(x.note)).join("") || "无"}</span><span class="spacer"></span><button class="btn pri" data-do="savecfg">保存为新的配置版本</button></div></div>`);
}
function collectConfig() {
  const out = Object.assign({}, G.cfg.config);
  $$("#cf [data-k]").forEach((el) => {
    const k = el.dataset.k;
    if (el.type === "checkbox") out[k] = el.checked;
    else if (el.dataset.net) {
      const [a, b] = el.value.split("|").map((x) => (x || "").trim());
      out[k] = a ? { cidrs: a.split(",").map((x) => x.trim()).filter(Boolean), ports: (b || "").split(",").map((x) => parseInt(x, 10)).filter((x) => x > 0) } : null;
    } else if (el.type === "number") out[k] = el.value === "" ? null : parseInt(el.value, 10);
    else out[k] = el.value;
  });
  Object.keys(out).forEach((k) => { if (out[k] === null || out[k] === "") delete out[k]; });
  return out;
}
async function renderHistory() {
  const runs = await api("GET", "/api/history");
  setHtml($("#hi"), `<div class="card"><h3>发布历史 <span class="sub">回滚也走同一套流程</span></h3><table><tr><th>#</th><th>类型</th><th>版本</th><th>结果</th><th>开始</th><th>用时</th><th></th></tr>
    ${runs.map((r) => `<tr><td>#${r.id}</td><td>${esc(KIND[r.kind] || r.kind)}</td><td>${esc(r.from_v || "未部署")} → ${esc(r.to_v)}</td><td><span class="pill ${(RUN_PILL[r.status] || RUN_PILL.pending)[0]}">${(RUN_PILL[r.status] || RUN_PILL.pending)[1]}</span></td>
      <td>${dt(r.created)}</td><td>${r.finished ? fmtS(r.finished - r.created) : "—"}</td><td><button class="btn sm" data-open-run="${r.id}">查看</button></td></tr>`).join("") || '<tr><td colspan="7" class="muted">还没有发布</td></tr>'}</table></div>`);
}
async function renderAudit() {
  const rows = await api("GET", "/api/audit");
  setHtml($("#au"), `<div class="card"><h3>操作记录 <span class="sub">不记录任何密钥的值</span></h3><table><tr><th style="width:170px">时间</th><th style="width:150px">动作</th><th>对象</th><th>说明</th></tr>
    ${rows.map((a) => `<tr><td>${dt(a.at)}</td><td>${esc(a.action)}</td><td>${esc(a.target)}</td><td class="small">${esc(a.detail)}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">无</td></tr>'}</table></div>`);
}

// -- 总入口 -----------------------------------------------------------------------------------------
const TITLES = { overview: "总览", release: "发布", packages: "发布包", config: "资源配置", history: "历史与回滚", audit: "操作记录" };
function render() {
  renderHead();
  if (G.page === "release") {
    const inStep = G.view !== null;
    $("#relmain").style.display = inStep ? "none" : "";
    $("#stepview").style.display = inStep ? "" : "none";
    renderK8s();
    if (inStep) renderStepView(); else { renderScan(); renderPlanBox(); renderPipe(); renderConsole(); }
  } else if (G.page === "overview") { renderK8s(); renderOverview(); }
}
async function go(page) {
  G.page = page;
  $$(".page").forEach((p) => p.classList.toggle("on", p.id === "page-" + page));
  $$("#nav button").forEach((b) => b.classList.toggle("on", b.dataset.page === page));
  $("#ptitle").textContent = TITLES[page];
  history.replaceState(null, "", "#" + page + (page === "release" && G.view !== null ? "/" + (G.view + 1) : ""));
  try {
    if (page === "packages") await renderPackages();
    if (page === "config") await renderConfig(true);
    if (page === "history") await renderHistory();
    if (page === "audit") await renderAudit();
  } catch (e) { toast(e.message); }
  render();
}

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-page],[data-step],[data-view],[data-back],[data-gate],[data-do],[data-go],[data-preview],[data-tab],[data-secret],[data-open-run]");
  if (!t) return;
  const d = t.dataset;
  if (d.page) return go(d.page);
  if (d.go) return go(d.go);
  if (d.step !== undefined) return openStep(+d.step);
  if (d.view !== undefined) return openStep(+d.view);
  if (d.back !== undefined) { G.view = null; return render(); }
  if (d.preview) return previewPlan(d.preview);
  if (d.tab) { cfgTab = d.tab; $$("#cf .tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === cfgTab)); $$("#cf .cfgsec").forEach((s) => s.classList.toggle("on", s.dataset.sec === cfgTab)); return; }
  if (d.openRun) { G.pinned = +d.openRun; G.runId = null; await refresh(true); return go("release"); }
  if (d.secret) {
    const name = d.secret;
    modal("设置:" + d.label, '<p class="small muted">只写入、不回显;操作记录里只留「更换了」与指纹。</p><input type="password" id="secval" style="width:100%;border:1px solid #cbd5e1;border-radius:8px;padding:8px 10px" autocomplete="off">',
      [{ t: "取消", on: closeModal }, { t: "保存", cls: "pri", on: async () => { const v = $("#secval").value; closeModal(); const r = await act("POST", `/api/secrets/${name}`, { value: v }, "已保存"); if (r) renderConfig(true); } }]);
    setTimeout(() => $("#secval") && $("#secval").focus(), 50);
    return;
  }
  if (d.gate) {
    const rid = G.run && G.run.id;
    if (d.gate === "cancel") return confirmDo("取消发布", "<p>停止这次发布。已经改动的部分不会自动还原,需要时请回滚。</p>", "取消发布", () => act("POST", `/api/runs/${rid}/gate`, { choice: "cancel" }, "已取消"));
    if (d.gate === "force") return confirmDo("强制转保留", "<p>正在执行的请求会被中断(数据先写回 NAS)。此操作写入操作记录。</p>", "强制转保留", () => act("POST", `/api/runs/${rid}/gate`, { choice: "force" }, "已强制"));
    return act("POST", `/api/runs/${rid}/gate`, { choice: d.gate }, { ok: "已确认", recheck: "重新核对", wait: "继续等待" }[d.gate]);
  }
  if (d.do === "scan") return startScan();
  if (d.do === "start") return startRun();
  if (d.do === "resume") return act("POST", `/api/runs/${G.run.id}/resume`, {}, "续跑");
  if (d.do === "rollback") return askRollback();
  if (d.do === "new") { G.pinned = null; G.fresh = true; G.runId = null; G.run = null; G.plan = null; G.sel = ""; await refresh(true); return; }
  if (d.do === "savecfg") { const r = await act("PUT", "/api/config", { config: collectConfig() }, "已保存"); if (r) renderConfig(true); return; }
  if (d.do === "wlexport") return exportWhitelist();
});
document.addEventListener("change", async (ev) => {
  if (ev.target.id === "pkgsel") { G.sel = ev.target.value; G.plan = null; G.pinned = null; G.fresh = true; await refresh(true); }
  if (ev.target.id === "wlfile" && ev.target.files[0]) {
    const text = await ev.target.files[0].text();
    const r = await act("POST", "/api/whitelist/export", { csv: text });
    if (r) toast(`已上传白名单导出文件:${r.rows} 行`);
  }
});
async function startScan() { await act("POST", "/api/scan", { tag: G.sel }, "开始扫描"); }
async function startRun() {
  const p = G.plan || {};
  confirmDo(`开始${KIND[p.kind] || "发布"}`, `<p>${esc(p.from_v || "未部署")} → <b>${esc(p.to_v)}</b>,池子${esc(POOLMODE[p.pool_mode] || "")}。改动集群前会停在「变更预览」请你确认。</p>`, "开始",
    async () => { const r = await act("POST", "/api/runs", { tag: G.sel }, "已开始"); if (r) { G.pinned = r.run; G.fresh = false; await refresh(true); } }, "pri");
}
function askRollback() {
  const tag = rollbackTag();
  confirmDo("回滚到 " + short(tag), "<p>以上一个版本为目标、走同一套步骤:镜像已在仓库,白名单只增不删不用动,知识库标准包不回滚。改动前同样会停在变更预览。</p>", "确认回滚",
    async () => { const r = await act("POST", "/api/rollback", { tag }, "开始回滚"); if (r) { G.pinned = r.run; await refresh(true); } });
}
function exportWhitelist() {
  const w = sh().whitelist; if (!w) return;
  const csv = "script_name,result,detail\n" + (w.problems || []).map((p) => [p.name, p.label, p.detail].map((x) => `"${String(x).replace(/"/g, '""')}"`).join(",")).join("\n");
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv" })); a.download = "whitelist-missing.csv"; a.click();
}
$("#b-scan").onclick = startScan;
$("#b-start").onclick = startRun;
$("#b-pause").onclick = () => act("POST", `/api/runs/${G.run.id}/pause`, {}, "已请求暂停(在安全点停下)");
$("#b-resume").onclick = () => act("POST", `/api/runs/${G.run.id}/resume`, {}, "续跑");
$("#b-rollback").onclick = askRollback;
$("#hostinfo").textContent = "本机访问 · " + location.host;

(async () => {
  const [want, stepArg] = (location.hash || "#release").slice(1).split("/");   // #release/5 = 直接打开第 5 步明细
  try { G.cfg = await api("GET", "/api/config"); } catch (_e) { /* 下面 refresh 会报 */ }
  await refresh(true);
  await go(TITLES[want] ? want : "release");
  if (want === "release" && stepArg) openStep(Math.max(0, parseInt(stepArg, 10) - 1));
  setInterval(() => { if (["release", "overview"].includes(G.page)) refresh(false); }, 1500);
})();
