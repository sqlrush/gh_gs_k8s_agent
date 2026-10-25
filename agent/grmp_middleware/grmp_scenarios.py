"""四个业务场景的中间件测试 —— 真实 HTTP，后端 og5，结果完整打印。

    python3 -m grmp_middleware.grmp_scenarios          跑全部
    python3 -m grmp_middleware.grmp_scenarios 2 3      只跑第 2、3 个
    RAW=1 python3 -m grmp_middleware.grmp_scenarios 2  额外打印完整 JSON

场景：
    1  访问常见动态性能视图（9 张）
    2  大表取 1000 行
    3  取复杂 SQL 的执行计划，SQL 带绑定变量
    4  给表加索引

服务在本进程内起（端口由系统分配），走真实 HTTP，不占固定端口。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common.grmp import script as sc                          # noqa: E402
from common.grmp.registry import Registry                     # noqa: E402
from common.grmp.settings import Settings                     # noqa: E402
from grmp_middleware.grmp_mock import instances as inst, risk, store as st  # noqa: E402
from grmp_middleware.grmp_mock.http_server import serve                 # noqa: E402
from grmp_middleware.grmp_mock.server import App                        # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOKEN = "0123456789abcdef0123456789abcdef"
DATA_IP = "10.0.0.9"
CONN = "og"
from common.grmp import paths as grmp_paths                             # noqa: E402

# 演示:没配就用样例前缀 —— 客户端与模拟中间件读的是同一个变量,两边一致
os.environ.setdefault(grmp_paths.PREFIX_ENV, grmp_paths.EXAMPLE_PREFIX)
PREFIX = grmp_paths.from_env()
LIST_PATH = PREFIX + "/common-operations"
INVOKE_PATH = LIST_PATH + "/invoke"
RAW = os.environ.get("RAW") == "1"

C_H, C_K, C_W, C_G, C_R = (
    "\033[1;36m", "\033[1;33m", "\033[1;31m", "\033[1;32m", "\033[0m",
)

# 仅本次测试用的脚本，不进仓库的 scripts/registry/ ——
# 脚本仓库只放真要交付给客户的东西。
EXTRA_SCRIPTS = """
name: bigtable.sample
description: 从大表取样（gsbench.orders 约 1341 万行）
sql: |
  select id, customer_id, status, amount, created_at
  from gsbench.orders
  limit {{limit}};
params:
  - key: limit
    type: INTEGER
    description: 返回条数上限
---
name: unsafe.passthrough
description: "⚠️ 任意 SQL 直通 —— 白名单的一个通用入口，仅用于演示"
# 整条是占位符，静态判不出只读，必须显式声明。
# 声明成 true：允许任意 SQL 进来，但会话钉在只读上，写操作由数据库挡回。
readonly: true
sql: |
  {{user_sql}}
params:
  - key: user_sql
    type: String
    description: 任意 SQL 正文
---
name: ddl.create_index
description: 给表加索引（写操作，需单独审批）
readonly: false
sql: |
  create index if not exists {{idx_name}} on gsbench.lock_targets({{col}});
params:
  - key: idx_name
    type: String
    description: 索引名
  - key: col
    type: String
    description: 列名
---
name: ddl.drop_index
description: 删索引（写操作，用于清理演示留下的索引）
readonly: false
sql: |
  drop index if exists gsbench.{{idx_name}};
params:
  - key: idx_name
    type: String
    description: 索引名
