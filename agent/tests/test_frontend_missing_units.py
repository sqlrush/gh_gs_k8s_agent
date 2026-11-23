"""索引里登记着、报告文件却不在(404)时,大盘退回同一类的上一份并说明;一份都取不到才报错。

2026-10-24 测试环境:NAS 放在 Mac 的 /tmp 下,macOS 每天 0 点的 tmp_cleaner 把三天没碰过的报告文件删了,
index.json 每次存档都重写、留了下来 —— Top SQL 累计榜最新一份(也是唯一一份)404,整页「报告读取失败 HTTP 404」。
在 Node 里用真的取数与建模(fetch 换成假的)钉住:
① 只退到同一类:健康检查同一种口径(此刻巡检不拿时段体检顶)、Top SQL 同一维度同一种榜;
② 页面上写明「最新一份报告(…)读取失败,显示的是 … 的那份」/「所选的那份…」;两种风格都写;
③ 只有 404 才退,网关 / 权限这类错误照常报错;一份都取不到 → reason 'gone',页面说清是文件不在了、给重跑;
   Top SQL 这一页签取不到时页签照常能切,再指一份最近的时段榜(不自动顶替)。
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
const M = await import(base + '/lib/missed.js');
const VH = await import(base + '/views/normal/health.js');
const VT = await import(base + '/views/normal/topsql.js');
const VW = await import(base + '/views/normal/wdr.js');
const PAPER = await import(base + '/views/press/paper.js');

let asked = [];
const serve = (map, fail = []) => { asked = []; globalThis.fetch = async (path) => {
  asked.push(path.split('/').pop());
  if (fail.some((p) => path.endsWith(p))) return { ok: false, status: 500 };
  const k = Object.keys(map).find((p) => path.endsWith(p));
  return k ? { ok: true, status: 200, json: async () => map[k], text: async () => '' } : { ok: false, status: 404 };
}; };
const text = (html) => html.replace(/<[^>]+>/g, ' ').replace(/&quot;/g, '"').replace(/\s+/g, ' ');
const ctx = { key: 'k', whoami: 'u', range: null, run: {}, target: { label: 'lab/og5' } };
const render = (V, m) => { const root = { innerHTML: '' }; V.render(root, m.ok && !m.missing ? { ...m, name: 'lab/og5' } : m, ctx); return root.innerHTML; };
const brief = (m) => ({ ok: m.ok, reason: m.reason || '', file: m.entry ? m.entry.file : '', missed: (m.missed || []).map((e) => e.file), note: M.missedText(m) });

/* ---------------------------------------------------------------- 健康检查 */
const WIN0 = { begin_ts: '2026-10-16 10:00:05', end_ts: '2026-10-16 11:00:07' }, WIN1 = { begin_ts: '2026-10-17 10:46:29', end_ts: '2026-10-17 11:06:30' };
const NOW0 = { at: '20261021T010000Z', file: '20261021T010000Z.json', overall: 0, counts: {} };                     // 10-21 09:00,在
const NOW1 = { at: '20261022T060300Z', file: '20261022T060300Z.json', overall: 0, counts: {} };                     // 10-22 14:03,不在
const SPAN0 = { at: '20261018T110000Z', file: '20261018T110000Z.span.json', mode: 'span', window: WIN0, overall: 0, counts: {} };   // 在
const SPAN1 = { at: '20261022T070000Z', file: '20261022T070000Z.span.json', mode: 'span', window: WIN1, overall: 0, counts: {} };   // 不在
const dims = [{ dimension: 'Overview', available: true, headline: '正常', headers: ['cache_hit'], rows: [['99.1']] }];
const hRep = { conn: 'lab/og5', overall: 0, findings: [], dims, sub_skills: [] };
const hSpan = { ...hRep, mode: 'span', window: WIN0 };
const HIDX = { '/health/k/index.json': [SPAN0, NOW0, NOW1, SPAN1] };   // 按 at 升序,与 common/reports.py 写的一样
serve({ ...HIDX, [NOW0.file]: hRep, [SPAN0.file]: hSpan });
const hLatest = await H.load('k', '', null);
const hPick = await H.load('k', NOW1.file, null);
const hRange = await H.load('k', '', { from: '2026-10-21T00:00', to: '2026-10-23T00:00' });
const health = { latest: brief(hLatest), pick: brief(hPick), range: brief(hRange),
  pickMode: hPick.span, banner: text(render(VH, hPick)) };
serve({ ...HIDX, '/health/k/latest.json': hRep, [NOW0.file]: hRep });
health.ok = brief(await H.load('k', '', null));
serve({ ...HIDX, [NOW0.file]: hRep }, [SPAN1.file]);
health.err = brief(await H.load('k', '', null));
serve(HIDX);
const hGone = await H.load('k', '', null);
health.gone = { ...brief(hGone), page: text(render(VH, hGone)) };
// u9500 的样子:此刻巡检全丢了、只剩时段体检;点开一份旧巡检 → 取不到,但要给一条出路(选中的那份一直钉着)
serve({ ...HIDX, [SPAN0.file]: hSpan });
const hStuck = await H.load('k', NOW1.file, null);
health.stuck = { ...brief(hStuck), alt: hStuck.alt && hStuck.alt.file, page: render(VH, hStuck) };

/* ---------------------------------------------------------------- Top SQL */
const tq = { conn: 'lab/og5', by: 'time', limit: 10, rows: [{ sql_id: '1', calls: 2, total_sec: 3, avg_ms: 1500, rows: 0, query: 'q' }] };
// u9400 的样子:累计榜只有一份(10-17 23:18),文件不在;其余都是时段榜
const C17 = { at: '20261017T151849Z', file: '20261017T151849Z.time.json' };
const S21 = { at: '20261021T134937Z', file: '20261021T134937Z.time.span.json', mode: 'span', window: WIN0 };
const S23 = { at: '20261023T033809Z', file: '20261023T033809Z.time.span.json', mode: 'span', window: WIN1 };
serve({ '/topsql/k/index.json': [C17, S21, S23], [S21.file]: { ...tq, mode: 'span', window: WIN0 }, [S23.file]: { ...tq, mode: 'span', window: WIN1 } });
const tGone = await T.load('k', 'time', null, '');
const topsql = { gone: { ...brief(tGone), alt: tGone.alt && tGone.alt.file, page: render(VT, tGone) } };
// 累计榜最新一份不在 → 退到同一维度的上一份累计榜,不拿平均耗时榜或时段榜顶
const C20 = { at: '20261020T020000Z', file: '20261020T020000Z.time.json' };
const A21 = { at: '20261021T020000Z', file: '20261021T020000Z.avg.json' };
const C22 = { at: '20261022T020000Z', file: '20261022T020000Z.time.json' };
const S22 = { at: '20261022T030000Z', file: '20261022T030000Z.time.span.json', mode: 'span', window: WIN0 };
const S22b = { at: '20261022T040000Z', file: '20261022T040000Z.time.span.json', mode: 'span', window: WIN1 };
serve({ '/topsql/k/index.json': [C20, A21, C22, S22, S22b], [C20.file]: tq, [A21.file]: { ...tq, by: 'avg' }, [S22.file]: { ...tq, mode: 'span', window: WIN0 } });
const tBack = await T.load('k', 'time', null, '');
topsql.back = { ...brief(tBack), banner: text(render(VT, tBack)) };
// 直达链接钉住的时段榜不在 → 退到上一份同维度的时段榜
const tPin = await T.load('k', 'time', null, S22b.file);
topsql.pin = { ...brief(tPin), span: tPin.span };

/* ---------------------------------------------------------------- WDR */
const wwin = (a, b) => ({ begin_id: a, end_id: b, begin_ts: '2026-10-17 10:00:00', end_ts: '2026-10-17 10:30:00', duration_min: 30 });
const wRep = (a, b) => ({ conn: 'lab/og5', overall: 0, window: wwin(a, b), findings: [], dims: [], native: {} });
const WE = [1, 2, 3, 4].map((i) => ({ at: `2026102${i}T020000Z`, file: `2026102${i}T020000Z.json`, window: wwin(10 * i, 10 * i + 1) }));
serve({ '/wdr/k/index.json': WE, [WE[0].file]: wRep(10, 11), [WE[1].file]: wRep(20, 21) });
const wPick = await W.load('k', WE[2].file, null);
const wRange = await W.load('k', '', { from: '2026-10-17T09:00', to: '2026-10-17T12:00' });
const wdr = { pick: { ...brief(wPick), prev: wPick.p && wPick.p.window.begin_id, banner: text(render(VW, wPick)) }, range: brief(wRange) };
// u9400 真数据的样子:清理从最老的删起,点开一份已丢的旧窗口时比它更旧的也都没了 → 显示离它最近的较新一份
serve({ '/wdr/k/index.json': WE, [WE[2].file]: wRep(30, 31), [WE[3].file]: wRep(40, 41) });
wdr.forward = brief(await W.load('k', WE[1].file, null));

/* ---------------------------------------------------------------- 文艺风格:同一份说明 */
globalThis.document = { getElementById: () => null };
serve({ ...HIDX, [NOW0.file]: hRep, [SPAN0.file]: hSpan, '/topsql/k/index.json': [C17, S21, S23], '/wdr/k/index.json': WE, [WE[1].file]: wRep(20, 21), [WE[0].file]: wRep(10, 11) });
const paperRoot = { innerHTML: '' };
await PAPER.render(paperRoot, ctx);
console.log(JSON.stringify({ health, topsql, wdr, paper: text(paperRoot.innerHTML) }));
"""


