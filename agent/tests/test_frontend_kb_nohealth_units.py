"""大盘知识库总览:没有状态报告、但目录读得到(2026-11-03 行方反馈:管理员导完知识,总览整块「还没有知识库状态报告」)。

在 Node 里用真的取数与建模(fetch 换成假的)把普通风格的总览画出来,钉住:
① 没有状态报告、目录读得到:照常显示目录里的数与最近案例;健康自检与缺口两处提示去查一次;不出整块空白;
② 这时智能体解读**不能**说「诊断发现都查得到知识」(缺口不知道),第一个快捷问题是查一次状态;
③ 有状态报告时解读照旧;状态报告与目录都没有时仍是整块「还没有知识库状态报告」。
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
globalThis.location = { hash: '', search: '' };
globalThis.history = { replaceState() {} };
globalThis.window = { scrollTo() {}, matchMedia: () => ({ matches: false }) };
const K = await import(base + '/pages/kb.js');
const I = await import(base + '/lib/insight.js');
const V = await import(base + '/views/normal/kb.js');

const CAT = { attached: true, cases: [{ id: 'C1', title: '清算作业锁等待', system: 'F-DEMO', occurred_at: '2026-08-27', severity: 'S3', conclusion: '已确认' }],
  groups: [], edges: [{ src: { kind: 'case', id: 'case:C1' }, dst: { kind: 'symptom', id: 'symptom:锁等待' } }], warnings: 0, items: [], types: { case: 1, defect: 2 }, topics: [], docsets: [] };
const HEALTH = { readonly: true, status: { attached: true, mode: '文件模式', version: '2.2.0', counts: { 'docs.case': 1 } }, pending: [], file_warnings: [], misses: [] };
const ok = (data) => ({ ok: true, status: 200, data });
const miss = { ok: false, status: 404, error: '' };
const draw = (h, c) => {
  const m = K.model(h, ok([]), c);
  const root = { innerHTML: '', dataset: {}, addEventListener() {} };
  V.render(root, m, {});
  const ins = I.kb(m);
  return { page: root.innerHTML, head: ins.head, src: ins.src, lines: ins.lines.filter(Boolean), chips: ins.chips };
};
console.log(JSON.stringify({ noHealth: draw(miss, ok(CAT)), healthy: draw(ok(HEALTH), ok(CAT)), nothing: draw(miss, miss) }));
"""


def text(h: str) -> str:
    """页面 HTML 去掉标签与转义后的文字。"""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h)))


@pytest.fixture(scope="module")
def got():
    out = subprocess.run(["node", "--input-type=module", "-e", _SCRIPT, "file://%s" % _FRONT],
                         capture_output=True, text=True, timeout=30, env={**os.environ, "TZ": "Asia/Shanghai"})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_overview_shows_catalog_numbers_without_a_health_report(got):
    page = text(got["noHealth"]["page"])
    assert "清算作业锁等待" in page, "最近案例照常显示"
    assert "查一次状态可见" in page, "覆盖环:不知道,不画成文件模式"
    assert "还没有知识库状态报告:" in page.replace("：", ":"), "健康自检提示去查一次"
    assert "查一次知识库状态后显示" in page, "缺口清单不写「没有缺口记录」"
    assert "没有缺口记录" not in page and "没有待处理项" not in page


def test_insight_does_not_claim_everything_is_findable_without_a_report(got):
    n = got["noHealth"]
    assert "都查得到" not in n["head"] and "还没查过知识库状态" in n["head"]
    assert n["src"] == "依据知识库目录"
    assert any("查一次知识库状态才知道" in x for x in n["lines"])
    assert n["chips"][0] == ["查一次知识库状态", "请查看知识库的健康状态。"]


def test_with_a_report_the_insight_is_unchanged(got):
    h = got["healthy"]
    assert h["head"] == "知识库可用，诊断发现都查得到知识"
    assert h["src"].startswith("依据知识库自检") and "查一次知识库状态" not in [c[0] for c in h["chips"]]
    assert "没有待处理项" in text(h["page"])


def test_no_report_and_no_catalog_is_still_the_empty_hero(got):
    page = text(got["nothing"]["page"])
    assert "还没有知识库状态报告" in page and "在会话里查一次知识库状态,结果会出现在这里".replace(",", "，") in page
    assert "智能体解读" not in page