"""

COMPLEX_SQL_BIND = (
    "SELECT c.region_id, o.status, count(*) AS order_cnt, "
    "sum(oi.amount) AS item_amount "
    "FROM gsbench.orders o "
    "JOIN gsbench.order_items oi ON oi.order_id = o.id "
    "JOIN gsbench.customers c ON c.id = o.customer_id "
    "WHERE o.created_at >= $1 AND o.amount > $2 "
    "GROUP BY c.region_id, o.status ORDER BY item_amount DESC LIMIT 20"
)
COMPLEX_SQL_LITERAL = (
    COMPLEX_SQL_BIND.replace("$1", "'2024-01-01 00:00:00'").replace("$2", "100")
)


# ---------------------------------------------------------------- 输出

def hdr(n, title):
    print("\n%s%s\n══ 场景 %s：%s%s" % (C_H, "═" * 76, n, title, C_R))


def sub(title):
    print("\n%s── %s%s" % (C_H, title, C_R))


def note(text):
    print("%s   %s%s" % (C_K, text, C_R))


def warn(text):
    print("%s   %s%s" % (C_W, text, C_R))


def good(text):
    print("%s   %s%s" % (C_G, text, C_R))


def show_request(path, body):
    print("   → POST %s" % path)
    print("     %s" % json.dumps(body, ensure_ascii=False))


def table(rows, max_rows=None, width=38):
    """把行字典列表渲染成表格。max_rows=None 表示全打。

    单列结果（典型如 EXPLAIN 的 QUERY PLAN）整行打、不截断 ——
    执行计划截断到几十个字符就完全读不出树形结构了。
    """
    if not rows:
        print("     (0 行)")
        return
    cols = list(rows[0].keys())

    if len(cols) == 1:
        col = cols[0]
        print("     %s" % col)
        print("     " + "-" * min(len(col) + 2, 60))
        for r in rows:
            print("     %s" % r[col])
        print("     共 %d 行" % len(rows))
        return

    shown = rows if max_rows is None or len(rows) <= max_rows else (
        rows[: max_rows // 2] + [None] + rows[-(max_rows // 2):]
    )
    w = {}
    for c in cols:
        vals = [len(str(r[c])[:width]) for r in rows if r is not None]
        w[c] = max([len(c)] + vals[:200])
        w[c] = min(w[c], width)
    line = "     " + "  ".join(c[: w[c]].ljust(w[c]) for c in cols)
    print(line)
    print("     " + "  ".join("-" * w[c] for c in cols))
    for r in shown:
        if r is None:
            print("     %s… 中间 %d 行省略（RAW=1 看完整 JSON）…"
                  % ("", len(rows) - max_rows))
            continue
        print("     " + "  ".join(str(r[c])[: w[c]].ljust(w[c]) for c in cols))
    print("     共 %d 行" % len(rows))


# ---------------------------------------------------------------- 传输

class Client:
    def __init__(self, base):
        self.base = base

    def post(self, path, body, token=TOKEN):
        data = json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"auth": token, "Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read()
                status = r.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        elapsed = time.time() - t0
        try:
            return status, json.loads(raw.decode()), len(raw), elapsed
        except ValueError:
            return status, None, len(raw), elapsed

    def ids(self):
        _s, body, _n, _t = self.post(LIST_PATH, {"dataIp": DATA_IP, "limit": 1000})
        if body.get("code") != "0":
            sys.exit("查清单失败：%s" % body.get("msg"))
        return {d["cmd_name"]: d["id"] for d in body["result"]["list"]}


def invoke(cli, ids, name, params=None, show=True):
    """按逻辑名调用。返回 (body, 字节数, 耗时)。"""
    body = {"dataIp": DATA_IP, "id": ids[name]}
    if params:
        body["param"] = [
            {"param_name": k, "param_value": v} for k, v in params.items()
        ]
    if show:
        show_request(INVOKE_PATH, body)
    _s, resp, nbytes, elapsed = cli.post(INVOKE_PATH, body)
    return resp, nbytes, elapsed


def report(resp, nbytes, elapsed, max_rows=None):
    if resp is None:
        warn("响应不是合法 JSON")
        return
    if resp.get("status") != "finished":
        warn("status=%s  msg=%s" % (resp.get("status"), resp.get("msg")))
        if "result" in resp:
            warn("注意：失败响应里出现了 result 键，与本实现约定不符")
        else:
            note("（失败响应不含 result 键 —— 本实现约定）")
        return
    result = resp["result"]
    print("   ← HTTP 200  status=finished  %d 字节  %.2fs" % (nbytes, elapsed))
    if result.get("type") != "array":
        note("type=%s  data=%r —— 该语句没有结果集（DDL/DML 走 Text 分支）"
             % (result.get("type"), result.get("data")))
        return
    table(result["data"], max_rows=max_rows)
    if RAW:
        print(json.dumps(resp, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- 场景

def scenario_1(cli, ids):
    hdr(1, "通过中间件访问常见动态性能视图")
    note("9 张视图，每张都是一条已注册脚本；结果完整打印")
    views = [
        ("perf.sessions", {"limit": "10"}, "会话与活动"),
        ("perf.wait_status", {"limit": "10"}, "线程等待状态与阻塞关系"),
        ("perf.wait_events", {"limit": "10"}, "等待事件汇总"),
        ("perf.locks", {"limit": "10"}, "锁与持有者"),
        ("perf.db_stat", None, "库级统计与命中率"),
        ("perf.memory", {"limit": "10"}, "实例内存分布"),
        ("perf.bgwriter", None, "检查点与后台写"),
        ("perf.table_stat", {"limit": "10"}, "表级统计与膨胀"),
        ("perf.instance_time", None, "实例时间模型"),
    ]
    ok = 0
    for name, params, desc in views:
        sub("%s —— %s" % (name, desc))
        resp, n, t = invoke(cli, ids, name, params)
        report(resp, n, t)
        if resp and resp.get("status") == "finished":
            ok += 1
    print()
    good("%d/%d 张视图通过中间件取到数据" % (ok, len(views)))


def scenario_2(cli, ids):
    hdr(2, "通过中间件从大表取 1000 行")
    note("gsbench.orders 约 1341 万行 / 1185 MB，取 1000 行")
    sub("limit=1000")
    resp, n, t = invoke(cli, ids, "bigtable.sample", {"limit": "1000"})
    report(resp, n, t, max_rows=10)
    if resp and resp.get("status") == "finished":
        rows = resp["result"]["data"]
        good("行数 %d  报文 %.1f KB  耗时 %.2fs  平均每行 %d 字节"
             % (len(rows), n / 1024.0, t, n // max(len(rows), 1)))
        note("所有值都是字符串：amount 的 %r、created_at 的 %r"
             % (rows[0]["amount"], rows[0]["created_at"]))

    sub("边界：超过 10000 行上限")
    note("本实现约定：超限报错不截断。截断会让调用方把「只取到前 N 行」当成「一共就这么多」")
    resp, n, t = invoke(cli, ids, "bigtable.sample", {"limit": "10001"})
    report(resp, n, t)


def scenario_3(cli, ids):
    hdr(3, "通过中间件取复杂 SQL 的执行计划（SQL 带绑定变量）")
    warn("本场景必须借道 unsafe.passthrough（SQL 直通脚本）——")
    warn("白名单模型下没有别的办法执行任意 SQL。这是安全策略问题，不是技术问题。")

    sub("3-A  直接 EXPLAIN 一条含 $1 的 SQL")
    note("SQL：EXPLAIN " + COMPLEX_SQL_BIND[:70] + " …")
    resp, n, t = invoke(cli, ids, "unsafe.passthrough",
                        {"user_sql": "EXPLAIN " + COMPLEX_SQL_BIND})
    report(resp, n, t)
    note("预期失败：EXPLAIN 不认识未绑定的 $1")

    sub("3-B  PREPARE 与 EXPLAIN EXECUTE 拆成两次调用")
    note("这是最自然的写法，也是白名单模型下走不通的写法")
    resp, n, t = invoke(cli, ids, "unsafe.passthrough", {
        "user_sql": "PREPARE p_demo AS " + COMPLEX_SQL_BIND
                    + "; SELECT 'prepared' AS status;"})
    report(resp, n, t)
    resp, n, t = invoke(cli, ids, "unsafe.passthrough",
                        {"user_sql": "EXPLAIN EXECUTE p_demo('2024-01-01', 100)"})
    report(resp, n, t)
    warn("预期失败：接口二每次调用是独立连接，PREPARE 出来的语句下一次调用就没了")

    sub("3-C  同一次调用里 PREPARE + EXPLAIN EXECUTE")
    note("绕开会话限制的唯一办法：把两条语句塞进同一次调用")
    resp, n, t = invoke(cli, ids, "unsafe.passthrough", {
        "user_sql": "PREPARE p_one AS " + COMPLEX_SQL_BIND
                    + "; EXPLAIN EXECUTE p_one('2024-01-01', 100);"})
    report(resp, n, t)
    plan_bind = _plan_lines(resp)
    good("这是真正的绑定变量计划")

    sub("3-D  参数用文本替换成字面量后 EXPLAIN（GRMP 的常规做法）")
    resp, n, t = invoke(cli, ids, "unsafe.passthrough",
                        {"user_sql": "EXPLAIN " + COMPLEX_SQL_LITERAL})
    report(resp, n, t)
    plan_literal = _plan_lines(resp)

    sub("3-对比  两个计划是不是同一个？")
    _compare_plans(plan_bind, plan_literal)


def _plan_lines(resp):
    if not resp or resp.get("status") != "finished":
        return []
    return [r["QUERY PLAN"] for r in resp["result"]["data"]]


def _root_cost(lines):
    """取根节点的总成本（cost=x..y 里的 y）。"""
    import re
    for line in lines:
        m = re.search(r"cost=[\d.]+\.\.([\d.]+)", line)
        if m:
            return float(m.group(1))
    return None


def _compare_plans(bind, literal):
    if not bind or not literal:
        warn("有一侧没取到计划，无法比较")
        return
    cb, cl = _root_cost(bind), _root_cost(literal)
    print("     %-14s %-12s %s" % ("", "根节点成本", "计划行数"))
    print("     %-14s %-12s %s" % ("-" * 14, "-" * 12, "-" * 8))
    print("     %-14s %-12s %d" % ("绑定变量", "%.0f" % cb, len(bind)))
    print("     %-14s %-12s %d" % ("字面量替换", "%.0f" % cl, len(literal)))
    same = [b.strip() for b in bind] == [l.strip() for l in literal]
    print()
    if same:
        note("两个计划完全相同 —— 本例中文本替换没有改变优化器的选择")
    else:
        ratio = max(cb, cl) / max(min(cb, cl), 1)
        warn("两个计划不同，根节点成本相差 %.1f 倍。" % ratio)
        warn("也就是说：**用 GRMP 的常规做法（文本替换）拿到的执行计划，")
        warn("并不是应用走绑定变量时真正执行的那个计划。**")
        warn("拿它去判断线上慢 SQL 的成因，结论可能是错的。")
        note("要拿到真计划，只能走 3-C 那种 PREPARE + EXPLAIN EXECUTE 串在一次调用里的写法，")
        note("而那需要 SQL 直通脚本 —— 又回到白名单开口的问题上。")


def scenario_4(cli, ids):
    hdr(4, "通过中间件给表加索引")
    note("目标：gsbench.lock_targets（1000 行的小表）")
    ddl = "create index if not exists idx_demo_zz on gsbench.lock_targets(id);"

    sub("4-A  注册期：没声明 readonly 的写脚本直接拒绝入库")
    bad = pathlib.Path(tempfile.mkdtemp()) / "bad.yaml"
    bad.write_text("name: x.no_decl\ndescription: d\nsql: |\n  %s\n" % ddl,
                   encoding="utf-8")
    try:
        sc.load_script(bad)
        warn("没有被拒 —— 与预期不符")
    except sc.ScriptError as exc:
        for line in str(exc).split("\n"):
            note(line)

    sub("4-B  注册期：风险标注（放行，只报告）")
    for item in risk.assess(ddl):
        note("[%s] %s" % (item.code, item.detail))

    sub("4-C  运行期：执行声明了 readonly: false 的建索引脚本")
    resp, n, t = invoke(cli, ids, "ddl.create_index",
                        {"idx_name": "idx_demo_zz", "col": "id"})
    report(resp, n, t)
    created = resp and resp.get("status") == "finished"
    if created:
        good("索引已建 —— 会话模式由脚本声明决定，声明了可写就真能写")

    sub("4-D  验证索引确实存在")
    resp, n, t = invoke(cli, ids, "unsafe.passthrough", {
        "user_sql": "select indexname from pg_indexes "
                    "where schemaname='gsbench' and indexname='idx_demo_zz'"})
    report(resp, n, t)

    sub("4-E  同一条 DDL 换成只读的 SQL 直通脚本 —— 应被挡住")
    note("unsafe.passthrough 声明的是 readonly: true，会话钉在只读上")
    resp, n, t = invoke(cli, ids, "unsafe.passthrough", {"user_sql": ddl})
    report(resp, n, t)

    sub("4-F  清理：删掉演示索引")
    resp, n, t = invoke(cli, ids, "ddl.drop_index", {"idx_name": "idx_demo_zz"})
    report(resp, n, t)

    print()
    note("会话是只读还是可写，只由**已注册脚本的声明**决定：")
    note("  · 请求体里没有对应字段，调用方无从指定（传了会被当未知字段拒绝）")
    note("  · 未声明且静态判不出只读的脚本，注册期就被拒，不会留到执行时才炸")
    note("  · 启动横幅会把所有可写脚本逐条列出来")
    warn("但「客户的 GRMP 执行诊断脚本时是不是只读会话」，文档全篇未提。")
    warn("这个开关是我们自己加的约束，不是复刻 —— 需要向客户确认一句话。")


# ---------------------------------------------------------------- 主流程

def build_store(tmp: pathlib.Path) -> st.ScriptStore:
    reg_dir = tmp / "registry"
    shutil.copytree(ROOT / "scripts" / "registry", reg_dir)
    extra = reg_dir / "_scenario"
    extra.mkdir()
    for i, chunk in enumerate(EXTRA_SCRIPTS.strip().split("\n---\n")):
        (extra / ("s%d.yaml" % i)).write_text(chunk.strip() + "\n", encoding="utf-8")

    store = st.ScriptStore(tmp / "sc.db")
    registry = Registry(reg_dir)
    print("%s准备：注册 %d 条脚本（仓库 %d 条 + 本场景专用 3 条）%s"
          % (C_H, len(registry.names()),
             len(registry.names()) - 3, C_R))
    for name in registry.names():
        store.register(registry.find(name))
    return store


def main(argv):
    want = set(argv)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="grmp_scen_"))
    try:
        store = build_store(tmp)
        app = App(store=store, instances=inst.InstanceMap({DATA_IP: CONN}),
                  token=TOKEN, settings=Settings())
        httpd = serve(app, port=0, quiet=True)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print("%s服务：http://127.0.0.1:%d（系统分配端口，不占用固定端口）%s"
              % (C_H, port, C_R))

        cli = Client("http://127.0.0.1:%d" % port)
        ids = cli.ids()
        print("%s脚本 ID 由 cmd_name 解析得来，未硬编码：%d 条%s"
              % (C_H, len(ids), C_R))

        for n, fn in ((1, scenario_1), (2, scenario_2),
                      (3, scenario_3), (4, scenario_4)):
            if not want or str(n) in want:
                fn(cli, ids)

        httpd.shutdown()
        httpd.server_close()
        print("\n%s完成。%s" % (C_H, C_R))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
