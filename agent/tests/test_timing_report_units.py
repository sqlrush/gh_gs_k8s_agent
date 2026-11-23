"""交互耗时报告(common/timing,v1.4.14):纯脚本统计一次提问每一步的用时。

造一份最小的 opencode 会话库(新版 SQLite 与旧版 JSON 两种)和 GRMP 计时日志,核对:分段之和等于总时长;
GRMP 记录对到正确的工具调用(并行的两个工具按脚本名分开);摘要版不带原文、业务库名;详细版遮掉 IP 与会话句柄;
结论规则;两次生成结果一致;代码不联网、不调模型,HTML 不引外部资源。
"""
import json
import pathlib
import re
import sqlite3
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.timing import analysis, cli, grmplog, store  # noqa: E402

T0 = 1_790_000_000_000          # 提问时刻(毫秒)
SKILL = "/root/.config/opencode/skills"


def _tool(pid, cmd, s, e, out="ok", exit_=0):
    return {"id": pid, "type": "tool", "tool": "bash",
            "state": {"status": "completed", "input": {"command": cmd, "description": "跑一下"},
                      "output": out, "metadata": {"exit": exit_, "truncated": False}, "time": {"start": s, "end": e}}}


def _conv(tool_secs=2.0, rounds=3, parallel=False):
    """一次提问:rounds 轮;第 1 轮调 vacuum.py(tool_secs 秒),parallel 时同一轮还并行调 wdr.py;最后一轮写回答。"""
    msgs = [("msg_u", {"id": "msg_u", "role": "user", "time": {"created": T0}},
             [{"id": "prt_u", "type": "text", "text": "看下 10.1.2.3 上的死元组为什么多"}])]
    t = T0 + 100
    for i in range(1, rounds + 1):
        mid = "msg_a%02d" % i
        ttft, think, write = 800, 1500, 300
        parts = [{"id": mid + "_r", "type": "reasoning", "text": "想一想 --session abcdef123456 要不要查 10.1.2.3",
                  "time": {"start": t + ttft, "end": t + ttft + think}}]
        last = i == rounds
        if last:
            parts.append({"id": mid + "_t", "type": "text", "text": "结论:死元组多是因为长事务。",
                          "time": {"start": t + ttft + think, "end": t + ttft + think + write}})
            done = t + ttft + think + write
        else:
            s = t + ttft + think + write
            e = s + int(tool_secs * 1000)
            parts.append(_tool(mid + "_x", "python3 %s/gaussdb-vacuum/scripts/vacuum.py --session abcdef123456 --top 5" % SKILL, s, e,
                               out="表 orders 死元组 12345"))
            if parallel and i == 1:
                parts.append(_tool(mid + "_y", "python3 %s/gaussdb-wdr/scripts/wdr.py snaps --session abcdef123456" % SKILL, s, e))
            done = e + 20
        tokens = {"input": 1000 * i, "output": 80, "reasoning": 400, "cache": {"read": 20000 + 5000 * i, "write": 0}}
        msgs.append((mid, {"id": mid, "role": "assistant", "modelID": "m-test", "finish": "stop" if last else "tool-calls",
                           "time": {"created": t, "completed": done}, "tokens": tokens}, parts))
        t = done + 50
    return msgs


def _sqlite(path: pathlib.Path, msgs):
    c = sqlite3.connect(str(path))
    c.executescript("create table session(id text, title text, parent_id text);"
                    "create table message(id text, session_id text, time_created int, time_updated int, data text);"
                    "create table part(id text, message_id text, session_id text, time_created int, time_updated int, data text);")
    c.execute("insert into session values ('ses_1', 't', null)")
    for mid, m, parts in msgs:
        c.execute("insert into message values (?,?,?,?,?)", (mid, "ses_1", m["time"]["created"], m["time"]["created"], json.dumps(m)))
        for p in parts:
            tm = p.get("time") or p.get("state", {}).get("time") or {"start": m["time"]["created"]}
            c.execute("insert into part values (?,?,?,?,?,?)",
                      (p["id"], mid, "ses_1", tm["start"], tm.get("end", tm["start"]), json.dumps(p)))
    c.commit()
    c.close()
    return path


def _json_store(root: pathlib.Path, msgs):
    st = root / "storage"
    (st / "session" / "proj").mkdir(parents=True)
    (st / "session" / "proj" / "ses_1.json").write_text(json.dumps({"id": "ses_1", "title": "t"}))
    for mid, m, parts in msgs:
        d = st / "message" / "ses_1"
        d.mkdir(parents=True, exist_ok=True)
        (d / (mid + ".json")).write_text(json.dumps(dict(m, sessionID="ses_1")))
        pd = st / "part" / mid
        pd.mkdir(parents=True, exist_ok=True)
        for p in parts:
            (pd / (p["id"] + ".json")).write_text(json.dumps(dict(p, messageID=mid)))
    return root


