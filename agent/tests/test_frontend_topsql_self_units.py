"""大盘 Top SQL 的「自身耗时」页签与「等待占比」列(gh_skill 60615b4:topsql 加 --by self / self_avg)。

2026-10-24 faultlab 在 og5 上测出,总耗时榜首是等锁的 UPDATE(CPU 0)—— 榜单答的是「谁等得最久」,用户问的是
「谁最拖慢系统」。技能用同源可减的「自身耗时 = CPU + 数据 IO」,等待占比 = 1 − 自身 / 总耗时(含等锁、轻量锁、
排队等 CPU,分不出是哪种)。在 Node 里用真的取数与建模(fetch 换成假的)把页面画出来,钉住:
① 两个新页签;存档文件名 <时间戳>.self_avg.json(维度带下划线)认得出;
② 累计榜每行的 cpu_sec / io_sec、时段榜的 self_sec 都能算出等待占比;≥80% 的标出来,解读卡说「主要在等,看锁阻塞」,
   这样的语句不给「调优」、给「被谁挡住」,首句照旧带时间与「按<排序维度>」;
③ 老报告(没有这几列)不画这一列、不写 0;某一行缺一列写「—」,不当 0。
没有 node 时跳过(交付环境的单测不依赖 node)。
"""
import html
import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_FRONT = _ROOT / "frontend"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")

_SCRIPT = r"""
const base = process.argv[1];
const T = await import(base + '/pages/topsql.js');
const I = await import(base + '/lib/insight.js');
const VT = await import(base + '/views/normal/topsql.js');
const PAPER = await import(base + '/views/press/paper.js');

const serve = (map) => { globalThis.fetch = async (path) => {
  const k = Object.keys(map).find((p) => path.endsWith(p));
  return k ? { ok: true, status: 200, json: async () => map[k], text: async () => '' } : { ok: false, status: 404 };
}; };
const ctx = { key: 'k', whoami: 'u', range: null, run: {}, target: { label: 'lab/og5' } };
const render = (m) => { const root = { innerHTML: '' }; VT.render(root, m.ok && !m.missing ? { ...m, name: 'lab/og5' } : m, ctx); return root.innerHTML; };
const unesc = (s) => s.replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const text = (html) => unesc(html.replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ');

// L1:等锁的 UPDATE(CPU 几乎为 0,99% 在等);S3:自己慢(20% 在等);Q7:缺一列(老白名单 + 新列的混合情况)
const L1 = { sql_id: 'L1', query: 'UPDATE fl.acct SET bal = bal - $1 WHERE id = $2', calls: 2, total_sec: 600, avg_ms: 300000, rows: 0, cpu_sec: 0.0, io_sec: 6.0 };
const S3 = { sql_id: 'S3', query: 'SELECT * FROM orders o WHERE NOT IN (SELECT ...)', calls: 100, total_sec: 50, avg_ms: 500, rows: 100, cpu_sec: 30, io_sec: 10 };
const Q7 = { sql_id: 'Q7', query: 'SELECT 7', calls: 10, total_sec: 5, avg_ms: 500, rows: 10, cpu_sec: 1.0, io_sec: null };
const old = (r) => { const { cpu_sec, io_sec, ...rest } = r; return rest; };
const rep = (by, rows, extra = {}) => ({ conn: 'lab/og5', by, limit: 10, rows, ...extra });
const WIN = { begin_ts: '2026-10-24 09:00:05', end_ts: '2026-10-24 09:20:07' };
const OLD = { at: '20261023T010000Z', file: '20261023T010000Z.time.json' };                       // 10-23 09:00,老报告
const SA = { at: '20261024T010000Z', file: '20261024T010000Z.self_avg.json' };                    // 10-24 09:00
const TM = { at: '20261024T020000Z', file: '20261024T020000Z.time.json' };                        // 10-24 10:00
const SP = { at: '20261024T030000Z', file: '20261024T030000Z.time.span.json', mode: 'span', window: WIN };
serve({ '/topsql/k/index.json': [OLD, SA, TM, SP],
  [OLD.file]: rep('time', [old(L1), old(S3)]), [SA.file]: rep('self_avg', [S3, L1, Q7]), [TM.file]: rep('time', [L1, S3, Q7]),
  [SP.file]: rep('time', [{ ...old(L1), total_sec: 100, self_sec: 2, is_new: false }, { ...old(S3), total_sec: 10, self_sec: 8, is_new: false }], { mode: 'span', window: WIN }) });

const out = {};
out.per = Object.keys(T.latestPerBy([OLD, SA, TM, SP]));
out.unit = { l1: [T.selfSec(L1), T.waitRatio(L1)], s3: [T.selfSec(S3), T.waitRatio(S3)], q7: [T.selfSec(Q7), T.waitRatio(Q7)],
  oldRow: T.selfSec(old(L1)), spanRow: T.waitRatio({ total_sec: 100, self_sec: 2 }), zero: T.waitRatio({ total_sec: 10, cpu_sec: 0, io_sec: 0 }) };
const sa = await T.load('k', 'self_avg', null, '');
const saIns = I.topsql(sa);
out.sa = { by: sa.by, file: sa.entry.file, page: render(sa), lines: saIns.lines.join(' | '), chips: saIns.chips };
const tm = await T.load('k', 'time', null, '');
const tmIns = I.topsql(tm);
out.tm = { page: render(tm), head: tmIns.head, lines: tmIns.lines.join(' | '), chips: tmIns.chips, dig: T.tDigPrompt(tm), redo: T.tRedoPrompt(tm) };
const od = await T.load('k', 'time', null, OLD.file);
out.old = { page: render(od), lines: I.topsql(od).lines.join(' | '), chips: I.topsql(od).chips };
const sp = await T.load('k', 'time', null, SP.file);
out.sp = { span: sp.span, page: render(sp), chips: I.topsql(sp).chips };
out.missing = render(await T.load('k', 'self', null, ''));
globalThis.document = { getElementById: () => null };
const paper = { innerHTML: '' };
await PAPER.render(paper, ctx);
out.paper = paper.innerHTML;
console.log(JSON.stringify(out));
"""