@pytest.fixture(scope="module")
def got():
    env = {**os.environ, "TZ": "Asia/Shanghai"}
    out = subprocess.run(["node", "--input-type=module", "-e", _SCRIPT, "file://%s" % _FRONT],
                         capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_SPAN1 = "时段 10-17 10:46 – 11:06"
_SPAN0 = "时段 10-16 10:00 – 11:00"


def test_health_falls_back_within_the_same_mode(got):
    h = got["health"]
    # 没有 latest.json(只跑过时段体检)、最新的时段体检文件不在 → 退到上一份时段体检,不拿此刻巡检顶
    assert h["latest"]["file"] == "20261018T110000Z.span.json" and h["latest"]["missed"] == ["20261022T070000Z.span.json"]
    assert h["latest"]["note"] == "最新一份报告（%s）读取失败，显示的是 %s 的那份。" % (_SPAN1, _SPAN0)
    # 指定的那份(此刻巡检)不在 → 退到上一份此刻巡检
    assert h["pick"]["file"] == "20261021T010000Z.json" and h["pickMode"] is False
    assert h["pick"]["note"] == "所选的那份报告（10-22 14:03）读取失败，显示的是 10-21 09:00 的那份。"
    assert h["pick"]["note"] in h["banner"] and "文件已不在存档目录里" in h["banner"], "普通风格页头下有这一条"
    # 时间段内最新一份不在 → 退到时间段内的上一份
    assert h["range"]["file"] == "20261021T010000Z.json"
    assert h["range"]["note"] == "最新一份报告（10-22 14:03）读取失败，显示的是 10-21 09:00 的那份。"
    # latest.json 在就用它,不提示
    assert h["ok"]["ok"] is True and h["ok"]["missed"] == [] and h["ok"]["note"] == ""


def test_health_only_404_falls_back_and_all_gone_is_an_explained_error(got):
    h = got["health"]
    assert h["err"]["ok"] is False and h["err"]["reason"] == "error", "500 之类不是「文件不在」,不能拿旧报告冒充"
    g = h["gone"]
    assert g["ok"] is False and g["reason"] == "gone" and len(g["missed"]) == 2
    assert "报告文件读取不到" in g["page"] and "索引里登记的 2 份报告（最近的是 %s）文件都已不在存档目录里" % _SPAN1 in g["page"]
    assert "在会话里跑一次健康检查" in g["page"], "给一键重跑"
    s = h["stuck"]
    assert s["reason"] == "gone" and s["missed"] == ["20261022T060300Z.json", "20261021T010000Z.json"], "只在此刻巡检里找"
    assert s["alt"] == "20261022T070000Z.span.json"
    assert 'data-pick="health|20261022T070000Z.span.json"' in s["page"] and "看时段体检（%s）" % _SPAN1 in s["page"], "报错页上给一条出路"


def test_topsql_gone_tab_keeps_tabs_and_points_to_a_span_board(got):
    g = got["topsql"]["gone"]
    # u9400 的样子:累计榜唯一一份不在。不能退到时段榜(那是另一种榜),也不整页报 HTTP 404
    assert g["ok"] is False and g["reason"] == "gone" and g["missed"] == ["20261017T151849Z.time.json"]
    assert g["alt"] == "20261023T033809Z.time.span.json", "指一份最近的时段榜"
    page = g["page"]
    assert "「总耗时」的报告文件读取不到" in page and "HTTP 404" not in page
    assert 'data-pick="topsql|20261023T033809Z.time.span.json"' in page and "看最近一份时段榜（%s）" % _SPAN1 in page
    assert 'data-by="avg"' in page, "页签照常画,能切到别的维度"
    assert "请按总耗时取一次 Top SQL。" in page


def test_topsql_falls_back_within_the_same_dimension_and_kind(got):
    b, p = got["topsql"]["back"], got["topsql"]["pin"]
    assert b["file"] == "20261020T020000Z.time.json", "同一维度的上一份累计榜,不是平均耗时榜、不是时段榜"
    assert b["note"] == "最新一份报告（10-22 10:00）读取失败，显示的是 10-20 10:00 的那份。" and b["note"] in b["banner"]
    assert p["file"] == "20261022T030000Z.time.span.json" and p["span"] == "span"
    assert p["note"] == "所选的那份报告（%s）读取失败，显示的是 %s 的那份。" % (_SPAN1, _SPAN0)


def test_wdr_falls_back(got):
    w = got["wdr"]
    assert w["pick"]["file"] == "20261022T020000Z.json" and w["pick"]["prev"] == 10, "上一窗口照常找"
    assert w["pick"]["note"] == "所选的那份报告（10-23 10:00）读取失败，显示的是 10-22 10:00 的那份。" and w["pick"]["note"] in w["pick"]["banner"]
    assert w["range"]["file"] == "20261022T020000Z.json" and w["range"]["missed"] == ["20261024T020000Z.json", "20261023T020000Z.json"]
    assert w["range"]["note"] == "最新的 2 份报告读取失败（最近的一份是 10-24 10:00），显示的是 10-22 10:00 的那份。"


def test_picked_old_report_whose_older_ones_are_gone_too_shows_the_nearest_newer(got):
    """清理总是从最老的删起:点开一份已丢的旧报告,往前全是空的。只往前退就只能报错 —— 改为先旧后新、找离它最近的。"""
    f = got["wdr"]["forward"]
    assert f["ok"] is True and f["file"] == "20261023T020000Z.json"
    assert f["missed"] == ["20261022T020000Z.json", "20261021T020000Z.json"], "先试想要的那份、再试上一份,然后才往新的找"
    assert f["note"] == "所选的那份报告（10-22 10:00）读取失败，显示的是 10-23 10:00 的那份。"


def test_press_style_says_the_same(got):
    paper = got["paper"]
    assert "最新一份报告（%s）读取失败，显示的是 %s 的那份。" % (_SPAN1, _SPAN0) in paper, "头条(健康检查)"
    assert "最新的 2 份报告读取失败（最近的一份是 10-24 10:00），显示的是 10-22 10:00 的那份。" in paper, "C1 WDR"
    assert "索引里登记的那份报告（10-17 23:18）文件已不在存档目录里" in paper, "B1 Top SQL 取不到时说清是文件不在了"