def _grmp_log(path: pathlib.Path, msgs, parallel=False):
    """给第 1 轮的 vacuum.py 写 3 次 GRMP(第 2 次失败、带业务库名);parallel 时给 wdr.py 写 1 次。"""
    tool = next(p for _, _, ps in msgs for p in ps if p.get("type") == "tool")
    s = tool["state"]["time"]["start"]
    recs = [
        {"ts": (s + 100) / 1000, "ms": 300, "kind": "invoke", "script": "vacuum.dead_tuples", "db": "bizdb_secret",
         "ok": True, "rows": 20, "bytes": 3000, "caller": "gaussdb-vacuum/vacuum.py"},
        {"ts": (s + 500) / 1000, "ms": 400, "kind": "invoke", "script": "vacuum.xmin_holder", "db": "",
         "ok": False, "error": "GrmpError", "caller": "gaussdb-vacuum/vacuum.py"},
        {"ts": (s + 1000) / 1000, "ms": 200, "kind": "invoke", "script": "vacuum.autovac_workers", "db": "postgres",
         "ok": True, "rows": 1, "bytes": 100, "caller": "gaussdb-vacuum/vacuum.py"},
    ]
    if parallel:
        recs.append({"ts": (s + 200) / 1000, "ms": 250, "kind": "invoke", "script": "wdr.snapshots", "db": "",
                     "ok": True, "rows": 5, "bytes": 500, "caller": "gaussdb-wdr/wdr.py"})
    recs.append({"ts": (T0 - 3_600_000) / 1000, "ms": 50, "kind": "list", "script": "接口一 · 取命令清单", "db": "",
                 "ok": True, "rows": 132, "caller": "gaussdb-login/login.py"})        # 一小时前的:不该被分进来
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\nnot json\n", encoding="utf-8")
    return path


def _build(tmp_path, detail=False, parallel=False, tool_secs=2.0, rounds=3, kind="sqlite"):
    msgs = _conv(tool_secs, rounds, parallel)
    src = _sqlite(tmp_path / "opencode.db", msgs) if kind == "sqlite" else _json_store(tmp_path / "oc", msgs)
    grmp = grmplog.load(str(_grmp_log(tmp_path / "grmp-timing.log", msgs, parallel)))
    qs, masked = analysis.questions(store.load(store.locate(str(src))), detail, grmp, cli._tz(8))
    return qs[0], masked


def test_segments_add_up_to_the_total(tmp_path):
    q, _ = _build(tmp_path)
    assert q["rounds_n"] == 3 and q["tools_n"] == 2
    assert sum(q["totals"].values()) == pytest.approx(q["total"], abs=0.01)
    r1 = q["rounds"][0]
    assert r1["ttft"] == pytest.approx(0.8) and r1["gen"] == pytest.approx(1.8) and r1["think"]["secs"] == pytest.approx(1.5)
    assert r1["grmp"] + r1["local"] == pytest.approx(2.0)


def test_grmp_records_land_on_the_right_tool(tmp_path):
    q, _ = _build(tmp_path)
    g = q["rounds"][0]["tools"][0]["grmp"]
    assert [x["name"] for x in g] == ["vacuum.dead_tuples", "vacuum.xmin_holder", "vacuum.autovac_workers"]
    assert q["rounds"][0]["grmp"] == pytest.approx(0.9) and q["grmp_n"] == 3 and q["grmp_fail"] == 1
    assert not q["rounds"][1]["tools"][0]["grmp"], "一小时前那条与第 2 轮的工具都不该分到"


def test_parallel_tools_are_split_by_script_name(tmp_path):
    q, _ = _build(tmp_path, parallel=True)
    vac, wdr = q["rounds"][0]["tools"]
    assert [x["name"] for x in wdr["grmp"]] == ["wdr.snapshots"]
    assert "wdr.snapshots" not in [x["name"] for x in vac["grmp"]]


def test_summary_carries_no_original_text(tmp_path):
    q, masked = _build(tmp_path, detail=False)
    blob = json.dumps(q, ensure_ascii=False)
    for leak in ("想一想", "死元组多是因为", "--top 5", "表 orders", "bizdb_secret", "10.1.2.3", "abcdef123456"):
        assert leak not in blob, leak
    assert q["rounds"][0]["tools"][0]["grmp"][0]["db"] == "业务库"
    assert q["rounds"][0]["tools"][0]["label"] == "gaussdb-vacuum · vacuum.py"


def test_detail_carries_text_but_masks_ip_and_session_handles(tmp_path):
    q, masked = _build(tmp_path, detail=True)
    r1 = q["rounds"][0]
    assert "想一想" in r1["think"]["parts"][0]["text"] and "--top 5" in r1["tools"][0]["cmd"]
    assert "表 orders 死元组" in r1["tools"][0]["out"] and r1["tools"][0]["grmp"][0]["db"] == "bizdb_secret"
    blob = json.dumps(q, ensure_ascii=False)
    assert "10.1.2.3" not in blob and "abcdef123456" not in blob
    assert masked.get("IP") and masked.get("会话句柄")


