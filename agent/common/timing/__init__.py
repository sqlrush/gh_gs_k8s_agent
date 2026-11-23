"""交互耗时报告(v1.4.14):读 opencode 会话库与 GRMP 计时日志,按提问 → 轮 → 段统计用时并写出慢在哪。

纯脚本统计,不调模型、不联网、不改任何数据;不是技能(没有 SKILL.md),模型看不到也调不到。
用法见 cli.py 文件头:python3 $GSDB_SKILLS_DIR/common/timing/cli.py --last 3 --html /tmp/耗时报告.html
"""
