"""大盘上所有「深挖」入口的首句都带报告的时间(待办「大盘」一节,用户 2026-10-19 提)。

在 Node 里用真的取数与建模(fetch 换成假的)把普通风格的三个页面画出来,逐个取页面上的 data-dig:
① **页面上每一个**深挖入口都带这份报告的时间 —— 有评估窗口的(时段体检、时段榜、WDR)带窗口(带秒 / 快照号),
   此刻巡检 / 累计榜带取数时间。以后加了入口忘了带时间,这里先红。
② 每类入口的首句逐字钉住:页头「在会话里深挖」、「重新巡检 / 重新取数」、解读卡追问、Top SQL 调优、
   sql_id 抽屉的「看执行计划 / 调优」(抽屉要 DOM,这里给一个最小的假 document)。
「重新巡检 / 重新取数」是让模型重跑:有窗口的重跑同一个窗口,此刻巡检 / 累计榜只交代「上一份是 MM-DD HH:MM 的报告」。
没有 node 时跳过(交付环境的单测不依赖 node)。
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_FRONT = _ROOT / "frontend"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")

_SCRIPT = r"""
const base = process.argv[1];
const H = await import(base + '/pages/health.js');
const T = await import(base + '/pages/topsql.js');
const W = await import(base + '/pages/wdr.js');
const I = await import(base + '/lib/insight.js');
const S = await import(base + '/lib/store.js');
const D = await import(base + '/lib/details.js');
const VH = await import(base + '/views/normal/health.js');
const VT = await import(base + '/views/normal/topsql.js');
const VW = await import(base + '/views/normal/wdr.js');

const serve = (map) => { globalThis.fetch = async (path) => {
  const k = Object.keys(map).find((p) => path.endsWith(p));
  return k ? { ok: true, status: 200, json: async () => map[k] } : { ok: false, status: 404 };
}; };
const unesc = (s) => s.replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const digs = (html) => [...html.matchAll(/data-dig="([^"]*)"/g)].map((x) => unesc(x[1]));
const ctxOf = (html) => unesc((/data-ctx="([^"]*)"/.exec(html) || [])[1] || '');
const render = (V, m) => { const root = { innerHTML: '' }; V.render(root, { ...m, name: 'lab/og5' }, { key: 'k', whoami: 'u', range: null, target: { label: 'lab/og5' } }); return root.innerHTML; };

/* ---------------------------------------------------------------- 健康检查 */
const NOW0 = { at: '20261021T010000Z', file: '20261021T010000Z.json', overall: 1, counts: { 1: 1 } };          // 10-21 09:00
const NOW1 = { at: '20261022T060300Z', file: '20261022T060300Z.json', overall: 2, counts: { 2: 2, 1: 1 } };    // 10-22 14:03
const WIN0 = { begin_ts: '2026-10-16 10:00:05', end_ts: '2026-10-16 11:00:07' };
const WIN = { begin_ts: '2026-10-17 10:46:29', end_ts: '2026-10-17 11:06:30' };
const SPAN0 = { at: '20261018T110000Z', file: '20261018T110000Z.span.json', mode: 'span', window: WIN0, overall: 1, counts: { 1: 1 } };
const SPAN1 = { at: '20261018T124017Z', file: '20261018T124017Z.span.json', mode: 'span', window: WIN, overall: 2, counts: { 2: 1 } };
const F_SQL = { severity: 2, dimension: 'Slow SQL', code: 'SLOW_SQL', metric: 'avg_ms', value: '1500', threshold: '>1000', evidence: '平均 1.5 s', sql_id: '2858996927' };
const F_DL = { severity: 2, dimension: 'Transactions / Concurrency', code: 'DEADLOCKS', metric: 'deadlocks 距上次新增', value: '3', threshold: '>=1', evidence: '……把日志段贴给我……', sql_id: '' };
const F_IDLE = { severity: 1, dimension: 'Long & Idle Transactions', code: 'IDLE_IN_XACT', metric: 'idle_in_xact_min', value: '42', threshold: '>30', evidence: 'pid 1234 空闲 42 分钟', sql_id: '' };
const nowRep = { conn: 'lab/og5', overall: 2, findings: [F_SQL, F_DL, F_IDLE],
  dims: [{ dimension: 'Slow SQL', available: true, headline: '1 条慢 SQL', headers: ['sql_id', 'calls', 'avg_ms'], rows: [['2858996927', '10', '1500']] },
    { dimension: 'Long & Idle Transactions', available: true, headline: '1 个空闲事务', headers: ['pid', '时长'], rows: [['1234', '2520']] },
    { dimension: 'Transactions / Concurrency', available: true, headline: '死锁 3 次', headers: ['项', '值'], rows: [['deadlocks', '3'], ['commit/rollback', '1000/10 (1.0%)']] }],
  sub_skills: [{ skill: 'gaussdb-slowsql', ok: true }, { skill: 'gaussdb-vacuum', ok: false, error: 'permission denied' }] };