def test_legacy_json_storage_reads_the_same(tmp_path):
    a, _ = _build(tmp_path / "a", kind="sqlite") if (tmp_path / "a").mkdir() is None else None
    b, _ = _build(tmp_path / "b", kind="json") if (tmp_path / "b").mkdir() is None else None
    for k in ("total", "rounds_n", "tools_n", "grmp_n"):
        assert a[k] == pytest.approx(b[k]), k
    assert a["totals"]["grmp"] == pytest.approx(b["totals"]["grmp"])


def test_rules_name_the_slow_part(tmp_path):
    slow_tool, _ = _build(tmp_path / "x", tool_secs=60) if (tmp_path / "x").mkdir() is None else None
    assert slow_tool["diag"][0]["title"] == "慢在一次工具调用" and slow_tool["diag"][0]["round"] == 1
    many, _ = _build(tmp_path / "y", tool_secs=0.2, rounds=18) if (tmp_path / "y").mkdir() is None else None
    assert "慢在轮数" in [d["title"] for d in many["diag"]]
    normal, _ = _build(tmp_path / "z") if (tmp_path / "z").mkdir() is None else None
    assert "有 GRMP 调用失败" in [d["title"] for d in normal["diag"]]


def test_grmp_heavy_question_says_so(tmp_path):
    msgs = _conv(tool_secs=6.0, rounds=2)
    src = _sqlite(tmp_path / "opencode.db", msgs)
    tool = next(p for _, _, ps in msgs for p in ps if p.get("type") == "tool")
    s = tool["state"]["time"]["start"]
    rec = {"ts": (s + 50) / 1000, "ms": 5800, "kind": "invoke", "script": "vacuum.dead_tuples", "db": "", "ok": True,
           "rows": 1, "caller": "gaussdb-vacuum/vacuum.py"}
    (tmp_path / "g.log").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    qs, _ = analysis.questions(store.load(src), False, grmplog.load(str(tmp_path / "g.log")), cli._tz(8))
    assert "慢在 GRMP 与数据库" in [d["title"] for d in qs[0]["diag"]]


def test_cli_end_to_end_is_deterministic(tmp_path, capsys):
    msgs = _conv()
    db = _sqlite(tmp_path / "opencode.db", msgs)
    log = _grmp_log(tmp_path / "grmp-timing.log", msgs)
    outs = []
    for n in (1, 2):
        html = tmp_path / ("r%d.html" % n)
        assert cli.main(["--db", str(db), "--grmp-log", str(log), "--html", str(html), "--text", str(tmp_path / "r.txt")]) == 0
        outs.append(re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\d(?=[\"（\s])", "T", html.read_text(encoding="utf-8")))
    assert outs[0] == outs[1]
    txt = (tmp_path / "r.txt").read_text(encoding="utf-8")
    assert "结论:" in txt and "有 GRMP 调用失败" in txt and "vacuum.xmin_holder" in txt
    assert cli.main(["--db", str(db), "--list"]) == 0 and "看下" in capsys.readouterr().out


def test_cli_time_window_and_errors(tmp_path, capsys):
    db = _sqlite(tmp_path / "opencode.db", _conv())
    assert cli.main(["--db", str(db), "--since", "2000-01-01", "--until", "2000-01-02"]) == 1
    assert cli.main(["--db", str(tmp_path / "nope")]) == 2
    assert "没找到" in capsys.readouterr().err


def test_html_has_no_external_resources():
    html = (_ROOT / "common" / "timing" / "report.html").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)\s*=\s*["']?https?:""", html, re.I)
    assert "<script src" not in html and "@import" not in html and "innerHTML" not in html


def test_timing_code_never_talks_to_the_network_or_a_model():
    for f in (_ROOT / "common" / "timing").glob("*.py"):
        code = f.read_text(encoding="utf-8")
        for bad in ("urllib", "http.client", "socket", "requests", "subprocess", "openai", "anthropic"):
            assert not re.search(r"^\s*(import|from)\s+%s\b" % re.escape(bad), code, re.M), (f.name, bad)


def test_big_outputs_in_one_round_are_reported_once(tmp_path):
    """一轮里两个工具都输出很多:下一轮上下文的涨幅只报一次(端到端里加载两个技能时曾各报一遍 +14.5K)。"""
    msgs = _conv(rounds=2, parallel=True)
    for _, _, ps in msgs:
        for p in ps:
            if p.get("type") == "tool":
                p["state"]["output"] = "x" * 6000
    qs, _ = analysis.questions(store.load(_sqlite(tmp_path / "opencode.db", msgs)), False, [], cli._tz(8))
    why = qs[0]["rounds"][0]["why"]
    assert sum("工具输出" in w for w in why) == 1
    assert any("工具输出共 12000 字（2 个工具），下一轮上下文 +" in w for w in why), why
