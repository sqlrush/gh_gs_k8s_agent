"""验收方案网页(docs/acceptance/index.html)的守卫:网页由 docs/智能体验收方案.md 生成,改了方案没重新生成就红。"""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("acceptance_render_guide", _ROOT / "acceptance" / "render_guide.py")
rg = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rg
_spec.loader.exec_module(rg)


def test_committed_page_is_up_to_date():
    assert rg.main(["--check"]) == 0, "改了 docs/智能体验收方案.md 之后要跑 python3 acceptance/render_guide.py"


def test_page_has_toc_flow_and_key_sections():
    page = rg.render(rg.SRC.read_text(encoding="utf-8"))
    assert '<nav class="toc">' in page and page.count('<div class="step">') == 8
    for title in ("一页看懂", "评分示例", "怎么看报告", "一轮怎么跑", "人工复核与重新评分"):
        assert title in page, title


def test_relative_links_point_to_github():
    assert rg.fix_link("../acceptance/README.md") == rg.REPO + "/blob/main/acceptance/README.md"
    assert rg.fix_link("../acceptance/bank/v1/") == rg.REPO + "/tree/main/acceptance/bank/v1"
    assert rg.fix_link("部署上线方案-v1.4.2.md") == rg.REPO + "/blob/main/docs/部署上线方案-v1.4.2.md"
    assert rg.fix_link("https://x.y/z") == "https://x.y/z"


def test_steps_block_renders_cards():
    html = rg.steps(["准备|配置`run.yaml`", "", "评分|规则 → 裁判"])
    assert html.count('class="step"') == 2 and "<code>run.yaml</code>" in html and "→</div>" in html