const spanRep = { conn: 'lab/og5', mode: 'span', window: WIN, overall: 2,
  findings: [{ severity: 2, dimension: 'Window IO / Temp / Checkpoint', code: 'WIN_TEMP_SPILL', metric: '时段临时文件溢出', value: '7.8G', threshold: '>512.0M', evidence: '库级合计', sql_id: '2858996927' },
    { severity: 2, dimension: 'Window Transactions', code: 'WIN_DEADLOCK', metric: '时段内死锁', value: '24 次', threshold: '>=1', evidence: '……把日志段贴给我……' }],
  dims: [{ dimension: 'Window Load', available: true, headline: '平均活跃会话 19', headers: ['指标', '值'], rows: [['平均活跃会话', '19.176']] },
    { dimension: 'Window Transactions', available: true, headline: '死锁 24 次', headers: ['指标', '值'], rows: [['死锁', '24']] },
    { dimension: 'Window IO / Temp / Checkpoint', available: true, headline: '溢出 7.8G', headers: ['指标', '值'], rows: [['临时文件溢出', '7.8G']] }], sub_skills: [] };
serve({ '/health/k/index.json': [NOW0, NOW1, SPAN0, SPAN1], '/health/k/latest.json': nowRep, [SPAN1.file]: spanRep });
const hNow = await H.load('k', '', null), hSpan = await H.load('k', SPAN1.file, null);
const hFail = H.model('k', { conn: 'lab/og5', overall: 0, findings: [], sub_skills: [],
  dims: [{ dimension: 'Overview', available: false, headline: '不可用', note: '连接中间件失败' }] }, [NOW0, NOW1], NOW1.file);
const health = {
  now: { digs: digs(render(VH, hNow)), ctx: ctxOf(render(VH, hNow)), chips: I.health(hNow).chips,
    redo: H.hRedoPrompt(hNow), dig: H.hDigPrompt(hNow), dim: H.dimPrompt(hNow.dims[1], hNow), about: H.hAbout({ ...hNow, name: 'lab/og5' }) },
  span: { digs: digs(render(VH, hSpan)), ctx: ctxOf(render(VH, hSpan)), chips: I.health(hSpan).chips,
    redo: H.hRedoPrompt(hSpan), dig: H.hDigPrompt(hSpan) },
  fail: { chips: I.health(hFail).chips },
};

/* ---------------------------------------------------------------- Top SQL */
const CUM = { at: '20261022T060500Z', file: '20261022T060500Z.avg.json' };                                      // 10-22 14:05
const SPAN_T = { at: '20261018T124105Z', file: '20261018T124105Z.time.span.json', mode: 'span', window: WIN };
const cumRep = { conn: 'lab/og5', by: 'avg', limit: 10, rows: [
  { sql_id: '222', calls: 1000, total_sec: 100, avg_ms: 100, rows: 0, query: 'select * from pg_stat_activity' },
  { sql_id: '111', calls: 20, total_sec: 30, avg_ms: 1500, rows: 0, query: 'select 1' },
  { sql_id: '333', calls: 2, total_sec: 10, avg_ms: 5000, rows: 0, query: 'select 3' }] };
const spanTRep = { conn: 'lab/og5', by: 'time', limit: 10, mode: 'span', window: WIN, rows: [{ sql_id: '1', calls: 2, total_sec: 3, avg_ms: 1500, rows: 0, query: 'q' }] };
const TSRV = { '/topsql/k/index.json': [CUM, SPAN_T], [CUM.file]: cumRep, [SPAN_T.file]: spanTRep,
  '/sqltune/k/222.json': { skipped: 'system', system_objects: ['pg_stat_activity'] } };
