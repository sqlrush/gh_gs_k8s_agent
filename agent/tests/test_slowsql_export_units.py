"""slowsql 的 CSV 导出。

实测踩到：慢 SQL 超过 10 条时脚本会写 CSV，但**导出目录不存在就直接崩**，
错误是 `[Errno 2] No such file or directory: .../csv/slow_sql_export_....csv`
—— 看不出是导出出了问题，像是取数失败。而它恰恰在「库里慢 SQL 多」时触发，
也就是最需要这条命令的时候。
"""
import csv
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-slowsql" / "scripts"))

import slowsql  # noqa: E402


class _Runner:
    """按脚本名给预置行；值全是字符串 —— 协议就是这个形态。"""

    def __init__(self, n):
        self.n = n

    def run(self, script, values=None):
        return [
            {"unique_sql_id": str(i), "query": "SELECT %d" % i, "calls": "1",
             "avg_ms": "1.0", "total_sec": "1.0", "cpu_sec": "1.0", "rows": "1"}
            for i in range(self.n)
        ]


def _fake_here(tmp_path, monkeypatch):
    # _HERE 是**文件**路径（Path(__file__).resolve()），不是目录 ——
    # parents[3] 因此是安装根目录。造假路径时层数要对上。
    monkeypatch.setattr(
        slowsql, "_HERE",
        tmp_path / "root" / "skills" / "gaussdb-slowsql" / "scripts" / "slowsql.py")
    return tmp_path / "root" / "tmp" / "csv"


def test_export_creates_its_directory(tmp_path, monkeypatch, capsys):
    """导出目录不存在时自己建，而不是崩掉整条命令。"""
    out = _fake_here(tmp_path, monkeypatch)
    rows = slowsql.slow_sql(_Runner(12), 100, 50, "2000-01-01 00:00:00", True)

    assert list(out.glob("slow_sql_export_*.csv")), "导出目录没建出来，或文件没写成"
    assert "数据已导出到" in capsys.readouterr().out
    assert len(rows) <= 5, "导出后返回的应是摘要行"


def test_exported_csv_holds_every_row_not_just_the_summary(tmp_path, monkeypatch):
    """CSV 里要有全部行 —— 摘要只显示几条，全量得能从文件里拿到。"""
    out = _fake_here(tmp_path, monkeypatch)
    slowsql.slow_sql(_Runner(12), 100, 50, "2000-01-01 00:00:00", True)

    path = next(out.glob("*.csv"))
    with open(path, newline="", encoding="utf-8") as f:
        body = list(csv.reader(f))
    assert len(body) == 13, "表头 1 行 + 数据 12 行"


def test_auto_export_is_off(tmp_path, monkeypatch):
    """**行数超阈值不再自动导出** —— e145e14 有意改成只有 --export 才导。

    这条钉的是「改过了」而不是「本该如此」：原先 rows > 阈值就写文件，
    现在只截断显示、不落盘。将来要改回自动导出，这条会红，提醒你两处
    （SLOWSQL_MAX_ROWS 的截断 与 导出）是分开的开关。
    """
    out = _fake_here(tmp_path, monkeypatch)
    rows = slowsql.slow_sql(_Runner(30), 100, 50, "2000-01-01 00:00:00", False)
    assert not out.exists(), "export=False 时不该产生任何文件"
    assert len(rows) <= 5, "超过 SLOWSQL_MAX_ROWS 仍应截断显示"


def test_no_export_below_the_threshold(tmp_path, monkeypatch):
    """行数不多时不该产生文件 —— 免得每次跑都留一堆垃圾。"""
    out = _fake_here(tmp_path, monkeypatch)
    slowsql.slow_sql(_Runner(3), 100, 50, "2000-01-01 00:00:00", False)
    assert not out.exists()


def test_export_dir_env_var_is_usable(tmp_path, monkeypatch):
    """**回归测试。** G_EXPORT_DIR 取回来是 str，而后面还要 `/ "csv"`。

    原先没转 Path，str / str 直接 TypeError —— 这个环境变量一旦设置，
    导出必崩。实测确认过。
    """
    _fake_here(tmp_path, monkeypatch)
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("G_EXPORT_DIR", str(target))
    slowsql.slow_sql(_Runner(3), 100, 50, "2000-01-01 00:00:00", True)
    assert list((target / "csv").glob("*.csv")), "G_EXPORT_DIR 没生效"


@pytest.mark.parametrize("raw", ["false", "False", "0", "no"])
def test_export_flag_rejects_the_bool_string_trap(raw):
    """`--export false` **绝不能被当成「要导出」**。

    argparse 的 type=bool 就是 bool(str)：非空字符串一律为真，于是
    `--export false` 反而打开了导出。与 bool("f") 是同一个坑，只是换了个地方。

    断言的是**安全性质**而不是某一种实现：
      · 当前用 action="store_true" —— argparse 直接拒掉多出来的值（SystemExit）
      · 若哪天改成 type=bool 并正确处理 —— 解析成 False
    两种都安全；不安全的只有第三种：解析成 True。所以这里只否定那一种。

    （这条曾经因为 build_parser 没拆出来而永远 skip —— 一个结构上跑不起来的
    守卫，在报告里显示成「跳过」，看着像覆盖，实际什么都不保护。）
    """
    ap = slowsql.build_parser()
    try:
        args = ap.parse_args(["-c", "og", "--export", raw])
    except SystemExit:
        return          # argparse 当场拒绝，安全
    assert args.export is not True, (
        "`--export %s` 被解析成了「要导出」—— 用户写的是关掉，实际打开了" % raw)