def text(h: str) -> str:
    """页面 HTML 去掉标签与转义后的文字。"""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h)))


@pytest.fixture(scope="module")
def got():
    env = {**os.environ, "TZ": "Asia/Shanghai"}
    out = subprocess.run(["node", "--input-type=module", "-e", _SCRIPT, "file://%s" % _FRONT],
                         capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_two_new_tabs_and_their_archive_names_are_recognised(got):
    assert set(got["per"]) == {"time", "self_avg"}, "<时间戳>.self_avg.json:维度名带下划线也要认得出"
    sa = got["sa"]
    assert sa["by"] == "self_avg" and sa["file"] == "20261024T010000Z.self_avg.json"
    assert 'data-by="self"' in sa["page"] and 'data-by="self_avg"' in sa["page"]
    assert ">自身耗时<" in sa["page"] and ">平均自身耗时<" in sa["page"]
    assert "「自身耗时」这个维度还没取过" in got["missing"] and "请按自身耗时取一次 Top SQL。" in got["missing"]


def test_self_seconds_and_wait_ratio_follow_the_skill(got):
    u = got["unit"]
    assert u["l1"] == [6.0, 0.99]
    assert u["s3"][0] == 40 and abs(u["s3"][1] - 0.2) < 1e-9
    assert u["q7"] == [None, None], "缺一列:不知道,不是 0"
    assert u["oldRow"] is None
    assert abs(u["spanRow"] - 0.98) < 1e-9, "时段 / 增量榜带的是 self_sec"
    assert u["zero"] == 1, "CPU 与 IO 都是 0:那就是全在等"


def test_wait_column_marks_the_waiters_and_the_card_points_to_lock_blocking(got):
    sa, tm = got["sa"], got["tm"]
    page = sa["page"]
    assert ">等待占比<" in page and '<th class="r">平均自身耗时<svg' in page, "按平均自身耗时排:这一列带排序箭头"
    assert 'data-lv="2"' in page and "99%" in page, "99% 在等的那一行标出来"
    assert "20%" in page and "—" in text(page), "S3 不标;Q7 缺一列写「—」"
    assert "自身耗时 = CPU + 数据 IO" in text(page), "按自身耗时排时说明口径"
    assert "主要在等" in sa["lines"] and "先看锁阻塞" in sa["lines"]
    assert "「自身耗时」页签" not in sa["lines"], "已经在自身耗时页签上,不再指过去"
    # 总耗时榜:榜首在等锁 —— 标题不说「最值得先看」,不给调优、给「被谁挡住」,并指去「自身耗时」页签
    assert tm["head"] == "L1 占上榜耗时 91.6%，但它主要在等，先看锁阻塞"           # 600 / (600 + 50 + 5)
    assert "不含等待的排名看「自身耗时」页签" in tm["lines"]
    chips = dict(tm["chips"])
    assert "调优 L1" not in chips and "L1 怎么优化" not in chips
    assert chips["L1 被谁挡住"] == ("请查10-24 10:00 取数的 Top SQL 累计榜(按总耗时)里 sql_id 为 L1 的语句被谁挡住了:"
                                  "它的耗时有 99% 不是 CPU / 数据 IO,主要在等(等锁、等轻量锁、排队等 CPU 都算在内)。")
    assert "等待占比 80% 以上的已标出" in text(tm["page"])


def test_deep_dive_prompts_keep_time_and_dimension(got):
    sa, tm = got["sa"], got["tm"]
    assert dict(sa["chips"])["先调哪一条"] == "请基于10-24 09:00 取数的 Top SQL 累计榜(按平均自身耗时)分析哪些语句最值得优化,给出优先级。"
    assert tm["redo"] == "请重新取一次当前库的 Top SQL(按总耗时)。上一份是 10-24 10:00 的报告,供对比。"
    assert dict(got["sp"]["chips"])["L1 被谁挡住"].startswith("请查10-24 09:00:05 – 09:20:07 的 Top SQL 时段榜(按总耗时)里 sql_id 为 L1")


def test_old_reports_without_the_columns_show_no_wait_column_and_no_zero(got):
    od = got["old"]
    assert "等待占比" not in od["page"] and "主要在等" not in od["lines"], "老报告没有这两列:不画,不写 0"
    assert not any("被谁挡住" in c[0] for c in od["chips"])
    assert "调优 L1" in dict(od["chips"]), "老报告照旧给调优"


def test_span_boards_use_self_sec_and_press_style_shows_the_column(got):
    assert got["sp"]["span"] == "span" and ">等待占比<" in got["sp"]["page"] and "98%" in got["sp"]["page"]
    paper = got["paper"]
    assert ">等待占比<" in paper and "<b>99%</b>" in paper, "文艺风格 B1 版:主要在等的加粗"
    assert "主要在等" in text(paper)