serve(TSRV);
const tCum = await T.load('k', 'avg', null, ''), tSpan = await T.load('k', 'avg', null, SPAN_T.file);
const topsql = {
  cum: { digs: digs(render(VT, tCum)), ctx: ctxOf(render(VT, tCum)), chips: I.topsql(tCum).chips, redo: T.tRedoPrompt(tCum), dig: T.tDigPrompt(tCum) },
  span: { digs: digs(render(VT, tSpan)), ctx: ctxOf(render(VT, tSpan)), redo: T.tRedoPrompt(tSpan), dig: T.tDigPrompt(tSpan), tune: T.tunePrompt(tSpan.rows[0], tSpan) },
  bare: T.tunePrompt(cumRep.rows[1]),
};

/* ---------------------------------------------------------------- WDR */
const WE0 = { at: '20261017T030000Z', file: '20261017T030000Z.json' }, WE1 = { at: '20261017T031000Z', file: '20261017T031000Z.json' };
const wdims = (dbt, spill) => [{ dimension: 'Load Profile', headers: ['DB time', 'commits', 'CPU占DBtime', '物理读'], rows: [[String(dbt), '100', '40', '1000']] },
  { dimension: 'Database Stat', headers: ['cache_hit', '临时溢出', '回滚率'], rows: [['99', String(spill), '1']] },
  { dimension: 'Top SQL', headers: ['sql_id', '占DB', 'query'], rows: [['4242', '50', 'select 2']] }];
const w0 = { conn: 'lab/og5', overall: 0, window: { begin_id: 2971, end_id: 2978, begin_ts: '2026-10-17 10:20:01', end_ts: '2026-10-17 10:43:02', duration_min: 23 }, findings: [], dims: wdims(1000, 50), native: {} };
const w1 = { conn: 'lab/og5', overall: 2, window: { begin_id: 2980, end_id: 2987, begin_ts: '2026-10-17 10:45:03', end_ts: '2026-10-17 11:08:10', duration_min: 23 },
  findings: [{ severity: 2, dimension: 'Wait Events / Classes', code: 'WDR_IO', metric: 'IO_EVENT 等待占比', value: '40%', threshold: '>30%', evidence: 'sqlid 4242 读得最多' }],
  dims: wdims(3000, 500), sql_texts: { 4242: 'select 2 from t' }, native: {} };
serve({ '/wdr/k/index.json': [WE0, WE1], '/wdr/k/latest.json': w1, [WE0.file]: w0, [WE1.file]: w1 });
const wm = await W.load('k', '', null);
const wdr = { digs: digs(render(VW, wm)), ctx: ctxOf(render(VW, wm)), chips: I.wdr(wm).chips, dig: W.wDigPrompt(wm) };

