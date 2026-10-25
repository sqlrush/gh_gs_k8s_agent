"""sqltune 根因规则(planrules):计划节点 + SQL 正文都能确定的才报,拿不准不报。

对应 2026-10-24 faultlab(og5)S1–S4:sqltune 只给了通用的 seq_scan / sort 警告,
「相关子查询逐行执行」「NOT IN 子计划 + 可空列」「索引列上套类型转换」「LIKE 前缀关联」
一个都没点到。下面的计划文本都是那一轮 sqltune / explain 在 og5 上拿到的原样输出。
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-sqltune" / "scripts"))
sys.path.insert(0, str(_ROOT))

import evidence  # noqa: E402
import planrules  # noqa: E402


def _idx(table, name, cols, schema="fl", unique=False):
    return evidence.IndexInfo(table, name, unique, unique, 10, 100,
                              "CREATE %sINDEX %s ON %s.%s USING btree (%s) TABLESPACE pg_default"
                              % ("UNIQUE " if unique else "", name, schema, table, cols))


def _col(table, column, null_frac=0.0):
    return evidence.ColumnStat(table, column, -1.0, null_frac, 8, 0.5)


FL_INDEXES = [
    _idx("customers", "customers_pkey", "id", unique=True),
    _idx("customers", "idx_customers_code", "code"),
    _idx("customers", "idx_customers_region", "region_id"),
    _idx("orders", "orders_pkey", "id", unique=True),
    _idx("orders", "idx_orders_cust", "cust_id"),
    _idx("payments", "payments_pkey", "id", unique=True),
    _idx("order_items", "idx_items_order", "order_id"),
    _idx("shipments", "idx_shipments_order", "order_id"),
    _idx("regions", "regions_pkey", "id", unique=True),
]
FL_COLUMNS = [_col("customers", "code", 0.010), _col("customers", "grade"),
              _col("orders", "cust_code"), _col("payments", "order_id")]


def _ev(plan, sql, indexes=FL_INDEXES, columns=FL_COLUMNS):
    return evidence.Evidence(sql=sql, version="openGauss 5.0.3", plan=plan, analyzed=False,
                             indexes=list(indexes), columns=list(columns))


def _kinds(findings):
    return [f.kind for f in findings]


def _by_kind(findings, kind):
    hits = [f for f in findings if f.kind == kind]
    assert hits, "没报 %s,实际:%s" % (kind, _kinds(findings))
    return hits


# ------------------------------------------------------------------ S1 相关子查询

S1_SQL = """SELECT o.id, c.name AS cust,
       (SELECT count(*) FROM fl.payments p WHERE p.order_id = o.id) AS pay_cnt,
       (SELECT max(p2.amount) FROM fl.payments p2
         WHERE p2.order_id = o.id AND p2.status = 'OK') AS max_pay
  FROM (SELECT * FROM fl.orders WHERE id <= 100000) o
  JOIN fl.customers c ON c.id = o.cust_id
 WHERE EXISTS (SELECT 1 FROM fl.shipments s
                WHERE s.order_id = o.id AND s.ship_date > o.order_date + 3)
 ORDER BY max_pay DESC NULLS LAST"""

S1_PLAN = """Sort  (cost=1438374249.14..1438374332.98 rows=33539 width=46)
  Sort Key: ((SubPlan 2)) DESC NULLS LAST
  ->  Hash Join  (cost=4163.38..1438371360.84 rows=33539 width=46)
        Hash Cond: (orders.cust_id = c.id)
        ->  Merge Semi Join  (cost=0.13..8084.38 rows=33539 width=20)
              Merge Cond: (orders.id = s.order_id)
              Join Filter: (s.ship_date > (orders.order_date + 3))
              ->  Index Scan using orders_pkey on orders  (cost=0.00..3064.55 rows=100994 width=24)
                    Index Cond: (id <= 100000)
              ->  Index Scan using idx_shipments_order on shipments s  (cost=0.00..3006.83 rows=100616 width=8)
                    Index Cond: (order_id <= 100000)
        ->  Hash  (cost=2833.50..2833.50 rows=200000 width=15)
              ->  Seq Scan on customers c  (cost=0.00..2833.50 rows=200000 width=15)
        SubPlan 1
          ->  Aggregate  (cost=20193.00..20193.01 rows=1 width=12)
                ->  Seq Scan on payments p  (cost=0.00..20193.00 rows=1 width=4)
                      Filter: (order_id = orders.id)
        SubPlan 2
          ->  Aggregate  (cost=22693.00..22693.01 rows=1 width=41)
                ->  Seq Scan on payments p2  (cost=0.00..22693.00 rows=1 width=9)
                      Filter: ((order_id = orders.id) AND (status = 'OK'::text))"""


def test_s1_correlated_subquery_without_index_is_the_root_cause():
    out = planrules.root_causes(_ev(S1_PLAN, S1_SQL))
    f = _by_kind(out, "correlated_subplan")
    assert len(f) == 1, "两个 SubPlan 扫的是同一张表的同一列,合成一条"
    f = f[0]
    assert "SubPlan 1" in f.detail and "SubPlan 2" in f.detail
    assert "payments" in f.detail and "order_id" in f.detail
    assert "payments(order_id)" in f.advice and "未验证" in f.advice
    assert "JOIN" in f.advice                       # 改写方向:预聚合后关联
    assert out[0].kind == "correlated_subplan", "根因排在通用警告前面"


def test_s1_index_exists_but_unused_is_said_differently():
    idx = FL_INDEXES + [_idx("payments", "idx_pay_order", "order_id")]
    f = _by_kind(planrules.root_causes(_ev(S1_PLAN, S1_SQL, indexes=idx)), "correlated_subplan")[0]
    assert "idx_pay_order" in f.advice and "payments(order_id)" not in f.advice


# ------------------------------------------------------------------ S2 LIKE 前缀关联

S2_SQL = """WITH lvl3 AS (SELECT substr(l2.cust_code, 1, 8) AS k, sum(l2.amt) AS amt FROM lvl2 l2 GROUP BY 1)
SELECT l3.k, c.name FROM lvl3 l3 JOIN fl.customers c ON c.code LIKE l3.k || '%'"""

S2_PLAN = """Sort  (cost=237503.98..237528.98 rows=10000 width=123)
  Sort Key: (sum(l2.amt)) DESC
  CTE lvl2
    ->  Hash Join  (cost=34902.00..76896.30 rows=350305 width=59)
          Hash Cond: (l1.order_id = o.id)
          ->  CTE Scan on lvl1 l1  (cost=0.00..23645.61 rows=350305 width=44)
                Filter: (order_id <= 1000000)
          ->  Hash  (cost=22304.00..22304.00 rows=1000000 width=19)
                ->  Seq Scan on orders o  (cost=0.00..22304.00 rows=1000000 width=19)
                      Filter: (id <= 1000000)
  ->  Nested Loop  (cost=10547.16..48654.80 rows=10000 width=123)
        ->  Hash Join  (cost=10521.28..46813.53 rows=10000 width=97)
              Hash Cond: (c.region_id = r.id)
              ->  Nested Loop  (cost=10509.15..46676.40 rows=10000 width=91)
                    Join Filter: (c.code ~~ ((substr(l2.cust_code, 1, 8)) || '%'::text))
                    ->  Seq Scan on customers c  (cost=0.00..3667.00 rows=200000 width=30)
                    ->  Materialize  (cost=10509.15..10509.43 rows=10 width=72)
                          ->  HashAggregate  (cost=10509.15..10509.28 rows=10 width=104)
                                Group By Key: substr(l2.cust_code, 1, 8)
                                ->  CTE Scan on lvl2 l2  (cost=0.00..7881.86 rows=350305 width=64)
              ->  Hash  (cost=9.00..9.00 rows=500 width=14)
                    ->  Seq Scan on regions r  (cost=0.00..9.00 rows=500 width=14)
        ->  Index Scan using products_pkey on products pdt  (cost=0.00..0.28 rows=1 width=17)
              Index Cond: (id = ((c.id % 50000) + 1))"""


def test_s2_like_whose_pattern_comes_from_the_other_side_cannot_use_the_index():
    f = _by_kind(planrules.root_causes(_ev(S2_PLAN, S2_SQL)), "like_join_pattern")[0]
    assert "c.code" in f.detail and "~~" in f.detail
    assert "idx_customers_code" in f.detail
    assert "Hash Join" in f.advice and "substr" in f.advice and "verify" in f.advice


def test_s2_normalized_text_with_a_placeholder_pattern_is_still_a_join_pattern():
    """按 sql_id 取到的是 `LIKE l3.k || ?`:模式仍取自另一侧的列,不因占位符改判。"""
    plan = S2_PLAN.replace("|| '%'::text", "|| '1'::text")
    sql = S2_SQL.replace("|| '%'", "|| ?")
    assert "like_join_pattern" in _kinds(planrules.root_causes(_ev(plan, sql), original_sql=sql))


# ------------------------------------------------------------------ S3 NOT IN

S3_SQL = """SELECT count(*) AS kept, sum(o.amount) AS amt
  FROM (SELECT * FROM fl.orders WHERE id <= 39295) o
  JOIN fl.regions   r ON r.id = o.region_id
 WHERE o.cust_code NOT IN (SELECT c.code FROM fl.customers c WHERE c.grade + 0 = r.zone)"""

S3_PLAN = """Aggregate  (cost=93229344.35..93229344.36 rows=1 width=45)
  ->  Nested Loop  (cost=0.00..93229244.55 rows=19960 width=5)
        ->  Nested Loop  (cost=0.00..93223644.20 rows=19960 width=9)
              ->  Nested Loop  (cost=0.00..93218053.20 rows=19960 width=13)
                    ->  Index Scan using orders_pkey on orders  (cost=0.00..1211.97 rows=39921 width=28)
                          Index Cond: (id <= 39295)
                    ->  Index Scan using regions_pkey on regions r  (cost=0.00..2335.02 rows=1 width=8)
                          Index Cond: (id = orders.region_id)
                          Filter: (NOT (SubPlan 1))
                          SubPlan 1
                            ->  Seq Scan on customers c  (cost=0.00..4667.00 rows=1000 width=15)
                                  Filter: ((grade + 0) = r.zone)
              ->  Index Only Scan using channels_pkey on channels ch  (cost=0.00..0.27 rows=1 width=4)
                    Index Cond: (id = orders.channel_id)"""


def test_s3_not_in_with_a_nullable_column_is_a_per_row_subplan():
    out = planrules.root_causes(_ev(S3_PLAN, S3_SQL))
    f = _by_kind(out, "not_in_subplan")[0]
    assert "NOT (SubPlan 1)" in f.detail and "逐行" in f.detail
    assert "customers.code" in f.detail and "NULL" in f.detail and "0.010" in f.detail
    assert "NOT EXISTS" in f.advice and "反连接" in f.advice
    assert "correlated_subplan" not in _kinds(out), "同一个子计划 NOT IN 那条已经说了逐行,不重复报"


def test_s3_not_in_without_null_stats_does_not_claim_the_column_is_nullable():
    f = _by_kind(planrules.root_causes(_ev(S3_PLAN, S3_SQL, columns=[])), "not_in_subplan")[0]
    assert "null_frac" not in f.detail
    assert "NOT EXISTS" in f.advice


def test_hashed_subplan_for_not_in_is_reported_with_its_own_wording():
    plan = ("Seq Scan on orders o  (cost=0.00..1.00 rows=1 width=4)\n"
            "  Filter: (NOT (hashed SubPlan 1))\n"
            "  SubPlan 1\n"
            "    ->  Seq Scan on customers c  (cost=0.00..1.00 rows=1 width=11)")
    sql = "SELECT * FROM fl.orders o WHERE o.cust_code NOT IN (SELECT c.code FROM fl.customers c)"
    f = _by_kind(planrules.root_causes(_ev(plan, sql)), "not_in_subplan")[0]
    assert "hashed" in f.detail and "work_mem" in f.detail


def test_plain_in_with_a_hashed_subplan_is_not_a_finding():
    plan = ("Seq Scan on orders o  (cost=0.00..1.00 rows=1 width=4)\n"
            "  Filter: (hashed SubPlan 1)\n"
            "  SubPlan 1\n"
            "    ->  Seq Scan on customers c  (cost=0.00..1.00 rows=1 width=11)")
    sql = "SELECT * FROM fl.orders o WHERE o.cust_code IN (SELECT c.code FROM fl.customers c)"
    assert planrules.root_causes(_ev(plan, sql)) == []


# ------------------------------------------------------------------ S4 列上套类型转换

S4_SQL = """SELECT o.id, (SELECT max(c.name) FROM fl.customers c WHERE c.code::bigint = o.cust_id) AS cust,
       (SELECT count(*) FROM fl.order_items i WHERE i.order_id = o.id) AS items
  FROM (SELECT * FROM fl.orders WHERE id <= 1) o"""

S4_PLAN = """Sort  (cost=4682.80..4682.81 rows=1 width=40)
  Sort Key: orders.amount DESC
  ->  Nested Loop  (cost=0.00..4682.79 rows=1 width=40)
        ->  Index Scan using orders_pkey on orders  (cost=0.00..1.37 rows=1 width=21)
              Index Cond: (id <= 1)
        SubPlan 1
          ->  Aggregate  (cost=4669.50..4669.51 rows=1 width=54)
                ->  Seq Scan on customers c  (cost=0.00..4667.00 rows=1000 width=22)
                      Filter: ((code)::bigint = orders.cust_id)
        SubPlan 2
          ->  Aggregate  (cost=4.46..4.47 rows=1 width=12)
                ->  Index Only Scan using idx_items_order on order_items i  (cost=0.00..4.45 rows=3 width=4)
                      Index Cond: (order_id = orders.id)"""


def test_s4_cast_on_an_indexed_column_disables_the_index():
    out = planrules.root_causes(_ev(S4_PLAN, S4_SQL))
    f = _by_kind(out, "cast_on_indexed_column")[0]
    assert "(code)::bigint" in f.detail and "idx_customers_code" in f.detail
    assert "显式" in f.detail                     # SQL 正文里就写着 c.code::bigint
    assert "表达式索引" in f.advice and "前导零" in f.advice
    sub = _by_kind(out, "correlated_subplan")
    assert len(sub) == 1 and "SubPlan 1" in sub[0].detail and "SubPlan 2" not in sub[0].detail
    assert "customers(code)" not in sub[0].advice, "索引已经有了,问题在转换,不能再劝建一遍"


def test_implicit_cast_is_named_as_implicit():
    plan = ("Seq Scan on customers c  (cost=0.00..1.00 rows=1 width=4)\n"
            "  Filter: ((code)::numeric = 42::numeric)")
    sql = "SELECT * FROM fl.customers c WHERE c.code = 42"
    f = _by_kind(planrules.root_causes(_ev(plan, sql)), "cast_on_indexed_column")[0]
    assert "隐式" in f.detail


def test_varchar_to_text_relabel_is_not_a_cast_problem():
    """varchar 列在计划里总显示成 (code)::text —— 那是二进制兼容的标注,索引照用。"""
    plan = ("Seq Scan on customers c  (cost=0.00..1.00 rows=1 width=4)\n"
            "  Filter: ((code)::text = 'x'::text)")
    assert planrules.root_causes(_ev(plan, "SELECT * FROM fl.customers c WHERE c.code = 'x'")) == []


def test_cast_on_a_column_without_index_is_left_to_the_generic_warnings():
    plan = ("Seq Scan on customers c  (cost=0.00..1.00 rows=1 width=4)\n"
            "  Filter: ((name)::bigint = 1)")
    assert planrules.root_causes(_ev(plan, "SELECT * FROM fl.customers c WHERE c.name::bigint = 1")) == []


def test_function_on_an_indexed_column_in_a_join_filter():
    plan = ("Nested Loop  (cost=0.00..9.00 rows=1 width=4)\n"
            "  Join Filter: (lower((c.code)::text) = (o.cust_code)::text)\n"
            "  ->  Seq Scan on orders o  (cost=0.00..1.00 rows=1 width=4)\n"
            "  ->  Seq Scan on customers c  (cost=0.00..1.00 rows=1 width=4)")
    sql = "SELECT * FROM fl.orders o JOIN fl.customers c ON lower(c.code) = o.cust_code"
    f = _by_kind(planrules.root_causes(_ev(plan, sql)), "func_on_indexed_column")[0]
    assert "lower" in f.detail and "idx_customers_code" in f.detail and "关联条件" in f.detail


# ------------------------------------------------------------------ LIKE 左模糊

def test_leading_wildcard_like_is_named_even_without_an_index():
    """左模糊建了 B-tree 索引也用不上 —— 说出来,免得被通用的「Filter 列上建索引」带偏。"""
    plan = ("Seq Scan on customers c  (cost=0.00..1.00 rows=1 width=4)\n"
            "  Filter: ((name)::text ~~ '%abc'::text)")
    sql = "SELECT * FROM fl.customers c WHERE c.name LIKE '%abc'"
    f = _by_kind(planrules.root_causes(_ev(plan, sql)), "like_leading_wildcard")[0]
    assert "'%abc'" in f.detail and "B-tree" in f.advice


def test_leading_wildcard_from_a_synthetic_placeholder_is_not_reported():
    """`LIKE ?` 的 '%test%' 是 sqltune 自己填的合成值,拿它报左模糊是冤枉 SQL。"""
    plan = ("Seq Scan on customers c  (cost=0.00..1.00 rows=1 width=4)\n"
            "  Filter: ((name)::text ~~ '%test%'::text)")
    out = planrules.root_causes(_ev(plan, "SELECT * FROM fl.customers c WHERE c.name LIKE '%test%'"),
                                original_sql="SELECT * FROM fl.customers c WHERE c.name LIKE ?")
    assert out == []


def test_prefix_like_with_a_constant_is_not_a_leading_wildcard():
    plan = ("Seq Scan on customers c  (cost=0.00..1.00 rows=1 width=4)\n"
            "  Filter: ((name)::text ~~ 'abc%'::text)")
    assert planrules.root_causes(_ev(plan, "SELECT * FROM fl.customers c WHERE c.name LIKE 'abc%'")) == []


# ------------------------------------------------------------------ 拿不准不报

def test_an_outer_column_in_another_conjunct_does_not_make_a_constant_like_a_join_pattern():
    plan = ("Nested Loop  (cost=0.00..9.00 rows=1 width=4)\n"
            "  ->  Seq Scan on orders o  (cost=0.00..1.00 rows=1 width=4)\n"
            "  ->  Index Scan using customers_pkey on customers c  (cost=0.00..1.00 rows=1 width=4)\n"
            "        Index Cond: (id = o.cust_id)\n"
            "        Filter: (((name)::text ~~ 'abc%'::text) AND (region_id = o.region_id))")
    sql = "SELECT * FROM fl.orders o JOIN fl.customers c ON c.id = o.cust_id AND c.region_id = o.region_id WHERE c.name LIKE 'abc%'"
    assert planrules.root_causes(_ev(plan, sql)) == []


def test_not_in_over_a_join_does_not_guess_which_table_the_column_belongs_to():
    plan = ("Seq Scan on orders o  (cost=0.00..1.00 rows=1 width=4)\n"
            "  Filter: (NOT (SubPlan 1))\n"
            "  SubPlan 1\n"
            "    ->  Seq Scan on regions r  (cost=0.00..1.00 rows=1 width=11)")
    sql = ("SELECT * FROM fl.orders o WHERE o.cust_code NOT IN "
           "(SELECT c.code FROM fl.regions r JOIN fl.customers c ON c.region_id = r.id)")
    f = _by_kind(planrules.root_causes(_ev(plan, sql)), "not_in_subplan")[0]
    assert "null_frac" not in f.detail, "c 不是 FROM 的第一张表 regions,拿 regions.code 的统计说可空是张冠李戴"


def test_an_expression_on_the_outer_side_of_a_nested_loop_is_not_blamed():
    """`o.amount * 2 = c.id`:表达式在外层,内层 customers 照样能按 id 走索引 —— 不是根因。"""
    plan = ("Nested Loop  (cost=0.00..9.00 rows=1 width=4)\n"
            "  Join Filter: ((o.cust_id + 0) = c.id)\n"
            "  ->  Seq Scan on customers c  (cost=0.00..1.00 rows=1 width=4)\n"
            "  ->  Seq Scan on orders o  (cost=0.00..1.00 rows=1 width=4)")
    idx = FL_INDEXES + [_idx("orders", "idx_orders_cust2", "cust_id")]
    out = planrules.root_causes(_ev(plan, "SELECT 1 FROM fl.orders o JOIN fl.customers c ON o.cust_id + 0 = c.id",
                                    indexes=idx))
    f = _by_kind(out, "func_on_indexed_column")[0]     # orders 在内层:它的 cust_id 被 + 0 包住,是根因
    assert "orders.cust_id" in f.detail
    swapped = plan.replace("Seq Scan on customers c", "@@").replace("Seq Scan on orders o", "Seq Scan on customers c") \
                  .replace("@@", "Seq Scan on orders o")
    assert planrules.root_causes(_ev(swapped, "SELECT 1", indexes=idx)) == [], "orders 在外层时不怪它"

def test_initplan_runs_once_and_is_not_a_per_row_subquery():
    plan = ("Seq Scan on orders o  (cost=0.00..1.00 rows=1 width=4)\n"
            "  Filter: (amount > $0)\n"
            "  InitPlan 1 (returns $0)\n"
            "    ->  Aggregate  (cost=1.00..1.01 rows=1 width=4)\n"
            "          ->  Seq Scan on payments p  (cost=0.00..1.00 rows=1 width=4)")
    assert planrules.root_causes(_ev(plan, "SELECT * FROM fl.orders o WHERE amount > (SELECT avg(amount) FROM fl.payments p)")) == []


def test_unparseable_plan_reports_nothing():
    assert planrules.root_causes(_ev("", "SELECT 1")) == []
    assert planrules.root_causes(_ev("garbage\n  more garbage", "SELECT 1")) == []


def test_evidence_report_lists_root_causes_with_the_generic_findings():
    ev = _ev(S4_PLAN, S4_SQL)
    out = planrules.root_causes(ev)
    rep = evidence.evidence_report(evidence.Evidence(sql=ev.sql, version="", plan=ev.plan, analyzed=False,
                                                     findings=out + evidence.scan_plan(ev.plan)))
    assert rep.index("cast_on_indexed_column") < rep.index("seq_scan")
