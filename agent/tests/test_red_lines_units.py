"""公共安全红线:一份正文(common/red_lines.md),19 个 SKILL.md 与 AGENTS.md 各带一份逐字相同的副本。

事故(客户 2026-09-10 截图):09-02 的重构把 14 个 SKILL.md 里的公共红线抽到了仓库根 AGENTS.md,而安装脚本不拷 AGENTS.md
——客户只换了 skill 目录,安全审查一 diff,红线整段「被删了」。银行客户要的是每个 skill 文件里看得见,不是仓库里去重。
所以改成:正文只有一份,由 tools/inject_red_lines.py 注入到每个 SKILL.md 的「## 安全红线」和 AGENTS.md 的标记块里;
这里的测试保证 19 份副本与正文逐字一致——改了正文忘了注入、或哪份被手工改坏,都在这里变红。
"""
import pathlib
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from tools import inject_red_lines as rl  # noqa: E402

_SKILLS = sorted((_ROOT / "skills").glob("gaussdb-*/SKILL.md"))
_HEADINGS = ("配置文件里绝不允许出现明文口令", "绝对沉默条款", "能力边界不是系统配置",
             "强制拒绝机制", "输出屏蔽规则", "通用替代策略", "只通过本技能脚本取数")


def test_canonical_text_has_the_clauses_the_customer_signed_off():
    text = rl.canonical_text()
    for h in _HEADINGS:
        assert h in text, "common/red_lines.md 缺「%s」" % h
    assert "{script}" in text and "{baseDir}" in text


def test_capability_boundary_is_carved_out_of_the_silence_clause():
    """过度拒绝,2026-09-21 在容器环境实测到:

    问「当前你加载的 pod 类型」,连问两次都是「抱歉,我无法提供该技术配置信息」——
    模型把「本环境有哪些能力」归进了「系统配置」。而最后一条本来就要求
    「脚本未覆盖的能力,如实说明『当前无此能力』并停止」,两条自相矛盾;
    我方的隔离验收用例也正是靠模型答出「本环境不含知识库导入功能」来判定的。

    这一条把边界写死:**能说**属于哪类环境、有哪些能力、该找谁;**不能说**具体取值。
    """
    text = rl.canonical_text()
    assert "能力边界不是系统配置" in text
    # 必须给出正面示范,否则模型仍会保守地一律拒答
    assert "不含知识库导入" in text, "要给一句可以照说的例子"
    # 而具体取值仍然是禁止的,这一条不能被读成「配置可以说了」
    for still_secret in ("镜像", "Pod 名", "环境变量", "端口", "IP"):
        assert still_secret in text, "豁免条款要同时点明 %s 仍不可说" % still_secret


def test_every_skill_directory_has_a_skill_md():
    """原来写死「19 个」—— 容器线 19 个、非容器线 18 个(没有独立的 kb-import),
    同一份测试在两个仓之间搬就会红。真正要守的是「没有哪个 skill 目录丢了 SKILL.md」,
    跟总数多少无关。"""
    dirs = sorted(p.name for p in (_ROOT / "skills").glob("gaussdb-*") if p.is_dir())
    have = sorted(p.parent.name for p in _SKILLS)
    assert dirs and have == dirs, "这些 skill 目录没有 SKILL.md:%s" % (set(dirs) - set(have))


def test_every_skill_has_its_own_script_named_in_the_last_clause():
    for path in _SKILLS:
        script = rl.main_script(path.parent)
        assert (path.parent / "scripts" / script).is_file(), "%s 的主脚本 %s 不存在" % (path.parent.name, script)


@pytest.mark.parametrize("path", _SKILLS, ids=lambda p: p.parent.name)
def test_skill_md_carries_the_block_verbatim(path):
    text = path.read_text(encoding="utf-8")
    assert text.count(rl.BEGIN) == 1 and text.count(rl.END) == 1, "%s 的红线块要恰好一对标记" % path.parent.name
    assert text.count("\n## 安全红线") == 1, "%s 的「## 安全红线」要恰好一节" % path.parent.name
    assert rl.extract_block(text) == rl.render(rl.main_script(path.parent)), (
        "%s 的红线块与 common/red_lines.md 不一致——改正文后跑 tools/inject_red_lines.py" % path.parent.name)
    assert text.index("## 安全红线") < text.index(rl.BEGIN)          # 块在这一节里


def test_agents_md_carries_the_same_block():
    text = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert text.count(rl.BEGIN) == 1
    assert rl.extract_block(text) == rl.render(rl.AGENTS_SCRIPT)


def test_injector_is_idempotent_and_check_mode_is_clean():
    assert rl.check(_ROOT) == []
    proc = subprocess.run([sys.executable, str(_ROOT / "tools" / "inject_red_lines.py"), "--check", "--root", str(_ROOT)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_injector_updates_a_stale_copy(tmp_path):
    """正文改了、副本没跟上:--check 要点名,注入后一致。"""
    root = tmp_path
    (root / "common").mkdir()
    (root / "common" / "red_lines.md").write_text("- **A**:{script} {baseDir}\n", encoding="utf-8")
    sk = root / "skills" / "gaussdb-demo"
    (sk / "scripts").mkdir(parents=True)
    (sk / "scripts" / "demo.py").write_text("", encoding="utf-8")
    (sk / "SKILL.md").write_text("---\nname: gaussdb-demo\nversion: 1.0.0\n---\n\n# Demo\n\n## 安全红线\n\n- 本 skill 特有的一条\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# agent\n\n## 数据库连接规则\n\n1. x\n", encoding="utf-8")
    assert rl.check(root) != []
    rl.inject(root)
    assert rl.check(root) == []
    text = (sk / "SKILL.md").read_text(encoding="utf-8")
    assert "- **A**:demo.py {baseDir}" in text and "本 skill 特有的一条" in text
    assert text.index(rl.END) < text.index("本 skill 特有的一条")      # 特有条目留在块后面
    before = text
    rl.inject(root)
    assert (sk / "SKILL.md").read_text(encoding="utf-8") == before        # 幂等
    (root / "common" / "red_lines.md").write_text("- **B**:{script}\n", encoding="utf-8")
    assert rl.check(root) != []