/* ---------------------------------------------------------------- sql_id 抽屉(最小的假 document) */
const els = {};
globalThis.document = { getElementById: (id) => els[id] || null, querySelector: () => null,
  body: { insertAdjacentHTML: (_, html) => { els[/id="([^"]+)"/.exec(html)[1]] = { innerHTML: '', classList: { add() {}, remove() {} } }; } } };
globalThis.requestAnimationFrame = (f) => f();
const drawerDigs = async (id) => { await D.openSql(id, 'k'); return digs(els.drawer.innerHTML); };
serve(TSRV);
S.clearModels(); S.setModel('topsql', tCum);
const dTop = await drawerDigs('111');
S.clearModels(); S.setModel('wdr', wm);
const dWdr = await drawerDigs('4242');
S.clearModels(); S.setModel('health', hNow);
const dHealth = await drawerDigs('2858996927');
S.clearModels();
const dNone = await drawerDigs('999');
console.log(JSON.stringify({ health, topsql, wdr, drawer: { dTop, dWdr, dHealth, dNone } }));
"""

_NOW = "10-22 14:03"
_WINQ = "10-17 10:46:29 – 11:06:30"
_CUM = "10-22 14:05"
_WDRW = "WDR 窗口 2026-10-17 10:45:03 – 2026-10-17 11:08:10(快照 2980 → 2987)"


@pytest.fixture(scope="module")
def got():
    env = {**os.environ, "TZ": "Asia/Shanghai"}
    out = subprocess.run(["node", "--input-type=module", "-e", _SCRIPT, "file://%s" % _FRONT],
                         capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _all_carry(prompts, when, where):
    assert prompts, "%s 页面上一个深挖入口都没找到" % where
    bad = [p for p in prompts if when not in p]
    assert not bad, "%s 有深挖入口没带报告时间「%s」:\n  %s" % (where, when, "\n  ".join(bad))
    old = [p for p in prompts if "最近一次" in p or "最近一个" in p]
    assert not old, "%s 还有写「最近一次」的首句(看的可能不是最近一份):%s" % (where, old)


def test_every_dig_on_each_page_carries_the_report_time(got):
    h, t, w = got["health"], got["topsql"], got["wdr"]
    _all_carry(h["now"]["digs"], _NOW, "健康检查(此刻巡检)")
    _all_carry(h["span"]["digs"], _WINQ, "健康检查(时段体检)")
    _all_carry(t["cum"]["digs"], _CUM, "Top SQL(累计榜)")
    _all_carry(t["span"]["digs"], _WINQ, "Top SQL(时段榜)")
    _all_carry(w["digs"], "快照 2980 → 2987", "WDR")
    # 解读卡追问(普通风格画在页上,⌘J 侧栏的建议问题也是它)
    for name, chips, when in [("健康检查", h["now"]["chips"], _NOW), ("时段体检", h["span"]["chips"], _WINQ),
                              ("采集全失败", h["fail"]["chips"], _NOW), ("Top SQL", t["cum"]["chips"], _CUM), ("WDR", w["chips"], _WDRW)]:
        _all_carry([p for _, p in chips], when, name + "解读卡追问")


def test_health_prompts(got):
    now, span, fail = got["health"]["now"], got["health"]["span"], dict(got["health"]["fail"]["chips"])
    assert now["dig"] == "请基于%s 的健康检查报告做一次深入分析,给出处理优先级。" % _NOW
    assert span["dig"] == "请基于%s 的时段体检报告做一次深入分析,给出处理优先级。" % _WINQ
    # 重新巡检:此刻巡检交代上一份的时间供对比,不让模型去分析旧报告;时段体检重跑同一个窗口
    assert now["redo"] == "请对当前库重新做一次完整健康检查。上一份是 %s 的报告,供对比。" % _NOW
    assert span["redo"] == "请对当前库重新做一次 %s 的时段体检。" % _WINQ
    assert now["redo"] in now["digs"] and now["dig"] in now["digs"], "页头两个按钮用的是同一个函数"
    chips = dict(now["chips"])
    assert chips["给出处置顺序"] == now["dig"]
    assert chips["和上次比变了什么"] == "请对比本次(%s)与上次(10-21 09:00)的健康检查,说明新增和消失的发现。" % _NOW
    assert dict(span["chips"])["和上次比变了什么"] == "请对比本次(%s)与上次(10-16 10:00:05 – 11:00:07)的时段体检,说明新增和消失的发现。" % _WINQ
    assert chips["vacuum 为什么失败"] == "请分析%s 的健康检查里子技能 gaussdb-vacuum 为什么执行失败(permission denied)。" % _NOW
    assert chips["解释 2858996927 为什么慢"] == "请解释并优化 sql_id 为 2858996927 的语句:%s 的健康检查发现 SLOW_SQL,avg_ms=1500(阈值 >1000)。" % _NOW
    # 此刻巡检的发现表与维度抽屉也带巡检时间(v1.3.0 只给时段体检带了窗口);死锁仍走专门问法,不搬「贴给我」
    dl = [p for p in now["digs"] if "死锁" in p][0]
    assert dl.startswith("请分析%s 的健康检查发现的死锁(deadlocks 距上次新增=3)" % _NOW) and "贴给我" not in dl
    assert now["dim"] == "请深入分析%s 健康检查的「长事务与空闲事务」维度:1 个空闲事务" % _NOW
    assert fail["排查采集为什么失败"] == "请排查%s 的健康检查为什么一个维度都没有采到:连接中间件失败" % _NOW
    assert fail["检查中间件连通性"] == "请检查当前实例到中间件的连通性,并说明怎么修复(%s 的健康检查一个维度都没有采到)。" % _NOW
    assert fail["重新巡检"] == now["redo"]
    # 解读卡提问框的前缀:时段体检的窗口也带秒(与深挖首句同一个口径)
    assert now["ctx"] == now["about"] == "关于实例 lab/og5 在 %s 的健康检查报告:" % _NOW
    assert span["ctx"] == "关于实例 lab/og5 在 %s 这段时间的时段体检报告:" % _WINQ


def test_topsql_prompts(got):
    cum, span = got["topsql"]["cum"], got["topsql"]["span"]
    src = "%s 取数的 Top SQL 累计榜" % _CUM
    assert cum["dig"] == "请基于%s(按平均耗时)分析哪些语句最值得优化,给出优先级。" % src
    assert span["dig"] == "请基于%s 的 Top SQL 时段榜(按总耗时)分析哪些语句最值得优化,给出优先级。" % _WINQ
    # 重新取数:按当前页签的维度取;累计榜交代上一份的时间,时段榜重取同一个窗口
    assert cum["redo"] == "请重新取一次当前库的 Top SQL(按平均耗时)。上一份是 %s 的报告,供对比。" % _CUM
    assert span["redo"] == "请重新取一次 %s 的 Top SQL(按总耗时)。" % _WINQ
    assert cum["redo"] in cum["digs"] and cum["dig"] in cum["digs"]
    tune111 = "请调优 sql_id 为 111 的语句(%s上调用 20 次,平均 1.5 s):select 1" % src
    assert tune111 in cum["digs"], "榜单与调优进度里的「调优」"
    chips = dict(cum["chips"])
    assert chips["调优 111"] == tune111 and chips["先调哪一条"] == cum["dig"]
    assert chips["系统 SQL 为什么占这么多"] == "请分析%s里系统语句 222 为什么占上榜耗时 71.4%%,是否需要调整监控采集频率。" % src
    assert not any("sql_id 为 222" in p for p in cum["digs"]), "系统 SQL 按策略不给调优入口"
    assert span["tune"] == "请调优 sql_id 为 1 的语句(%s 的 Top SQL 时段榜上调用 2 次,平均 1.5 s):q" % _WINQ
    assert got["topsql"]["bare"] == "请调优 sql_id 为 111 的语句(调用 20 次,平均 1.5 s):select 1", "没有榜单模型时照旧"
    assert cum["ctx"] == "关于实例 lab/og5 在 %s 取的 Top SQL 榜单:" % _CUM
    assert span["ctx"] == "关于实例 lab/og5 在 %s 的 Top SQL(窗口内增量):" % _WINQ


def test_wdr_prompts(got):
    w = got["wdr"]
    assert w["dig"] == "请分析%s的 DB Time 构成,说明是算力型还是等待型负载。" % _WDRW and w["dig"] in w["digs"]
    chips = dict(w["chips"])
    assert chips["4242 为什么占这么多 DB Time"] == "请分析%s内 sql_id 为 4242 的语句为什么占这么多 DB Time。" % _WDRW
    assert chips["IO 等待从哪来"] == "请分析%s里 IO_EVENT 等待的来源:哪些对象、哪些文件、哪些语句。" % _WDRW
    # 说「上升 / 变化」的带上对比的那个窗口
    assert chips["临时溢出怎么降"] == "请找出%s里临时溢出的语句,并给出 work_mem 或改写建议:临时溢出比上一窗口(快照 2971 → 2978)明显上升。" % _WDRW
    assert chips["负载为什么变了"] == "请解读%s,说明与上一窗口(快照 2971 → 2978)相比负载变化的主要原因。" % _WDRW
    assert w["ctx"] == "关于实例 lab/og5 的%s:" % _WDRW


def test_sql_drawer_prompts_follow_the_report_it_came_from(got):
    d = got["drawer"]
    plan = "请取 sql_id 为 %s 的执行计划并解读（必要时对比改写前后的计划）。"
    # 在 Top SQL 榜上:调优用同一个 tunePrompt,看计划写出自哪一份榜
    assert d["dTop"] == ["请调优 sql_id 为 111 的语句(%s 取数的 Top SQL 累计榜上调用 20 次,平均 1.5 s):select 1" % _CUM,
                         plan % "111" + "这条语句出自%s 取数的 Top SQL 累计榜。" % _CUM]
    # 不在榜上、WDR 窗口里有它
    assert d["dWdr"] == ["请调优 sql_id 为 4242 的语句。这条语句出自%s。" % _WDRW, plan % "4242" + "这条语句出自%s。" % _WDRW]
    # 不在榜上、健康检查的发现里有它
    assert d["dHealth"] == ["请调优 sql_id 为 2858996927 的语句。这条语句出自%s 的健康检查。" % _NOW,
                            plan % "2858996927" + "这条语句出自%s 的健康检查。" % _NOW]
    # 哪份报告都没有它:不猜
    assert d["dNone"] == ["请调优 sql_id 为 999 的语句。", plan % "999"]
